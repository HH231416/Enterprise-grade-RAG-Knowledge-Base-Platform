"""图构建批次、候选关系与生效批次指针。

三张表的职责边界：
- GraphBuildBatch：一次构图任务的持久状态与来源快照。技术批次，不等于政策生效版本。
- GraphCandidate：批次内的候选关系，必须带原文证据与可核验位置。
- GraphActiveBatch：文档 → 当前生效批次的唯一指针。在线召回只认这个指针。

生效指针由业务库控制，Neo4j 写入成功但指针切换失败时，
该批次不会进入在线回答（发布可见性不由存储层自证）。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonType, PrimaryKeyBigInt, UuidType


class GraphBuildBatch(Base):
    """一次按文档的构图批次。

    状态机：
        pending → running → pending_review → approved → searchable
        running → failed（llm_failed / invalid_output / evidence_rejected）
        approved → publish_failed（写图失败，可重试，不重复发布）
        同快照重建：新批次走 pending_review，原 searchable 批次保持有效
        来源/分块/公开范围/可检索状态变化：原批次置 superseded
    """

    __tablename__ = "graph_build_batches"
    __table_args__ = (
        Index("uq_graph_batches_uuid", "batch_uuid", unique=True),
        Index("idx_graph_batches_doc", "doc_uuid", "status"),
        # 同一文档同一来源快照只允许存在一个批次，重复构建命中唯一约束实现幂等
        Index("uq_graph_batches_snapshot", "doc_uuid", "source_fingerprint", unique=True),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    batch_uuid: Mapped[uuid.UUID] = mapped_column(UuidType, default=uuid.uuid4, nullable=False)
    doc_uuid: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("documents.doc_uuid", ondelete="CASCADE"), nullable=False,
    )
    doc_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # public / restricted。第一版只有 public 允许进入图谱。
    access_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="restricted")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    extractor_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_template_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # candidates / empty / llm_failed / invalid_output / evidence_rejected
    extraction_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class GraphCandidate(Base):
    """批次内的候选关系。

    每条候选必须绑定 doc_uuid + chunk_index + evidence_text，
    「两个实体在同一段文字出现」不构成关系，因此没有证据的候选不允许入库。
    """

    __tablename__ = "graph_candidates"
    __table_args__ = (
        Index("uq_graph_candidates_uuid", "candidate_uuid", unique=True),
        Index("idx_graph_candidates_batch", "batch_uuid", "review_status"),
        Index("idx_graph_candidates_chunk", "doc_uuid", "chunk_index"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    candidate_uuid: Mapped[uuid.UUID] = mapped_column(UuidType, default=uuid.uuid4, nullable=False)
    batch_uuid: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("graph_build_batches.batch_uuid"), nullable=False,
    )
    doc_uuid: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("documents.doc_uuid", ondelete="CASCADE"), nullable=False,
    )
    chunk_uuid: Mapped[uuid.UUID] = mapped_column(UuidType, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_entity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_entity_name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_entity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_entity_name: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence_page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 已知适用条件。未知保持 NULL 且 applicability_known=False，不允许模型补写。
    applicability: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    applicability_known: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # pending / approved / rejected
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    validation_errors: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    def to_relation_record(self, source_fingerprint: str):
        """转为存储层关系记录。调用方需保证 review_status == 'approved'。"""
        from app.graph.types import EvidenceRef, GraphRelationRecord

        return GraphRelationRecord(
            relation_type=self.relation_type,
            source_key=self.source_entity_key,
            source_type=self.source_entity_type,
            source_name=self.source_entity_name,
            target_key=self.target_entity_key,
            target_type=self.target_entity_type,
            target_name=self.target_entity_name,
            evidence=EvidenceRef(
                doc_uuid=self.doc_uuid,
                chunk_uuid=self.chunk_uuid,
                chunk_index=self.chunk_index,
                evidence_text=self.evidence_text,
                section_title=self.evidence_section,
                page_no=self.evidence_page_no,
            ),
            batch_uuid=str(self.batch_uuid),
            source_fingerprint=source_fingerprint,
            applicability=(self.applicability or {}).get("note")
            if isinstance(self.applicability, dict)
            else None,
            applicability_known=bool(self.applicability_known),
        )


class GraphActiveBatch(Base):
    """文档 → 当前生效批次的唯一指针。

    在线召回只读取本表指向的批次。未被指针选中的批次不进入回答，
    即使其图数据已写入 Neo4j。
    """

    __tablename__ = "graph_active_batches"
    __table_args__ = (Index("uq_graph_active_doc", "doc_uuid", unique=True),)

    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    doc_uuid: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("documents.doc_uuid", ondelete="CASCADE"), nullable=False,
    )
    batch_uuid: Mapped[uuid.UUID] = mapped_column(
        UuidType, ForeignKey("graph_build_batches.batch_uuid"), nullable=False,
    )
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    activated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
