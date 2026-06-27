"""数据库层：session 持久化 + 对话审计。

表（启动时幂等建表）：
- bot_state：每个 chat_id 的 UI 状态（当前目录、各目录权限模式）
- sessions：一行一个对话，代理主键 id；claude_session_id 首次执行前为 NULL
- messages：对话审计，每轮用户输入 + LLM 输出各一行，append-only
"""

import psycopg2
import psycopg2.extras


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
