"""文档摄入服务：编排"解析 → 分块 → 向量化索引"的完整生命周期。

核心流程：
1. 解析 (Parsing)：根据文件扩展名选择解析器，提取文本块
2. 分块 (Chunking)：按配置的策略（fixed/structural/table-aware/parent-child/semantic）切分文本
3. 索引 (Indexing)：将分块文本向量化并存入向量数据库

状态机：
  document.status: pending → processing → active / failed
  job.status:      pending → running → success / failed
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime  # UTC 统一时区，datetime 记录任务时间
from pathlib import Path

from sqlalchemy.orm import Session  # 数据库会话

from app.core.errors import AppError, ErrorCode  # 自定义异常
from app.integrations.embedding import EmbeddingProvider
from app.ingestion.chunking import build_chunks  # 分块构建函数
from app.ingestion.types import ChunkingOptions   # 分块选项数据类
from app.ingestion.parser_registry import ParserRegistry  # 解析器注册表（策略模式）
from app.models.document import Document          # 文档 ORM 模型
from app.models.document_chunk import DocumentChunk  # 分块 ORM 模型
from app.models.ingestion_job import IngestionJob  # 摄入任务 ORM 模型
from app.repositories.document_chunks import DocumentChunkRepository  # 分块仓库
from app.repositories.documents import DocumentRepository  # 文档仓库
from app.repositories.jobs import JobRepository  # 任务仓库
from app.services.indexing import IndexingService  # 稠密向量索引服务
from app.services.sparse_indexing import SparseIndexingService  # 稀疏索引服务


logger = logging.getLogger(__name__)  # 模块级日志记录器


class IngestionService:
    """文档摄入服务：编排解析、分块、向量化的核心业务流程。

    依赖多个 Repository（数据访问层）+ Service（索引服务）+ ParserRegistry（解析器注册表）。
    """

    def __init__(self, session: Session, settings=None) -> None:
        self.session = session          # 数据库会话
        self.settings = settings        # 应用配置（可选，为 None 则跳过向量化）
        self.documents = DocumentRepository(session)  # 文档数据访问
        self.jobs = JobRepository(session)            # 任务数据访问
        self.chunks = DocumentChunkRepository(session)  # 分块数据访问
        self.parser_registry = ParserRegistry()       # 解析器注册表
        self.sparse_indexing = SparseIndexingService() # 稀疏索引服务

    def process_upload(self, document: Document, job: IngestionJob) -> int:
        return self.process_upload_with_cleanup(document=document, job=job, stale_chunk_ids=[])

    def process_upload_with_cleanup(
        self,
        *,
        document: Document,
        job: IngestionJob,
        stale_chunk_ids: list[str],
    ) -> int:
        document.status = "processing"
        document.parse_status = "running"
        job.status = "running"
        job.current_step = "parsing"
        job.started_at = datetime.now(UTC)
        self.session.commit()

        try:
            file_path = Path(document.file_path)
            parsed_blocks = self.parser_registry.parse(document.file_ext, file_path)
            if not parsed_blocks:
                raise AppError(
                    code=ErrorCode.DOCUMENT_PARSE_FAILED,
                    message="document parser produced no text blocks",
                    status_code=422,
                )

            job.current_step = "chunking"
            self.session.commit()

            chunk_options = self._build_chunking_options(document)
            semantic_embedder = None
            if chunk_options.strategy.strip().lower() == "semantic":
                if self.settings is None:
                    raise AppError(
                        code=ErrorCode.EMBEDDING_FAILED,
                        message="semantic chunking requires an embedding provider",
                        status_code=422,
                    )
                provider = EmbeddingProvider(self.settings)
                semantic_embedder = lambda texts: [result.vector for result in provider.embed_texts(texts)]
            chunk_payloads = build_chunks(
                parsed_blocks,
                options=chunk_options,
                semantic_embedder=semantic_embedder,
            )
            if not chunk_payloads:
                raise AppError(
                    code=ErrorCode.CHUNKING_FAILED,
                    message="chunking produced no chunks",
                    status_code=422,
                )

            chunk_models = [
                DocumentChunk(
                    chunk_uuid=uuid.uuid4(),
                    doc_uuid=document.doc_uuid,
                    chunk_index=payload.chunk_index,
                    chunk_type=payload.chunk_type,
                    section_title=payload.section_title,
                    page_no=payload.page_no,
                    sheet_name=payload.sheet_name,
                    row_start=payload.row_start,
                    row_end=payload.row_end,
                    token_count=payload.token_count,
                    char_count=payload.char_count,
                    chunk_text=payload.chunk_text,
                    metadata_json=payload.metadata_json,
                    parent_chunk_uuid=(
                        uuid.UUID(payload.parent_chunk_uuid) if payload.parent_chunk_uuid else None
                    ),
                    chunk_group_uuid=(
                        uuid.UUID(payload.chunk_group_uuid) if payload.chunk_group_uuid else None
                    ),
                    chunk_level=payload.chunk_level,
                    context_text=payload.context_text,
                )
                for payload in chunk_payloads
            ]

            self.chunks.replace_for_document(document.doc_uuid, chunk_models)
            self.sparse_indexing.replace_document_chunks(
                document=document,
                stale_chunk_ids=stale_chunk_ids,
                chunks=chunk_models,
            )
            document.status = "active"
            document.parse_status = "success"
            document.index_status = "pending"
            self.session.commit()
            if self.settings is not None:
                indexed_count = IndexingService(self.settings, self.session).index_document_with_cleanup(
                    document=document,
                    job=job,
                    stale_chunk_ids=stale_chunk_ids,
                )
            else:
                indexed_count = 0
                job.status = "success"
                job.current_step = "chunking_completed"
                job.finished_at = datetime.now(UTC)
                self.session.commit()
            logger.info(
                "Parsed document %s into %s chunks",
                document.doc_uuid,
                len(chunk_models),
            )
            return len(chunk_models) if indexed_count >= 0 else len(chunk_models)
        except AppError as exc:
            self._mark_failed(document.doc_uuid, job.job_uuid, exc.message, str(exc.code))
            raise
        except Exception as exc:
            logger.exception("Document ingestion failed")
            self._mark_failed(
                document.doc_uuid,
                job.job_uuid,
                str(exc),
                str(ErrorCode.DOCUMENT_PARSE_FAILED),
            )
            raise AppError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                message="document parsing failed",
                status_code=422,
            ) from exc

    def reprocess_document(self, doc_uuid: uuid.UUID) -> tuple[Document, IngestionJob, int]:
        document = self.documents.get_by_uuid(doc_uuid)
        if document is None or document.deleted_at is not None:
            raise AppError(
                code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="document not found",
                status_code=404,
            )

        job = IngestionJob(
            doc_uuid=document.doc_uuid,
            job_type="reindex",
            status="pending",
            current_step="created",
        )
        self.jobs.add(job)
        self.session.commit()
        self.session.refresh(job)
        stale_chunk_ids = self.chunks.list_chunk_ids_for_document(document.doc_uuid)
        chunk_count = self.process_upload_with_cleanup(
            document=document,
            job=job,
            stale_chunk_ids=stale_chunk_ids,
        )
        return document, job, chunk_count

    def resume_job(self, *, doc_uuid: uuid.UUID, job_uuid: uuid.UUID) -> int:
        document = self.documents.get_by_uuid(doc_uuid)
        job = self.jobs.get_by_uuid(job_uuid)
        if document is None:
            raise AppError(
                code=ErrorCode.DOCUMENT_NOT_FOUND,
                message="document not found",
                status_code=404,
            )
        if job is None:
            raise AppError(
                code=ErrorCode.JOB_NOT_FOUND,
                message="job not found",
                status_code=404,
            )
        stale_chunk_ids = self.chunks.list_chunk_ids_for_document(document.doc_uuid) if job.job_type == "reindex" else []
        return self.process_upload_with_cleanup(
            document=document,
            job=job,
            stale_chunk_ids=stale_chunk_ids,
        )

    def _mark_failed(
        self,
        doc_uuid: uuid.UUID,
        job_uuid: uuid.UUID,
        error_message: str,
        error_code: str,
    ) -> None:
        self.session.rollback()
        document = self.documents.get_by_uuid(doc_uuid)
        job = self.jobs.get_by_uuid(job_uuid)
        if document is None or job is None:
            return
        
        # 删除已上传的文件
        if document.file_path:
            try:
                file_path = Path(document.file_path)
                if file_path.exists():
                    file_path.unlink()
                    logger.info("Deleted failed upload file: %s", file_path)
            except Exception as e:
                logger.warning("Failed to delete upload file: %s", e)
        
        document.status = "failed"
        document.parse_status = "failed"
        job.status = "failed"
        job.current_step = "failed"
        job.error_code = error_code
        job.error_message = error_message
        job.finished_at = datetime.now(UTC)
        self.session.commit()

    @staticmethod
    def _get_chunking_strategy(document: Document) -> str:
        value = document.extra_meta.get("chunking_strategy") if document.extra_meta else None
        return str(value).strip() if value else "fixed"

    @staticmethod
    def _build_chunking_options(document: Document) -> ChunkingOptions:
        extra_meta = document.extra_meta or {}
        return ChunkingOptions(
            strategy=IngestionService._get_chunking_strategy(document),
            max_chars=IngestionService._read_int_option(extra_meta, "max_chars", default=1200, minimum=100, maximum=8000),
            overlap_chars=IngestionService._read_int_option(extra_meta, "overlap_chars", default=150, minimum=0, maximum=2000),
            table_rows_per_chunk=IngestionService._read_int_option(
                extra_meta,
                "table_rows_per_chunk",
                default=20,
                minimum=1,
                maximum=500,
            ),
            parent_max_chars=IngestionService._read_int_option(
                extra_meta,
                "parent_max_chars",
                default=3000,
                minimum=1000,
                maximum=8000,
            ),
            child_max_chars=IngestionService._read_int_option(
                extra_meta,
                "child_max_chars",
                default=600,
                minimum=200,
                maximum=2000,
            ),
            min_chunk_sentences=IngestionService._read_int_option(
                extra_meta,
                "min_chunk_sentences",
                default=3,
                minimum=1,
                maximum=50,
            ),
            max_chunk_sentences=IngestionService._read_int_option(
                extra_meta,
                "max_chunk_sentences",
                default=20,
                minimum=2,
                maximum=100,
            ),
            similarity_threshold=IngestionService._read_float_option(
                extra_meta,
                "similarity_threshold",
                default=0.5,
                minimum=0.0,
                maximum=1.0,
            ),
            merge_window=IngestionService._read_int_option(
                extra_meta,
                "merge_window",
                default=3,
                minimum=1,
                maximum=10,
            ),
        )

    @staticmethod
    def _read_int_option(
        extra_meta: dict,
        key: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        value = extra_meta.get(key)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _read_float_option(
        extra_meta: dict,
        key: str,
        *,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        value = extra_meta.get(key)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(parsed, maximum))
