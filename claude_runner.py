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


def run_claude(task, work_dir, timeout, session_id=None, skip_permissions=False, history=None):
    cmd = ["claude", "-p", task, "--output-format", "stream-json", "--verbose"]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
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
    new_session_id = None

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
                errors = event.get("errors", [])
                # session 失效：用历史记录重建 context，开新 session 重跑
                if session_id and any("No conversation found" in e for e in errors):
                    print(f"[warn] session {session_id[:8]}… expired, rebuilding context")
                    recovered_task = build_task_with_history(task, history or [])
                    return run_claude(recovered_task, work_dir, timeout, session_id=None, skip_permissions=skip_permissions)
                return f"[error] {'; '.join(errors) or 'unknown error'}", None
            new_session_id = event.get("session_id")
            result = event.get("result") or "".join(text_parts).strip()
            # result 为空说明 session 状态异常（如权限拒绝后的残留），清掉重跑
            if not result:
                if session_id:
                    print(f"[warn] session {session_id[:8]}… returned empty, retrying fresh")
                    recovered_task = build_task_with_history(task, history or [])
                    return run_claude(recovered_task, work_dir, timeout, session_id=None, skip_permissions=skip_permissions)
                return "(no output)", new_session_id
            if timed_out:
                result += f"\n\n[超时（>{timeout}s），响应可能不完整]"
            return result, new_session_id
        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))

    result = "".join(text_parts).strip() or stderr.strip() or "(no output)"
    if timed_out:
        result += f"\n\n[超时（>{timeout}s），以上为已生成内容]"
    return result, new_session_id


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
