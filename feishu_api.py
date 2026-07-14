"""飞书交互层：基于官方 lark-oapi SDK 的长连接（WebSocket）接收 + 发送/下载。

接收改用长连接事件（`lark.ws.Client` + `EventDispatcherHandler` 订阅
`im.message.receive_v1`），不再轮询 messages 列表；发送/下载走 `lark.Client`
（SDK 内部缓存并自动刷新 tenant_access_token，无需手写 token 缓存）。

与传输无关的部分（文本分段、消息解析、会话列表构造）原样保留，供 Bot 复用。
"""

import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


# ------------------------------------------------------------------ 客户端 / 长连接

def build_client(app_id, app_secret):
    """构造发送/下载用的 SDK 客户端；SDK 内部管理 tenant_access_token（线程安全）。
    同一 app_id 复用一个即可。"""
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def build_event_handler(chatmap, log_level=lark.LogLevel.INFO):
    """构造长连接事件分发器：收到 im.message.receive_v1 后按 chat_id 路由到对应 Bot。
    chatmap: {chat_id: bot}，bot 需实现 on_event(msg_dict)。不归本进程管的 chat 忽略。

    注意：回调跑在 ws 的 asyncio 事件循环里、且同步执行——必须快速返回，
    重活由 Bot.on_event 丢进自己的队列处理，切勿在此做网络 IO。"""
    def on_message(data):
        try:
            chat_id = data.event.message.chat_id
            bot = chatmap.get(chat_id)
            if bot is None:
                return  # 该 app 在其它群里的消息，不归本进程管
            msg = event_to_msg(data)
            if msg:
                bot.on_event(msg)
        except Exception as e:  # 事件循环里的异常必须吞掉，否则会掀翻长连接
            print(f"[feishu][event-error] {e}")

    return (lark.EventDispatcherHandler.builder("", "", log_level)
            .register_p2_im_message_receive_v1(on_message)
            .build())


def build_ws_client(app_id, app_secret, chatmap, log_level=lark.LogLevel.INFO):
    """构造某 app 的长连接客户端（已注册事件分发器）。start() 阻塞、自动重连。

    同一 app_id 全局只应有一条连接：长连接是 cluster 模式、不广播，多开只有
    随机一条能收到事件。多个群（chat_id）共用一个 app 时，它们共享这一条连接，
    靠 chatmap 按 chat_id 分发。"""
    handler = build_event_handler(chatmap, log_level)
    return lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=log_level)


# ------------------------------------------------------------------ 事件归一

def event_to_msg(data):
    """把 P2ImMessageReceiveV1 事件归一成内部 msg 字典（结构与旧轮询一致），
    供 Bot.handle_message 消费。仅处理真人用户消息；无法产出内容时返回 None。
    返回形如 {id, create_time, chat_id, kind, text/images/file_key/...}。"""
    ev = data.event
    msg = ev.message
    # 只处理真人用户消息（机器人自身/系统消息忽略）
    if ev.sender is None or ev.sender.sender_type != "user":
        return None

    base = {"id": msg.message_id, "create_time": msg.create_time, "chat_id": msg.chat_id}
    mtype = msg.message_type
    try:
        content = json.loads(msg.content)
    except Exception:
        return None

    mention_keys = [m.key for m in (msg.mentions or []) if getattr(m, "key", None)]

    if mtype == "text":
        text = _strip_mentions(content.get("text", ""), mention_keys).strip()
        if not text:
            return None
        return {**base, "kind": "text", "text": text}
    if mtype in ("image", "file", "media", "audio"):
        parsed = _parse_file_message(mtype, content)
        return {**base, **parsed} if parsed else None
    if mtype == "post":
        text, images = _parse_post(_unwrap_post(content))
        text = _strip_mentions(text, mention_keys).strip()
        if text or images:
            return {**base, "kind": "post", "text": text, "images": images}
        return None
    # 其余类型（sticker/location/合并转发等）暂不支持，仍返回以便留痕+提示
    return {**base, "kind": "unsupported", "msg_type": mtype, "raw": msg.content}


def _strip_mentions(text, mention_keys):
    """把 @机器人 在正文里的占位符（如 @_user_1）去掉，避免污染交给 Claude 的任务文本。"""
    for key in mention_keys:
        text = text.replace(key, "")
    return text


def _unwrap_post(content):
    """post 事件正文一般是 {"title":.., "content":[[...]]}；若带语言包
    {"zh_cn": {...}} 则取第一个 locale 的内层。统一成 _parse_post 可解析的结构。"""
    if isinstance(content, dict) and "content" not in content and "title" not in content:
        for v in content.values():
            if isinstance(v, dict):
                return v
    return content


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


# ------------------------------------------------------------------ 下载 / 发送

def download_to_file(client, message_id, file_key, resource_type, fileobj, max_bytes):
    """下载消息资源到 fileobj，超过 max_bytes 抛 ValueError。
    resource_type='image' 用 type=image，其它用 type=file。返回 (size, content_type)。
    注：SDK 会把资源一次性读进内存，故这里下载后按大小校验再落盘。"""
    rtype = "image" if resource_type == "image" else "file"
    req = (GetMessageResourceRequest.builder()
           .message_id(message_id)
           .file_key(file_key)
           .type(rtype)
           .build())
    resp = client.im.v1.message_resource.get(req)
    if not resp.success():
        raise RuntimeError(f"download resource failed: code={resp.code} "
                           f"msg={resp.msg} log_id={resp.get_log_id()}")
    content = resp.file.getvalue() if resp.file is not None else b""
    size = len(content)
    if size > max_bytes:
        raise ValueError(f"资源超过大小上限 {max_bytes} 字节")
    content_type = ""
    if resp.raw is not None and resp.raw.headers:
        content_type = (resp.raw.headers.get("Content-Type")
                        or resp.raw.headers.get("content-type") or "")
    fileobj.write(content)
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


def reply_message(client, message_id, text, tag="reply"):
    """回复消息（走 SDK，token 由 client 内部管理）。网络/接口失败记日志但不抛出，
    避免中断消息处理导致结果丢失。每段都审计飞书响应（返回的 message_id / code / msg）。
    返回 SendResult（可当 bool 用）。"""
    chunks = split_text(text)
    total = len(chunks)
    sends = []
    for i, chunk in enumerate(chunks):
        try:
            req = (ReplyMessageRequest.builder()
                   .message_id(message_id)
                   .request_body(ReplyMessageRequestBody.builder()
                                 .msg_type("text")
                                 .content(json.dumps({"text": chunk}))
                                 .build())
                   .build())
            resp = client.im.v1.message.reply(req)
            code = resp.code
            sent_id = resp.data.message_id if resp.data is not None else None
            snd = {"ok": resp.success(), "code": code, "msg": resp.msg,
                   "sent_id": sent_id, "chars": len(chunk)}
            if resp.success():
                print(f"[feishu][{tag}][ok] reply_to={message_id} {i+1}/{total} "
                      f"sent_id={sent_id} chars={len(chunk)}")
            else:
                print(f"[feishu][{tag}][fail] reply_to={message_id} {i+1}/{total} "
                      f"code={code} msg={resp.msg} log_id={resp.get_log_id()}")
        except Exception as e:
            snd = {"ok": False, "code": None, "msg": str(e), "sent_id": None, "chars": len(chunk)}
            print(f"[feishu][{tag}][error] reply_to={message_id} {i+1}/{total} {e}")
        sends.append(snd)
    return SendResult(sends)


def upload_file(client, file_path, file_name, file_type="stream"):
    """上传本地文件到飞书，返回 file_key（用于发文件消息）。失败抛异常。
    file_type 用通用的 'stream'（补丁是文本 .patch 文件，走通用二进制流即可）。"""
    with open(file_path, "rb") as f:
        body = (CreateFileRequestBody.builder()
                .file_type(file_type)
                .file_name(file_name)
                .file(f)
                .build())
        req = CreateFileRequest.builder().request_body(body).build()
        resp = client.im.v1.file.create(req)
    if not resp.success():
        raise RuntimeError(f"upload file failed: code={resp.code} "
                           f"msg={resp.msg} log_id={resp.get_log_id()}")
    return resp.data.file_key


def reply_file(client, message_id, file_key, tag="file"):
    """以「文件消息」回复某条消息（file_key 由 upload_file 得到）。
    返回 SendResult（与 reply_message 一致，便于统一审计）。"""
    try:
        req = (ReplyMessageRequest.builder()
               .message_id(message_id)
               .request_body(ReplyMessageRequestBody.builder()
                             .msg_type("file")
                             .content(json.dumps({"file_key": file_key}))
                             .build())
               .build())
        resp = client.im.v1.message.reply(req)
        sent_id = resp.data.message_id if resp.data is not None else None
        snd = {"ok": resp.success(), "code": resp.code, "msg": resp.msg,
               "sent_id": sent_id, "chars": None}
        if resp.success():
            print(f"[feishu][{tag}][ok] reply_to={message_id} file sent_id={sent_id}")
        else:
            print(f"[feishu][{tag}][fail] reply_to={message_id} "
                  f"code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
    except Exception as e:
        snd = {"ok": False, "code": None, "msg": str(e), "sent_id": None, "chars": None}
        print(f"[feishu][{tag}][error] reply_to={message_id} {e}")
    return SendResult([snd])


def build_sessions_prompt(sessions, current_idx):
    """sessions: list of {"id": session_id_or_None, "label": str}"""
    lines = ["对话列表，回复序号切换："]
    for i, s in enumerate(sessions, 1):
        marker = " ◀ 当前" if i - 1 == current_idx else ""
        lines.append(f"{i}. {s['label']}{marker}")
    return "\n".join(lines)
