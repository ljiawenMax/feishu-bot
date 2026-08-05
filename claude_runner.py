"""Claude Code 执行层：调用 claude CLI 执行任务、解析 stream-json 输出，
以及删除 Claude 在磁盘上保存的 session 文件。"""

import json
import os
import shutil
import subprocess


def build_task_with_history(task, history):
    """将对话历史拼入 prompt，恢复 session 失效后的 context。"""
    if not history:
        return task
    lines = ["[以下是之前的对话记录，请根据此继续]\n"]
    for turn in history:
        role = "用户" if turn["role"] == "user" else "Claude"
        lines.append(f"{role}: {turn['content']}")
    lines.append(f"\n[用户新消息]\n{task}")
    return "\n".join(lines)


SYNC_SYSTEM_PROMPT = (
    "你在一次性非交互环境中执行任务：没有独立后台进程，也无法在结束后主动通知用户。"
    "请在本次运行内同步完成全部工作（含数据下载、解析、计算与分析），把最终结果直接作为回复输出。"
    "即使耗时很久也要持续执行到真正完成，严禁回复「已在后台运行」「稍后通知你」之类未完成的话。"
)


WIKI_SYSTEM_PROMPT = (
    "本次 prompt 中若带有「知识库参考资料」，那是从飞书知识库检索出的相关条目。"
    "回答时优先依据这些资料，不要凭印象改写其中的命令、参数或步骤；"
    "答完在末尾用「来源：<文档标题> <链接>」列出实际引用到的资料（没用到的不要列）。"
    "若参考资料与问题无关或不足以回答，明确说明「知识库中未找到相关内容」，再用你自己的知识作答，"
    "并提示可以把这个问题补进知识库。"
)


# ---------------------------------------------------------------- 本轮动作回报
#
# stream-json 的 assistant 消息里 content 是一组块：`text` 是它**说**的话，`tool_use` 是它
# **做**的事。只收 text 的话，飞书里永远只看得到「它说了什么」，看不到「它做了什么」——
# 2026-08-04 的事故正是这样：那一轮实际发生了一个 Write 写进 .claude/settings.local.json
# 把 LLM 通道打瘫，而回复读起来只像一段配置建议，没人看得出文件已经落到磁盘上了。
#
# 只报「写」和「执行」，不报 Read/Grep/Glob：只读操作量大且无风险，全报会把正文淹掉。
# Bash 必须报——群里 /unsafe 常开，能跑任意命令，这恰恰最该被看见。
# 绝不回显 Write 的 content / Edit 的 new_string：可能很大，也可能含密钥。
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Update")
ACTIONS_MAX = 8          # 超出折叠，避免撑爆飞书分段
BASH_MAX_CHARS = 90


def _collect_action(block, actions):
    name, inp = block.get("name"), block.get("input") or {}
    if not isinstance(inp, dict):
        return
    if name in WRITE_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path")
        if path:
            actions.append(("write", str(path)))
    elif name == "Bash":
        cmd = (inp.get("command") or "").strip()
        if cmd:
            actions.append(("bash", " ".join(cmd.split())))


def format_actions(actions, work_dir):
    """把本轮动作拼成一段附在回复末尾。去重（同一文件改 5 次只报一次）+ 截断。"""
    seen, uniq = set(), []
    for kind, detail in actions:
        key = (kind, detail)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((kind, detail))
    if not uniq:
        return ""
    lines = ["", "━━━━━━━━━━", "📝 本轮动作"]
    for kind, detail in uniq[:ACTIONS_MAX]:
        if kind == "write":
            try:
                shown = os.path.relpath(detail, work_dir)   # 绝对路径太长会刷屏
            except ValueError:
                shown = detail
            if shown.startswith(".."):                      # 落在 work_dir 之外，保留绝对路径才看得出越界
                shown = detail
            lines.append(f"  ✏️ 写入 {shown}")
        else:
            cmd = detail if len(detail) <= BASH_MAX_CHARS else detail[:BASH_MAX_CHARS] + "…"
            lines.append(f"  ▶️ {cmd}")
    if len(uniq) > ACTIONS_MAX:
        lines.append(f"  …等 {len(uniq)} 项")
    return "\n".join(lines)


def run_claude(task, work_dir, timeout, session_id=None, permit=False, extra_dirs=None,
               history=None, model=None, unsafe=False, extra_system=None,
               report_actions=True):
    """extra_system：追加一段系统提示（CLI 支持多次 --append-system-prompt）。
    知识库回答策略走这里，不动通用的 SYNC_SYSTEM_PROMPT。
    report_actions：把本轮的写/执行动作附在返回文本末尾（见 format_actions）；
    压缩摘要那类「产物要原样当上下文用」的调用要关掉，否则动作清单会混进摘要。"""
    cmd = ["claude", "-p", task, "--output-format", "stream-json", "--verbose",
           "--append-system-prompt", SYNC_SYSTEM_PROMPT]
    if extra_system:
        cmd += ["--append-system-prompt", extra_system]
    # unsafe 优先级最高：跳过全部权限校验（可执行任意命令，含 bash）
    # permit = acceptEdits：可在工作区（cwd + extra_dirs）内读写/改文件，但不执行任意命令
    if unsafe:
        cmd += ["--dangerously-skip-permissions"]
    elif permit:
        cmd += ["--permission-mode", "acceptEdits"]
    for d in (extra_dirs or []):
        cmd += ["--add-dir", d]
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]

    proc = subprocess.Popen(cmd, cwd=work_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        timed_out = True

    text_parts = []
    actions = []       # 本轮的写/执行动作，附在回复末尾（见 format_actions）
    new_session_id = None
    used_model = None  # 实际产出答复的主模型（取最后一条 assistant 的 model）

    def tail():
        return format_actions(actions, work_dir) if report_actions else ""

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type")
        if etype == "result":
            if event.get("is_error"):
                error_message = format_claude_error(event)

                # session 失效：用历史记录重建 context，开新 session 重跑
                if session_id and "No conversation found" in error_message:
                    print(f"[warn] session {session_id[:8]}… expired, rebuilding context")

                    recovered_task = build_task_with_history(
                        task,
                        history or []
                    )

                    return run_claude(
                        recovered_task,
                        work_dir,
                        timeout,
                        session_id=None,
                        permit=permit,
                        extra_dirs=extra_dirs,
                        model=model,
                        unsafe=unsafe,
                        extra_system=extra_system,
                        report_actions=report_actions,
                    )

                return f"[error] {error_message}" + tail(), None, used_model
            new_session_id = event.get("session_id")
            result = event.get("result") or "".join(text_parts).strip()
            # result 为空说明 session 状态异常（如权限拒绝后的残留），清掉重跑
            if not result:
                if session_id:
                    print(f"[warn] session {session_id[:8]}… returned empty, retrying fresh")
                    recovered_task = build_task_with_history(task, history or [])
                    return run_claude(recovered_task, work_dir, timeout, session_id=None,
                                      permit=permit, extra_dirs=extra_dirs, model=model,
                                      unsafe=unsafe, extra_system=extra_system,
                                      report_actions=report_actions)
                return "(no output)", new_session_id, used_model
            if timed_out:
                result += f"\n\n[超时（>{timeout}s），响应可能不完整]"
            return result + tail(), new_session_id, used_model
        elif etype == "assistant":
            msg = event.get("message", {})
            if msg.get("model"):
                used_model = msg["model"]
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    _collect_action(block, actions)

    result = "".join(text_parts).strip() or stderr.strip() or "(no output)"
    if timed_out:
        result += f"\n\n[超时（>{timeout}s），以上为已生成内容]"
    return result + tail(), new_session_id, used_model

def format_claude_error(event):
    status = event.get("api_error_status")

    message = (
        "; ".join(event.get("errors", []))
        or event.get("result")
        or event.get("message")
        or "unknown error"
    )

    if "usage limit" in message.lower():
        return f"额度已用尽（订阅使用限额），请等待额度重置或升级套餐: {message}"

    mapping = {
        401: "认证失败",
        403: "权限不足",
        429: "请求受限（限流或额度不足）",
        500: "Claude 服务异常",
    }

    prefix = mapping.get(status)

    if prefix:
        return f"{prefix}: {message}"

    return message

def session_context(claude_session_id, work_dir):
    """读取磁盘 session 文件，返回最近一轮的 context 占用信息。

    返回 dict（无文件/无用量记录时返回 None）：
      input          本轮新输入 token
      cache_creation 本轮写入缓存 token
      cache_read     本轮命中缓存 token
      output         本轮输出 token
      total_input    input+cache_creation+cache_read ≈ 当前 context 窗口占用
      model          最近一条 assistant 消息的模型
      user_turns     文件中真实用户消息条数（不含工具结果）
    """
    project_key = work_dir.replace("/", "-")
    path = os.path.expanduser(f"~/.claude/projects/{project_key}/{claude_session_id}.jsonl")
    if not os.path.exists(path):
        return None
    last_usage = None
    last_model = None
    user_turns = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "assistant":
                msg = event.get("message", {})
                usage = msg.get("usage")
                if usage:
                    last_usage = usage
                    last_model = msg.get("model") or last_model
            elif etype == "user":
                # 只数真正的用户输入；工具结果也是 user 事件，content 为 list，需排除
                content = event.get("message", {}).get("content")
                if isinstance(content, str) or (
                    isinstance(content, list)
                    and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                ):
                    user_turns += 1
    if not last_usage:
        return None
    inp = last_usage.get("input_tokens", 0)
    cc = last_usage.get("cache_creation_input_tokens", 0)
    cr = last_usage.get("cache_read_input_tokens", 0)
    return {
        "input": inp,
        "cache_creation": cc,
        "cache_read": cr,
        "output": last_usage.get("output_tokens", 0),
        "total_input": inp + cc + cr,
        "model": last_model,
        "user_turns": user_turns,
    }


def list_all_sessions():
    """扫描 ~/.claude/projects 下全部 project 目录的 session 文件——本机所有 Claude Code
    会话，不限于本 bot 的 work_dir/chat_id。按最近修改时间倒序返回：
    [{"project", "session_id", "title", "mtime"}, ...]（project/title 缺失时可能为 None）。"""
    base = os.path.expanduser("~/.claude/projects")
    results = []
    if not os.path.isdir(base):
        return results
    for project_dir in os.scandir(base):
        if not project_dir.is_dir():
            continue
        for entry in os.scandir(project_dir.path):
            if not entry.is_file() or not entry.name.endswith(".jsonl"):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            cwd, title = _peek_session_meta(entry.path)
            results.append({
                "project": cwd or project_dir.name,
                "session_id": entry.name[:-len(".jsonl")],
                "title": title,
                "mtime": mtime,
            })
    results.sort(key=lambda r: r["mtime"], reverse=True)
    return results


def _peek_session_meta(path, max_lines=200):
    """只读文件前若干行取 cwd（首条带 cwd 的事件）与 ai-title（Claude 生成的会话摘要），
    两者都拿到即提前退出，避免整份读大文件。"""
    cwd = None
    title = None
    try:
        with open(path) as f:
            for i, line in enumerate(f):
                if i >= max_lines or (cwd and title):
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not cwd and event.get("cwd"):
                    cwd = event["cwd"]
                if not title and event.get("type") == "ai-title":
                    title = event.get("aiTitle")
    except OSError:
        pass
    return cwd, title


def delete_claude_session(claude_session_id, work_dir):
    """删除 Claude Code 在磁盘上保存的 session 文件"""
    project_key = work_dir.replace("/", "-")
    base = os.path.expanduser(f"~/.claude/projects/{project_key}")
    jsonl = os.path.join(base, f"{claude_session_id}.jsonl")
    if os.path.exists(jsonl):
        os.remove(jsonl)
    sub = os.path.join(base, claude_session_id)
    if os.path.isdir(sub):
        shutil.rmtree(sub)
