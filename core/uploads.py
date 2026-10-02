"""附件的路径校验与上下文数据操作；不依赖飞书或数据库。"""
import os
import re

SEND_FILE_RE = re.compile(r"\[\[SEND_FILE:([^\]\r\n]+)\]\]")
MAX_FILES = 3
MAX_BYTES = 20 * 1024 * 1024


def validate_path(path):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("必须是绝对路径")
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise ValueError("文件不存在或不是普通文件")
    if os.path.getsize(real) > MAX_BYTES:
        raise ValueError("超过 20MB，拒绝发送")
    return real


def extract_files(text):
    paths, rejected = [], []
    for raw in SEND_FILE_RE.findall(text):
        path = raw.strip()
        try:
            real = validate_path(path)
            if real in paths:
                continue
            if len(paths) >= MAX_FILES:
                raise ValueError("一条回复最多发 3 个文件")
            paths.append(real)
        except (ValueError, OSError) as e:
            rejected.append(f"{os.path.basename(path)}：{e}")
    return SEND_FILE_RE.sub("", text).strip(), paths, rejected


def expire(items, now, ttl):
    return [u for u in items if u["ts"] >= now - ttl]


def attach_block(items):
    return "\n".join(["", "[随消息附带的文件，可直接读取]"] +
                     [f"{u['path']}（{u['size_human']}）" for u in items])
