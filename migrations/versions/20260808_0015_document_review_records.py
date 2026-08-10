"""add document_review_records table

Revision ID: 20260808_0015
Revises: 20260808_0014
Create Date: 2026-08-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0015"
down_revision: str | None = "20260808_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_review_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("record_uuid", sa.Uuid(), nullable=False),
        sa.Column("doc_uuid", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("actioned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["doc_uuid"], ["documents.doc_uuid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_review_records_doc_uuid", "document_review_records", ["doc_uuid"])
    op.create_index("idx_review_records_action", "document_review_records", ["action"])
    op.create_index("idx_review_records_created_at", "document_review_records", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_review_records_created_at", "document_review_records")
    op.drop_index("idx_review_records_action", "document_review_records")
    op.drop_index("idx_review_records_doc_uuid", "document_review_records")
    op.drop_table("document_review_records")
