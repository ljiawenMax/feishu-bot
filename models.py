"""SQLAlchemy ORM 模型（MySQL）。

三张表：
- bot_state：每个 chat_id 的 UI 状态
- sessions：一行一个对话（类名 Conversation，避开 SQLAlchemy 自身的 Session）
- messages：对话审计，append-only

MySQL 适配要点：
- 被索引/做主键的字符串列用 VARCHAR（MySQL 不能直接对 TEXT 建索引/主键）
- content 用 LONGTEXT，容纳较长的 LLM 输出
- 字符集 utf8mb4，支持中文与 emoji
- claude_session_id 的唯一性用普通 UNIQUE 索引即可：MySQL 的唯一索引允许多个 NULL，
  等价于原 PostgreSQL 的部分唯一索引
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TABLE_KW = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class Base(DeclarativeBase):
    pass


class BotState(Base):
    __tablename__ = "bot_state"
    __table_args__ = TABLE_KW

    chat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_dir_name: Mapped[str | None] = mapped_column(String(128))
    permit_modes: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped["DateTime"] = mapped_column(DateTime, server_default=func.now())


class Conversation(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("uq_sessions_claude", "chat_id", "claude_session_id", unique=True),
        TABLE_KW,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dir_name: Mapped[str] = mapped_column(String(128), nullable=False)
    claude_session_id: Mapped[str | None] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(255))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped["DateTime"] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped["DateTime"] = mapped_column(DateTime, server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_messages_session", "session_id"),
        Index("idx_messages_chat_dir", "chat_id", "dir_name", "created_at"),
        TABLE_KW,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="SET NULL")
    )
    chat_id: Mapped[str | None] = mapped_column(String(64))
    dir_name: Mapped[str | None] = mapped_column(String(128))
    claude_session_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(16))
    content: Mapped[str | None] = mapped_column(LONGTEXT)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped["DateTime"] = mapped_column(DateTime, server_default=func.now())


class Upload(Base):
    """上传文件台账（独立于会话 messages）。"""

    __tablename__ = "uploads"
    __table_args__ = (
        Index("idx_uploads_chat", "chat_id", "created_at"),
        TABLE_KW,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[str | None] = mapped_column(String(128))
    chat_id: Mapped[str | None] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(16))  # image/file/media/audio
    file_name: Mapped[str | None] = mapped_column(String(255))
    path: Mapped[str | None] = mapped_column(String(1024))
    size: Mapped[int | None] = mapped_column(BigInteger)
    content_type: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped["DateTime"] = mapped_column(DateTime, server_default=func.now())
