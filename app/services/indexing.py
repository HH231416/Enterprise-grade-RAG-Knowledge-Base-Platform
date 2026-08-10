from __future__ import annotations

from collections import OrderedDict
import hashlib
import logging
import threading
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.trace import get_trace_id
from app.integrations.embedding import EmbeddingProvider
from app.integrations.vector_store import VectorStoreClient
from app.models.document import Document
from app.models.ingestion_job import IngestionJob
from app.models.llm_call_log import LlmCallLog
from app.repositories.document_chunks import DocumentChunkRepository
from app.repositories.llm_call_logs import LlmCallLogRepository


logger = logging.getLogger(__name__)


class IndexingService:
    # Reuse vectors for unchanged extracted images while retaining a separate
    # vector record and document-level access metadata for every occurrence.
    _image_embedding_cache: OrderedDict[str, object] = OrderedDict()
    _image_embedding_cache_lock = threading.Lock()
    _image_embedding_cache_max_entries = 4096

    def __init__(self, settings: Settings, session: Session) -> None:
        self.settings = settings
        self.session = session
        self.embedding_provider = EmbeddingProvider(settings)
        self.vector_store = VectorStoreClient(settings)
        self.chunks = DocumentChunkRepository(session)
        self.llm_logs = LlmCallLogRepository(session)

    def index_document(self, document: Document, job: IngestionJob) -> int:
        return self.index_document_with_cleanup(document=document, job=job, stale_chunk_ids=[])

    def index_document_with_cleanup(
        self,
        *,
        document: Document,
        job: IngestionJob,
        stale_chunk_ids: list[str],
    ) -> int:
        chunks = self.chunks.list_for_document(document.doc_uuid)
        if not chunks:
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message="no chunks available for indexing",
                status_code=422,
            )

        document.index_status = "running"
        job.current_step = "embedding"
        self.session.commit()

        try:
            if stale_chunk_ids:
                self.vector_store.delete_embeddings(chunk_ids=stale_chunk_ids)

            embeddings = self._embed_chunks(chunks)
            self._record_embedding_logs(embeddings)

            job.current_step = "vector_upsert"
            self.session.commit()

            vector_records = self.vector_store.upsert_embeddings(
                embeddings=[item.vector for item in embeddings],
                metadatas=[
                    {
                        "doc_uuid": str(document.doc_uuid),
                        "chunk_uuid": str(chunk.chunk_uuid),
                        "chunk_text": chunk.chunk_text,
                        "source_type": document.source_type,
                        "source_module": document.source_module,
                        "file_ext": document.file_ext,
                        "version": document.version,
                        "page_no": chunk.page_no,
                        "sheet_name": chunk.sheet_name,
                        "section_title": chunk.section_title,
                        "access_level": document.access_level,
                        "owner_dept": document.owner_dept,
                        "owner_role": document.owner_role,
                        "created_at": document.created_at.isoformat(),
                        "updated_at": document.updated_at.isoformat(),
                    }
                    for chunk in chunks
                ],
            )

            chunk_uuid_to_vector = {
                chunk.chunk_uuid: (record.vector_id, record.collection)
                for chunk, record in zip(chunks, vector_records, strict=True)
            }
            self.chunks.update_vector_bindings(chunk_uuid_to_vector)

            document.index_status = "success"
            document.status = "active"
            job.current_step = "indexed"
            job.status = "success"
            job.finished_at = datetime.now(UTC)
            self.session.commit()
            logger.info(
                "Indexed document %s with %s vectors",
                document.doc_uuid,
                len(vector_records),
            )
            return len(vector_records)
        except AppError:
            self._mark_failed(document, job, ErrorCode.VECTOR_WRITE_FAILED, "indexing failed")
            raise
        except Exception as exc:
            logger.exception("Document indexing failed")
            self._mark_failed(document, job, ErrorCode.VECTOR_WRITE_FAILED, str(exc))
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message="vector indexing failed",
                status_code=422,
            ) from exc

    def _record_embedding_logs(self, embeddings) -> None:
        trace_id = get_trace_id()
        for embedding in embeddings:
            self.llm_logs.add(
                LlmCallLog(
                    trace_id=trace_id,
                    provider_type="embedding",
                    provider_name=embedding.provider_name,
                    model_name=embedding.model_name,
                    request_type="embed",
                    request_tokens=embedding.request_tokens,
                    response_tokens=None,
                    latency_ms=0,
                    status="success",
                )
            )
        self.session.commit()

    def _embed_chunks(self, chunks):
        """Embed chunks, reusing vectors for identical cached image content."""
        embeddings = [None] * len(chunks)
        uncached_indexes: list[int] = []
        image_groups: dict[str, list[int]] = {}

        for index, chunk in enumerate(chunks):
            cache_key = self._image_embedding_cache_key(chunk)
            if cache_key is None:
                uncached_indexes.append(index)
                continue
            cached = self._get_cached_image_embedding(cache_key)
            if cached is not None:
                embeddings[index] = cached
                continue
            image_groups.setdefault(cache_key, []).append(index)

        requests: list[tuple[str | None, int]] = [(None, index) for index in uncached_indexes]
        requests.extend((cache_key, indexes[0]) for cache_key, indexes in image_groups.items())
        if requests:
            generated = self.embedding_provider.embed_texts(
                [chunks[index].chunk_text for _, index in requests]
            )
            for (cache_key, index), embedding in zip(requests, generated, strict=True):
                if cache_key is None:
                    embeddings[index] = embedding
                    continue
                self._cache_image_embedding(cache_key, embedding)
                for grouped_index in image_groups[cache_key]:
                    embeddings[grouped_index] = self._copy_embedding(embedding)

        if any(embedding is None for embedding in embeddings):
            raise AppError(
                code=ErrorCode.EMBEDDING_FAILED,
                message="embedding cache produced an incomplete result set",
                status_code=422,
            )
        return embeddings

    def _image_embedding_cache_key(self, chunk) -> str | None:
        image_hash = (chunk.metadata_json or {}).get("image_hash")
        if not isinstance(image_hash, str) or not image_hash:
            return None
        text_hash = hashlib.sha256(chunk.chunk_text.encode("utf-8")).hexdigest()
        return "|".join(
            (
                image_hash,
                text_hash,
                self.settings.embedding_api_base or "",
                self.settings.embedding_model or "",
                self.settings.embedding_local_model or "",
                str(self.settings.embedding_vector_size),
            )
        )

    @staticmethod
    def _copy_embedding(embedding):
        return type(embedding)(
            text=embedding.text,
            vector=list(embedding.vector),
            model_name=embedding.model_name,
            provider_name=embedding.provider_name,
            request_tokens=embedding.request_tokens,
        )

    @classmethod
    def _get_cached_image_embedding(cls, cache_key: str):
        with cls._image_embedding_cache_lock:
            embedding = cls._image_embedding_cache.get(cache_key)
            if embedding is None:
                return None
            cls._image_embedding_cache.move_to_end(cache_key)
            return cls._copy_embedding(embedding)

    @classmethod
    def _cache_image_embedding(cls, cache_key: str, embedding) -> None:
        with cls._image_embedding_cache_lock:
            cls._image_embedding_cache[cache_key] = cls._copy_embedding(embedding)
            cls._image_embedding_cache.move_to_end(cache_key)
            while len(cls._image_embedding_cache) > cls._image_embedding_cache_max_entries:
                cls._image_embedding_cache.popitem(last=False)

    def _mark_failed(
        self,
        document: Document,
        job: IngestionJob,
        error_code: int,
        error_message: str,
    ) -> None:
        self.session.rollback()
        document.index_status = "failed"
        document.status = "failed"
        job.status = "failed"
        job.current_step = "failed"
        job.error_code = str(error_code)
        job.error_message = error_message
        job.finished_at = datetime.now(UTC)
        self.session.commit()
