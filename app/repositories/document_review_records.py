from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document_review_record import DocumentReviewRecord


class DocumentReviewRecordRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_record(
        self,
        *,
        doc_uuid: uuid.UUID,
        action: str,
        actor: str | None = None,
        comment: str | None = None,
    ) -> DocumentReviewRecord:
        record = DocumentReviewRecord(
            doc_uuid=doc_uuid,
            action=action,
            actor=actor,
            comment=comment,
            actioned_at=datetime.now(),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def list_for_document(self, doc_uuid: uuid.UUID) -> list[DocumentReviewRecord]:
        stmt = (
            select(DocumentReviewRecord)
            .where(DocumentReviewRecord.doc_uuid == doc_uuid)
            .order_by(DocumentReviewRecord.actioned_at.desc())
        )
        return list(self.session.scalars(stmt))
