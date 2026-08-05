"""`.env.<name>` 解析（含跨行 JSON 值）。

**刻意与 feishu_claude.load_env_file 重复**：ops 包在主程序被改坏时仍要能跑，
import feishu_claude 会拖进 db / feishu_api / bot 整条链——其中任意一个坏掉，
救援脚本自己先起不来。几十行重复代码换救援路径不依赖被救对象，值。
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bracket_balance(text):
    """text 中未闭合的 [ / { 数量（字符串内的括号不计）。"""
    depth = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    return depth


def load(env_name="local"):
    env_file = os.path.join(BASE_DIR, f".env.{env_name}")
    env_vars = {}
    with open(env_file) as f:
        lines = f.readlines()
    i, n = 0, len(lines)
    while i < n:
        stripped = lines[i].strip()
        i += 1
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if _bracket_balance(value) > 0:
            parts = [value]
            while i < n and _bracket_balance("".join(parts)) > 0:
                parts.append(lines[i].rstrip("\n"))
                i += 1
            value = "".join(parts).strip()
        env_vars[key] = value
    return env_vars


def bots(env):
    try:
        return json.loads(env.get("BOTS") or "[]")
    except json.JSONDecodeError:
        return []
