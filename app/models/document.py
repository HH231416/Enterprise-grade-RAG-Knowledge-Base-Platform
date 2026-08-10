from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import JsonType, PrimaryKeyBigInt, UuidType
from app.db.base import Base
from app.models.mixins import TimestampMixin


# ---------------------------------------------------------------------------
# 分块管线使用的轻量数据对象（与 ORM 层解耦）
# ---------------------------------------------------------------------------

@dataclass
class SemanticSegment:
    """语义段落 — 语义分块过程中的中间结果。

    由 SemanticChunker 在分块时生成，记录一组语义相近的句子及其在原文中的位置。
    """

    sentences: list[str] = field(default_factory=list)
    """构成该段落的句子列表"""

    start_index: int = 0
    """起始句子在原始句子列表中的索引"""

    end_index: int = 0
    """结束句子在原始句子列表中的索引"""


@dataclass
class DocumentChunk:
    """分块结果 — 分块管线中使用的轻量数据对象。

    与 app.models.document_chunk.DocumentChunk（SQLAlchemy ORM 模型）不同，
    本类是纯数据对象，用于在分块、解析等管线阶段传递数据，
    之后由 service 层负责将其转换为 ORM 模型后持久化到数据库。
    """

    text: str
    """分块后的文本内容"""

    chunk_index: int = 0
    """该块在文档中的序号"""


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("uq_documents_doc_uuid", "doc_uuid", unique=True),
        Index("idx_documents_source_module", "source_module"),
        Index("idx_documents_source_type", "source_type"),
        Index("idx_documents_status", "status"),
        Index("idx_documents_parse_status", "parse_status"),
        Index("idx_documents_index_status", "index_status"),
        Index("idx_documents_access_level", "access_level"),
        Index("idx_documents_owner_dept", "owner_dept"),
        Index("idx_documents_created_at", "created_at"),
        Index("idx_documents_file_hash", "file_hash"),
        Index("idx_documents_review_status", "review_status"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    doc_uuid: Mapped[uuid.UUID] = mapped_column(
        UuidType,
        default=uuid.uuid4,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_module: Mapped[str] = mapped_column(String(64), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(16), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1", server_default="v1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="approved", server_default="approved")
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    index_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    access_level: Mapped[str] = mapped_column(String(64), nullable=False, default="internal", server_default="internal")
    owner_dept: Mapped[str | None] = mapped_column(String(64))
    owner_role: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list] = mapped_column(JsonType, nullable=False, default=list, server_default="[]")
    extra_meta: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict, server_default="{}")
    # 解析结果快照（ParsedBlock 列表序列化）。待审核文档在审核阶段不生成 chunk，
    # 审核通过后发布时用这份快照按配置策略分块，保证"审核看到的就是进索引的"。
    # 同时使分块预览不依赖源文件仍可访问。
    parsed_blocks: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_comment: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
