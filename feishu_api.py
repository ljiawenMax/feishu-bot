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
        if item.get("msg_type") != "text":
            continue
        # 过滤 bot 自身发出的消息
        if item.get("sender", {}).get("sender_type") != "user":
            continue
        try:
            content = json.loads(item["body"]["content"])
            text = content.get("text", "").strip()
        except Exception:
            continue
        if not text:
            continue
        messages.append({
            "id": item["message_id"],
            "text": text,
            "create_time": item["create_time"],
        })
    return messages


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


def reply_message(token, message_id, text):
    """回复消息。网络/接口失败时记录日志但不抛出，避免中断消息处理导致结果丢失。
    返回是否全部分段发送成功。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    chunks = split_text(text)
    ok = True
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
            if data.get("code") != 0:
                ok = False
                print(f"[warn] reply chunk {i+1}/{len(chunks)} failed: {data}")
        except requests.RequestException as e:
            ok = False
            print(f"[warn] reply chunk {i+1}/{len(chunks)} network error: {e}")
    return ok


def build_dir_prompt(dir_names):
    lines = ["请选择工作目录，回复序号："]
    for i, name in enumerate(dir_names, 1):
        lines.append(f"{i}. {name}")
    return "\n".join(lines)


def build_sessions_prompt(dir_name, sessions, current_idx):
    """sessions: list of {"id": session_id_or_None, "label": str}"""
    lines = [f"[{dir_name}] 的对话列表，回复序号切换："]
    for i, s in enumerate(sessions, 1):
        marker = " ◀ 当前" if i - 1 == current_idx else ""
        lines.append(f"{i}. {s['label']}{marker}")
    return "\n".join(lines)
