"""知识库索引与检索层：markdown 镜像 → 按小节切块 → ollama 向量 → 混合召回。

**刻意不依赖 Bot / 飞书 SDK**（只依赖本项目的 db 层，且可传 None 降级为纯向量），
输入是目录路径与 query、输出是命中片段，将来若要拆成独立服务可整个文件搬走。

**切块按标题小节，不按字数**：知识库的写法是「一篇文档记录大量问题、一个问题一个
小节」，所以一个小节 = 一个 chunk = 一个可独立回答的条目。每个 chunk 文本前缀
「文档标题 › 各级小节标题」面包屑，既提升向量召回，也给全文匹配增加词面信号。

**混合召回（向量 + MySQL ngram 全文）**：实测下来两路互补且都不可省——
纯向量在术语不一致时会翻车（问「网关挂了返回502」时 Nginx 条目被 MySQL 条目挤掉、
分差仅 0.009），而全文路靠 502 这个词精确命中；反过来「怎么报销出差的钱」这类
完全换词的问法只有向量能召回。两路各取 Top-N 后用 RRF 融合。
"""

from core.locking import locked_space

import json
import math
import os
import re

import requests

DOCS_DIR = "docs"
MANIFEST = "manifest.json"
CHUNKS_FILE = "chunks.jsonl"
VECTORS_FILE = "vectors.npy"
META_FILE = "index_meta.json"

# 切块参数：与 CLAUDE.md 里的「知识库编写规范」是一套，调这里要同步改文档
MAX_CHARS = 800      # 超过此长度的小节二次硬切
SPLIT_CHARS = 500    # 二次切分目标长度
OVERLAP = 80         # 二次切分重叠，避免切点横穿句子
MIN_CHARS = 50       # 过短的小节并入相邻块

RRF_K = 60           # RRF 融合常数，业界惯例取 60
RECALL_N = 20        # 每路召回数量

# bge 系列要求 query 侧加检索指令前缀（passage 侧不加），少了这个召回会明显变差
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


def space_dir(cache_dir, space_id):
    """索引按 space_id 分区，不按 chat_id——多个群配同一知识库时共享一份索引。"""
    return os.path.join(os.path.expanduser(cache_dir), str(space_id))


# ------------------------------------------------------------------ 切块

_FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def parse_doc(text):
    """拆出 frontmatter 元信息与正文。返回 (meta_dict, body)。"""
    meta, body = {}, text
    m = _FM_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if v:
                meta[k.strip()] = v.strip()
        body = text[m.end():]
    return meta, body


def _split_long(text):
    """长小节二次硬切：按 SPLIT_CHARS 切、保留 OVERLAP 重叠，尽量落在换行处。"""
    out, start = [], 0
    while start < len(text):
        end = min(start + SPLIT_CHARS, len(text))
        if end < len(text):
            nl = text.rfind("\n", start + SPLIT_CHARS // 2, end)
            if nl > start:
                end = nl
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return [x for x in out if x]


def chunk_markdown(md, meta):
    """把一篇文档切成条目级 chunk，返回 [{heading_trail, text, ...}]。

    以 heading 为边界，一节一块；标题层级用栈维护，使每块都带完整标题路径。"""
    _, body = parse_doc(md) if md.startswith("---") else ({}, md)
    doc_title = meta.get("title") or "(无标题)"
    crumb = meta.get("breadcrumb") or doc_title

    sections = []           # [(trail_list, body_text)]
    stack, buf = [], []

    def flush():
        text = "\n".join(buf).strip()
        if text or stack:
            sections.append((list(stack), text))
        buf.clear()

    in_code = False
    for line in body.splitlines():
        stripped = line.strip()
        # 代码块内不认标题：shell 注释 `# xxx` 在排障文档里遍地都是，
        # 不跟踪围栏就会被误判成小节边界，在条目中间切一刀
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            buf.append(line)
            continue
        m = None if in_code else _HEADING_RE.match(stripped)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            del stack[level - 1:]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(title)
        else:
            buf.append(line)
    flush()

    # 过短的小节并进前一块，避免产生一堆语义不足的碎片
    merged = []
    for trail, text in sections:
        if (merged and len(text) < MIN_CHARS
                and len(merged[-1][1]) + len(text) < MAX_CHARS):
            prev_trail, prev_text = merged[-1]
            head = " › ".join(x for x in trail if x)
            merged[-1] = (prev_trail, f"{prev_text}\n{head}\n{text}".strip())
        else:
            merged.append((trail, text))

    chunks = []
    for trail, text in merged:
        heads = [x for x in trail if x]
        full_crumb = " › ".join([crumb] + heads)
        if not text and not heads:
            continue
        pieces = _split_long(text) if len(text) > MAX_CHARS else [text]
        for i, piece in enumerate(pieces):
            # 面包屑进正文：向量与全文两路都吃这段文字
            body_text = f"{full_crumb}\n{piece}".strip()
            chunks.append({
                "doc_title": doc_title,
                "url": meta.get("url", ""),
                "breadcrumb": full_crumb,
                "heading_trail": heads,
                "part": i,
                "text": body_text,
            })
    return chunks


# ------------------------------------------------------------------ embedding（ollama）

def embed(texts, cfg, batch=32):
    """调 ollama /api/embed 批量取向量。cfg 需含 embed_url / embed_model。

    keep_alive 走请求参数而非 systemd 环境变量：由本项目自己控制模型驻留时长，
    不动系统配置。"""
    url = cfg["embed_url"].rstrip("/") + "/api/embed"
    model = cfg["embed_model"]
    out = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        last = None
        for attempt in range(cfg.get("embed_attempts", 3)):
            try:
                r = requests.post(url, json={"model": model, "input": part,
                                             "keep_alive": cfg.get("keep_alive", "30m")},
                                  timeout=cfg.get("embed_timeout", 300))
                r.raise_for_status()
                vecs = r.json().get("embeddings")
                if not vecs or len(vecs) != len(part):
                    raise RuntimeError(f"embedding 数量不匹配: {len(vecs or [])}/{len(part)}")
                out.extend(vecs)
                break
            except Exception as e:
                last = e
        else:
            raise RuntimeError(f"ollama embedding 失败（模型 {model}）: {last}")
    return out


def _normalize(vec):
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


# ------------------------------------------------------------------ 建索引

def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


@locked_space(0)
def build(dest, cfg, session_factory=None, log=print):
    """（增量）重建索引。只对 manifest 里 edit_time 变化的文档重新切块+编码，
    其余复用上一版向量。返回 chunk 总数。

    换 embedding 模型会导致维度变化，index_meta 里存了模型名，不一致时强制全量重建。"""
    import numpy as np

    docs_dir = os.path.join(dest, DOCS_DIR)
    manifest = _read_json(os.path.join(dest, MANIFEST), {})
    meta = _read_json(os.path.join(dest, META_FILE), {})
    old_chunks = _load_chunks(dest)
    old_vecs = None
    if old_chunks and meta.get("model") == cfg["embed_model"]:
        try:
            old_vecs = np.load(os.path.join(dest, VECTORS_FILE))
        except (OSError, ValueError, EOFError):
            old_vecs = None
    if old_vecs is None or len(old_chunks) != len(old_vecs):
        old_chunks, old_vecs = [], None
        if meta.get("model") and meta.get("model") != cfg["embed_model"]:
            log(f"[wiki-index] embedding 模型变了（{meta.get('model')} → "
                f"{cfg['embed_model']}），全量重建")

    # 旧 chunk 按 node_token 归拢，供未变更文档复用
    prev_by_node = {}
    for i, c in enumerate(old_chunks):
        prev_by_node.setdefault(c["node_token"], []).append((c, i))

    chunks, reuse_idx, pending = [], [], []
    for node_token, info in manifest.items():
        fname = info.get("file")
        if not fname:
            continue
        prev = prev_by_node.get(node_token)
        if prev and old_vecs is not None and prev[0][0].get("edit_time") == info.get("edit_time"):
            for c, i in prev:
                chunks.append(c)
                reuse_idx.append(i)
            continue
        try:
            with open(os.path.join(docs_dir, fname)) as f:
                raw = f.read()
        except OSError:
            continue
        fmeta, _ = parse_doc(raw)
        fmeta.setdefault("title", info.get("title", ""))
        fmeta.setdefault("url", info.get("url", ""))
        for j, ch in enumerate(chunk_markdown(raw, fmeta)):
            ch.update({"node_token": node_token, "edit_time": info.get("edit_time"),
                       "chunk_id": f"{node_token}#{j}"})
            chunks.append(ch)
            reuse_idx.append(-1)
            pending.append(len(chunks) - 1)

    if not chunks:
        log("[wiki-index] 没有可索引的内容")
        _save(dest, [], None, cfg)
        if session_factory is not None:
            _sync_db(session_factory, os.path.basename(dest.rstrip("/")), [], log)
        return 0

    dim = int(old_vecs.shape[1]) if old_vecs is not None and len(old_vecs) else 0
    if pending:
        log(f"[wiki-index] 需要编码 {len(pending)} 个 chunk（复用 {len(chunks) - len(pending)} 个）")
        vecs = embed([chunks[i]["text"] for i in pending], cfg)
        dim = len(vecs[0])
    matrix = np.zeros((len(chunks), dim), dtype=np.float32)
    for pos, i in enumerate(reuse_idx):
        if i >= 0:
            matrix[pos] = old_vecs[i]
    for k, pos in enumerate(pending):
        matrix[pos] = np.asarray(_normalize(vecs[k]), dtype=np.float32)

    _save(dest, chunks, matrix, cfg)
    if session_factory is not None:
        _sync_db(session_factory, os.path.basename(dest.rstrip("/")), chunks, log)
    log(f"[wiki-index] 索引完成：{len(chunks)} chunk / {len(manifest)} 文档 / dim={dim}")
    return len(chunks)


def _save(dest, chunks, matrix, cfg):
    """锁内写临时文件并 rename；build/load 共用目录锁，保证 chunk 与向量成对读取。"""
    import numpy as np

    os.makedirs(dest, exist_ok=True)
    tmp = os.path.join(dest, CHUNKS_FILE + ".tmp")
    with open(tmp, "w") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.replace(tmp, os.path.join(dest, CHUNKS_FILE))
    if matrix is not None:
        # np.save 会给不以 .npy 结尾的路径自动补后缀，故临时名也用 .npy 结尾
        vtmp = os.path.join(dest, "vectors.tmp.npy")
        np.save(vtmp, matrix)
        os.replace(vtmp, os.path.join(dest, VECTORS_FILE))
    with open(os.path.join(dest, META_FILE), "w") as f:
        json.dump({"model": cfg["embed_model"], "count": len(chunks),
                   "dim": int(matrix.shape[1]) if matrix is not None else 0}, f)


def _load_chunks(dest):
    path = os.path.join(dest, CHUNKS_FILE)
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return out


def _sync_db(session_factory, space_id, chunks, log=print):
    """把 chunk 文本同步进 MySQL 供全文检索（按 space_id 整段替换）。失败不致命——
    全文那一路挂了还有向量路，不该阻断同步。"""
    import db
    rows = [{"chunk_id": c["chunk_id"], "doc_title": c["doc_title"], "url": c["url"],
             "breadcrumb": c["breadcrumb"], "text": c["text"]} for c in chunks]
    try:
        with session_factory() as s:
            db.replace_wiki_chunks(s, space_id, rows)
            s.commit()
    except Exception as e:
        log(f"[wiki-index][db-error] 全文索引同步失败（不影响向量检索）: {e}")


# ------------------------------------------------------------------ 检索

class Index:
    """加载好的索引：chunks 列表 + 归一化向量矩阵。整体替换、读侧无锁。"""

    def __init__(self, space_id, chunks, matrix):
        self.space_id = space_id
        self.chunks = chunks
        self.matrix = matrix

    def __len__(self):
        return len(self.chunks)


@locked_space(0)
def load(dest):
    """加载索引；没有索引返回 None。"""
    import numpy as np

    chunks = _load_chunks(dest)
    if not chunks:
        return None
    try:
        matrix = np.load(os.path.join(dest, VECTORS_FILE))
    except (OSError, ValueError, EOFError):
        return None
    if matrix.ndim != 2:
        return None
    if len(matrix) != len(chunks):
        return None
    return Index(os.path.basename(dest.rstrip("/")), chunks, matrix)


def _vector_hits(idx, query, cfg, n=RECALL_N):
    import numpy as np

    query_cfg = {**cfg, "embed_timeout": cfg.get("query_timeout", 10), "embed_attempts": 1}
    qv = np.asarray(_normalize(embed([QUERY_PREFIX + query], query_cfg)[0]), dtype=np.float32)
    if qv.shape[0] != idx.matrix.shape[1]:
        raise RuntimeError(f"query 维度 {qv.shape[0]} 与索引 {idx.matrix.shape[1]} 不一致，"
                           f"请重建索引（--rebuild）")
    scores = idx.matrix @ qv
    top = np.argsort(-scores)[:n]
    return [(int(i), float(scores[i])) for i in top]


def _fulltext_hits(idx, query, session_factory, n=RECALL_N):
    """MySQL ngram 全文召回。返回 [(chunk 下标, 分数)]；不可用时返回空列表。"""
    if session_factory is None:
        return []
    import db
    try:
        with session_factory() as s:
            rows = db.search_wiki_chunks(s, idx.space_id, query, n)
    except Exception as e:
        print(f"[wiki-index][ft-error] 全文检索不可用（降级为纯向量）: {e}")
        return []
    pos = {c["chunk_id"]: i for i, c in enumerate(idx.chunks)}
    return [(pos[cid], score) for cid, score in rows if cid in pos]


def search(idx, query, cfg, top_k=5, session_factory=None):
    """混合检索：向量路 + 全文路各取 Top-N，RRF 融合后返回 top_k 个 chunk。

    RRF 只看排名不看绝对分，天然规避「两路分数量纲不同」的问题。"""
    if idx is None or not len(idx):
        return []
    vector_error = None
    try:
        vec = _vector_hits(idx, query, cfg)
    except Exception as e:
        vector_error = e
        vec = []
        print(f"[wiki-index][vector-error] 向量检索不可用，尝试全文检索: {e}")
    ft = _fulltext_hits(idx, query, session_factory)
    if vector_error and not ft:
        raise RuntimeError(f"向量检索失败，全文检索无可用结果: {vector_error}") from vector_error
    fused = {}
    for rank, (i, _s) in enumerate(vec):
        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (i, _s) in enumerate(ft):
        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
    order = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]
    vmap, fmap = dict(vec), dict(ft)
    out = []
    for i, score in order:
        c = idx.chunks[i]
        out.append({**c, "score": score,
                    "vec_score": vmap.get(i), "ft_score": fmap.get(i)})
    return out


def format_reference(hits, max_chars=3000):
    """把命中片段拼成注入 prompt 的「参考资料」块。带编号与来源链接，
    便于 Claude 在回答末尾引用。"""
    if not hits:
        return ""
    lines = ["[知识库参考资料（来自飞书知识库，回答时优先依据它，并在末尾注明来源）]"]
    used = 0
    for i, h in enumerate(hits, 1):
        text = h["text"].strip()
        # 面包屑是切块时为检索拼进正文的，这里标题行已经有了，去掉重复的那份省 token
        crumb = h.get("breadcrumb", "")
        if crumb and text.startswith(crumb):
            text = text[len(crumb):].lstrip("\n")
        if used + len(text) > max_chars:
            text = text[:max(max_chars - used, 0)]
        if not text:
            break
        lines.append(f"\n【资料{i}】{h['breadcrumb']}\n{text}")
        if h.get("url"):
            lines.append(f"来源：{h['doc_title']} {h['url']}")
        used += len(text)
        if used >= max_chars:
            break
    return "\n".join(lines)


def debug_search(idx, query, cfg, top_k=5, session_factory=None):
    """自检 CLI 用：分别打印向量路、全文路、融合结果，便于判断是哪一路出问题。"""
    vector_error = None
    try:
        vec = _vector_hits(idx, query, cfg)
    except Exception as e:
        vector_error = e
        vec = []
        print(f"[wiki-index][vector-error] 向量检索不可用，尝试全文检索: {e}")
    ft = _fulltext_hits(idx, query, session_factory)
    if vector_error and not ft:
        raise RuntimeError(f"向量检索失败，全文检索无可用结果: {vector_error}") from vector_error

    def label(i):
        c = idx.chunks[i]
        return f"{c['breadcrumb']}"[:70]

    print(f"\n=== 向量路 Top{min(5, len(vec))} ===")
    for r, (i, s) in enumerate(vec[:5], 1):
        print(f"  {r}. [{s:.3f}] {label(i)}")
    print(f"\n=== 全文路 Top{min(5, len(ft))} ===")
    if not ft:
        print("  （无结果或全文索引不可用）")
    for r, (i, s) in enumerate(ft[:5], 1):
        print(f"  {r}. [{s:.3f}] {label(i)}")

    hits = search(idx, query, cfg, top_k, session_factory)
    print(f"\n=== RRF 融合 Top{len(hits)} ===")
    for r, h in enumerate(hits, 1):
        v = f"{h['vec_score']:.3f}" if h["vec_score"] is not None else "-"
        f_ = f"{h['ft_score']:.3f}" if h["ft_score"] is not None else "-"
        print(f"  {r}. [rrf={h['score']:.4f} vec={v} ft={f_}] {h['breadcrumb'][:70]}")
        print(f"     {h['text'][:120].replace(chr(10), ' ')}…")
    return hits
