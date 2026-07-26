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
import threading
import time
from datetime import datetime, timezone

import requests

CRED_PATH = os.path.expanduser("~/.claude/.credentials.json")
ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

_cache = {"data": None, "ts": 0.0, "sub": None}
_ver = None
# 单飞锁：全局任一时刻至多一个真实请求在飞，多 chat 并发只放一个打接口、其余复用结果
_lock = threading.Lock()
# 429 退避截止（epoch 秒）：命中限流后在此之前所有调用都只读缓存、绝不发请求
_blocked_until = 0.0


class NoCredentials(Exception):
    pass


class TokenExpired(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, msg="用量端点限流（429）", retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


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


def fetch_usage(token, timeout=15):
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        # User-Agent 必须像 claude-code，否则被狠狠限流持续 429
        "User-Agent": f"claude-cli/{_claude_version()} (external)",
    }
    r = requests.get(ENDPOINT, headers=headers, timeout=timeout)
    if r.status_code == 401:
        raise TokenExpired("OAuth token 已过期，请在终端跑一次 claude 刷新登录")
    if r.status_code == 429:
        ra = r.headers.get("retry-after")
        try:
            ra = float(ra) if ra is not None else None
        except (TypeError, ValueError):
            ra = None
        raise RateLimited(retry_after=ra)
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


def _fresh(now, max_age):
    return _cache["data"] is not None and now - _cache["ts"] < max_age


def _single_flight(max_age, wait_timeout, timeout):
    """单飞取用量，返回 (data, sub, stale)。
    - 缓存够新（< max_age 秒）：直接用，不发请求。
    - 处于 429 退避期：只读缓存并标 stale，绝不发请求。
    - 否则抢单飞锁：抢到→双重检查后发唯一一次真实请求；抢不到（别人正在刷）→
      最多等 wait_timeout 秒，其间别人刷好就复用新值，否则回退缓存并标 stale。
    因此全局任一时刻至多一个真实请求在飞，多 chat 并发只会有一个打到接口。
    命中 429 时按 Retry-After（缺省 240s）设置全局退避。任何失败都不抛。"""
    global _blocked_until
    now = time.time()
    if _fresh(now, max_age):
        return _cache["data"], _cache["sub"], False
    if now < _blocked_until:
        return _cache["data"], _cache["sub"], _cache["data"] is not None

    if not _lock.acquire(timeout=wait_timeout):
        return _cache["data"], _cache["sub"], _cache["data"] is not None
    try:
        now = time.time()
        if _fresh(now, max_age):                 # 等锁期间别人可能已刷好
            return _cache["data"], _cache["sub"], False
        if now < _blocked_until:
            return _cache["data"], _cache["sub"], _cache["data"] is not None
        try:
            token, sub = read_token()
            data = fetch_usage(token, timeout=timeout)
            _cache.update(data=data, ts=time.time(), sub=sub)
            return data, sub, False
        except RateLimited as e:
            _blocked_until = time.time() + (e.retry_after or 240)
        except Exception:
            pass
        return _cache["data"], _cache["sub"], _cache["data"] is not None
    finally:
        _lock.release()


def report_inline(max_age=30, wait_timeout=8, timeout=8):
    """给每条回复用的紧凑用量串，如 "5h 42% · 周 68%"；≥80% 加 ⚠️。
    同步取值（可能阻塞几秒等一次真实请求），但走单飞：多 chat 并发只放一个打
    接口、其余复用其结果或读缓存，避免并发触发 429。数据陈旧（限流/超时回退
    缓存）时串尾加 "·旧"。任何异常都不抛，取不到就返回 None。"""
    try:
        data, _sub, stale = _single_flight(max_age, wait_timeout, timeout)
        if data is None:
            return None
        parts = []
        for key, label in (("five_hour", "5h"), ("seven_day", "周")):
            v = data.get(key) or {}
            p = v.get("utilization")
            if p is not None:
                mark = "⚠️" if p >= 80 else ""
                parts.append(f"{label} {mark}{p:.0f}%")
        if not parts:
            return None
        s = " · ".join(parts)
        return f"{s} ·旧" if stale else s
    except Exception:
        return None


def report(min_interval=60):
    """/usage 命令用：返回完整用量文本。min_interval 秒内复用缓存。与 inline 共用
    单飞锁与 429 退避，避免和逐条回复的刷新并发打接口。NoCredentials/TokenExpired
    照常抛给命令调用方展示。"""
    global _blocked_until
    now = time.time()
    if _fresh(now, min_interval):
        return format_usage(_cache["data"], _cache["sub"], from_cache=True)
    token, sub = read_token()          # 无凭证/结构异常直接抛给调用方
    if now < _blocked_until:           # 退避期内不发请求，回退缓存
        if _cache["data"] is not None:
            return format_usage(_cache["data"], _cache["sub"], from_cache=True) + "\n（端点限流退避中，显示上次结果）"
        raise RateLimited("用量端点限流（429），请稍后再试")
    with _lock:
        now = time.time()
        if _fresh(now, min_interval):  # 等锁期间别人可能已刷好
            return format_usage(_cache["data"], _cache["sub"], from_cache=True)
        try:
            data = fetch_usage(token, timeout=15)
        except RateLimited as e:
            _blocked_until = time.time() + (e.retry_after or 240)
            if _cache["data"] is not None:
                return format_usage(_cache["data"], _cache["sub"], from_cache=True) + "\n（端点限流，显示上次结果）"
            raise
        _cache.update(data=data, ts=time.time(), sub=sub)
        return format_usage(data, sub)
