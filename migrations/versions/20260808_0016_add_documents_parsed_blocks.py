"""add documents.parsed_blocks snapshot column

Revision ID: 20260808_0016
Revises: 20260808_0015
Create Date: 2026-08-08
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0016"
down_revision: str | None = "20260808_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 解析结果快照：待审核阶段只解析不 chunk，发布时用该快照分块建索引
    op.add_column("documents", sa.Column("parsed_blocks", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "parsed_blocks")
