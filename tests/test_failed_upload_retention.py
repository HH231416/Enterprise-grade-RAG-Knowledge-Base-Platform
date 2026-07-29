from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.models.document import Document
from app.models.ingestion_job import IngestionJob
from app.services.failed_upload_cleanup import FailedUploadCleanupService
from app.services.ingestion import IngestionService


def _failed_document(file_path: str) -> Document:
    return Document(
        title="Failed upload",
        source_type="file",
        source_module="general",
        file_name="failed.txt",
        file_ext="txt",
        mime_type="text/plain",
        file_path=file_path,
        file_hash="a" * 64,
        file_size=1,
        status="pending",
    )


def test_mark_failed_keeps_source_file_for_retry(db_session):
    settings = get_settings().model_copy(update={"failed_upload_retention_hours": 24})
    file_path = settings.resolved_raw_data_dir / "failed.txt"
    file_path.write_text("source", encoding="utf-8")
    document = _failed_document(str(file_path))
    db_session.add(document)
    db_session.flush()
    job = IngestionJob(doc_uuid=document.doc_uuid)
    db_session.add(job)
    db_session.commit()

    IngestionService(db_session, settings)._mark_failed(
        document.doc_uuid,
        job.job_uuid,
        "parser failed",
        str(ErrorCode.DOCUMENT_PARSE_FAILED),
    )
    db_session.refresh(document)
    db_session.refresh(job)

    assert file_path.exists()
    assert document.status == "failed"
    assert document.parse_status == "failed"
    assert document.extra_meta["failed_file_retention_hours"] == 24
    assert document.extra_meta["failed_file_retention_until"]
    assert job.status == "failed"


def test_cleanup_purges_only_expired_failed_source_file(db_session):
    settings = get_settings().model_copy(update={"failed_upload_retention_hours": 24})
    file_path = settings.resolved_raw_data_dir / "expired.txt"
    file_path.write_text("source", encoding="utf-8")
    document = _failed_document(str(file_path))
    document.status = "failed"
    document.parse_status = "failed"
    db_session.add(document)
    db_session.commit()

    now = datetime.now(UTC)
    db_session.execute(
        update(Document)
        .where(Document.doc_uuid == document.doc_uuid)
        .values(updated_at=now - timedelta(hours=25))
    )
    db_session.commit()

    purged = FailedUploadCleanupService(db_session, settings).purge_expired_files(now=now)
    db_session.refresh(document)

    assert purged == 1
    assert not file_path.exists()
    assert document.status == "failed_expired"
    assert document.extra_meta["failed_file_purged_at"] == now.isoformat()


def test_cleanup_keeps_file_inside_retention_window(db_session):
    settings = get_settings().model_copy(update={"failed_upload_retention_hours": 24})
    file_path = settings.resolved_raw_data_dir / "recent.txt"
    file_path.write_text("source", encoding="utf-8")
    document = _failed_document(str(file_path))
    document.status = "failed"
    db_session.add(document)
    db_session.commit()

    purged = FailedUploadCleanupService(db_session, settings).purge_expired_files()
    db_session.refresh(document)

    assert purged == 0
    assert file_path.exists()
    assert document.status == "failed"


def test_reprocess_rejects_missing_expired_source_file(db_session):
    settings = get_settings()
    document = _failed_document(str(settings.resolved_raw_data_dir / "purged.txt"))
    document.status = "failed_expired"
    db_session.add(document)
    db_session.commit()

    with pytest.raises(AppError) as error:
        IngestionService(db_session, settings).reprocess_document(document.doc_uuid)

    assert error.value.code == ErrorCode.DOCUMENT_PARSE_FAILED
    assert error.value.status_code == 422
    assert "retention window has expired" in error.value.message
