"""qa conversation owner and graph build batches

Revision ID: 20261008_0017
Revises: 20260808_0016
Create Date: 2026-10-08

说明：
- qa_conversations 新增 owner_user_id。历史会话保持 NULL，不做任何自动归属推断。
  NULL 会话不会出现在任何用户的会话列表中，也无法被读取/发送/删除。
- 新增图构建批次相关表。批次是技术构建单位，不等于校园政策的生效版本。

本迁移仅提供文件，未在真实数据库执行。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261008_0017"
down_revision: Union[str, None] = "20260808_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------- 会话归属 ----------
    op.add_column("qa_conversations", sa.Column("owner_user_id", sa.String(128), nullable=True))
    op.create_index(
        "idx_qa_conversations_owner",
        "qa_conversations",
        ["owner_user_id", "updated_at"],
    )

    # ---------- 图构建批次 ----------
    op.create_table(
        "graph_build_batches",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "batch_uuid",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "doc_uuid",
            sa.Uuid(),
            sa.ForeignKey("documents.doc_uuid", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("doc_version", sa.String(64), nullable=False),
        # 来源快照指纹：file_hash + 分块结果指纹 + 公开范围 + 可检索状态
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        # public / restricted：仅 public 允许进入图构建与在线召回
        sa.Column("access_scope", sa.String(32), nullable=False),
        # pending / running / pending_review / approved / searchable /
        # publish_failed / superseded / failed
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("extractor_name", sa.String(64), nullable=True),
        sa.Column("prompt_template_version", sa.String(64), nullable=True),
        # 抽取结果分类：candidates / empty / llm_failed / invalid_output / evidence_rejected
        sa.Column("extraction_result", sa.String(32), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("approved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_graph_batches_uuid", "graph_build_batches", ["batch_uuid"], unique=True)
    op.create_index("idx_graph_batches_doc", "graph_build_batches", ["doc_uuid", "status"])
    op.create_index(
        "uq_graph_batches_snapshot",
        "graph_build_batches",
        ["doc_uuid", "source_fingerprint"],
        unique=True,
    )

    # ---------- 候选关系 ----------
    op.create_table(
        "graph_candidates",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("candidate_uuid", sa.Uuid(), nullable=False),
        sa.Column(
            "batch_uuid",
            sa.Uuid(),
            sa.ForeignKey("graph_build_batches.batch_uuid"),
            nullable=False,
        ),
        sa.Column(
            "doc_uuid",
            sa.Uuid(),
            sa.ForeignKey("documents.doc_uuid", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_uuid", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("relation_type", sa.String(64), nullable=False),
        sa.Column("source_entity_key", sa.String(255), nullable=False),
        sa.Column("source_entity_type", sa.String(32), nullable=False),
        sa.Column("source_entity_name", sa.String(255), nullable=False),
        sa.Column("target_entity_key", sa.String(255), nullable=False),
        sa.Column("target_entity_type", sa.String(32), nullable=False),
        sa.Column("target_entity_name", sa.String(255), nullable=False),
        # 原文证据与可核验位置
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("evidence_section", sa.String(255), nullable=True),
        sa.Column("evidence_page_no", sa.Integer(), nullable=True),
        # 已知适用条件；未知保持 NULL，不允许模型补写
        sa.Column("applicability", sa.JSON(), nullable=True),
        sa.Column("applicability_known", sa.Boolean(), nullable=False, server_default=sa.false()),
        # pending / approved / rejected
        sa.Column("review_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("validation_errors", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_graph_candidates_uuid", "graph_candidates", ["candidate_uuid"], unique=True)
    op.create_index("idx_graph_candidates_batch", "graph_candidates", ["batch_uuid", "review_status"])
    op.create_index("idx_graph_candidates_chunk", "graph_candidates", ["doc_uuid", "chunk_index"])

    # ---------- 生效批次指针 ----------
    op.create_table(
        "graph_active_batches",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "doc_uuid",
            sa.Uuid(),
            sa.ForeignKey("documents.doc_uuid", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "batch_uuid",
            sa.Uuid(),
            sa.ForeignKey("graph_build_batches.batch_uuid"),
            nullable=False,
        ),
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("activated_by", sa.String(128), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_graph_active_doc", "graph_active_batches", ["doc_uuid"], unique=True)


def downgrade() -> None:
    op.drop_table("graph_active_batches")
    op.drop_table("graph_candidates")
    op.drop_table("graph_build_batches")
    op.drop_index("idx_qa_conversations_owner", table_name="qa_conversations")
    op.drop_column("qa_conversations", "owner_user_id")
