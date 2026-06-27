import argparse
import json
import os
import shutil
import subprocess
import time

import psycopg2
import psycopg2.extras
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


# ---------------------------------------------------------------------------
# 数据库层：session 持久化 + 审计
# ---------------------------------------------------------------------------

def get_db_conn(env):
    conn = psycopg2.connect(
        host=env["DB_HOST"],
        port=int(env.get("DB_PORT", 5432)),
        dbname=env["DB_NAME"],
        user=env["DB_USER"],
        password=env["DB_PASSWORD"],
    )
    conn.autocommit = True
    return conn


def init_db(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                chat_id        TEXT PRIMARY KEY,
                last_dir_name  TEXT,
                permit_modes   JSONB DEFAULT '{}',
                updated_at     TIMESTAMP DEFAULT now()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id                 SERIAL PRIMARY KEY,
                chat_id            TEXT NOT NULL,
                dir_name           TEXT NOT NULL,
                claude_session_id  TEXT,
                label              TEXT,
                is_current         BOOLEAN DEFAULT FALSE,
                position           INT DEFAULT 0,
                created_at         TIMESTAMP DEFAULT now(),
                updated_at         TIMESTAMP DEFAULT now()
            );
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_claude
                ON sessions (chat_id, claude_session_id)
                WHERE claude_session_id IS NOT NULL;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id                 BIGSERIAL PRIMARY KEY,
                session_id         INT REFERENCES sessions(id) ON DELETE SET NULL,
                chat_id            TEXT,
                dir_name           TEXT,
                claude_session_id  TEXT,
                role               TEXT,
                content            TEXT,
                is_error           BOOLEAN DEFAULT FALSE,
                timed_out          BOOLEAN DEFAULT FALSE,
                created_at         TIMESTAMP DEFAULT now()
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_dir ON messages (chat_id, dir_name, created_at);")


def db_load_state(conn, chat_id):
    """从 DB 恢复 last_dir_name / permit_modes / dir_sessions（含 _row_id 与 history）"""
    state = {"last_dir_name": None, "permit_modes": {}, "dir_sessions": {}}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_dir_name, permit_modes FROM bot_state WHERE chat_id = %s",
            (chat_id,),
        )
        row = cur.fetchone()
        if row:
            state["last_dir_name"] = row[0]
            state["permit_modes"] = row[1] or {}

        # 重建每个目录的 session 列表
        cur.execute(
            """SELECT id, dir_name, claude_session_id, label, is_current
               FROM sessions WHERE chat_id = %s
               ORDER BY dir_name, position, id""",
            (chat_id,),
        )
        for row_id, dir_name, claude_sid, label, is_current in cur.fetchall():
            data = state["dir_sessions"].setdefault(
                dir_name, {"list": [], "current": 0, "history": []}
            )
            data["list"].append({"id": claude_sid, "label": label, "_row_id": row_id})
            if is_current:
                data["current"] = len(data["list"]) - 1

        # 为每个目录的当前 session 重建对话历史（最近 40 条）
        for dir_name, data in state["dir_sessions"].items():
            if not data["list"]:
                continue
            cur_entry = data["list"][data["current"]]
            data["history"] = db_load_history(conn, chat_id, cur_entry["id"])

    return state


def db_load_history(conn, chat_id, claude_sid, limit=40):
    """读取某个 Claude session 的最近 limit 条对话（按时间正序返回）"""
    if not claude_sid:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT role, content FROM messages
               WHERE chat_id = %s AND claude_session_id = %s AND role IS NOT NULL
               ORDER BY id DESC LIMIT %s""",
            (chat_id, claude_sid, limit),
        )
        rows = cur.fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def db_save_bot_state(conn, chat_id, last_dir_name, permit_modes):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO bot_state (chat_id, last_dir_name, permit_modes, updated_at)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (chat_id) DO UPDATE SET
                   last_dir_name = EXCLUDED.last_dir_name,
                   permit_modes  = EXCLUDED.permit_modes,
                   updated_at    = now()""",
            (chat_id, last_dir_name, psycopg2.extras.Json(permit_modes)),
        )


def db_insert_session(conn, chat_id, dir_name, label, position, claude_sid=None):
    """新建一条 session 行，返回其代理主键 _row_id"""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sessions (chat_id, dir_name, claude_session_id, label, position)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (chat_id, dir_name, claude_sid, label, position),
        )
        return cur.fetchone()[0]


def db_update_session(conn, row_id, claude_sid=None, label=None):
    """更新已存在 session 行的 claude_session_id / label"""
    sets, params = [], []
    if claude_sid is not None:
        sets.append("claude_session_id = %s")
        params.append(claude_sid)
    if label is not None:
        sets.append("label = %s")
        params.append(label)
    if not sets:
        return
    sets.append("updated_at = now()")
    params.append(row_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = %s", params)


def db_set_current(conn, chat_id, dir_name, row_id):
    """把 row_id 置为当前 session，同 (chat_id, dir_name) 下其余置 FALSE"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET is_current = (id = %s) WHERE chat_id = %s AND dir_name = %s",
            (row_id, chat_id, dir_name),
        )


def db_delete_session(conn, row_id):
    """删除 session 行；messages 经 FK ON DELETE SET NULL 保留"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE id = %s", (row_id,))


def db_append_message(conn, session_row_id, chat_id, dir_name, claude_sid,
                      role, content, is_error=False, timed_out=False):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages
               (session_id, chat_id, dir_name, claude_session_id, role, content, is_error, timed_out)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (session_row_id, chat_id, dir_name, claude_sid, role, content, is_error, timed_out),
        )


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
    """回复消息。网络/接口失败时记录日志但不抛出，避免中断消息处理导致结果丢失。
    返回是否全部分段发送成功。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    chunks = split_text(text)
    ok = True
    for i, chunk in enumerate(chunks):
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": chunk}),
        }
        try:
            resp = requests.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply",
                headers=headers,
                json=body,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                ok = False
                print(f"[warn] reply chunk {i+1}/{len(chunks)} failed: {data}")
        except requests.RequestException as e:
            ok = False
            print(f"[warn] reply chunk {i+1}/{len(chunks)} network error: {e}")
    return ok


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

    default_name = dir_names[0] if dir_names else "daily-assistant"

    # 数据库连接
    conn = get_db_conn(env)
    init_db(conn)
    db_holder = {"conn": conn}

    def db_call(fn, *a, **kw):
        """执行 DB 操作，连接失效时自动重连重试一次，失败不致命"""
        try:
            return fn(db_holder["conn"], *a, **kw)
        except psycopg2.Error as e:
            print(f"[db-error] {e}; reconnecting")
            try:
                db_holder["conn"] = get_db_conn(env)
                return fn(db_holder["conn"], *a, **kw)
            except Exception as e2:
                print(f"[db-error] retry failed: {e2}")
                return None

    # 恢复持久化状态
    _state = db_call(db_load_state, chat_id) or {}
    _saved_dir = _state.get("last_dir_name")
    last_dir = {
        "name": _saved_dir if _saved_dir in work_dirs else default_name,
        "path": work_dirs[_saved_dir if _saved_dir in work_dirs else default_name],
    }
    # {dir_name: {"list": [{id, label, _row_id}, ...], "current": idx, "history": [{role, content}, ...]}}
    dir_sessions = _state.get("dir_sessions", {})

    pending = None  # "dir" | "session"，当前等待用户回复序号的类型
    permit_modes = _state.get("permit_modes", {})  # {dir_name: bool}，每个目录独立的权限模式
    last_task = None      # {"text", "msg_id", "dir_name", "session_id"}，用于 /permit 重跑

    def save():
        db_call(db_save_bot_state, chat_id, last_dir["name"], permit_modes)
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

    def new_session_entry(label=None):
        ts = time.strftime("%m-%d %H:%M")
        return {"id": None, "label": label or f"新对话 {ts}", "_row_id": None}

    def make_session(dir_name, label=None):
        """创建内存 session 条目并在 DB 中建行，返回条目（含 _row_id）"""
        entry = new_session_entry(label)
        position = len(dir_sessions.get(dir_name, {}).get("list", []))
        entry["_row_id"] = db_call(db_insert_session, chat_id, dir_name, entry["label"], position)
        return entry

    def _load_history(dir_name, claude_sid):
        return db_call(db_load_history, chat_id, claude_sid) or []

    def update_session(dir_name, session_id, first_task=None):
        """首次执行时写入 session_id 和标签，并同步到 DB"""
        data = dir_sessions.setdefault(dir_name, {"list": [], "current": 0})
        entry = data["list"][data["current"]]
        entry["id"] = session_id
        new_label = None
        if first_task and entry["label"].startswith("新对话"):
            ts = time.strftime("%m-%d %H:%M")
            entry["label"] = f"{ts} {first_task[:20]}{'…' if len(first_task) > 20 else ''}"
            new_label = entry["label"]
        if entry.get("_row_id"):
            db_call(db_update_session, entry["_row_id"], claude_sid=session_id, label=new_label)

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
                            save()
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
                            if sessions[idx].get("_row_id"):
                                db_call(db_set_current, chat_id, dir_name, sessions[idx]["_row_id"])
                            # 切换 session 后重建该对话的历史
                            dir_sessions[dir_name]["history"] = _load_history(dir_name, sessions[idx]["id"])
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

                if text.lower() == "/help":
                    reply_message(token, msg["id"], "\n".join([
                        "可用命令：",
                        "/ls        — 列出工作目录，回复序号切换",
                        "/sessions  — 列出当前目录的对话，回复序号切换",
                        "/new       — 新建对话（清空历史）",
                        "/name <名称> — 重命名当前对话",
                        "/del <序号> — 删除指定对话（序号见 /sessions）",
                        "/permit    — 开关当前目录写文件/执行命令权限",
                        "/retry     — 用当前权限重跑上一条任务",
                        "/help      — 显示此帮助",
                    ]))
                    continue

                if text.lower().startswith("/name "):
                    new_label = text[6:].strip()
                    if new_label:
                        data = dir_sessions.get(dir_name)
                        if data and data["list"]:
                            entry = data["list"][data["current"]]
                            entry["label"] = new_label
                            if entry.get("_row_id"):
                                db_call(db_update_session, entry["_row_id"], label=new_label)
                            reply_message(token, msg["id"], f"当前对话已命名为「{new_label}」")
                        else:
                            reply_message(token, msg["id"], "当前没有活跃的对话")
                    else:
                        reply_message(token, msg["id"], "用法：/name <名称>")
                    continue

                if text.lower() == "/new":
                    data = dir_sessions.setdefault(dir_name, {"list": [], "current": 0, "history": []})
                    entry = make_session(dir_name)
                    data["list"].append(entry)
                    data["current"] = len(data["list"]) - 1
                    data["history"] = []  # 清空历史，真正从零开始
                    if entry.get("_row_id"):
                        db_call(db_set_current, chat_id, dir_name, entry["_row_id"])
                    pending = None
                    reply_message(token, msg["id"], f"[{dir_name}] 已创建新对话（共 {len(data['list'])} 个），发送任务即可开始")
                    print(f"[new-session] {dir_name} total={len(data['list'])}")
                    continue

                if text.lower() == "/permit":
                    cur = permit_modes.get(dir_name, False)
                    permit_modes[dir_name] = not cur
                    status = "已开启（后续任务可写文件/执行命令）" if permit_modes[dir_name] else "已关闭"
                    save()
                    reply_message(token, msg["id"], f"[{dir_name}] 权限模式 {status}")
                    print(f"[permit] {dir_name}={permit_modes[dir_name]}")
                    if permit_modes[dir_name] and last_task:
                        reply_message(token, msg["id"], f"是否用新权限重跑上一条任务？发送 /retry 确认")
                    continue

                if text.lower().startswith("/del"):
                    arg = text[4:].strip()
                    data = dir_sessions.get(dir_name)
                    if not arg.isdigit():
                        reply_message(token, msg["id"], "用法：/del <序号>（序号见 /sessions）")
                        continue
                    if not data or not data["list"]:
                        reply_message(token, msg["id"], f"[{dir_name}] 当前没有可删除的对话")
                        continue
                    didx = int(arg) - 1
                    if not (0 <= didx < len(data["list"])):
                        reply_message(token, msg["id"], f"无效序号，请输入 1～{len(data['list'])}")
                        continue
                    removed = data["list"].pop(didx)
                    # 删磁盘上的 Claude session 文件
                    if removed["id"]:
                        try:
                            delete_claude_session(removed["id"], work_dirs[dir_name])
                        except Exception as e:
                            print(f"[del] remove claude session file failed: {e}")
                    # 删 DB session 行（审计记录经 SET NULL 保留）
                    if removed.get("_row_id"):
                        db_call(db_delete_session, removed["_row_id"])
                    # 修正 current 指针
                    if not data["list"]:
                        # 删空了，补一个新的空对话
                        new_entry = make_session(dir_name)
                        data["list"].append(new_entry)
                        data["current"] = 0
                        data["history"] = []
                        if new_entry.get("_row_id"):
                            db_call(db_set_current, chat_id, dir_name, new_entry["_row_id"])
                    else:
                        if didx <= data["current"]:
                            data["current"] = max(0, data["current"] - 1)
                        cur_entry = data["list"][data["current"]]
                        if cur_entry.get("_row_id"):
                            db_call(db_set_current, chat_id, dir_name, cur_entry["_row_id"])
                        data["history"] = _load_history(dir_name, cur_entry["id"])
                    pending = None
                    reply_message(token, msg["id"], f"[{dir_name}] 已删除对话「{removed['label']}」（剩 {len(data['list'])} 个）")
                    print(f"[del] {dir_name} removed idx={didx} row={removed.get('_row_id')}")
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
                    # 落库审计
                    _rdata = dir_sessions.get(t["dir_name"], {})
                    _rentry = _rdata["list"][_rdata["current"]] if _rdata.get("list") else None
                    _rrow = _rentry.get("_row_id") if _rentry else None
                    _rsid = (_rentry.get("id") if _rentry else None) or new_session_id
                    db_call(db_append_message, _rrow, chat_id, t["dir_name"], _rsid, "user", t["text"])
                    db_call(db_append_message, _rrow, chat_id, t["dir_name"], _rsid, "assistant", result,
                            result.startswith("[error]"), "超时" in result)
                    reply_message(token, t["msg_id"], result)
                    print(f"[retry] done")
                    continue

                # --- 执行任务 ---
                pending = None
                is_new = dir_sessions.get(dir_name) is None or get_current_session(dir_name) is None
                session_id = get_current_session(dir_name)

                # 首次使用此目录，自动初始化 session 条目（建 DB 行）
                if dir_sessions.get(dir_name) is None:
                    entry = make_session(dir_name)
                    dir_sessions[dir_name] = {"list": [entry], "current": 0, "history": []}
                    if entry.get("_row_id"):
                        db_call(db_set_current, chat_id, dir_name, entry["_row_id"])

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

                # 落库审计：用户输入 + LLM 输出各一条
                _data = dir_sessions.get(dir_name, {})
                _entry = _data["list"][_data["current"]] if _data.get("list") else None
                _row_id = _entry.get("_row_id") if _entry else None
                _sid = (_entry.get("id") if _entry else None) or new_session_id
                _is_err = result.startswith("[error]")
                _timed_out = "超时" in result
                db_call(db_append_message, _row_id, chat_id, dir_name, _sid, "user", text)
                db_call(db_append_message, _row_id, chat_id, dir_name, _sid, "assistant", result, _is_err, _timed_out)

                reply_message(token, msg["id"], result)
                print(f"[done] replied to {msg['id']}")

        except Exception as e:
            print(f"[error] {e}")

        time.sleep(5 if pending else current_interval)


PID_FILE = os.path.expanduser("~/run/feishu_bot/run.pid")


def write_pid():
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def remove_pid():
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    write_pid()
    try:
        main()
    except KeyboardInterrupt:
        print("\n[feishu-claude-bot] stopped")
    finally:
        remove_pid()
