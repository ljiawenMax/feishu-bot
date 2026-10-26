"""会话列表的纯数据操作。

会话状态在 bot 里是一个字典：

    {"list": [entry, ...], "current": <int 下标>, "history": [...]}

entry = {"id": Codex session_id 或 None, "label": str, "model": None, "effort": None,
         "cwd": None 或接管来的原项目目录, "external": bool, "_row_id": DB 行号}

本模块只动这个结构本身，**不碰 DB、不发飞书消息**：DB 写入（`insert_session` /
`set_current` / `delete_session`）与用户回执留在 `bot.py`，因为它们各自需要
`chat_id` 与 `message_id`——那正是这层刻意不知道的东西。
"""

import os
import time

import codex_runner


def empty():
    """一个空的会话状态（新 chat 或 DB 里没记录时的初值）。"""
    return {"list": [], "current": 0, "history": []}


def new_entry(label=None, claude_sid=None, cwd=None, external=False):
    """构造一个内存会话条目。`_row_id` 由调用方写 DB 后回填。

    claude_sid/cwd/external 只在「接管本机已有会话」时给：cwd 记住那个会话原本的
    项目目录（`--resume` 必须在原目录下跑），external 标记「盘上的文件不属于我们」。
    """
    ts = time.strftime("%m-%d %H:%M")
    return {"id": claude_sid, "label": label or f"新对话 {ts}", "model": None, "effort": None,
            "cwd": cwd, "external": external, "_row_id": None}


def current(data):
    """当前会话条目；列表为空返回 None。"""
    if not data["list"]:
        return None
    return data["list"][data["current"]]


def current_sid(data):
    """当前会话的 Codex session_id；无会话或尚未首次执行返回 None。"""
    entry = current(data)
    return entry["id"] if entry else None


def entry_cwd(entry, default_cwd):
    """某个会话条目实际该在哪个目录下跑 Codex：接管来的本机会话用它自己的原项目目录，
    其余一律是调用方给的默认（bot 的 work_dir）。Codex 会按 ID 查会话文件，cwd 必须仍跟着会话走，以免续聊时改错项目。"""
    if entry and entry.get("cwd"):
        return entry["cwd"]
    return default_cwd


def find_by_sid(data, sid):
    """按 Codex session_id 找下标；找不到返回 None。
    接管时用它判断「这个本机会话是不是已经接管过」——重复 append 会撞
    `sessions` 表的 (chat_id, claude_session_id) 唯一索引。"""
    return next((i for i, e in enumerate(data["list"]) if e.get("id") == sid), None)


def append_and_focus(data, entry, clear_history=True):
    """把新条目挂到列表末尾并切过去，返回条目总数。

    `/new`、接管本机会话共用；压缩复用原条目。clear_history=False 用于接管——
    那种会话的历史要从盘上读，不是清空。
    """
    data["list"].append(entry)
    data["current"] = len(data["list"]) - 1
    if clear_history:
        data["history"] = []
    return len(data["list"])


def remove_at(data, idx):
    """从列表摘掉第 idx 个条目（**只改内存结构**），返回被摘掉的条目。

    指针修正在这里一起做：删的是当前项或它前面的项时，`current` 要往前挪一格，
    否则指针会落到别人身上（或越界）。删空的情况由调用方补一个新条目——补新条目
    要写 DB，是这层不做的事。
    """
    removed = data["list"].pop(idx)
    if not data["list"]:
        data["current"] = 0
    elif idx <= data["current"]:
        data["current"] = max(0, data["current"] - 1)
    return removed


def attach_precheck(s, isdir=os.path.isdir):
    """接管一个本机会话前的可行性检查；可以接管返回 None，否则返回拒绝文案。

    三道硬条件：必须知道原工作区、工作区仍存在、会话文件仍存在。
    """
    cwd, sid = s.get("cwd"), s.get("session_id")
    if not cwd:
        return "这个会话没记录工作目录，无法接管"
    if not isdir(cwd):
        return f"原项目目录已不存在，无法接管：{cwd}"
    if not codex_runner.session_exists(sid, cwd):
        return "会话文件已不存在，无法接管"
    return None


def filter_local(sessions, keyword):
    """按关键词过滤 `codex_runner.list_all_sessions()` 的结果（标题或项目路径子串，
    大小写不敏感）。关键词为空时原样返回。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return sessions
    return [s for s in sessions
            if kw in (s["title"] or "").lower() or kw in (s["project"] or "").lower()]
