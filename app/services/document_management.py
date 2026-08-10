from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.core.config import get_settings
from app.integrations.vector_store import VectorStoreClient
from app.repositories.document_chunks import DocumentChunkRepository
from app.repositories.documents import DocumentRepository
from app.repositories.jobs import JobRepository
from app.schemas.documents import (
    BatchDocumentOperationItem,
    BatchDocumentOperationResponse,
    DocumentDetail,
    DocumentListItem,
    DocumentUpdateRequest,
)
from app.schemas.pagination import PaginatedResponse
from app.services.ingestion import IngestionService
from app.services.sparse_indexing import SparseIndexingService
from app.models.ingestion_job import IngestionJob


class DocumentManagementService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.documents = DocumentRepository(session)
        self.chunks = DocumentChunkRepository(session)
        self.jobs = JobRepository(session)
        self.vector_store = VectorStoreClient(self.settings)
        self.sparse_indexing = SparseIndexingService()

    def list_documents(
        self,
        *,
        page: int,
        page_size: int,
        source_type: str | None = None,
        source_module: str | None = None,
        parse_status: str | None = None,
        index_status: str | None = None,
        review_status: str | None = None,
        keyword: str | None = None,
    ) -> PaginatedResponse[DocumentListItem]:
        documents, total = self.documents.list_documents(
            page=page,
            page_size=page_size,
            source_type=source_type,
            source_module=source_module,
            parse_status=parse_status,
            index_status=index_status,
            review_status=review_status,
            keyword=keyword,
        )
        return PaginatedResponse(
            items=[self._build_document_list_item(document) for document in documents],
            total=total,
            page=page,
            page_size=page_size,
        )

    def get_document_detail(self, doc_uuid: UUID) -> DocumentDetail:
        document = self.documents.get_by_uuid(doc_uuid)
        if document is None:
            raise AppError(
                code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="document not found",
                status_code=404,
            )
        chunk_count = self.documents.get_chunk_count(doc_uuid)
        return DocumentDetail(
            **self._build_document_list_item(document).model_dump(),
            file_name=document.file_name,
            file_size=document.file_size,
            file_path=document.file_path,
            tags=document.tags,
            extra_meta=document.extra_meta,
            chunk_count=chunk_count,
        )

    @staticmethod
    def _build_document_list_item(document) -> DocumentListItem:
        return DocumentListItem(
            doc_uuid=document.doc_uuid,
            title=document.title,
            source_type=document.source_type,
            source_module=document.source_module,
            version=document.version,
            parse_status=document.parse_status,
            index_status=document.index_status,
            review_status=document.review_status,
            access_level=document.access_level,
            owner_dept=document.owner_dept,
            owner_role=document.owner_role,
            created_by=document.created_by,
            created_at=document.created_at,
            updated_at=document.updated_at,
            file_ext=document.file_ext,
            file_exists=Path(document.file_path).exists() if document.file_path else False,
        )

    def require_document(self, doc_uuid: UUID):
        document = self.documents.get_by_uuid(doc_uuid)
        if document is None:
            raise AppError(
                code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="document not found",
                status_code=404,
            )
        return document

    def create_reindex_job(self, doc_uuid: UUID) -> IngestionJob:
        document = self.require_document(doc_uuid)
        job = IngestionJob(
            doc_uuid=document.doc_uuid,
            job_type="reindex",
            status="pending",
            current_step="queued",
        )
        self.jobs.add(job)
        self.session.commit()
        self.session.refresh(job)
        return job

    def update_document(
        self, doc_uuid: UUID, request: DocumentUpdateRequest,
        upload_actor: str | None = None,
    ) -> DocumentDetail:
        document = self.documents.get_by_uuid(doc_uuid)
        if document is None:
            raise AppError(
                code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="document not found",
                status_code=404,
            )

        # ── 第一层：所有权校验 ──
        # 普通用户只能修改自己上传的文档；管理员和内容管理员可修改所有人
        if upload_actor is not None and document.created_by is not None:
            if upload_actor != document.created_by:
                allowed_roles = self.settings.document_upload_allowed_roles
                if not allowed_roles or not self._actor_has_role(upload_actor, allowed_roles):
                    raise AppError(
                        code=ErrorCode.PERMISSION_DENIED,
                        message="you can only update documents you uploaded",
                        status_code=403,
                    )

        # ── 第二层：access_level/owner_dept/owner_role 字段校验 ──
        self._validate_metadata_fields(
            upload_actor=upload_actor,
            access_level=request.access_level,
            owner_dept=request.owner_dept,
            owner_role=request.owner_role,
        )

        # ── 第三层：已审核文档改可见范围 → 重新审核 ──
        triggers_review = (
            document.review_status == "approved"
            and self._is_visibility_change(request, document)
        )
        if triggers_review:
            from app.repositories.document_review_records import DocumentReviewRecordRepository
            DocumentReviewRecordRepository(self.session).add_record(
                doc_uuid=doc_uuid,
                action="resubmitted",
                actor=upload_actor,
                comment="visibility scope changed, re-entered review",
            )
            document.review_status = "pending"
            document.status = "draft"
            self.session.commit()

        self.documents.update_metadata(
            doc_uuid,
            title=request.title,
            source_type=request.source_type,
            source_module=request.source_module,
            version=request.version,
            access_level=request.access_level,
            owner_dept=request.owner_dept,
            owner_role=request.owner_role,
            tags=request.tags,
            extra_meta=request.extra_meta,
        )
        self.session.commit()
        return self.get_document_detail(doc_uuid)

    def delete_document(self, doc_uuid: UUID) -> None:
        document = self.documents.get_by_uuid(doc_uuid)
        if document is None:
            raise AppError(
                code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="document not found",
                status_code=404,
            )
        chunk_ids = self.chunks.list_chunk_ids_for_document(doc_uuid)
        self.vector_store.delete_embeddings(chunk_ids=chunk_ids)
        self.sparse_indexing.remove_document_chunks(chunk_ids)
        self.chunks.delete_for_document(doc_uuid)
        self.documents.soft_delete(doc_uuid, datetime.now(UTC))
        self.session.commit()
        self._delete_file_if_exists(document.file_path)

    def batch_delete_documents(self, doc_uuids: list[UUID]) -> BatchDocumentOperationResponse:
        items: list[BatchDocumentOperationItem] = []
        for doc_uuid in doc_uuids:
            try:
                self.delete_document(doc_uuid)
                items.append(
                    BatchDocumentOperationItem(
                        doc_uuid=doc_uuid,
                        success=True,
                        message="deleted",
                    )
                )
            except AppError as exc:
                items.append(
                    BatchDocumentOperationItem(
                        doc_uuid=doc_uuid,
                        success=False,
                        message=exc.message,
                    )
                )
        success_count = sum(1 for item in items if item.success)
        return BatchDocumentOperationResponse(
            total=len(items),
            success_count=success_count,
            failed_count=len(items) - success_count,
            items=items,
        )

    def batch_reindex_documents(self, doc_uuids: list[UUID]) -> BatchDocumentOperationResponse:
        items: list[BatchDocumentOperationItem] = []
        ingestion = IngestionService(self.session, self.settings)
        for doc_uuid in doc_uuids:
            try:
                document, job, chunk_count = ingestion.reprocess_document(doc_uuid)
                items.append(
                    BatchDocumentOperationItem(
                        doc_uuid=document.doc_uuid,
                        success=True,
                        message="reindexed",
                        job_uuid=job.job_uuid,
                        chunk_count=chunk_count,
                    )
                )
            except AppError as exc:
                items.append(
                    BatchDocumentOperationItem(
                        doc_uuid=doc_uuid,
                        success=False,
                        message=exc.message,
                    )
                )
        success_count = sum(1 for item in items if item.success)
        return BatchDocumentOperationResponse(
            total=len(items),
            success_count=success_count,
            failed_count=len(items) - success_count,
            items=items,
        )

    def _actor_has_role(self, user_id: str, allowed_roles: str) -> bool:
        """检查用户是否拥有指定角色之一。"""
        from app.repositories.identity import IdentityRepository
        identity = IdentityRepository(self.session)
        user = identity.get_user(user_id)
        if user is None:
            return False
        user_roles = set(user.roles if user.roles else [])
        required = {r.strip() for r in allowed_roles.split(",") if r.strip()}
        return bool(user_roles & required)

    def _validate_metadata_fields(
        self,
        *,
        upload_actor: str | None,
        access_level: str | None,
        owner_dept: str | None,
        owner_role: str | None,
    ) -> None:
        """校验上传者是否有权限设置给定的 access_level/owner_dept/owner_role。

        规则：
        - 未开启身份校验（upload_actor=None）：不限制，向后兼容
        - 可信用户（通过角色白名单）：不限制
        - 普通认证用户：禁止设为 public；department 必须是自己所属部门；
          role 必须是自己拥有的角色
        """
        if upload_actor is None:
            return

        allowed_roles = self.settings.document_upload_allowed_roles
        if allowed_roles and self._actor_has_role(upload_actor, allowed_roles):
            return  # 可信用户，不限制

        # 普通用户：限制 access_level
        if access_level == "public":
            raise AppError(
                code=ErrorCode.INVALID_REQUEST,
                message="only trusted uploaders can set access_level=public",
                status_code=422,
            )

        # 普通用户：校验 department 归属
        if owner_dept is not None:
            from app.repositories.identity import IdentityRepository
            identity = IdentityRepository(self.session)
            user = identity.get_user(upload_actor)
            user_depts = set(user.departments if user and user.departments else [])
            if owner_dept not in user_depts:
                raise AppError(
                    code=ErrorCode.INVALID_REQUEST,
                    message=f"you can only set owner_dept to your own departments",
                    status_code=422,
                )

        # 普通用户：校验 role 权限
        if owner_role is not None:
            from app.repositories.identity import IdentityRepository
            identity = IdentityRepository(self.session)
            user = identity.get_user(upload_actor)
            user_roles = set(user.roles if user and user.roles else [])
            if owner_role not in user_roles:
                raise AppError(
                    code=ErrorCode.INVALID_REQUEST,
                    message=f"you can only set owner_role to your own roles",
                    status_code=422,
                )

    @staticmethod
    def _is_visibility_change(request, document) -> bool:
        """判断本次更新是否改变了文档的可见范围（触发重新审核）。"""
        visibility_fields = {"access_level", "owner_dept", "owner_role"}
        request_data = request.model_dump(exclude_none=True)
        for field in visibility_fields:
            if field in request_data:
                new_val = request_data[field]
                old_val = getattr(document, field, None)
                if new_val != old_val:
                    return True
        return False

    @staticmethod
    def _delete_file_if_exists(file_path: str) -> None:
        if not file_path:
            return
        path = Path(file_path)
        if path.exists():
            path.unlink()
