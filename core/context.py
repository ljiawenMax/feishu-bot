"""context 占用计算与分级提醒文案。

每轮 `--resume` 都会把整段对话重喂给模型，忘了开新对话时 context 越滚越大、
每轮重复烧订阅额度。这层只负责「算出占了多少」与「该说什么」，
「怎么说」（回复到群里还是返回给 tool）留给调用方。
"""

import codex_runner

# 窗口从 Codex 的 token_count.model_context_window 读取，不假设固定大小

# 护栏阈值（百分比）：随每条回复 inline 提示，越高越醒目
WARN = 60   # 软提醒：建议开新对话
HIGH = 80   # 强提醒
HARD = 92   # 硬闸：未开自动压缩时，触顶即暂停执行、要求手动处理


def pct(session_id, cwd, log=None):
    """读盘取某 session 当前 context 占用百分比（只读、不阻塞）；取不到返回 None。

    log 是可选的日志回调（bot 传它自己带名字的 print），这层不知道自己属于哪个 bot。
    """
    if not session_id:
        return None
    try:
        info = codex_runner.session_context(session_id, cwd)
    except Exception as e:
        if log:
            log(f"[context-pct-error] {e}")
        return None
    if not info or not info.get("context_window"):
        return None
    return min(info["total_input"] / info["context_window"] * 100, 100)


def warning(value):
    """按 context 占用给分级提醒文案（附在回复末尾）；未越阈值返回空串。"""
    if value is None:
        return ""
    if value >= HIGH:
        return (f"⚠️ 当前对话 context 已达 {value:.0f}%，强烈建议 /new 开新对话"
                "（或直接说「压缩一下上下文」），否则每轮都在重复消耗额度")
    if value >= WARN:
        return (f"💡 当前对话 context 已达 {value:.0f}%，可考虑 /new 开新对话"
                "（或说「压缩一下上下文」）以节省额度")
    return ""
