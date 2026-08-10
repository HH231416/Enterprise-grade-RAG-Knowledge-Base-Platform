"""文档审核历史记录 — 追加式日志，记录每次状态变更。

与 Document.reviewed_by/at/comment（最新审核快照）不同，
本表记录完整的审核生命周期：提交、通过、拒绝、撤回、重新提交。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import PrimaryKeyBigInt, UuidType
from app.models.mixins import TimestampMixin


class DocumentReviewRecord(TimestampMixin, Base):
    __tablename__ = "document_review_records"
    __table_args__ = (
        Index("idx_review_records_doc_uuid", "doc_uuid"),
        Index("idx_review_records_action", "action"),
        Index("idx_review_records_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    record_uuid: Mapped[uuid.UUID] = mapped_column(UuidType, default=uuid.uuid4, nullable=False)
    doc_uuid: Mapped[uuid.UUID] = mapped_column(
        UuidType,
        ForeignKey("documents.doc_uuid", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(
        String(32), nullable=False,
    )
    """动作类型: submitted | auto_approved | approved | rejected | revoked | resubmitted"""
    actor: Mapped[str | None] = mapped_column(String(128))
    """操作人 user_id，系统自动操作时为 system"""
    comment: Mapped[str | None] = mapped_column(Text)
    """审核意见或自动通过原因"""
    actioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    """操作时间"""
