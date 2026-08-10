"""add document review_status and audit fields

Revision ID: 20260808_0014
Revises: 20260719_0013
Create Date: 2026-08-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0014"
down_revision: str | None = "20260719_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("review_status", sa.String(32), nullable=False, server_default="approved"),
    )
    op.create_index("idx_documents_review_status", "documents", ["review_status"])
    op.add_column("documents", sa.Column("reviewed_by", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("review_comment", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "review_comment")
    op.drop_column("documents", "reviewed_at")
    op.drop_column("documents", "reviewed_by")
    op.drop_index("idx_documents_review_status", "documents")
    op.drop_column("documents", "review_status")
