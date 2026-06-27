"""SQLAlchemy ORM 模型。

三张表（与原生 SQL 版结构一致，create_all 对已存在的表幂等跳过）：
- bot_state：每个 chat_id 的 UI 状态
- sessions：一行一个对话（类名 Conversation，避开 SQLAlchemy 自身的 Session）
- messages：对话审计，append-only
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BotState(Base):
    __tablename__ = "bot_state"

    chat_id: Mapped[str] = mapped_column(Text, primary_key=True)
    last_dir_name: Mapped[str | None] = mapped_column(Text)
    permit_modes: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'"))
    updated_at: Mapped["TIMESTAMP"] = mapped_column(TIMESTAMP, server_default=func.now())


class Conversation(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(Text, nullable=False)
    dir_name: Mapped[str] = mapped_column(Text, nullable=False)
    claude_session_id: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped["TIMESTAMP"] = mapped_column(TIMESTAMP, server_default=func.now())
    updated_at: Mapped["TIMESTAMP"] = mapped_column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index(
            "uq_sessions_claude",
            "chat_id",
            "claude_session_id",
            unique=True,
            postgresql_where=text("claude_session_id IS NOT NULL"),
        ),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="SET NULL")
    )
    chat_id: Mapped[str | None] = mapped_column(Text)
    dir_name: Mapped[str | None] = mapped_column(Text)
    claude_session_id: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    is_error: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped["TIMESTAMP"] = mapped_column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index("idx_messages_session", "session_id"),
        Index("idx_messages_chat_dir", "chat_id", "dir_name", "created_at"),
    )
