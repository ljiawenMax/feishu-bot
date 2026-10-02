"""飞书知识库同步层：把 wiki 知识空间拉成本地 markdown 镜像，供 wiki_index 建索引。

**为什么不用 `docx.document.raw_content`**：它返回纯文本，标题与正文无法区分。
知识库的典型写法是「一篇文档记录大量独立问题、每个问题一个小节」，丢掉 heading
层级后只能按字数硬切，切点会横穿条目、把问题和它的解决方案劈开，检索质量直接崩。
所以走 `docx.document_block.list` 取结构化 block 自转 markdown，保留标题层级，
让 wiki_index 能「按小节切块 = 一个问题一个 chunk」。接口异常时才回退 raw_content。

**块类型判断不看 block_type 数字枚举**：Block 对象每种类型只有对应的那个属性非空
（heading1 / text / bullet / code …），按属性判断比硬编码枚举稳，飞书加新类型也不会错位。

**增量**：manifest.json 记 node_token → obj_edit_time，只重拉编辑时间变化的文档；
远端已删除的节点同步删掉本地 md。

也可作为自检 CLI 独立运行（不启动服务即可验证权限/抓取/切块）：
    python -m wiki_sync --env local --bot <name> --spaces
    python -m wiki_sync --env local --bot <name> --sync
    python -m wiki_sync --env local --bot <name> --search "问题"
"""

from core.locking import locked_space

import hashlib
import json
import os
import re
import shutil

from lark_oapi.api.docx.v1 import ListDocumentBlockRequest, RawContentDocumentRequest
from lark_oapi.api.wiki.v2 import ListSpaceNodeRequest, ListSpaceRequest

MANIFEST = "manifest.json"
DOCS_DIR = "docs"
# 单次翻页上限，防接口异常导致死循环
MAX_PAGES = 200

# block_type 数字 → Block 上承载内容的属性名。只列会产出正文的类型，其余走降级处理。
_HEADING_ATTRS = [f"heading{i}" for i in range(1, 10)]


def _api(resp, what):
    """统一校验 SDK 响应，失败抛带 log_id 的异常（飞书排障全靠 log_id）。"""
    if not resp.success():
        raise RuntimeError(f"{what} failed: code={resp.code} msg={resp.msg} "
                           f"log_id={resp.get_log_id()}")
    return resp.data


def _next_page(data, seen):
    token = data.page_token
    if not token or token in seen:
        raise RuntimeError("翻页令牌缺失或重复，结果不完整")
    seen.add(token)
    return token


# ------------------------------------------------------------------ 知识空间 / 节点树

def list_spaces(client):
    """列出应用可见的知识空间，返回 [{space_id, name}]。

    返回空列表通常不是没建知识库，而是**机器人应用还没被加为知识库成员**
    （知识库 → 设置 → 成员管理 → 添加应用）；这种情况飞书不报权限错，只给空列表。"""
    spaces, token, seen = [], None, set()
    for _ in range(MAX_PAGES):
        b = ListSpaceRequest.builder().page_size(50)
        if token:
            b = b.page_token(token)
        data = _api(client.wiki.v2.space.list(b.build()), "list spaces")
        for s in (data.items or []):
            spaces.append({"space_id": s.space_id, "name": s.name})
        if not data.has_more:
            break
        token = _next_page(data, seen)
    else:
        raise RuntimeError("知识空间翻页超过上限，结果不完整")
    return spaces


def _list_children(client, space_id, parent_node_token=None):
    """列某个父节点下的直接子节点（翻页取全）。"""
    items, token, seen = [], None, set()
    for _ in range(MAX_PAGES):
        b = ListSpaceNodeRequest.builder().space_id(space_id).page_size(50)
        if parent_node_token:
            b = b.parent_node_token(parent_node_token)
        if token:
            b = b.page_token(token)
        data = _api(client.wiki.v2.space_node.list(b.build()), "list space nodes")
        items.extend(data.items or [])
        if not data.has_more:
            break
        token = _next_page(data, seen)
    else:
        raise RuntimeError("知识库节点翻页超过上限，结果不完整，取消同步")
    return items


def walk_tree(client, space_id, root_node=None):
    """深度优先遍历知识空间节点树，返回扁平列表：
    [{node_token, obj_token, obj_type, title, url, edit_time, breadcrumb}, ...]

    breadcrumb 是从根到该节点的标题路径（不含自身），会被拼进 chunk 文本——
    知识库目录树本身就是检索信号，所以文档别平铺在根节点。"""
    out, visited = [], set()

    def walk(parent_token, trail):
        for n in _list_children(client, space_id, parent_token):
            if not n.node_token or n.node_token in visited:
                raise RuntimeError("知识库节点重复或形成环，取消同步")
            visited.add(n.node_token)
            title = (n.title or "").strip() or "(无标题)"
            out.append({
                "node_token": n.node_token,
                "obj_token": n.obj_token,
                "obj_type": n.obj_type,
                "title": title,
                "url": n.url or "",
                "edit_time": n.obj_edit_time or n.node_create_time or 0,
                "breadcrumb": list(trail),
            })
            if n.has_child:
                walk(n.node_token, trail + [title])

    walk(root_node, [])
    return out


# ------------------------------------------------------------------ docx block → markdown

def _text_of(text_obj):
    """把 Text（block 的内容载体）拼成纯字符串：取各 element 的 text_run.content，
    mention_doc/equation 等降级取能拿到的文字。"""
    if text_obj is None:
        return ""
    parts = []
    for el in (text_obj.elements or []):
        run = getattr(el, "text_run", None)
        if run is not None and run.content:
            parts.append(run.content)
            continue
        for alt in ("mention_doc", "mention_user", "equation", "inline_file", "link_preview"):
            sub = getattr(el, alt, None)
            if sub is None:
                continue
            for field in ("title", "content", "name", "url"):
                v = getattr(sub, field, None)
                if v:
                    parts.append(str(v))
                    break
            break
    return "".join(parts)


def _block_kind(block):
    """按「哪个属性非空」判定块类型，返回 (kind, text_obj)。未知类型返回 ("", None)。"""
    for i, attr in enumerate(_HEADING_ATTRS, start=1):
        t = getattr(block, attr, None)
        if t is not None:
            return f"h{i}", t
    for attr in ("text", "bullet", "ordered", "code", "quote", "todo", "equation"):
        t = getattr(block, attr, None)
        if t is not None:
            return attr, t
    for attr in ("divider", "image", "table", "callout", "quote_container", "grid",
                 "sheet", "bitable", "file", "iframe", "mindnote", "board", "diagram"):
        if getattr(block, attr, None) is not None:
            return attr, None
    return "", None


def blocks_to_markdown(blocks):
    """把扁平 block 列表还原成 markdown（保留 heading 层级、列表缩进、代码块）。

    blocks 是扁平的，靠 parent_id/children 还原树；从 page 块（无 parent 或 parent
    不在集合内）开始深度优先输出。"""
    by_id = {b.block_id: b for b in blocks if b.block_id}
    lines = []
    ordered_seq = {}  # parent_id -> 有序列表当前序号，保证同级编号连续

    def emit(block, depth):
        kind, text_obj = _block_kind(block)
        content = _text_of(text_obj).rstrip()
        indent = "  " * max(depth, 0)

        if kind.startswith("h") and kind[1:].isdigit():
            level = int(kind[1:])
            if content:
                lines.append("")
                lines.append(f"{'#' * min(level, 6)} {content}")
                lines.append("")
        elif kind == "text":
            if content:
                lines.append(f"{indent}{content}")
        elif kind == "bullet":
            lines.append(f"{indent}- {content}")
        elif kind == "ordered":
            pid = block.parent_id or ""
            ordered_seq[pid] = ordered_seq.get(pid, 0) + 1
            lines.append(f"{indent}{ordered_seq[pid]}. {content}")
        elif kind == "todo":
            done = bool(getattr(getattr(block, "todo", None), "style", None)
                        and block.todo.style.done)
            lines.append(f"{indent}- [{'x' if done else ' '}] {content}")
        elif kind == "code":
            lines.append("```")
            lines.extend(content.splitlines() or [""])
            lines.append("```")
        elif kind in ("quote", "quote_container", "callout"):
            if content:
                lines.append(f"> {content}")
            elif kind in ("quote_container", "callout"):
                pass  # 内容在子块里，靠下面递归输出
        elif kind == "equation":
            if content:
                lines.append(f"$$ {content} $$")
        elif kind == "divider":
            lines.append("")
            lines.append("---")
            lines.append("")
        elif kind == "image":
            lines.append(f"{indent}[图片]")  # 第一版不抓图片，留痕提醒「别只截图」
        elif kind == "table":
            _emit_table(block, lines)
            return  # 表格自己处理了子块，别再递归
        elif kind in ("sheet", "bitable", "mindnote", "file", "iframe", "board", "diagram"):
            lines.append(f"{indent}[{kind}]")
        elif content:
            lines.append(f"{indent}{content}")  # 未知类型：能取到文字就别丢

        # 列表/引用等容器的子块需要缩进；heading 的子块不缩进（它们是正文）
        nest = depth + 1 if kind in ("bullet", "ordered", "todo") else depth
        for cid in (block.children or []):
            child = by_id.get(cid)
            if child is not None:
                emit(child, nest)

    def _emit_table(block, out):
        """表格渲染成 markdown 表。table.cells 是按行优先排列的 cell block id 列表。"""
        tbl = block.table
        cells = list(tbl.cells or [])
        cols = getattr(getattr(tbl, "property", None), "column_size", None) or len(cells) or 1
        texts = []
        for cid in cells:
            cell = by_id.get(cid)
            buf = []
            if cell is not None:
                for gid in (cell.children or []):
                    g = by_id.get(gid)
                    if g is not None:
                        _, t = _block_kind(g)
                        buf.append(_text_of(t).strip())
            texts.append(" ".join(x for x in buf if x).replace("|", "\\|") or " ")
        rows = [texts[i:i + cols] for i in range(0, len(texts), cols)]
        if not rows:
            return
        out.append("")
        out.append("| " + " | ".join(rows[0]) + " |")
        out.append("|" + "---|" * len(rows[0]))
        for r in rows[1:]:
            r = r + [" "] * (len(rows[0]) - len(r))
            out.append("| " + " | ".join(r) + " |")
        out.append("")

    roots = [b for b in blocks
             if b.block_id and (not b.parent_id or b.parent_id not in by_id)]
    for root in roots:
        # page 块自身的 text 是文档标题，frontmatter 里已有，这里只输出其子块
        for cid in (root.children or []):
            child = by_id.get(cid)
            if child is not None:
                emit(child, 0)

    md = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def fetch_markdown(client, node):
    """取一个 wiki 节点的正文 markdown。docx 走 block API 保留结构；
    失败则回退 raw_content（丢层级但保底有正文）；非文档类型返回占位说明。"""
    obj_type, obj_token = node.get("obj_type"), node.get("obj_token")
    if obj_type != "docx" or not obj_token:
        return f"（此节点类型为 {obj_type}，未抓取正文，请点击来源链接查看）"

    try:
        blocks, token, seen = [], None, set()
        for _ in range(MAX_PAGES):
            b = ListDocumentBlockRequest.builder().document_id(obj_token).page_size(500)
            if token:
                b = b.page_token(token)
            data = _api(client.docx.v1.document_block.list(b.build()), "list blocks")
            blocks.extend(data.items or [])
            if not data.has_more:
                break
            token = _next_page(data, seen)
        else:
            raise RuntimeError("文档 block 翻页超过上限，改用完整原文")
        md = blocks_to_markdown(blocks)
        if md:
            return md
    except Exception as e:
        print(f"[wiki-sync][block-fallback] {node.get('title')}: {e}")

    req = RawContentDocumentRequest.builder().document_id(obj_token).build()
    data = _api(client.docx.v1.document.raw_content(req), "raw content")
    return (data.content or "").strip()


# ------------------------------------------------------------------ 落盘 / 增量同步

_UNSAFE = re.compile(r"[^\w一-鿿.-]+")


def safe_filename(title, node_token):
    """文件名消毒：只留字母数字汉字点横线，并带上完整 node_token 的摘要避免前缀碰撞
    （同名文档在知识库里很常见）。"""
    base = _UNSAFE.sub("_", (title or "").strip()).strip("_.")[:60] or "untitled"
    suffix = hashlib.sha256((node_token or "").encode()).hexdigest()[:24]
    return f"{base}-{suffix}.md"


def _frontmatter(node):
    crumb = " › ".join(node["breadcrumb"] + [node["title"]])
    return "\n".join([
        "---",
        f"title: {node['title']}",
        f"url: {node['url']}",
        f"node_token: {node['node_token']}",
        f"obj_type: {node['obj_type']}",
        f"breadcrumb: {crumb}",
        f"edit_time: {node['edit_time']}",
        "---",
        "",
    ])


def read_manifest(dest):
    path = os.path.join(dest, MANIFEST)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(dest, manifest):
    path = os.path.join(dest, MANIFEST)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


@locked_space(2)
def sync_space(client, space_id, dest, root_node=None, log=print):
    """把知识空间增量同步到 dest 目录，返回统计 dict。

    dest 必须在 work_dir 之外——gitsync 每轮 `git add -A`，镜像落进 work_dir
    会被整个提交进用户的 git 历史。"""
    docs_dir = os.path.join(dest, DOCS_DIR)
    os.makedirs(docs_dir, exist_ok=True)
    old = read_manifest(dest)
    nodes = walk_tree(client, space_id, root_node)
    stats = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "failed": 0}
    manifest = {}

    for node in nodes:
        tok = node["node_token"]
        prev = old.get(tok)
        fname = safe_filename(node["title"], tok)
        fpath = os.path.join(docs_dir, fname)
        changed = (prev is None
                   or prev.get("edit_time") != node["edit_time"]
                   or prev.get("file") != fname
                   or not os.path.exists(fpath))
        if not changed:
            stats["unchanged"] += 1
            manifest[tok] = prev
            continue
        try:
            body = fetch_markdown(client, node)
        except Exception as e:
            log(f"[wiki-sync][fail] {node['title']}: {e}")
            stats["failed"] += 1
            if prev:  # 抓取失败保留上一版，别把已有内容弄丢
                manifest[tok] = prev
            continue
        with open(fpath, "w") as f:
            f.write(_frontmatter(node) + body + "\n")
        # 标题改过导致文件名变化时，删掉旧文件避免同一文档留两份
        if prev and prev.get("file") and prev["file"] != fname:
            _rm(os.path.join(docs_dir, prev["file"]))
        stats["added" if prev is None else "updated"] += 1
        manifest[tok] = {"edit_time": node["edit_time"], "title": node["title"],
                         "file": fname, "url": node["url"],
                         "breadcrumb": node["breadcrumb"]}

    for tok, meta in old.items():
        if tok not in manifest and meta.get("file"):
            _rm(os.path.join(docs_dir, meta["file"]))
            stats["removed"] += 1

    _write_manifest(dest, manifest)
    return stats


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


@locked_space(0)
def clear_space(dest):
    """清空某空间的本地镜像与索引（换 embedding 模型/需要全量重建时用）。"""
    shutil.rmtree(dest, ignore_errors=True)


# ------------------------------------------------------------------ 自检 CLI

def _cli():
    import argparse

    import feishu_api
    import feishu_claude

    p = argparse.ArgumentParser(description="飞书知识库同步/检索自检工具")
    p.add_argument("--env", default="local", help="配置名（读取 .env.<name>）")
    p.add_argument("--bot", help="BOTS 里的机器人 name；--spaces 时可只给 --app-of")
    p.add_argument("--spaces", action="store_true", help="列出应用可见的知识空间")
    p.add_argument("--sync", action="store_true", help="增量同步 + 重建索引")
    p.add_argument("--rebuild", action="store_true", help="强制全量重建索引")
    p.add_argument("--search", metavar="Q", help="检索调试：打印向量路/全文路/融合结果")
    p.add_argument("--top-k", type=int, default=5)
    args = p.parse_args()

    cfg = feishu_claude.load_config(args.env)
    bots = cfg["bots"]
    bc = next((b for b in bots if b.get("name") == args.bot), None)
    if bc is None:
        if not args.bot and bots:
            bc = bots[0]
            print(f"[i] 未指定 --bot，用第一个机器人的凭证：{bc.get('name')}")
        else:
            raise SystemExit(f"找不到机器人 {args.bot!r}，可选：{[b.get('name') for b in bots]}")

    client = feishu_api.build_client(bc["app_id"], bc["app_secret"])

    if args.spaces:
        spaces = list_spaces(client)
        if not spaces:
            print("没有可见的知识空间。\n"
                  "→ 多半是机器人应用还没被加为知识库成员："
                  "知识库 → 设置 → 成员管理 → 添加应用（飞书对此不报权限错，只返回空列表）")
            return
        print(f"可见知识空间（{len(spaces)} 个）：")
        for s in spaces:
            print(f"  space_id={s['space_id']}  {s['name']}")
        return

    wiki_cfg = bc.get("wiki") or {}
    space_id = wiki_cfg.get("space_id")
    if not space_id:
        raise SystemExit(f"机器人 {bc.get('name')!r} 未配置 wiki.space_id，"
                         f"先跑 --spaces 拿到 space_id 填进 .env.{args.env}")

    import wiki_index
    dest = wiki_index.space_dir(cfg["wiki"]["cache_dir"], space_id)

    import db
    engine = db.init_engine(cfg["db"])
    session_factory = db.make_session_factory(engine)
    if args.sync or args.rebuild:
        from core.locking import index_lock
        with index_lock(dest):
            if args.rebuild:
                clear_space(dest)
                print(f"[i] 已清空 {dest}，将全量重建")
            stats = sync_space(client, space_id, dest, wiki_cfg.get("root_node"))
            print(f"同步完成：{stats}")
            n = wiki_index.build(dest, cfg["wiki"], session_factory=session_factory)
            print(f"索引完成：{n} 个 chunk")

    if args.search:
        idx = wiki_index.load(dest)
        if idx is None:
            raise SystemExit("尚无索引，先跑 --sync")
        wiki_index.debug_search(idx, args.search, cfg["wiki"], top_k=args.top_k,
                                session_factory=session_factory)


if __name__ == "__main__":
    _cli()
