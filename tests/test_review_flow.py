"""Design D 审核流测试：审核与分块解耦。

核心断言：
- 待审核文档只解析 + 解析快照落库，不生成 chunk、不调 Embedding；
- 审核通过发布时按配置的真实策略分块（不再降级为 fixed）；
- 发布成功后 parse_status=success，满足统一可检索条件（修复原缺陷）；
- 分块预览读取落库快照，源文件删除后仍可用。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings, reset_settings_cache
from app.ingestion.types import ParsedBlock
from app.main import create_app
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.ingestion_job import IngestionJob
from app.repositories.document_chunks import DocumentChunkRepository
from app.services.chunking import ChunkingService
from app.services.eligibility import apply_document_eligibility
from app.services.ingestion import IngestionService

_REVIEW_TEXT = (
    "# 调岗规则\n"
    "调岗申请需要增加二级审批。\n\n"
    "# 生效日期\n"
    "审批通过后的次月1日生效。\n"
)


def _make_pending_document(session, tmp_path, *, strategy: str = "structural"):
    file_path = tmp_path / "review.txt"
    file_path.write_text(_REVIEW_TEXT, encoding="utf-8")
    doc = Document(
        doc_uuid=uuid4(),
        title="调岗规则",
        source_type="note",
        source_module="general",
        file_name="review.txt",
        file_ext="txt",
        mime_type="text/plain",
        file_path=str(file_path),
        file_hash=hashlib.sha256(b"review").hexdigest(),
        file_size=file_path.stat().st_size,
        version="v1",
        status="draft",
        review_status="pending",
        parse_status="pending",
        index_status="pending",
        access_level="internal",
        extra_meta={"chunking_strategy": strategy},
    )
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc


def _add_ingest_job(session, doc):
    job = IngestionJob(
        doc_uuid=doc.doc_uuid,
        job_type="ingest",
        status="pending",
        current_step="created",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_pending_upload_parses_without_chunking(db_session, tmp_path):
    """待审核阶段：只解析 + 快照落库，无 chunk、状态为 awaiting_review。"""
    settings = get_settings()
    doc = _make_pending_document(db_session, tmp_path)
    job = _add_ingest_job(db_session, doc)

    count = IngestionService(db_session, settings).process_upload(doc, job)
    assert count == 0

    db_session.refresh(doc)
    assert doc.parse_status == "awaiting_review"
    assert doc.status == "draft"
    assert doc.index_status == "pending"
    assert doc.parsed_blocks and len(doc.parsed_blocks) == 1
    assert doc.parsed_blocks[0]["text"].startswith("# 调岗规则")

    chunks = DocumentChunkRepository(db_session).list_for_document(doc.doc_uuid)
    assert chunks == []


def test_publish_uses_configured_strategy_and_becomes_eligible(db_session, tmp_path):
    """发布阶段：按配置的 structural 策略分块，补齐状态满足统一可检索条件。"""
    settings = get_settings()
    doc = _make_pending_document(db_session, tmp_path)
    job = _add_ingest_job(db_session, doc)
    IngestionService(db_session, settings).process_upload(doc, job)

    # 模拟管理员通过审核（由 approve 端点设置 review_status）
    doc.review_status = "approved"
    db_session.commit()

    publish_job, vector_count = IngestionService(db_session, settings).publish_document(document=doc)

    db_session.refresh(doc)
    assert publish_job.status == "success"
    assert doc.review_status == "approved"
    assert doc.status == "active"
    assert doc.parse_status == "success"
    assert doc.index_status == "success"

    chunks = DocumentChunkRepository(db_session).list_for_document(doc.doc_uuid)
    assert chunks, "publish 后必须生成 chunk"
    # 配置的 structural 策略被尊重，不再被降级为 fixed
    assert all(chunk.metadata_json.get("chunking_strategy") == "structural" for chunk in chunks)
    assert all(chunk.section_title for chunk in chunks)

    # 满足统一可检索条件（parse_status=success 修复后应可被检索）
    stmt = apply_document_eligibility(select(Document)).where(Document.doc_uuid == doc.doc_uuid)
    assert db_session.scalars(stmt).first() is not None


def test_publish_honors_semantic_when_configured(db_session, tmp_path):
    """发布阶段：配置 semantic 策略时按语义分块（不再静默降级为 fixed）。"""
    settings = get_settings()
    doc = _make_pending_document(db_session, tmp_path, strategy="semantic")
    job = _add_ingest_job(db_session, doc)
    IngestionService(db_session, settings).process_upload(doc, job)
    assert DocumentChunkRepository(db_session).list_for_document(doc.doc_uuid) == []

    doc.review_status = "approved"
    db_session.commit()
    IngestionService(db_session, settings).publish_document(document=doc)

    db_session.refresh(doc)
    assert doc.parse_status == "success"
    chunks = DocumentChunkRepository(db_session).list_for_document(doc.doc_uuid)
    assert chunks
    assert all(chunk.metadata_json.get("chunking_strategy") == "semantic" for chunk in chunks)


def test_preview_works_without_source_file_after_pending(db_session, tmp_path):
    """待审核后源文件被清理，分块预览仍可读取落库快照。"""
    settings = get_settings()
    doc = _make_pending_document(db_session, tmp_path)
    job = _add_ingest_job(db_session, doc)
    IngestionService(db_session, settings).process_upload(doc, job)

    Path(doc.file_path).unlink()  # 模拟源文件被清理

    result = ChunkingService(db_session).get_document_preview_text(doc_uuid=doc.doc_uuid)
    assert result["char_count"] > 0
    assert "调岗规则" in result["text"]
    assert result["block_count"] == 1


def test_full_review_cycle_via_api(db_session, monkeypatch, tmp_path):
    """端到端：开启审核 + 上传身份校验 → 待审核（无 chunk）→ 审核通过 → 可检索。"""
    monkeypatch.setenv("INGESTION_MODE", "sync")
    monkeypatch.setenv("DOCUMENT_REVIEW_ENABLED", "true")
    monkeypatch.setenv("UPLOAD_REQUIRES_AUTH", "true")
    monkeypatch.setenv("TRUSTED_IDENTITY_HEADER_ENABLED", "true")
    reset_settings_cache()

    client = TestClient(create_app())

    upload = client.post(
        "/api/v1/documents/upload",
        data={
            "source_type": "note",
            "source_module": "general",
            "extra_meta": '{"chunking_strategy":"structural"}',
        },
        files={"file": ("review.txt", _REVIEW_TEXT.encode("utf-8"), "text/plain")},
        headers={"X-User-Id": "u1001"},
    )
    assert upload.status_code == 200
    payload = upload.json()["data"]
    assert payload["review_status"] == "pending"
    assert payload["chunk_count"] == 0

    doc_uuid = UUID(payload["doc_uuid"])
    doc = db_session.query(Document).filter(Document.doc_uuid == doc_uuid).one()
    assert doc.review_status == "pending"
    assert doc.parse_status == "awaiting_review"
    assert doc.parsed_blocks
    assert DocumentChunkRepository(db_session).list_for_document(doc_uuid) == []

    # 待审核预览读取落库快照
    preview = client.get(f"/api/v1/chunking/documents/{doc_uuid}/preview-text")
    assert preview.status_code == 200
    assert "调岗规则" in preview.json()["data"]["text"]

    # 通过审核 → 发布：分块 + 建索引
    approve = client.post(f"/api/v1/admin/documents/{doc_uuid}/approve", json={})
    assert approve.status_code == 200
    assert approve.json()["data"]["review_status"] == "approved"

    db_session.expire_all()
    doc = db_session.query(Document).filter(Document.doc_uuid == doc_uuid).one()
    assert doc.review_status == "approved"
    assert doc.status == "active"
    assert doc.parse_status == "success"
    assert doc.index_status == "success"

    chunks = db_session.query(DocumentChunk).filter(
        DocumentChunk.doc_uuid == doc_uuid
    ).all()
    assert chunks
    assert all(chunk.vector_id for chunk in chunks)
    assert all(chunk.metadata_json.get("chunking_strategy") == "structural" for chunk in chunks)
