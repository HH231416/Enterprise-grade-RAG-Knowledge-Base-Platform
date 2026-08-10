"""add answer-layer judge fields to evaluation tables

Revision ID: 20260719_0013
Revises: 20260614_0012
Create Date: 2026-07-19
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0013"
down_revision: Union[str, None] = "20260614_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("evaluation_queries", sa.Column("reference_answer", sa.Text(), nullable=True))
    op.add_column("evaluation_results", sa.Column("answer_text", sa.Text(), nullable=True))
    op.add_column("evaluation_results", sa.Column("answer_faithfulness_score", sa.Float(), nullable=True))
    op.add_column("evaluation_results", sa.Column("answer_relevance_score", sa.Float(), nullable=True))
    op.add_column("evaluation_results", sa.Column("citation_coverage_score", sa.Float(), nullable=True))
    op.add_column("evaluation_results", sa.Column("answer_judge_verdict", sa.String(length=16), nullable=True))
    op.add_column("evaluation_results", sa.Column("answer_judge_reason", sa.Text(), nullable=True))
    op.add_column("evaluation_results", sa.Column("answer_judge_latency_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("evaluation_results", "answer_judge_latency_ms")
    op.drop_column("evaluation_results", "answer_judge_reason")
    op.drop_column("evaluation_results", "answer_judge_verdict")
    op.drop_column("evaluation_results", "citation_coverage_score")
    op.drop_column("evaluation_results", "answer_relevance_score")
    op.drop_column("evaluation_results", "answer_faithfulness_score")
    op.drop_column("evaluation_results", "answer_text")
    op.drop_column("evaluation_queries", "reference_answer")
