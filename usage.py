"""查询 Claude 订阅用量（官方 oauth/usage 端点）。

数据来源：GET https://api.anthropic.com/api/oauth/usage —— Claude Code 交互式 /usage
背后的端点，返回 5 小时窗 / 7 天的利用率与重置时间。需用本机已登录的 OAuth token
（~/.claude/.credentials.json）。该端点未公开且限流极狠：必须带与 claude-code 一致的
User-Agent，且按需调用 + 缓存，绝不轮询。
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import requests

CRED_PATH = os.path.expanduser("~/.claude/.credentials.json")
ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

_cache = {"data": None, "ts": 0.0, "sub": None}
_ver = None


class NoCredentials(Exception):
    pass


class TokenExpired(Exception):
    pass


class RateLimited(Exception):
    pass


def _claude_version():
    global _ver
    if _ver:
        return _ver
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"\d+\.\d+\.\d+", out)
        _ver = m.group(0) if m else "2.1.181"
    except Exception:
        _ver = "2.1.181"
    return _ver


def read_token():
    """读取本机 OAuth accessToken 与订阅类型。"""
    if not os.path.exists(CRED_PATH):
        raise NoCredentials("未找到 ~/.claude/.credentials.json（请先在终端登录过 claude）")
    try:
        with open(CRED_PATH) as f:
            oauth = json.load(f)["claudeAiOauth"]
        return oauth["accessToken"], oauth.get("subscriptionType")
    except Exception:
        raise NoCredentials("凭证文件结构异常或不含 OAuth token")


def fetch_usage(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        # User-Agent 必须像 claude-code，否则被狠狠限流持续 429
        "User-Agent": f"claude-cli/{_claude_version()} (external)",
    }
    r = requests.get(ENDPOINT, headers=headers, timeout=15)
    if r.status_code == 401:
        raise TokenExpired("OAuth token 已过期，请在终端跑一次 claude 刷新登录")
    if r.status_code == 429:
        raise RateLimited("用量端点限流（429）")
    r.raise_for_status()
    return r.json()


def _reset_str(iso):
    try:
        delta = (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return str(iso)
    if delta <= 0:
        return "即将重置"
    h, m = int(delta // 3600), int((delta % 3600) // 60)
    if h >= 24:
        return f"约 {h // 24} 天 {h % 24} 小时后重置"
    if h:
        return f"约 {h} 小时 {m} 分后重置"
    return f"约 {m} 分后重置"


def _mark(pct):
    return "⚠️ " if pct is not None and pct >= 80 else ""


def format_usage(data, sub=None, from_cache=False):
    lines = [f"Claude 订阅用量{f'（{sub}）' if sub else ''}"]
    for key, label in (("five_hour", "5 小时窗"), ("seven_day", "7 天"),
                       ("seven_day_sonnet", "7 天 Sonnet"), ("seven_day_opus", "7 天 Opus")):
        v = data.get(key) or {}
        p = v.get("utilization")
        if p is not None:
            lines.append(f"• {label}：{_mark(p)}{p:.0f}%（{_reset_str(v.get('resets_at'))}）")
    eu = data.get("extra_usage") or {}
    if eu.get("is_enabled"):
        lines.append(f"• 额外用量：已用 {eu.get('used_credits')}/{eu.get('monthly_limit')} credits")
    if from_cache:
        lines.append("（数据来自缓存）")
    return "\n".join(lines)


def report(min_interval=60):
    """返回格式化的用量文本。min_interval 秒内复用缓存，避免触发端点限流。"""
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < min_interval:
        return format_usage(_cache["data"], _cache["sub"], from_cache=True)
    token, sub = read_token()
    try:
        data = fetch_usage(token)
    except RateLimited:
        if _cache["data"] is not None:
            return format_usage(_cache["data"], _cache["sub"], from_cache=True) + "\n（端点限流，显示上次结果）"
        raise
    _cache.update(data=data, ts=now, sub=sub)
    return format_usage(data, sub)
