"""图谱构图服务。

职责：
- 校验文档是否满足图构建条件（公开范围 + 权威可检索状态）
- 计算来源快照指纹
- 创建或复用构建批次（幂等）
- 逐分块调用 LLM 受约束抽取并做程序校验，落候选

不做的事：
- 不做关系语义判定（交给人工批次审核）
- 不发布图索引（由 publish 模块在审核通过后执行）
- 不做规则兜底抽取
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.graph.errors import GraphScopeError
from app.graph.extractor import LlmRelationExtractor, PROMPT_TEMPLATE_VERSION
from app.graph.types import ACCESS_SCOPE_PUBLIC, SourceSnapshot
from app.graph.validator import validate_chunk_relations
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.graph import GraphCandidate
from app.repositories.graph_batches import GraphBatchRepository
from app.services.eligibility import apply_document_eligibility

logger = logging.getLogger(__name__)

# 复用校园权限检查器的公开访问级别语义，避免自造一套矛盾的字符串判断。
PUBLIC_ACCESS_LEVELS = frozenset({"public", "open", "公开"})


def is_public_document(document: Document) -> bool:
    """第一版图谱仅收录公开资料。

    判定语义与 app/permissions/campus.py 的公开分支保持一致。
    """
    return str(document.access_level or "").strip().lower() in PUBLIC_ACCESS_LEVELS


def compute_source_fingerprint(
    document: Document, chunks: list[DocumentChunk]
) -> str:
    """来源快照指纹。

    覆盖内容哈希、分块结果与可检索状态。任一项变化都会让旧批次失去有效性，
    因此旧批次不再作为当前证据使用。
    """
    parts: list[str] = [
        f"file_hash={document.file_hash or ''}",
        f"version={document.version or ''}",
        f"access_level={document.access_level or ''}",
        f"review_status={document.review_status or ''}",
        f"status={document.status or ''}",
    ]
    for chunk in sorted(chunks, key=lambda c: (c.chunk_index if c.chunk_index is not None else 0)):
        parts.append(
            f"{chunk.chunk_index}:{chunk.chunk_uuid}:"
            f"{hashlib.sha256((chunk.chunk_text or '').encode('utf-8')).hexdigest()[:16]}"
        )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class BuildOutcome:
    batch_uuid: str
    created: bool
    status: str
    extraction_result: str | None
    candidate_count: int = 0
    rejected_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    source_fingerprint: str = ""


class GraphBuildService:
    def __init__(
        self,
        session: Session,
        extractor: LlmRelationExtractor | None = None,
    ) -> None:
        self.session = session
        self.batches = GraphBatchRepository(session)
        self.extractor = extractor

    # -- 前置校验 -----------------------------------------------------------

    def load_eligible_document(self, doc_uuid: str) -> Document:
        """按统一可检索条件读取文档。不满足条件直接拒绝，不进入图构建。"""
        stmt = apply_document_eligibility(select(Document).where(Document.doc_uuid == doc_uuid))
        document = self.session.scalar(stmt)
        if document is None:
            raise GraphScopeError(
                f"document {doc_uuid} is not in a retrievable state for graph build"
            )
        return document

    def assert_public_scope(self, document: Document) -> str:
        """校验公开范围。非公开资料不得进入图构建、发布与在线召回。"""
        if not is_public_document(document):
            raise GraphScopeError(
                "graph scope is limited to public campus rules and procedure guides"
            )
        return ACCESS_SCOPE_PUBLIC

    def build_snapshot(self, document: Document) -> tuple[SourceSnapshot, list[DocumentChunk]]:
        chunks = list(
            self.session.scalars(
                select(DocumentChunk)
                .where(
                    DocumentChunk.doc_uuid == document.doc_uuid,
                    DocumentChunk.deleted_at.is_(None),
                )
                .order_by(DocumentChunk.chunk_index)
            ).all()
        )
        snapshot = SourceSnapshot(
            doc_uuid=document.doc_uuid,
            doc_version=document.version or "v1",
            source_fingerprint=compute_source_fingerprint(document, chunks),
            chunk_count=len(chunks),
            access_scope=ACCESS_SCOPE_PUBLIC,
            file_hash=document.file_hash or "",
        )
        return snapshot, chunks

    # -- 构建 ---------------------------------------------------------------

    def start_batch(self, doc_uuid: str, *, actor: str | None = None) -> BuildOutcome:
        """创建或复用构建批次（幂等）。不执行抽取。"""
        document = self.load_eligible_document(doc_uuid)
        self.assert_public_scope(document)
        snapshot, chunks = self.build_snapshot(document)

        if not chunks:
            raise GraphScopeError("document has no retrievable chunks for graph build")

        existing = self.batches.get_by_snapshot(doc_uuid, snapshot.source_fingerprint)
        if existing is not None:
            return BuildOutcome(
                batch_uuid=str(existing.batch_uuid),
                created=False,
                status=existing.status,
                extraction_result=existing.extraction_result,
                candidate_count=existing.candidate_count,
                rejected_count=existing.rejected_count,
                source_fingerprint=snapshot.source_fingerprint,
            )

        # 来源变化：旧批次立即失效，不等后台物理清理
        self.batches.mark_superseded(doc_uuid)
        self.batches.deactivate(doc_uuid)

        batch, created = self.batches.try_create_batch(
            doc_uuid=doc_uuid,
            doc_version=snapshot.doc_version,
            source_fingerprint=snapshot.source_fingerprint,
            chunk_count=snapshot.chunk_count,
            access_scope=ACCESS_SCOPE_PUBLIC,
            status="pending",
            extractor_name=self._extractor_name(),
            prompt_template_version=self._prompt_version(),
            created_by=actor,
        )
        self.session.commit()
        return BuildOutcome(
            batch_uuid=str(batch.batch_uuid),
            created=created,
            status=batch.status,
            extraction_result=batch.extraction_result,
            source_fingerprint=snapshot.source_fingerprint,
        )

    def run_extraction(self, batch_uuid: str) -> BuildOutcome:
        """执行抽取与程序校验，落候选。

        抽取失败（模型调用失败 / 输出非法）时批次置 failed，不静默兜底。
        """
        from uuid import UUID

        batch = self.batches.get_by_uuid(UUID(batch_uuid))
        if batch is None:
            raise GraphScopeError(f"graph batch {batch_uuid} not found")

        document = self.load_eligible_document(batch.doc_uuid)
        self.assert_public_scope(document)

        # 来源快照变化时不执行抽取，要求重新构建
        snapshot, chunks = self.build_snapshot(document)
        if not snapshot.is_same_source(
            SourceSnapshot(
                doc_uuid=batch.doc_uuid,
                doc_version=batch.doc_version,
                source_fingerprint=batch.source_fingerprint,
                chunk_count=batch.chunk_count,
                access_scope=batch.access_scope,
            )
        ):
            self.batches.set_status(
                batch.batch_uuid,
                "failed",
                error_code="source_snapshot_changed",
                error_message="document changed after batch creation; rebuild required",
                extraction_result="llm_failed",
            )
            self.session.commit()
            return BuildOutcome(
                batch_uuid=str(batch.batch_uuid),
                created=False,
                status="failed",
                extraction_result="llm_failed",
                error_code="source_snapshot_changed",
                error_message="document changed after batch creation; rebuild required",
            )

        extractor = self.extractor or LlmRelationExtractor(self._settings())
        self.batches.set_status(batch.batch_uuid, "running")
        self.batches.delete_candidates(batch.batch_uuid)
        self.session.commit()

        accepted: list[GraphCandidate] = []
        rejected_count = 0
        hard_failure: tuple[str, str] | None = None
        extraction_result = "empty"

        for chunk in chunks:
            outcome = extractor.extract(
                doc_title=document.title,
                chunk_text=chunk.chunk_text or "",
                chunk_index=chunk.chunk_index if chunk.chunk_index is not None else 0,
            )
            if outcome.result == "llm_failed":
                hard_failure = (outcome.error_code or "llm_call_failed", outcome.error_message or "")
                break
            if outcome.result == "invalid_output":
                hard_failure = (outcome.error_code or "invalid_json", outcome.error_message or "")
                break
            if outcome.result == "empty":
                continue

            validation = validate_chunk_relations(
                outcome.relations,
                doc_uuid=document.doc_uuid,
                chunk_uuid=chunk.chunk_uuid,
                chunk_index=chunk.chunk_index if chunk.chunk_index is not None else 0,
                chunk_text=chunk.chunk_text or "",
                batch_uuid=str(batch.batch_uuid),
                source_fingerprint=batch.source_fingerprint,
            )
            rejected_count += len(validation.rejected)
            accepted.extend(self._to_candidates(validation, batch.batch_uuid, batch.source_fingerprint))
            if validation.has_accepted:
                extraction_result = "candidates"

        if hard_failure is not None:
            code, message = hard_failure
            self.batches.delete_candidates(batch.batch_uuid)
            self.batches.set_status(
                batch.batch_uuid,
                "failed",
                error_code=code,
                error_message=message,
                extraction_result="llm_failed" if "llm" in code else "invalid_output",
            )
            self.session.commit()
            return BuildOutcome(
                batch_uuid=str(batch.batch_uuid),
                created=False,
                status="failed",
                extraction_result="llm_failed" if "llm" in code else "invalid_output",
                error_code=code,
                error_message=message,
            )

        self.batches.add_candidates(accepted)
        if accepted:
            self.batches.set_status(
                batch.batch_uuid,
                "pending_review",
                extraction_result="candidates",
            )
        else:
            # 成功执行但没有通过校验的候选
            self.batches.set_status(
                batch.batch_uuid,
                "failed",
                extraction_result="evidence_rejected" if rejected_count else "empty",
                error_message=(
                    "all extracted relations failed evidence validation"
                    if rejected_count
                    else "no relation extracted"
                ),
            )
        self.batches.refresh_candidate_counts(batch.batch_uuid)
        self.session.commit()

        return BuildOutcome(
            batch_uuid=str(batch.batch_uuid),
            created=False,
            status="pending_review" if accepted else "failed",
            extraction_result=extraction_result if accepted else "evidence_rejected",
            candidate_count=len(accepted),
            rejected_count=rejected_count,
        )

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _to_candidates(validation, batch_uuid, source_fingerprint: str) -> list[GraphCandidate]:
        rows: list[GraphCandidate] = []
        for relation in validation.accepted:
            rows.append(
                GraphCandidate(
                    batch_uuid=batch_uuid,
                    doc_uuid=relation.evidence.doc_uuid,
                    chunk_uuid=relation.evidence.chunk_uuid,
                    chunk_index=relation.evidence.chunk_index,
                    relation_type=relation.relation_type,
                    source_entity_key=relation.source_key,
                    source_entity_type=relation.source_type,
                    source_entity_name=relation.source_name,
                    target_entity_key=relation.target_key,
                    target_entity_type=relation.target_type,
                    target_entity_name=relation.target_name,
                    evidence_text=relation.evidence.evidence_text,
                    evidence_section=relation.evidence.section_title,
                    evidence_page_no=relation.evidence.page_no,
                    applicability=(
                        {"note": relation.applicability} if relation.applicability_known else None
                    ),
                    applicability_known=relation.applicability_known,
                    review_status="pending",
                )
            )
        return rows

    @staticmethod
    def _settings():
        from app.core.config import get_settings

        return get_settings()

    def _extractor_name(self) -> str:
        if self.extractor is not None:
            return self.extractor.name
        return str(getattr(self._settings(), "graph_extractor_name", "llm-constrained-v1"))

    def _prompt_version(self) -> str:
        configured = getattr(self._settings(), "graph_prompt_template_version", None)
        return str(configured or PROMPT_TEMPLATE_VERSION)
