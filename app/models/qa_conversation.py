from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonType, PrimaryKeyBigInt, UuidType


class QaConversation(Base):
    __tablename__ = "qa_conversations"
    __table_args__ = (
        Index("uq_qa_conversations_id", "conversation_id", unique=True),
        Index("idx_qa_conversations_updated", "updated_at"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UuidType, default=uuid.uuid4, nullable=False)
    title: Mapped[str] = mapped_column(String(255), server_default="新对话", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class QaConversationMessage(Base):
    __tablename__ = "qa_conversation_messages"
    __table_args__ = (
        Index("uq_qa_messages_id", "message_id", unique=True),
        Index("idx_qa_messages_conversation", "conversation_id"),
        Index("idx_qa_messages_created", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    message_id: Mapped[uuid.UUID] = mapped_column(UuidType, default=uuid.uuid4, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UuidType, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    citations: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JsonType, server_default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
