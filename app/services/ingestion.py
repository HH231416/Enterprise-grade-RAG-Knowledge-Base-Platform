"""文档摄入服务：编排"解析 → 分块 → 向量化索引"的完整生命周期。

核心流程（普通模式）：
1. 解析 (Parsing)：根据文件扩展名选择解析器，提取文本块
2. 分块 (Chunking)：按配置的策略切分文本
3. 稀疏索引 (Sparse)：写入 BM25 索引
4. 向量索引 (Indexing)：Embedding + 写入向量数据库

审核模式（Design D：审核与分块解耦）：
- 待审核文档只执行解析并把解析快照落库，不生成 chunk、不调 Embedding、
  不建索引——审核是"内容闸门"，审核界面展示的就是最终会进索引的解析文本。
- 审核通过后创建独立的 publish job：用落库的解析快照按配置的真实策略分块
  （语义分块此时允许外调 Embedding）→ 稀疏索引 → 稠密向量 → 上线。

状态机：
  普通文档：   pending → running → success / failed
  审核文档摄入：pending → parsing → awaiting_review（无 chunk）
  审核文档发布：pending → chunking → running → success / failed
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from datetime import UTC, datetime  # UTC 统一时区，datetime 记录任务时间
from pathlib import Path

from sqlalchemy.orm import Session  # 数据库会话

from app.core.errors import AppError, ErrorCode  # 自定义异常
from app.integrations.embedding import EmbeddingProvider
from app.ingestion.chunking import build_chunks  # 分块构建函数
from app.ingestion.types import ChunkingOptions, ParsedBlock  # 分块选项、解析块数据类
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
        # 待审核文档只做解析+分块，延迟稀疏索引和向量化
        review_pending = document.review_status == "pending"
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

            # 持久化解析结果快照：待审核文档审核阶段不生成 chunk，
            # 发布时用快照按真实配置策略分块，保证"审核看到的就是进索引的"。
            document.parsed_blocks = [block.to_dict() for block in parsed_blocks]

            # ── 待审核文档：解析即止，不生成 chunk、不调 Embedding ──
            # 审核与分块解耦：审核是内容闸门（审解析文本），分块是索引工艺（发布时做）。
            # 语义分块要外调 Embedding，审核前外发会让审核形同虚设，故整体推迟。
            if review_pending:
                self._clear_failed_file_retention(document)
                document.status = "draft"
                document.parse_status = "awaiting_review"
                document.index_status = "pending"
                job.status = "awaiting_review"
                job.current_step = "parsing_completed"
                job.finished_at = datetime.now(UTC)
                self.session.commit()
                logger.info("Parsed document %s, awaiting review", document.doc_uuid)
                return 0

            # ── 普通/自动通过文档：继续分块 + 建索引 ──
            job.current_step = "chunking"
            self.session.commit()

            chunk_models = self._build_chunks_for_document(document, parsed_blocks)
            self.chunks.replace_for_document(document.doc_uuid, chunk_models)
            self._clear_failed_file_retention(document)
            self.session.commit()

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
        self._require_retryable_source_file(document)

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
        self._require_retryable_source_file(document)
        stale_chunk_ids = self.chunks.list_chunk_ids_for_document(document.doc_uuid) if job.job_type == "reindex" else []
        return self.process_upload_with_cleanup(
            document=document,
            job=job,
            stale_chunk_ids=stale_chunk_ids,
        )

    def publish_document(
        self,
        *,
        document: Document,
        admin_actor: str | None = None,
    ) -> tuple[IngestionJob, int]:
        """审核通过后发布文档：按真实策略分块 + 稀疏索引 + Embedding + 向量写入。

        分块用摄入时落库的解析快照，配置的 chunking_strategy（含 semantic）此时生效，
        因为审核已通过，外调 Embedding 是合法动作。
        发布成功后 document.status → active, parse_status → success（补上统一可检索条件）。
        """
        parsed_blocks = self._load_parsed_blocks(document)

        job = IngestionJob(
            doc_uuid=document.doc_uuid,
            job_type="publish",
            status="running",
            current_step="chunking",
            started_at=datetime.now(UTC),
            created_by=admin_actor,
        )
        self.jobs.add(job)
        self.session.commit()
        self.session.refresh(job)

        try:
            # 按文档配置的真实策略分块（此时允许语义分块外调 Embedding）
            chunks = self._build_chunks_for_document(document, parsed_blocks)
            self.chunks.replace_for_document(document.doc_uuid, chunks)
            self.session.commit()

            # 稀疏索引（审核前跳过的步骤）
            job.current_step = "sparse_indexing"
            self.session.commit()
            self.sparse_indexing.replace_document_chunks(
                document=document,
                stale_chunk_ids=[],
                chunks=chunks,
            )
            document.index_status = "pending"
            self.session.commit()

            # Embedding + 向量写入
            if self.settings is not None:
                indexed_count = IndexingService(self.settings, self.session).index_document_with_cleanup(
                    document=document,
                    job=job,
                    stale_chunk_ids=[],
                )
            else:
                indexed_count = 0
                job.status = "success"
                job.current_step = "sparse_indexing_completed"
                job.finished_at = datetime.now(UTC)
                document.index_status = "success"
                document.status = "active"
                self.session.commit()

            # 审核流补上 parse_status=success，满足统一可检索条件
            # （摄入阶段该值为 awaiting_review，发布成功后才算解析成功可检索）
            document.parse_status = "success"
            self.session.commit()

            logger.info(
                "Published document %s with %s chunks / %s vectors",
                document.doc_uuid,
                len(chunks),
                indexed_count,
            )
            return job, indexed_count
        except AppError:
            self._mark_index_failed(document.doc_uuid, job.job_uuid)
            raise
        except Exception as exc:
            logger.exception("Document publish failed")
            self._mark_index_failed(document.doc_uuid, job.job_uuid)
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message="document publish failed",
                status_code=422,
            ) from exc

    def _load_parsed_blocks(self, document: Document) -> list[ParsedBlock]:
        """获取解析块：优先用摄入时落库的快照，缺失时回退从源文件重新解析。

        快照保证"审核看到的就是分块嵌入的"，且不依赖源文件仍可访问。
        """
        snapshot = getattr(document, "parsed_blocks", None)
        if snapshot:
            blocks = [ParsedBlock.from_dict(block) for block in snapshot if block]
            if blocks:
                return blocks
        file_path = Path(document.file_path)
        if not file_path.is_file():
            raise AppError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                message="document file unavailable, parsed snapshot missing",
                status_code=422,
            )
        parsed_blocks = self.parser_registry.parse(document.file_ext, file_path)
        if not parsed_blocks:
            raise AppError(
                code=ErrorCode.DOCUMENT_PARSE_FAILED,
                message="document parser produced no text blocks",
                status_code=422,
            )
        return parsed_blocks

    def _build_chunks_for_document(
        self,
        document: Document,
        parsed_blocks: list[ParsedBlock],
    ) -> list[DocumentChunk]:
        """按文档配置的分块策略构建分块模型。

        语义分块会调用外部 Embedding：普通文档在摄入阶段、审核文档在发布阶段
        （已通过审核）调用都是合法的。
        """
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
        return [self._payload_to_chunk_model(document, payload) for payload in chunk_payloads]

    @staticmethod
    def _payload_to_chunk_model(document: Document, payload) -> DocumentChunk:
        return DocumentChunk(
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

    def _mark_index_failed(
        self,
        doc_uuid: uuid.UUID,
        job_uuid: uuid.UUID,
    ) -> None:
        self.session.rollback()
        document = self.documents.get_by_uuid(doc_uuid)
        job = self.jobs.get_by_uuid(job_uuid)
        if document is None or job is None:
            return
        document.index_status = "failed"
        job.status = "failed"
        job.current_step = "failed"
        job.finished_at = datetime.now(UTC)
        self.session.commit()

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
        
        # Keep the source file through the retry window; scheduled cleanup removes it later.
        retention_hours = (
            self.settings.failed_upload_retention_hours
            if self.settings is not None
            else 72
        )
        retention_until = datetime.now(UTC) + timedelta(hours=retention_hours)
        document.extra_meta = {
            **(document.extra_meta or {}),
            "failed_file_retention_until": retention_until.isoformat(),
            "failed_file_retention_hours": retention_hours,
        }
        
        document.status = "failed"
        document.parse_status = "failed"
        job.status = "failed"
        job.current_step = "failed"
        job.error_code = error_code
        job.error_message = error_message
        job.finished_at = datetime.now(UTC)
        self.session.commit()

    @staticmethod
    def _clear_failed_file_retention(document: Document) -> None:
        document.extra_meta = {
            key: value
            for key, value in (document.extra_meta or {}).items()
            if not key.startswith("failed_file_")
        }

    @staticmethod
    def _require_retryable_source_file(document: Document) -> None:
        if document.file_path and Path(document.file_path).is_file():
            return
        raise AppError(
            code=ErrorCode.DOCUMENT_PARSE_FAILED,
            message="source file is unavailable because its failed-upload retention window has expired",
            status_code=422,
        )

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
