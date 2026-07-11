"""飞书 API 交互层：获取 token、拉取消息、回复消息，以及回复文本的构造与分段。

token 缓存按 app_id 分键，便于将来单进程驱动多个机器人（不同 app_id 互不串号）。
"""

import json
import time

import requests

_token_cache = {}  # app_id -> {"token": str, "expires_at": float}


def get_token(app_id, app_secret):
    now = time.time()
    cached = _token_cache.get(app_id)
    if cached and now < cached["expires_at"]:
        return cached["token"]
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"get token failed: {data}")
    _token_cache[app_id] = {
        "token": data["tenant_access_token"],
        "expires_at": now + data.get("expire", 7200) - 60,  # 提前 60s 刷新
    }
    return _token_cache[app_id]["token"]


def fetch_new_messages(token, chat_id, since_ts):
    """拉取 since_ts 之后的用户消息，返回 list of {id, text, create_time}"""
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "start_time": since_ts,
        "page_size": 20,
    }
    resp = requests.get(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers=headers,
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"fetch messages failed: {data}")

    items = data.get("data", {}).get("items", [])
    messages = []
    for item in items:
        # 过滤 bot 自身发出的消息
        if item.get("sender", {}).get("sender_type") != "user":
            continue
        msg_type = item.get("msg_type")
        try:
            content = json.loads(item["body"]["content"])
        except Exception:
            continue

        base = {"id": item["message_id"], "create_time": item["create_time"]}

        if msg_type == "text":
            text = content.get("text", "").strip()
            if not text:
                continue
            messages.append({**base, "kind": "text", "text": text})
        elif msg_type in ("image", "file", "media", "audio"):
            parsed = _parse_file_message(msg_type, content)
            if parsed:
                messages.append({**base, **parsed})
        elif msg_type == "post":
            text, images = _parse_post(content)
            if text or images:
                messages.append({**base, "kind": "post", "text": text, "images": images})
        # 其余类型（sticker/location/合并转发等）暂不支持，仍返回以便留痕+提示
        else:
            messages.append({**base, "kind": "unsupported", "msg_type": msg_type,
                             "raw": item.get("body", {}).get("content", "")})
    return messages


def _parse_post(content):
    """解析富文本 post：返回 (拼接的文字, [image_key, ...])。
    post content 形如 {"title": "..", "content": [[{tag,text/image_key,...}, ...], ...]}"""
    texts, images = [], []
    title = content.get("title") if isinstance(content, dict) else None
    if title:
        texts.append(title)
    for para in (content.get("content", []) if isinstance(content, dict) else []):
        parts = []
        for el in para:
            tag = el.get("tag")
            if tag in ("text", "a"):
                parts.append(el.get("text", ""))
            elif tag == "img":
                key = el.get("image_key")
                if key:
                    images.append(key)
        line = "".join(parts).strip()
        if line:
            texts.append(line)
    return "\n".join(texts).strip(), images


def _parse_file_message(msg_type, content):
    """把图片/文件/音视频消息解析成统一结构，返回 None 表示无法处理。
    返回 {kind, resource_type, file_key, file_name}。"""
    if msg_type == "image":
        key = content.get("image_key")
        if not key:
            return None
        return {"kind": "file", "resource_type": "image", "file_key": key, "file_name": ""}
    # file / media / audio 都走 type=file 下载
    key = content.get("file_key")
    if not key:
        return None
    return {
        "kind": "file",
        "resource_type": "file",
        "file_key": key,
        "file_name": content.get("file_name", ""),
    }


def download_to_file(token, message_id, file_key, resource_type, fileobj, max_bytes):
    """流式下载消息资源到 fileobj，超过 max_bytes 抛 ValueError。
    resource_type: 'image' 用 type=image，其它用 type=file。返回 (size, content_type)。"""
    rtype = "image" if resource_type == "image" else "file"
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
    with requests.get(url, headers=headers, params={"type": rtype},
                      stream=True, timeout=60) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        size = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"资源超过大小上限 {max_bytes} 字节")
            fileobj.write(chunk)
    return size, content_type


def split_text(text, max_len=3900):
    """按换行符切分，确保每段不超过 max_len 字符。"""
    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_len and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        # 单行超长时强制硬切
        while len(line) > max_len:
            chunks.append(line[:max_len])
            line = line[max_len:]
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


class SendResult:
    """reply_message 的返回：布尔上下文里等价于「是否全部成功」，
    同时用 .sends 暴露每段飞书响应明细（供审计落库）。"""

    def __init__(self, sends):
        self.sends = sends  # list of {ok, code, msg, sent_id, chars}

    @property
    def ok(self):
        return all(s["ok"] for s in self.sends) if self.sends else True

    def __bool__(self):
        return self.ok


def reply_message(token, message_id, text, tag="reply"):
    """回复消息。网络/接口失败时记录日志但不抛出，避免中断消息处理导致结果丢失。
    每段都审计飞书响应（返回的 message_id / code / msg）。返回 SendResult（可当 bool 用）。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    chunks = split_text(text)
    total = len(chunks)
    sends = []
    for i, chunk in enumerate(chunks):
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": chunk}),
        }
        try:
            resp = requests.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                headers=headers,
                json=body,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            code = data.get("code")
            sent_id = (data.get("data") or {}).get("message_id")
            snd = {"ok": code == 0, "code": code, "msg": data.get("msg"),
                   "sent_id": sent_id, "chars": len(chunk)}
            if code == 0:
                print(f"[feishu][{tag}][ok] reply_to={message_id} {i+1}/{total} "
                      f"sent_id={sent_id} chars={len(chunk)}")
            else:
                print(f"[feishu][{tag}][fail] reply_to={message_id} {i+1}/{total} "
                      f"code={code} msg={data.get('msg')}")
        except requests.RequestException as e:
            snd = {"ok": False, "code": None, "msg": str(e), "sent_id": None, "chars": len(chunk)}
            print(f"[feishu][{tag}][error] reply_to={message_id} {i+1}/{total} {e}")
        sends.append(snd)
    return SendResult(sends)


def build_sessions_prompt(sessions, current_idx):
    """sessions: list of {"id": session_id_or_None, "label": str}"""
    lines = ["对话列表，回复序号切换："]
    for i, s in enumerate(sessions, 1):
        marker = " ◀ 当前" if i - 1 == current_idx else ""
        lines.append(f"{i}. {s['label']}{marker}")
    return "\n".join(lines)
