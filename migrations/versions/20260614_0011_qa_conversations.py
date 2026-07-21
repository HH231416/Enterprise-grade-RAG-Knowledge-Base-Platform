"""qa conversations

Revision ID: 20260614_0011
Revises: 20260526_0010
Create Date: 2026-06-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260614_0011"
down_revision: Union[str, None] = "20260526_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qa_conversations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(255), server_default="新对话", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_qa_conversations_id", "qa_conversations", ["conversation_id"], unique=True)
    op.create_index("idx_qa_conversations_updated", "qa_conversations", ["updated_at"])

    op.create_table(
        "qa_conversation_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_qa_messages_id", "qa_conversation_messages", ["message_id"], unique=True)
    op.create_index("idx_qa_messages_conversation", "qa_conversation_messages", ["conversation_id"])
    op.create_index("idx_qa_messages_created", "qa_conversation_messages", ["conversation_id", "created_at"])


def downgrade() -> None:
    op.drop_table("qa_conversation_messages")
    op.drop_table("qa_conversations")
