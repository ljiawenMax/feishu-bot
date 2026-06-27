"""上传文件的安全下载与存储。

把飞书消息里的图片/文件/压缩包下载到隔离目录
~/run/feishu_bot/uploads/<chat_id>/，最小权限（目录 700、文件 600、绝不加执行位），
文件名消毒防路径穿越，不自动解压，超过大小上限拒绝。bot 只存不跑。
"""

import os
import re

import feishu_api

UPLOAD_ROOT = os.path.expanduser("~/run/feishu_bot/uploads")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB（可调；飞书硬上限 100MB）

# 文件名白名单：字母数字 . _ - 与中文，其余替换为 _
_SAFE_RE = re.compile(r"[^0-9A-Za-z._\-一-鿿]")

_CT_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "application/pdf": ".pdf", "application/zip": ".zip",
    "application/gzip": ".gz", "application/x-tar": ".tar",
    "audio/opus": ".opus", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
    "video/mp4": ".mp4",
}


class TooLarge(Exception):
    pass


def _safe_filename(name):
    """只取 basename + 白名单字符 + 去前导点/横线 + 截断，空则回退 file。"""
    name = (name or "").replace("\x00", "")
    name = os.path.basename(name)
    name = _SAFE_RE.sub("_", name)
    name = name.lstrip("._-")
    name = name[:120]
    return name or "file"


def _ext_from_content_type(ct):
    ct = (ct or "").split(";")[0].strip().lower()
    return _CT_EXT.get(ct, ".bin")


def _human(n):
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{int(f)}{unit}" if unit == "B" else f"{f:.1f}{unit}"
        f /= 1024


def _ensure_dir(chat_id):
    """建 root/<chat_id> 目录并显式 chmod 700（规避 umask）。"""
    safe_chat = _SAFE_RE.sub("_", (chat_id or "unknown"))[:64] or "unknown"
    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    os.chmod(UPLOAD_ROOT, 0o700)
    d = os.path.join(UPLOAD_ROOT, safe_chat)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _safe_remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


def save_upload(token, msg, chat_id):
    """下载并安全存储一个上传文件。返回 {path, file_name, size, size_human, content_type}。
    超过大小上限抛 TooLarge；其它失败抛原异常（已清理临时文件）。"""
    d = _ensure_dir(chat_id)
    prefix = _safe_filename(msg["id"])[-16:] or "msg"  # 唯一前缀，避免覆盖
    raw = msg.get("file_name") or ""
    safe = _safe_filename(raw) if raw else (
        "image" if msg.get("resource_type") == "image" else "file"
    )

    part = os.path.join(d, f".{prefix}.part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(part, flags, 0o600)
    except FileExistsError:
        _safe_remove(part)
        fd = os.open(part, flags, 0o600)

    try:
        with os.fdopen(fd, "wb") as f:
            size, content_type = feishu_api.download_to_file(
                token, msg["id"], msg["file_key"], msg.get("resource_type"),
                f, MAX_UPLOAD_BYTES,
            )
    except ValueError as e:  # 超过大小上限
        _safe_remove(part)
        raise TooLarge(str(e))
    except Exception:
        _safe_remove(part)
        raise

    # 无扩展名时按 Content-Type 补
    if os.path.splitext(safe)[1]:
        final_name = f"{prefix}_{safe}"
    else:
        final_name = f"{prefix}_{safe}{_ext_from_content_type(content_type)}"
    final_path = os.path.join(d, final_name)
    os.replace(part, final_path)   # 原子落定
    os.chmod(final_path, 0o600)    # 600，绝不加执行位

    return {
        "path": final_path, "file_name": final_name, "size": size,
        "size_human": _human(size), "content_type": content_type,
    }
