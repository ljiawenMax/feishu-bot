import argparse
import json
import os
import subprocess
import time

import requests

BASE_DIR = os.path.dirname(__file__)


def load_env_file(env_name):
    env_file = os.path.join(BASE_DIR, f".env.{env_name}")
    env_vars = {}
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


_token_cache = {"token": None, "expires_at": 0.0}


def get_token(app_id, app_secret):
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"get token failed: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + data.get("expire", 7200) - 60  # 提前 60s 刷新
    return _token_cache["token"]


def fetch_new_messages(token, chat_id, since_ts):
    """拉取 since_ts 之后的用户消息，返回 list of {id, text, create_time}"""
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "start_time": since_ts,
        "page_size": 20,
    }
    resp = requests.get(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers=headers,
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"fetch messages failed: {data}")

    items = data.get("data", {}).get("items", [])
    messages = []
    for item in items:
        if item.get("msg_type") != "text":
            continue
        # 过滤 bot 自身发出的消息
        if item.get("sender", {}).get("sender_type") != "user":
            continue
        try:
            content = json.loads(item["body"]["content"])
            text = content.get("text", "").strip()
        except Exception:
            continue
        if not text:
            continue
        messages.append({
            "id": item["message_id"],
            "text": text,
            "create_time": item["create_time"],
        })
    return messages


def build_dir_prompt(dir_names):
    lines = ["请选择工作目录，回复序号："]
    for i, name in enumerate(dir_names, 1):
        lines.append(f"{i}. {name}")
    return "\n".join(lines)


def build_sessions_prompt(dir_name, sessions, current_idx):
    """sessions: list of {"id": session_id_or_None, "label": str}"""
    lines = [f"[{dir_name}] 的对话列表，回复序号切换："]
    for i, s in enumerate(sessions, 1):
        marker = " ◀ 当前" if i - 1 == current_idx else ""
        lines.append(f"{i}. {s['label']}{marker}")
    return "\n".join(lines)


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


def split_text(text, max_len=3900):
    """按换行符切分，确保每段不超过 max_len 字符。"""
    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_len and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        # 单行超长时强制硬切
        while len(line) > max_len:
            chunks.append(line[:max_len])
            line = line[max_len:]
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def reply_message(token, message_id, text):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": chunk}),
        }
        resp = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
            headers=headers,
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            print(f"[warn] reply chunk {i+1}/{len(chunks)} failed: {data}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="prod", choices=["test", "prod"], help="运行环境")
    args = parser.parse_args()

    env = load_env_file(args.env)

    app_id = env["APP_ID"]
    app_secret = env["APP_SECRET"]
    chat_id = env["CHAT_ID"]
    work_dirs = json.loads(env["WORK_DIRS"])
    dir_names = list(work_dirs.keys())
    poll_interval = int(env["POLL_INTERVAL"])
    task_timeout = int(env["TASK_TIMEOUT"])

    default_name = "daily-assistant"
    last_dir = {"name": default_name, "path": work_dirs[default_name]}

    # {dir_name: {"list": [{id, label}, ...], "current": idx, "history": [{role, content}, ...]}}
    dir_sessions = {}

    pending = None  # "dir" | "session"，当前等待用户回复序号的类型
    permit_modes = {}     # {dir_name: bool}，每个目录独立的权限模式
    last_task = None      # {"text", "msg_id", "dir_name", "session_id"}，用于 /permit 重跑
    last_ts = str(int(time.time()))
    last_poll_time = time.time()
    current_interval = 5

    print(f"[feishu-claude-bot] env={args.env}, backoff 5s~{poll_interval}s")
    print(f"  work_dirs: {dir_names}")
    print(f"  default:   {default_name}")
    print(f"  chat_id:   {chat_id}")

    def get_current_session(dir_name):
        data = dir_sessions.get(dir_name)
        if not data:
            return None
        return data["list"][data["current"]]["id"]

    def update_session(dir_name, session_id, first_task=None):
        """首次执行时写入 session_id 和标签"""
        data = dir_sessions.setdefault(dir_name, {"list": [], "current": 0})
        entry = data["list"][data["current"]]
        entry["id"] = session_id
        if first_task and entry["label"].startswith("新对话"):
            ts = time.strftime("%m-%d %H:%M")
            entry["label"] = f"{ts} {first_task[:20]}{'…' if len(first_task) > 20 else ''}"

    def new_session_entry(label=None):
        ts = time.strftime("%m-%d %H:%M")
        return {"id": None, "label": label or f"新对话 {ts}"}

    while True:
        try:
            now = time.time()
            # 距上次轮询超过 60s，说明电脑曾休眠，跳过积压消息
            if now - last_poll_time > 60:
                last_ts = str(int(now))
                print("[info] woke from sleep, resetting message timestamp")
            last_poll_time = now

            token = get_token(app_id, app_secret)
            messages = fetch_new_messages(token, chat_id, last_ts)

            if messages:
                current_interval = 5
            else:
                current_interval = min(current_interval * 4, poll_interval)

            for msg in messages:
                text = msg["text"].strip()
                last_ts = str(int(msg["create_time"]) // 1000 + 1)
                dir_name = last_dir["name"]

                # --- 等待序号时，数字消息用于选择 ---
                if pending and text.isdigit():
                    idx = int(text) - 1
                    if pending == "dir":
                        if 0 <= idx < len(dir_names):
                            last_dir = {"name": dir_names[idx], "path": work_dirs[dir_names[idx]]}
                            pending = None
                            reply_message(token, msg["id"], f"已切换到 [{last_dir['name']}]")
                            print(f"[dir] switched to {last_dir['name']}")
                        else:
                            reply_message(token, msg["id"], f"无效序号，请输入 1～{len(dir_names)}")
                    elif pending == "session":
                        sessions = dir_sessions.get(dir_name, {}).get("list", [])
                        if 0 <= idx < len(sessions):
                            dir_sessions[dir_name]["current"] = idx
                            label = sessions[idx]["label"]
                            pending = None
                            reply_message(token, msg["id"], f"已切换到对话 {idx + 1}：{label}")
                            print(f"[session] {dir_name} switched to idx={idx}")
                        else:
                            reply_message(token, msg["id"], f"无效序号，请输入 1～{len(sessions)}")
                    continue

                # --- 命令处理 ---
                if text.lower() == "/ls":
                    pending = "dir"
                    reply_message(token, msg["id"], build_dir_prompt(dir_names))
                    continue

                if text.lower() == "/sessions":
                    data = dir_sessions.get(dir_name)
                    if not data or not data["list"]:
                        reply_message(token, msg["id"], f"[{dir_name}] 当前只有 1 个对话，发送 /new 创建新对话")
                    else:
                        pending = "session"
                        reply_message(token, msg["id"], build_sessions_prompt(dir_name, data["list"], data["current"]))
                    continue

                if text.lower() == "/new":
                    data = dir_sessions.setdefault(dir_name, {"list": [], "current": 0, "history": []})
                    data["list"].append(new_session_entry())
                    data["current"] = len(data["list"]) - 1
                    data["history"] = []  # 清空历史，真正从零开始
                    pending = None
                    reply_message(token, msg["id"], f"[{dir_name}] 已创建新对话（共 {len(data['list'])} 个），发送任务即可开始")
                    print(f"[new-session] {dir_name} total={len(data['list'])}")
                    continue

                if text.lower() == "/permit":
                    cur = permit_modes.get(dir_name, False)
                    permit_modes[dir_name] = not cur
                    status = "已开启（后续任务可写文件/执行命令）" if permit_modes[dir_name] else "已关闭"
                    reply_message(token, msg["id"], f"[{dir_name}] 权限模式 {status}")
                    print(f"[permit] {dir_name}={permit_modes[dir_name]}")
                    if permit_modes[dir_name] and last_task:
                        reply_message(token, msg["id"], f"是否用新权限重跑上一条任务？发送 /retry 确认")
                    continue

                if text.lower() == "/retry" and last_task:
                    t = last_task
                    reply_message(token, msg["id"], f"正在 [{t['dir_name']}] 重跑任务，请稍候…")
                    try:
                        result, new_session_id = run_claude(t["text"], work_dirs[t["dir_name"]], task_timeout, t["session_id"], skip_permissions=permit_modes.get(t["dir_name"], False))
                        if new_session_id:
                            update_session(t["dir_name"], new_session_id)
                    except subprocess.TimeoutExpired:
                        result = f"[error] 任务超时（>{task_timeout}s）"
                    except Exception as e:
                        result = f"[error] 执行失败: {e}"
                    reply_message(token, t["msg_id"], result)
                    print(f"[retry] done")
                    continue

                # --- 执行任务 ---
                pending = None
                is_new = dir_sessions.get(dir_name) is None or get_current_session(dir_name) is None
                session_id = get_current_session(dir_name)

                # 首次使用此目录，自动初始化 session 条目
                if dir_sessions.get(dir_name) is None:
                    dir_sessions[dir_name] = {"list": [new_session_entry()], "current": 0}

                history = dir_sessions.get(dir_name, {}).get("history", [])
                last_task = {"text": text, "msg_id": msg["id"], "dir_name": dir_name, "session_id": session_id}
                permit_mode = permit_modes.get(dir_name, False)
                print(f"[task] dir={dir_name} permit={permit_mode} session={'new' if is_new else session_id[:8] + '…'} | {text[:60]}")
                reply_message(token, msg["id"], f"正在 [{dir_name}] 执行任务，请稍候…")
                try:
                    result, new_session_id = run_claude(text, last_dir["path"], task_timeout, session_id, skip_permissions=permit_mode, history=history)
                    if new_session_id:
                        update_session(dir_name, new_session_id, first_task=text if is_new else None)
                    # 更新对话历史（保留最近 20 轮避免 prompt 过长）
                    if not result.startswith("[error]"):
                        data = dir_sessions.setdefault(dir_name, {"list": [], "current": 0, "history": []})
                        data.setdefault("history", [])
                        data["history"].append({"role": "user", "content": text})
                        data["history"].append({"role": "assistant", "content": result})
                        if len(data["history"]) > 40:  # 20 轮 × 2
                            data["history"] = data["history"][-40:]
                except subprocess.TimeoutExpired:
                    result = f"[error] 任务超时（>{task_timeout}s）"
                except Exception as e:
                    result = f"[error] 执行失败: {e}"
                reply_message(token, msg["id"], result)
                print(f"[done] replied to {msg['id']}")

        except Exception as e:
            print(f"[error] {e}")

        time.sleep(5 if pending else current_interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[feishu-claude-bot] stopped")
