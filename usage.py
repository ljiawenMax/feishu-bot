"""ChatGPT/Codex 订阅用量，走官方 app-server account/rateLimits/read。

由 CLI 管理登录与刷新；不读取或转发 auth.json。/usage 按需查询、60 秒缓存；
回复末尾只读缓存，避免每轮启动 app-server 或额外请求。
"""

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from codex_runner import NoCredentials, TokenExpired, RateLimited, read_account_data

_cache = {"data": None, "ts": 0.0, "sub": None}
_lock = threading.Lock()
_blocked_until = 0.0


def fetch_usage(timeout=20):
    return read_account_data(lambda rpc: rpc("account/rateLimits/read", 3), timeout=timeout)


def _windows(data):
    buckets = data.get("rateLimitsByLimitId") or {}
    if not buckets:
        limits = data.get("rateLimits") or {}
        buckets = {limits.get("limitId") or "codex": limits}
    for bucket_id, limits in buckets.items():
        for key in ("primary", "secondary"):
            window = limits.get(key) or {}
            if window.get("usedPercent") is not None:
                yield limits.get("limitName") or bucket_id, window


def _label(minutes):
    if not minutes:
        return "额度窗口"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def _reset_str(epoch):
    if epoch is None:
        return "重置时间未知"
    return "重置于 " + datetime.fromtimestamp(epoch, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")


def format_usage(data, sub=None, from_cache=False):
    lines = [f"ChatGPT / Codex 订阅用量{f'（{sub}）' if sub else ''}"]
    windows = list(_windows(data))
    for name, window in windows:
        pct = window["usedPercent"]
        mark = "⚠️ " if pct >= 80 else ""
        lines.append(f"• {name} {_label(window.get('windowDurationMins'))}："
                     f"{mark}{pct:.0f}%（{_reset_str(window.get('resetsAt'))}，北京时间）")
    if not windows:
        lines.append("当前账户未返回额度窗口数据")
    if from_cache:
        lines.append("（数据来自缓存）")
    return "\n".join(lines)


def report(min_interval=60):
    global _blocked_until
    with _lock:
        now = time.time()
        if _cache["data"] is not None and now - _cache["ts"] < min_interval:
            return format_usage(_cache["data"], _cache["sub"], True)
        if now < _blocked_until:
            if _cache["data"] is not None:
                return format_usage(_cache["data"], _cache["sub"], True) + "\n（查询退避中，显示上次结果）"
            raise RateLimited("用量查询暂时限流，请稍后再试")
        try:
            data, sub = fetch_usage()
        except (NoCredentials, TokenExpired):
            raise
        except Exception:
            _blocked_until = time.time() + 60
            if _cache["data"] is not None:
                return format_usage(_cache["data"], _cache["sub"], True) + "\n（查询失败，显示上次结果）"
            raise
        _cache.update(data=data, ts=time.time(), sub=sub)
        return format_usage(data, sub)


def report_inline(max_age=60):
    """仅展示最近 /usage 的缓存；不在任务回复路径发起网络请求。"""
    data = _cache["data"]
    if data is None or time.time() - _cache["ts"] >= max_age:
        return None
    return " · ".join(f"{name} {_label(w.get('windowDurationMins'))} {w['usedPercent']:.0f}%"
                      for name, w in _windows(data)) or None
