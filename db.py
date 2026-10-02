"""数据库层：SQLAlchemy engine/session 管理 + ORM 数据访问。

所有数据访问函数首参为 ORM Session，返回纯 dict/标量（不返回 ORM 实例，
避免 session 关闭后的 DetachedInstanceError）。调用方（Bot.db）负责开 session、
commit/rollback。engine 由 launcher 创建一次、多线程共享，连接池负责重连。
"""

from urllib.parse import quote_plus

from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import sessionmaker

from models import (
    AuditLog,
    Base,
    BotState,
    Conversation,
    Message,
    Unhandled,
    Upload,
    WikiChunk,
)


def init_engine(db_cfg):
    url = (
        f"mysql+pymysql://{quote_plus(db_cfg['user'])}:{quote_plus(db_cfg['password'])}"
        f"@{db_cfg['host']}:{db_cfg['port']}/{db_cfg['dbname']}?charset=utf8mb4"
    )
    # pool_recycle：MySQL 默认 8h 断空闲连接，主动回收避免用到失效连接
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600, future=True,
                         connect_args={"connect_timeout": 10, "read_timeout": 30, "write_timeout": 30})


def create_all(engine):
    Base.metadata.create_all(engine)


def migrate(engine):
    """给已存在的表补列（create_all 不会改已存在表）。幂等。"""
    from sqlalchemy import text
    adds = [
        ("sessions", "model", "VARCHAR(64)"),
        ("messages", "model", "VARCHAR(64)"),
        ("sessions", "cwd", "VARCHAR(512)"),
        ("sessions", "external", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ]
    with engine.begin() as c:
        for tbl, col, typ in adds:
            exists = c.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=DATABASE() AND table_name=:t AND column_name=:col"
            ), {"t": tbl, "col": col}).first()
            if not exists:
                c.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}"))
                print(f"[migrate] {tbl}.{col} 已添加")

        # wiki_chunks 的中文全文索引：create_all 建不了带 parser 的索引，只能在这里补。
        # ngram 分词器（ngram_token_size=2）是 MySQL 内置的中文分词方案。
        has_ft = c.execute(text(
            "SELECT 1 FROM information_schema.statistics "
            "WHERE table_schema=DATABASE() AND table_name='wiki_chunks' "
            "AND index_name='ft_wiki_text'"
        )).first()
        if not has_ft:
            c.execute(text("ALTER TABLE wiki_chunks "
                           "ADD FULLTEXT INDEX ft_wiki_text (text) WITH PARSER ngram"))
            print("[migrate] wiki_chunks.ft_wiki_text 全文索引已创建")


def make_session_factory(engine):
    return sessionmaker(engine)


# --------------------------------------------------------------- 数据访问

def load_state(s, chat_id):
    """从 DB 恢复 permit / unsafe + 单一会话列表（含 _row_id 与 history）"""
    state = {"permit": False, "unsafe": False,
             "sessions": {"list": [], "current": 0, "history": []}}

    bs = s.get(BotState, chat_id)
    if bs:
        state["permit"] = bool(bs.permit)
        state["unsafe"] = bool(bs.unsafe)

    # 重建该 chat 的 session 列表（一 chat 一 work_dir，扁平单列）
    data = state["sessions"]
    rows = s.scalars(
        select(Conversation)
        .where(Conversation.chat_id == chat_id)
        .order_by(Conversation.position, Conversation.id)
    ).all()
    for conv in rows:
        data["list"].append({"id": conv.claude_session_id, "label": conv.label,
                             "model": conv.model, "cwd": conv.cwd,
                             "external": bool(conv.external), "_row_id": conv.id})
        if conv.is_current:
            data["current"] = len(data["list"]) - 1

    # 摘要与新会话在同一事务保存，首次续聊前重启也不会丢失上下文。
    empty_rows = {entry["_row_id"]: entry for entry in data["list"] if not entry["id"]}
    if empty_rows:
        summaries = s.execute(select(Message.session_id, Message.content)
                              .where(Message.chat_id == chat_id,
                                     Message.session_id.in_(empty_rows), Message.role == "summary")
                              .order_by(Message.id)).all()
        for row_id, summary in summaries:
            empty_rows[row_id]["seed"] = summary

    # 为当前 session 重建对话历史（最近 40 条）
    if data["list"]:
        cur_entry = data["list"][data["current"]]
        data["history"] = load_history(s, chat_id, cur_entry["id"])

    return state


def load_history(s, chat_id, claude_sid, limit=40):
    """读取某个 Claude session 的最近 limit 条对话（按时间正序返回）"""
    if not claude_sid:
        return []
    rows = s.execute(
        select(Message.role, Message.content, Message.created_at)
        .where(
            Message.chat_id == chat_id,
            Message.claude_session_id == claude_sid,
            Message.role.in_(("user", "assistant")),
        )
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()
    return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in reversed(rows)]


def save_bot_state(s, chat_id, permit, unsafe):
    s.merge(BotState(chat_id=chat_id, permit=permit, unsafe=unsafe))


def insert_session(s, chat_id, label, position, claude_sid=None, cwd=None, external=False, seed=None):
    """新建一条 session 行，返回其代理主键 _row_id。
    cwd/external 只在「接管本机已有会话」时给：cwd 是该 session 的原项目目录，
    external 让 /del 只解绑不删盘。"""
    conv = Conversation(
        chat_id=chat_id,
        claude_session_id=claude_sid, label=label, position=position,
        cwd=cwd, external=external,
    )
    s.add(conv)
    s.flush()  # 拿到自增主键
    if seed:
        append_message(s, conv.id, chat_id, None, "summary", seed)
    return conv.id


def update_session(s, row_id, claude_sid=None, label=None):
    """更新已存在 session 行的 claude_session_id / label"""
    values = {}
    if claude_sid is not None:
        values["claude_session_id"] = claude_sid
    if label is not None:
        values["label"] = label
    if not values:
        return
    s.execute(update(Conversation).where(Conversation.id == row_id).values(**values))


def set_session_model(s, row_id, model):
    """设置某 session 的模型（model 可为 None，表示用默认）。"""
    s.execute(update(Conversation).where(Conversation.id == row_id).values(model=model))


def set_current(s, chat_id, row_id):
    """把 row_id 置为当前 session，同 chat_id 下其余置 FALSE"""
    s.execute(
        update(Conversation)
        .where(Conversation.chat_id == chat_id)
        .values(is_current=(Conversation.id == row_id))
    )


def delete_session(s, row_id):
    """删除 session 行；messages 经 FK ON DELETE SET NULL 保留"""
    s.execute(delete(Conversation).where(Conversation.id == row_id))


def append_message(s, session_row_id, chat_id, claude_sid,
                   role, content, is_error=False, timed_out=False, model=None):
    s.add(Message(
        session_id=session_row_id, chat_id=chat_id,
        claude_session_id=claude_sid, role=role, content=content,
        is_error=is_error, timed_out=timed_out, model=model,
    ))


def record_upload(s, message_id, chat_id, resource_type, file_name, path, size, content_type):
    s.add(Upload(
        message_id=message_id, chat_id=chat_id, resource_type=resource_type,
        file_name=file_name, path=path, size=size, content_type=content_type,
    ))


def record_audit(s, chat_id, kind, tag, message_id=None, ok=True, code=None,
                 model=None, elapsed_ms=None, chars=None, detail=None):
    """审计：LLM 执行 / 飞书外发的响应信息落库。"""
    s.add(AuditLog(
        chat_id=chat_id, kind=kind, tag=tag, message_id=message_id, ok=ok,
        code=code, model=model, elapsed_ms=elapsed_ms, chars=chars, detail=detail,
    ))


def replace_wiki_chunks(s, space_id, rows):
    """整段替换某知识空间的全文检索副本（先删后插）。

    不做逐行 diff：chunk_id 会随小节增删整体漂移，逐行对齐的复杂度远大于重插；
    这张表只是索引副本，权威数据在本地 chunks.jsonl。"""
    s.execute(delete(WikiChunk).where(WikiChunk.space_id == space_id))
    s.flush()
    for r in rows:
        s.add(WikiChunk(space_id=space_id, chunk_id=r["chunk_id"],
                        doc_title=r.get("doc_title"), url=r.get("url"),
                        breadcrumb=r.get("breadcrumb"), text=r.get("text")))


def search_wiki_chunks(s, space_id, query, limit=20):
    """ngram 全文召回，返回 [(chunk_id, score)]（按分数降序，已剔除 0 分）。

    用 NATURAL LANGUAGE MODE：BOOLEAN MODE 下用户问句里的 +-~<>* 等字符会被当成
    操作符，中文自然语言提问很容易踩到。"""
    from sqlalchemy import text as sql_text
    rows = s.execute(sql_text(
        "SELECT chunk_id, MATCH(text) AGAINST(:q IN NATURAL LANGUAGE MODE) AS score "
        "FROM wiki_chunks WHERE space_id = :sid "
        "HAVING score > 0 ORDER BY score DESC LIMIT :lim"
    ), {"q": query, "sid": space_id, "lim": int(limit)}).all()
    return [(r[0], float(r[1])) for r in rows]


def record_unhandled(s, message_id, chat_id, msg_type, content):
    s.add(Unhandled(
        message_id=message_id, chat_id=chat_id, msg_type=msg_type, content=content,
    ))


def search_history(s, chat_id, query, limit=20):
    """查本群全部会话历史；转义 LIKE 通配符，限制结果大小。"""
    rows = s.scalars(select(Message).where(
        Message.chat_id == chat_id, Message.content.contains(query, autoescape=True)
    ).order_by(Message.id.desc()).limit(limit)).all()
    return [{"role": r.role, "content": (r.content or "")[:4000],
             "session_id": r.claude_session_id,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


def list_uploads(s, chat_id, limit=10):
    rows = s.scalars(select(Upload).where(Upload.chat_id == chat_id)
                     .order_by(Upload.id.desc()).limit(limit)).all()
    return [{"name": r.file_name, "path": r.path, "size": r.size,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]
