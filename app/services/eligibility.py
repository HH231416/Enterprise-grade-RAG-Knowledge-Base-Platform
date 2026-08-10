"""统一可检索文档条件。

所有生产检索入口（普通检索、调试检索、问答、知识库接口、
评测回放、稀疏索引重建）必须使用此条件，禁止各入口自行拼装。

审核开关决定新文档初始状态（approved 或 pending），不控制此条件。
"""

from __future__ import annotations

from app.models.document import Document

DOCUMENT_ELIGIBILITY_CONDITIONS = [
    Document.deleted_at.is_(None),
    Document.review_status == "approved",
    Document.status == "active",
    Document.parse_status == "success",
    Document.index_status == "success",
]


def apply_document_eligibility(stmt):
    """对 SQLAlchemy select 语句应用统一可检索条件。"""
    for condition in DOCUMENT_ELIGIBILITY_CONDITIONS:
        stmt = stmt.where(condition)
    return stmt
