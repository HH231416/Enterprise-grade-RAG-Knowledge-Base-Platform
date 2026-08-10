from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_settings_dep, get_trace_id, require_admin_actor
from app.core.config import Settings
from app.core.responses import success_response
from app.schemas.admin import (
    AccessPolicyCreateRequest,
    AccessPolicyMigrationRequest,
    AccessPolicyUpdateRequest,
    DepartmentCreateRequest,
    DepartmentUpdateRequest,
    DocumentReviewRequest,
    DocumentReviewResponse,
    RoleCreateRequest,
    RoleUpdateRequest,
    SystemConfigCreateRequest,
    SystemConfigUpdateRequest,
    UserCreateRequest,
    UserStatusUpdateRequest,
    UserUpdateRequest,
)
from app.repositories.document_review_records import DocumentReviewRecordRepository
from app.services.admin_management import AdminManagementService
from app.services.document_management import DocumentManagementService
from app.services.ingestion import IngestionService


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_actor)])


@router.get("/system-configs", response_model=dict)
def list_system_configs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None),
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).list_system_configs(page=page, page_size=page_size, keyword=keyword)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.get("/system-configs/{config_id}", response_model=dict)
def get_system_config(
    config_id: int,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).get_system_config(config_id)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.post("/system-configs", response_model=dict)
def create_system_config(
    request: SystemConfigCreateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).create_system_config(request)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.patch("/system-configs/{config_id}", response_model=dict)
def update_system_config(
    config_id: int,
    request: SystemConfigUpdateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).update_system_config(config_id, request)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.get("/access-policies", response_model=dict)
def list_access_policies(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    resource_type: str | None = Query(default=None),
    resource_id: str | None = Query(default=None),
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).list_access_policies(
        page=page,
        page_size=page_size,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    return success_response(data.model_dump(mode="json"), trace_id)


@router.post("/access-policies/migrate-to-resource-permissions", response_model=dict)
def migrate_access_policies_to_resource_permissions(
    request: AccessPolicyMigrationRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    admin_actor_id: str | None = Depends(require_admin_actor),
):
    data = AdminManagementService(session).migrate_access_policies_to_resource_permissions(
        request,
        actor_id=admin_actor_id,
    )
    return success_response(data.model_dump(mode="json"), trace_id)


@router.get("/access-policies/{policy_id}", response_model=dict)
def get_access_policy(
    policy_id: int,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).get_access_policy(policy_id)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.post("/access-policies", response_model=dict)
def create_access_policy(
    request: AccessPolicyCreateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).create_access_policy(request)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.patch("/access-policies/{policy_id}", response_model=dict)
def update_access_policy(
    policy_id: int,
    request: AccessPolicyUpdateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).update_access_policy(policy_id, request)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.delete("/access-policies/{policy_id}", response_model=dict)
def delete_access_policy(
    policy_id: int,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    AdminManagementService(session).delete_access_policy(policy_id)
    return success_response({"policy_id": policy_id, "deleted": True}, trace_id)


@router.get("/departments", response_model=dict)
def list_departments(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).list_departments(
        page=page,
        page_size=page_size,
        keyword=keyword,
        status=status,
    )
    return success_response(data.model_dump(mode="json"), trace_id)


@router.get("/departments/{dept_code}", response_model=dict)
def get_department(
    dept_code: str,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).get_department(dept_code)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.post("/departments", response_model=dict)
def create_department(
    request: DepartmentCreateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).create_department(request)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.patch("/departments/{dept_code}", response_model=dict)
def update_department(
    dept_code: str,
    request: DepartmentUpdateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).update_department(dept_code, request)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.get("/roles", response_model=dict)
def list_roles(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).list_roles(
        page=page,
        page_size=page_size,
        keyword=keyword,
        status=status,
    )
    return success_response(data.model_dump(mode="json"), trace_id)


@router.get("/roles/{role_code}", response_model=dict)
def get_role(
    role_code: str,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).get_role(role_code)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.post("/roles", response_model=dict)
def create_role(
    request: RoleCreateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).create_role(request)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.patch("/roles/{role_code}", response_model=dict)
def update_role(
    role_code: str,
    request: RoleUpdateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).update_role(role_code, request)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.get("/users", response_model=dict)
def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).list_users(
        page=page,
        page_size=page_size,
        keyword=keyword,
        status=status,
    )
    return success_response(data.model_dump(mode="json"), trace_id)


@router.get("/users/{user_id}", response_model=dict)
def get_user(
    user_id: str,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).get_user(user_id)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.patch("/users/{user_id}/status", response_model=dict)
def update_user_status(
    user_id: str,
    request: UserStatusUpdateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    admin_actor_id: str | None = Depends(require_admin_actor),
):
    data = AdminManagementService(session).update_user_status(
        user_id,
        request,
        actor_id=admin_actor_id,
    )
    return success_response(data.model_dump(mode="json"), trace_id)


@router.post("/users", response_model=dict)
def create_user(
    request: UserCreateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).create_user(request)
    return success_response(data.model_dump(mode="json"), trace_id)


@router.patch("/users/{user_id}", response_model=dict)
def update_user(
    user_id: str,
    request: UserUpdateRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    data = AdminManagementService(session).update_user(user_id, request)
    return success_response(data.model_dump(mode="json"), trace_id)


# ── 文档审核 ──────────────────────────────────────────────────────
# 状态机: pending → approved / rejected
#         approved → revoked → approved
#         rejected → pending (重新提交)


@router.get("/documents/pending", response_model=dict)
def list_pending_documents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    """列出待审核文档（review_status=pending）"""
    data = DocumentManagementService(session).list_documents(
        page=page, page_size=page_size, review_status="pending",
    )
    return success_response(data.model_dump(mode="json"), trace_id)


@router.post("/documents/{doc_uuid}/approve", response_model=dict)
def approve_document(
    doc_uuid: UUID,
    request: DocumentReviewRequest | None = None,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """通过审核：触发索引构建，review_status → approved, status → active"""
    service = DocumentManagementService(session)
    review_records = DocumentReviewRecordRepository(session)
    doc = service.require_document(doc_uuid)

    comment = request.comment if request else None

    # 记录审核历史
    review_records.add_record(
        doc_uuid=doc_uuid, action="approved", actor=admin_actor, comment=comment,
    )

    # 更新文档审核快照
    doc.review_status = "approved"
    doc.reviewed_by = admin_actor
    doc.reviewed_at = datetime.now(UTC)
    doc.review_comment = comment
    session.commit()

    # 触发索引构建（稀疏索引 + Embedding + 向量写入）
    publish_job, vector_count = IngestionService(session, settings).publish_document(
        document=doc, admin_actor=admin_actor,
    )
    session.refresh(doc)

    return success_response(
        DocumentReviewResponse(
            doc_uuid=doc_uuid, review_status="approved",
            message=f"document approved, {vector_count} vectors indexed",
        ).model_dump(mode="json"),
        trace_id,
    )


@router.post("/documents/{doc_uuid}/reject", response_model=dict)
def reject_document(
    doc_uuid: UUID,
    request: DocumentReviewRequest | None = None,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """拒绝文档：标记为 rejected，保留解析快照和文件以便 resubmit"""
    service = DocumentManagementService(session)
    review_records = DocumentReviewRecordRepository(session)
    doc = service.require_document(doc_uuid)

    comment = request.comment if request else None
    review_records.add_record(
        doc_uuid=doc_uuid, action="rejected", actor=admin_actor, comment=comment,
    )

    doc.review_status = "rejected"
    doc.status = "inactive"
    doc.reviewed_by = admin_actor
    doc.reviewed_at = datetime.now(UTC)
    doc.review_comment = comment
    session.commit()

    return success_response(
        DocumentReviewResponse(
            doc_uuid=doc_uuid, review_status="rejected",
            message="document rejected",
        ).model_dump(mode="json"),
        trace_id,
    )


@router.post("/documents/{doc_uuid}/revoke", response_model=dict)
def revoke_document(
    doc_uuid: UUID,
    request: DocumentReviewRequest | None = None,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """撤回已发布文档：review_status → revoked，立即下架但不删除文件/分块/向量"""
    service = DocumentManagementService(session)
    review_records = DocumentReviewRecordRepository(session)
    doc = service.require_document(doc_uuid)

    if doc.review_status != "approved":
        from app.core.errors import AppError, ErrorCode
        raise AppError(
            code=ErrorCode.INVALID_REQUEST,
            message="only approved documents can be revoked",
            status_code=422,
        )

    comment = request.comment if request else None
    review_records.add_record(
        doc_uuid=doc_uuid, action="revoked", actor=admin_actor, comment=comment,
    )

    doc.review_status = "revoked"
    doc.status = "inactive"
    doc.reviewed_by = admin_actor
    doc.reviewed_at = datetime.now(UTC)
    doc.review_comment = comment
    session.commit()

    return success_response(
        DocumentReviewResponse(
            doc_uuid=doc_uuid, review_status="revoked",
            message="document revoked, removed from retrieval",
        ).model_dump(mode="json"),
        trace_id,
    )


@router.post("/documents/{doc_uuid}/resubmit", response_model=dict)
def resubmit_document(
    doc_uuid: UUID,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    admin_actor: str | None = Depends(require_admin_actor),
):
    """重新提交已拒绝文档：review_status → pending"""
    service = DocumentManagementService(session)
    review_records = DocumentReviewRecordRepository(session)
    doc = service.require_document(doc_uuid)

    if doc.review_status not in ("rejected", "revoked"):
        from app.core.errors import AppError, ErrorCode
        raise AppError(
            code=ErrorCode.INVALID_REQUEST,
            message="only rejected or revoked documents can be resubmitted",
            status_code=422,
        )

    review_records.add_record(
        doc_uuid=doc_uuid, action="resubmitted", actor=admin_actor,
        comment="resubmitted for review",
    )

    doc.review_status = "pending"
    doc.status = "draft"
    session.commit()

    return success_response(
        DocumentReviewResponse(
            doc_uuid=doc_uuid, review_status="pending",
            message="document resubmitted for review",
        ).model_dump(mode="json"),
        trace_id,
    )
