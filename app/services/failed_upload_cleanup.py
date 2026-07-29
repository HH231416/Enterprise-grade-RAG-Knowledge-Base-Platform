from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.document import Document


logger = logging.getLogger(__name__)


class FailedUploadCleanupService:
    """Purge expired raw files for failed ingestion jobs without deleting records."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def purge_expired_files(self, now: datetime | None = None) -> int:
        current_time = now or datetime.now(UTC)
        cutoff = current_time - timedelta(hours=self.settings.failed_upload_retention_hours)
        documents = list(
            self.session.scalars(
                select(Document)
                .where(Document.status == "failed")
                .where(Document.deleted_at.is_(None))
                .where(Document.updated_at < cutoff)
                .order_by(Document.updated_at.asc())
                .limit(self.settings.failed_upload_cleanup_batch_size)
            )
        )
        purged = 0
        raw_root = self.settings.resolved_raw_data_dir.resolve()
        for document in documents:
            path = Path(document.file_path)
            try:
                resolved_path = path.resolve()
                resolved_path.relative_to(raw_root)
            except (OSError, ValueError):
                logger.error(
                    "Refusing to purge failed document %s outside raw storage: %s",
                    document.doc_uuid,
                    path,
                )
                continue

            if resolved_path.exists():
                try:
                    resolved_path.unlink()
                    purged += 1
                except OSError:
                    logger.exception("Failed to purge expired upload file: %s", resolved_path)
                    continue

            document.extra_meta = {
                **(document.extra_meta or {}),
                "failed_file_purged_at": current_time.isoformat(),
            }
            document.status = "failed_expired"

        self.session.commit()
        logger.info("Purged %s expired failed-upload files", purged)
        return purged
