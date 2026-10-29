"""图谱管理接口。

授权说明：
- 全部路由挂 require_admin_actor，但该依赖在 admin_auth_enabled=false 时返回 None。
  仅挂依赖不等于授权完成，因此所有图审批与发布操作额外要求非空管理员标识，
  否则返回 403，不会继续执行。
- 图候选审批与文档审核是两个独立事件，不复用 Document.review_status。

公开范围：第一版图谱仅收录公开校园制度与办事指南。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_settings_dep, get_trace_id, require_admin_actor
from app.core.config import Settings
from app.core.responses import success_response
from app.graph.builder import GraphBuildService
from app.graph.errors import GraphAdminRequiredError
from app.graph.publish import GraphPublishService
from app.repositories.graph_batches import GraphBatchRepository
from app.schemas.graph import (
    ApproveBatchRequest,
    BuildBatchRequest,
    GraphBatchDetailResponse,
    GraphBatchSummaryResponse,
    GraphCandidateResponse,
    PublishBatchResponse,
)


router = APIRouter(
    prefix="/admin/graph",
    tags=["admin-graph"],
    dependencies=[Depends(require_admin_actor)],
)


def _require_admin(admin_actor: str | None) -> str:
    """图审批与发布必须有可信管理员身份。"""
    if not admin_actor:
        raise GraphAdminRequiredError()
    return admin_actor


# ---------------------------------------------------------------------------
# 构建批次
# ---------------------------------------------------------------------------


@router.post("/batches", response_model=dict)
def create_batch(
    payload: BuildBatchRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """创建构建批次（幂等）。不执行 LLM 抽取。"""
    _require_admin(admin_actor)
    if not settings.graph_enabled:
        data = {"created": False, "reason": "graph feature is disabled"}
        return success_response(data, trace_id)

    service = GraphBuildService(session)
    outcome = service.start_batch(payload.doc_uuid, actor=admin_actor)
    data = {
        "batch_uuid": outcome.batch_uuid,
        "created": outcome.created,
        "status": outcome.status,
        "source_fingerprint": outcome.source_fingerprint,
    }
    return success_response(data, trace_id)


@router.post("/batches/{batch_uuid}/extract", response_model=dict)
def run_extraction(
    batch_uuid: UUID,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """执行 LLM 受约束抽取与程序校验，落候选待审。"""
    _require_admin(admin_actor)
    if not settings.graph_enabled:
        return success_response({"ran": False, "reason": "graph feature is disabled"}, trace_id)

    service = GraphBuildService(session)
    outcome = service.run_extraction(str(batch_uuid))
    data = {
        "batch_uuid": outcome.batch_uuid,
        "ran": True,
        "status": outcome.status,
        "extraction_result": outcome.extraction_result,
        "candidate_count": outcome.candidate_count,
        "rejected_count": outcome.rejected_count,
        "error_code": outcome.error_code,
        "error_message": outcome.error_message,
    }
    return success_response(data, trace_id)


@router.post("/batches/{batch_uuid}/retry", response_model=dict)
def retry_batch(
    batch_uuid: UUID,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """受控重试。来源未变化时复用原批次重跑抽取，不新建批次。"""
    _require_admin(admin_actor)
    if not settings.graph_enabled:
        return success_response({"retried": False, "reason": "graph feature is disabled"}, trace_id)

    service = GraphBuildService(session)
    outcome = service.run_extraction(str(batch_uuid))
    data = {
        "batch_uuid": outcome.batch_uuid,
        "retried": True,
        "status": outcome.status,
        "extraction_result": outcome.extraction_result,
        "candidate_count": outcome.candidate_count,
        "error_code": outcome.error_code,
        "error_message": outcome.error_message,
    }
    return success_response(data, trace_id)


# ---------------------------------------------------------------------------
# 审核与发布
# ---------------------------------------------------------------------------


@router.get("/batches/{batch_uuid}", response_model=dict)
def get_batch(
    batch_uuid: UUID,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """查看批次状态、候选关系与原文证据，供人工剔除。"""
    _require_admin(admin_actor)
    repo = GraphBatchRepository(session)
    batch = repo.get_by_uuid(batch_uuid)
    if batch is None:
        return success_response({"found": False}, trace_id)

    candidates = repo.list_candidates(batch_uuid)
    data = GraphBatchDetailResponse(
        found=True,
        batch=GraphBatchSummaryResponse(
            batch_uuid=str(batch.batch_uuid),
            doc_uuid=batch.doc_uuid,
            doc_version=batch.doc_version,
            status=batch.status,
            extraction_result=batch.extraction_result,
            access_scope=batch.access_scope,
            source_fingerprint=batch.source_fingerprint,
            chunk_count=batch.chunk_count,
            candidate_count=batch.candidate_count,
            approved_count=batch.approved_count,
            rejected_count=batch.rejected_count,
            retry_count=batch.retry_count,
            extractor_name=batch.extractor_name,
            prompt_template_version=batch.prompt_template_version,
            error_code=batch.error_code,
            error_message=batch.error_message,
            reviewed_by=batch.reviewed_by,
            reviewed_at=batch.reviewed_at,
            published_at=batch.published_at,
        ),
        candidates=[
            GraphCandidateResponse(
                candidate_uuid=str(c.candidate_uuid),
                doc_uuid=c.doc_uuid,
                chunk_uuid=c.chunk_uuid,
                chunk_index=c.chunk_index,
                relation_type=c.relation_type,
                source_entity_name=c.source_entity_name,
                source_entity_type=c.source_entity_type,
                target_entity_name=c.target_entity_name,
                target_entity_type=c.target_entity_type,
                evidence_text=c.evidence_text,
                evidence_section=c.evidence_section,
                evidence_page_no=c.evidence_page_no,
                applicability=(c.applicability or {}).get("note") if isinstance(c.applicability, dict) else None,
                applicability_known=bool(c.applicability_known),
                review_status=c.review_status,
            )
            for c in candidates
        ],
    )
    return success_response(data.model_dump(mode="json"), trace_id)


@router.post("/batches/{batch_uuid}/approve", response_model=dict)
def approve_batch(
    batch_uuid: UUID,
    payload: ApproveBatchRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """剔除错误候选后批准其余候选。不自动发布。"""
    _require_admin(admin_actor)
    if not settings.graph_enabled:
        return success_response({"approved": False, "reason": "graph feature is disabled"}, trace_id)

    service = GraphPublishService(session)
    outcome = service.approve_batch(
        batch_uuid,
        rejected_candidate_uuids=payload.rejected_candidate_uuids,
        admin_actor=admin_actor,
    )
    data = PublishBatchResponse(
        batch_uuid=outcome.batch_uuid,
        published=outcome.published,
        status=outcome.status,
        approved_count=outcome.approved_count,
        rejected_count=outcome.rejected_count,
        message=outcome.message,
    )
    return success_response(data.model_dump(mode="json"), trace_id)


@router.post("/batches/{batch_uuid}/publish", response_model=dict)
def publish_batch(
    batch_uuid: UUID,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """写入图索引并切换生效指针。幂等：重复调用不重复发布。"""
    _require_admin(admin_actor)
    if not settings.graph_enabled:
        return success_response({"published": False, "reason": "graph feature is disabled"}, trace_id)

    service = GraphPublishService(session)
    outcome = service.publish_batch(batch_uuid, admin_actor=admin_actor)
    data = PublishBatchResponse(
        batch_uuid=outcome.batch_uuid,
        published=outcome.published,
        status=outcome.status,
        approved_count=outcome.approved_count,
        rejected_count=outcome.rejected_count,
        message=outcome.message,
    )
    return success_response(data.model_dump(mode="json"), trace_id)


# ---------------------------------------------------------------------------
# 文档图状态
# ---------------------------------------------------------------------------


@router.get("/documents/{doc_uuid}", response_model=dict)
def get_document_graph_status(
    doc_uuid: str,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """文档的图构建状态与失败原因，供前端展示。"""
    _require_admin(admin_actor)
    repo = GraphBatchRepository(session)
    batches = repo.list_batches(doc_uuid=doc_uuid)
    pointer = repo.get_active(doc_uuid)
    data = {
        "doc_uuid": doc_uuid,
        "active_batch_uuid": str(pointer.batch_uuid) if pointer is not None else None,
        "graph_active": pointer is not None,
        "batches": [
            {
                "batch_uuid": str(b.batch_uuid),
                "status": b.status,
                "extraction_result": b.extraction_result,
                "access_scope": b.access_scope,
                "candidate_count": b.candidate_count,
                "approved_count": b.approved_count,
                "rejected_count": b.rejected_count,
                "retry_count": b.retry_count,
                "error_code": b.error_code,
                "error_message": b.error_message,
                "created_at": b.created_at.isoformat() if b.created_at else None,
                "reviewed_at": b.reviewed_at.isoformat() if b.reviewed_at else None,
                "published_at": b.published_at.isoformat() if b.published_at else None,
            }
            for b in batches
        ],
    }
    return success_response(data, trace_id)
