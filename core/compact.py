"""上下文压缩：把一段对话历史摘要成可接续的上下文。

只做「生成摘要」这一件事——开新对话、把摘要设为 seed、给用户回执，都由 `bot.py`
编排（那些要写 DB、要发飞书消息）。拆开的理由：摘要生成是纯能力，
将来 tool 与命令都要用；而回执只有飞书这一条路。
"""

import codex_runner

# 摘要独立运行一次，不恢复业务会话
MODEL = None  # 使用 Codex 当前默认模型，避免硬编码订阅不支持的模型

PROMPT = (
    "下面是一段对话记录。请把它压缩成一份简洁但完整的上下文摘要，供另一个助手无缝接续对话：\n"
    "- 保留关键事实、结论、已确定的决策、用户的偏好与约束、尚未完成的任务与待办；\n"
    "- 保留必要的文件名/路径/命令/数据等具体信息；\n"
    "- 去掉寒暄与冗余，用要点式中文，不要复述原文、不要评论。\n"
    "只输出摘要正文。"
)


def summarize(history, work_dir, timeout, log=None):
    """把历史（[{role, content}, ...]）摘要成一段接续用上下文；失败返回 None。

    用 session_id=None 单跑一次、跑完删掉这条一次性 session 文件，不污染会话列表。
    `report_actions=False`：这是内部调用，动作清单混进摘要会被下一轮当成上下文。
    """
    convo = []
    for t in history:
        who = "用户" if t["role"] == "user" else "GPT"
        convo.append(f"{who}: {t.get('content') or ''}")
    prompt = PROMPT + "\n\n" + "\n\n".join(convo)
    try:
        result, sid, _ = codex_runner.run_codex(
            prompt, work_dir, min(timeout, 300),
            session_id=None, permit=False, extra_dirs=[], history=None,
            model=MODEL, unsafe=False, report_actions=False, lightweight=True)
    except Exception as e:
        if log:
            log(f"[compact-error] {e}")
        return None
    if sid:
        try:
            codex_runner.delete_codex_session(sid, work_dir)
        except Exception as e:
            if log:
                log(f"[compact-cleanup] {e}")
    if not result or result.startswith("[error]") or result == "(no output)":
        return None
    return result


def history_from_disk(session_id, cwd, rounds=12, max_chars=2000, log=None):
    """从磁盘 session 文件读最近若干轮当摘要素材；读不到返回 []。

    给「接管来的本机会话」用：它在 `messages` 表里没有任何历史（那些对话发生在终端里），
    而这类会话恰恰最容易一上来就顶到阈值——本地历史为空就放弃压缩的话，
    护栏一触发就是死路。
    """
    try:
        return codex_runner.session_tail(session_id, cwd, rounds=rounds, max_chars=max_chars)
    except Exception as e:
        if log:
            log(f"[compact-tail-error] {e}")
        return []
