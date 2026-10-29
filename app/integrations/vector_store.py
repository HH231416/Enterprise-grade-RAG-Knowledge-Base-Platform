"""向量存储客户端：封装本地存储和远程 Milvus/Zilliz 两种向量数据库后端。

支持操作：upsert（插入/更新）、search（相似度搜索）、delete（删除）、drop_collection（删除集合）。

Provider 策略：
- local：进程内内存存储（开发/测试）
- milvus：自建 Milvus 向量数据库
- zilliz：Zilliz Cloud 托管向量数据库（兼容 Milvus API）
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

import logging  # 日志记录
import time  # 性能计时（perf_counter）
from dataclasses import dataclass  # 数据类
from math import sqrt  # 平方根：余弦相似度计算
from uuid import uuid4  # UUID 生成：本地模式向量 ID

from app.core.config import Settings  # 全局配置
from app.core.errors import AppError, ErrorCode  # 自定义异常
from app.core.metrics import (
    vector_store_operation_duration_seconds,  # 向量操作耗时直方图
    vector_store_operations_total,            # 向量操作次数计数器
)


logger = logging.getLogger(__name__)  # 模块级日志记录器


@dataclass(slots=True)
class VectorRecord:
    vector_id: str
    metadata: dict
    collection: str
    embedding: list[float] | None = None


@dataclass(slots=True)
class SearchResult:
    vector_id: str
    score: float
    metadata: dict
    collection: str


class VectorStoreClient:
    _local_store: dict[str, list[VectorRecord]] = {}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def upsert_embeddings(
        self,
        *,
        embeddings: list[list[float]],
        metadatas: list[dict],
        collection_name: str | None = None,
    ) -> list[VectorRecord]:
        if self._provider() in ("milvus", "zilliz"):
            return self._upsert_milvus(
                embeddings=embeddings,
                metadatas=metadatas,
                collection_name=collection_name,
            )
        return self._upsert_local(
            embeddings=embeddings,
            metadatas=metadatas,
            collection_name=collection_name,
        )

    def search(
        self,
        *,
        query_embedding: list[float],
        top_k: int,
        filters: dict | None = None,
        collection_name: str | None = None,
    ) -> list[SearchResult]:
        if self._provider() in ("milvus", "zilliz"):
            return self._search_milvus(
                query_embedding=query_embedding,
                top_k=top_k,
                filters=filters,
                collection_name=collection_name,
            )
        return self._search_local(
            query_embedding=query_embedding,
            top_k=top_k,
            filters=filters,
            collection_name=collection_name,
        )

    @property
    def supports_native_sparse_search(self) -> bool:
        """远程 Milvus/Zilliz 使用同一 Collection 内的 BM25 稀疏向量。"""
        return self._provider() in ("milvus", "zilliz")

    def search_sparse(
        self,
        *,
        query_text: str,
        top_k: int,
        filters: dict | None = None,
        collection_name: str | None = None,
    ) -> list[SearchResult]:
        """使用 Milvus 内置 BM25 Function 生成的稀疏向量召回候选。"""
        if not query_text.strip() or not self.supports_native_sparse_search:
            return []
        return self._search_milvus_sparse(
            query_text=query_text,
            top_k=top_k,
            filters=filters,
            collection_name=collection_name,
        )

    def delete_embeddings(
        self,
        *,
        chunk_ids: list[str],
        collection_name: str | None = None,
    ) -> int:
        if not chunk_ids:
            return 0
        if self._provider() in ("milvus", "zilliz"):
            return self._delete_milvus(chunk_ids=chunk_ids, collection_name=collection_name)
        return self._delete_local(chunk_ids=chunk_ids, collection_name=collection_name)

    def drop_collection(self, *, collection_name: str | None = None) -> bool:
        if self._provider() in ("milvus", "zilliz"):
            return self._drop_milvus(collection_name=collection_name)
        return self._drop_local(collection_name=collection_name)

    def probe(self) -> bool:
        provider = self._provider()
        if provider == "local":
            return True

        uri, token, collection = self._get_milvus_config()
        if not uri:
            return False

        try:
            from pymilvus import MilvusClient
        except ImportError:
            return False

        try:
            client = MilvusClient(uri=uri, token=token)
            client.has_collection(collection_name=collection)
            return True
        except Exception:
            logger.exception("%s probe failed", provider.capitalize())
            return False

    def _upsert_local(
        self,
        *,
        embeddings: list[list[float]],
        metadatas: list[dict],
        collection_name: str | None,
    ) -> list[VectorRecord]:
        collection = self._resolve_collection_name(collection_name)
        records: list[VectorRecord] = []
        for metadata, embedding in zip(metadatas, embeddings, strict=True):
            record = VectorRecord(
                vector_id=f"vec_{uuid4().hex}",
                metadata=metadata,
                collection=collection,
                embedding=embedding,
            )
            records.append(record)

        self._local_store.setdefault(collection, []).extend(records)
        logger.info(
            "Upserted %s vector records into local collection %s",
            len(records),
            collection,
        )
        return records

    def _search_local(
        self,
        *,
        query_embedding: list[float],
        top_k: int,
        filters: dict | None,
        collection_name: str | None,
    ) -> list[SearchResult]:
        collection = self._resolve_collection_name(collection_name)
        records = self._local_store.get(collection, [])
        matches: list[SearchResult] = []
        for record in records:
            if not record.embedding:
                continue
            if filters and not self._metadata_matches(record.metadata, filters):
                continue
            matches.append(
                SearchResult(
                    vector_id=record.vector_id,
                    score=self._cosine_similarity(query_embedding, record.embedding),
                    metadata=record.metadata,
                    collection=record.collection,
                )
            )

        matches.sort(key=lambda item: item.score, reverse=True)
        return matches[:top_k]

    def _delete_local(self, *, chunk_ids: list[str], collection_name: str | None) -> int:
        collection = self._resolve_collection_name(collection_name)
        records = self._local_store.get(collection, [])
        chunk_id_set = set(chunk_ids)
        before_count = len(records)
        self._local_store[collection] = [
            record
            for record in records
            if str(record.metadata.get("chunk_uuid")) not in chunk_id_set
        ]
        return before_count - len(self._local_store[collection])

    def _drop_local(self, *, collection_name: str | None) -> bool:
        collection = self._resolve_collection_name(collection_name)
        return self._local_store.pop(collection, None) is not None

    def _upsert_milvus(
        self,
        *,
        embeddings: list[list[float]],
        metadatas: list[dict],
        collection_name: str | None,
    ) -> list[VectorRecord]:
        t0 = time.perf_counter()
        provider = self._provider()
        uri, token, collection = self._get_milvus_config(collection_name=collection_name)
        if not uri:
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message=f"{provider} provider uri is not configured",
                status_code=422,
            )

        try:
            from pymilvus import DataType, MilvusClient
        except ImportError as exc:
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message="pymilvus is required for milvus or zilliz provider",
                status_code=500,
            ) from exc

        client = MilvusClient(uri=uri, token=token)
        self._ensure_collection(client=client, data_type_cls=DataType, collection=collection)

        rows = []
        for metadata, embedding in zip(metadatas, embeddings, strict=True):
            rows.append(
                {
                    "chunk_uuid": metadata["chunk_uuid"],
                    "chunk_text": str(metadata.get("chunk_text", "")),
                    "vector": embedding,
                    **metadata,
                }
            )

        client.upsert(collection_name=collection, data=rows)
        vector_store_operations_total.labels(provider=provider, operation="upsert").inc()
        vector_store_operation_duration_seconds.labels(provider=provider, operation="upsert").observe(
            time.perf_counter() - t0
        )
        return [
            VectorRecord(
                vector_id=str(metadata["chunk_uuid"]),
                metadata=metadata,
                collection=collection,
                embedding=embedding,
            )
            for metadata, embedding in zip(metadatas, embeddings, strict=True)
        ]

    def _delete_milvus(self, *, chunk_ids: list[str], collection_name: str | None) -> int:
        uri, token, collection = self._get_milvus_config(collection_name=collection_name)
        if not uri:
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message=f"{self._provider()} provider is not configured",
                status_code=422,
            )

        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message="pymilvus is required for milvus or zilliz provider",
                status_code=500,
            ) from exc

        try:
            client = MilvusClient(uri=uri, token=token)
            delete_result = client.delete(collection_name=collection, ids=chunk_ids)
            return int(delete_result.get("delete_count", 0))
        except Exception as exc:
            logger.warning("Milvus delete failed for collection %s: %s", collection, exc)
            return 0

    def _drop_milvus(self, *, collection_name: str | None) -> bool:
        uri, token, collection = self._get_milvus_config(collection_name=collection_name)
        if not uri:
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message=f"{self._provider()} provider is not configured",
                status_code=422,
            )

        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message="pymilvus is required for milvus or zilliz provider",
                status_code=500,
            ) from exc

        try:
            client = MilvusClient(uri=uri, token=token)
            if not client.has_collection(collection_name=collection):
                return False
            client.drop_collection(collection_name=collection)
            return True
        except Exception as exc:
            logger.warning("Milvus drop collection failed for %s: %s", collection, exc)
            return False

    def _search_milvus(
        self,
        *,
        query_embedding: list[float],
        top_k: int,
        filters: dict | None,
        collection_name: str | None,
    ) -> list[SearchResult]:
        t0 = time.perf_counter()
        provider = self._provider()
        uri, token, collection = self._get_milvus_config(collection_name=collection_name)
        if not uri:
            raise AppError(
                code=ErrorCode.RETRIEVAL_FAILED,
                message=f"{provider} provider is not configured",
                status_code=422,
            )

        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise AppError(
                code=ErrorCode.RETRIEVAL_FAILED,
                message="pymilvus is required for milvus or zilliz provider",
                status_code=500,
            ) from exc

        try:
            client = MilvusClient(uri=uri, token=token)
            search_response = client.search(
                collection_name=collection,
                anns_field="vector",
                data=[query_embedding],
                limit=top_k,
                filter=self._build_zilliz_filter_expression(filters),
                output_fields=self._search_output_fields(),
                search_params={"metric_type": "COSINE"},
            )
            first_result_set = search_response[0] if search_response else []
            vector_store_operations_total.labels(provider=provider, operation="search").inc()
            vector_store_operation_duration_seconds.labels(provider=provider, operation="search").observe(
                time.perf_counter() - t0
            )
            return [
                SearchResult(
                    vector_id=str(hit.get("id") or hit.get("chunk_uuid")),
                    score=float(hit.get("distance", 0.0)),
                    metadata=self._extract_search_metadata(hit),
                    collection=collection,
                )
                for hit in first_result_set
            ]
        except Exception as exc:
            logger.warning("Milvus search failed for collection %s: %s", collection, exc)
            return []

    def _search_milvus_sparse(
        self,
        *,
        query_text: str,
        top_k: int,
        filters: dict | None,
        collection_name: str | None,
    ) -> list[SearchResult]:
        t0 = time.perf_counter()
        provider = self._provider()
        uri, token, collection = self._get_milvus_config(collection_name=collection_name)
        if not uri:
            raise AppError(
                code=ErrorCode.RETRIEVAL_FAILED,
                message=f"{provider} provider is not configured",
                status_code=422,
            )

        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise AppError(
                code=ErrorCode.RETRIEVAL_FAILED,
                message="pymilvus is required for milvus or zilliz provider",
                status_code=500,
            ) from exc

        try:
            client = MilvusClient(uri=uri, token=token)
            search_response = client.search(
                collection_name=collection,
                anns_field="sparse_vector",
                data=[query_text],
                limit=top_k,
                filter=self._build_zilliz_filter_expression(filters),
                output_fields=self._search_output_fields(),
                search_params={"metric_type": "BM25"},
            )
            first_result_set = search_response[0] if search_response else []
            vector_store_operations_total.labels(provider=provider, operation="sparse_search").inc()
            vector_store_operation_duration_seconds.labels(
                provider=provider, operation="sparse_search"
            ).observe(time.perf_counter() - t0)
            return [
                SearchResult(
                    vector_id=str(hit.get("id") or hit.get("chunk_uuid")),
                    score=float(hit.get("distance", 0.0)),
                    metadata=self._extract_search_metadata(hit),
                    collection=collection,
                )
                for hit in first_result_set
            ]
        except Exception as exc:
            logger.warning("Milvus sparse search failed for collection %s: %s", collection, exc)
            return []

    def _provider(self) -> str:
        provider = self.settings.vector_store_provider.lower()
        return provider if provider in ("milvus", "zilliz") else "local"

    def _get_milvus_config(self, collection_name: str | None = None) -> tuple[str | None, str, str]:
        if self._provider() == "milvus":
            return (
                self.settings.milvus_uri,
                self.settings.milvus_token or "",
                self._resolve_collection_name(collection_name, provider="milvus"),
            )
        return (
            self.settings.zilliz_uri,
            self.settings.zilliz_token or "",
            self._resolve_collection_name(collection_name, provider="zilliz"),
        )

    def _resolve_collection_name(self, collection_name: str | None, provider: str | None = None) -> str:
        """解析并返回最终要使用的向量数据库集合（Collection）的名称。

        此方法解决了在多种向量数据库配置下，动态选择正确集合名称的问题。
        其决策逻辑如下：
        1. 优先使用调用时直接传入的 `collection_name`。
        2. 若未传入，则根据向量数据库的 `provider`（"milvus" 或 "zilliz"）
           从系统配置中获取默认的集合名称。

        Args:
            collection_name: 外部指定的集合名称，具有最高优先级。
            provider: 外部指定的向量数据库提供商，用于辅助决定默认集合。

        Returns:
            最终确定的集合名称字符串。
        """
        # 如果显式提供了集合名称，则直接返回，这是最高优先级。
        if collection_name:
            return collection_name

        # 确定有效的提供商：优先使用传入的 provider，否则回退到实例的默认 provider。
        effective_provider = provider or self._provider()

        # 根据提供商返回相应的默认集合名称。
        if effective_provider == "milvus":
            return self.settings.milvus_collection
        return self.settings.zilliz_collection

    def _ensure_collection(self, *, client, data_type_cls, collection: str) -> None:
        if client.has_collection(collection_name=collection):
            return

        if self._provider() == "zilliz":
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message=(
                    "Create the Zilliz collection with the BM25 sparse-vector schema before use; "
                    "automatic collection creation is only supported for self-hosted Milvus."
                ),
                status_code=422,
            )

        schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field(
            field_name="chunk_uuid",
            datatype=data_type_cls.VARCHAR,
            is_primary=True,
            max_length=64,
        )
        schema.add_field(
            field_name="vector",
            datatype=data_type_cls.FLOAT_VECTOR,
            dim=self.settings.embedding_vector_size,
        )
        # Milvus 在写入时根据 chunk_text 自动生成 BM25 稀疏向量；文本、
        # 稠密向量、稀疏向量及动态标量元数据因此共处一个 Collection。
        # BM25 全文检索要求输入字段 enable_analyzer=True（缺失会返回空结果），
        # 并用 jieba 中文分析器分词：与本地/评测 BM25 的 jieba 分词保持一致。
        schema.add_field(
            field_name="chunk_text",
            datatype=data_type_cls.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"tokenizer": "jieba", "filter": ["lowercase"]},
        )
        schema.add_field(
            field_name="sparse_vector",
            datatype=data_type_cls.SPARSE_FLOAT_VECTOR,
        )
        try:
            from pymilvus import Function, FunctionType
        except ImportError as exc:
            raise AppError(
                code=ErrorCode.VECTOR_WRITE_FAILED,
                message="pymilvus with BM25 Function support is required for Milvus hybrid retrieval",
                status_code=500,
            ) from exc
        schema.add_function(
            Function(
                name="chunk_text_bm25",
                input_field_names=["chunk_text"],
                output_field_names=["sparse_vector"],
                function_type=FunctionType.BM25,
            )
        )
        index_params = client.prepare_index_params()
        index_type = "IVF_FLAT"
        index_params.add_index(
            field_name="vector",
            index_type=index_type,
            metric_type="COSINE",
            params={"nlist": 1024} if index_type == "IVF_FLAT" else {},
        )
        index_params.add_index(
            field_name="sparse_vector",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
        client.create_collection(
            collection_name=collection,
            schema=schema,
            index_params=index_params,
        )

    @staticmethod
    def _search_output_fields() -> list[str]:
        return [
            "chunk_uuid",
            "doc_uuid",
            "run_uuid",
            "chunk_index",
            "title",
            "source_module",
            "source_type",
            "file_ext",
            "version",
            "page_no",
            "sheet_name",
            "section_title",
            "access_level",
            "owner_dept",
            "owner_role",
            "updated_at",
        ]

    @staticmethod
    def _metadata_matches(metadata: dict, filters: dict) -> bool:
        for key, value in filters.items():
            if value is None:
                continue
            candidate = metadata.get(key)
            if isinstance(value, list):
                if candidate not in value:
                    return False
            elif candidate != value:
                return False
        return True

    @staticmethod
    def _build_zilliz_filter_expression(filters: dict | None) -> str:
        if not filters:
            return ""

        expressions: list[str] = []
        for key, value in filters.items():
            if value is None:
                continue
            if isinstance(value, list):
                normalized_values = [item for item in value if item is not None]
                if not normalized_values:
                    continue
                rendered_items = ", ".join(
                    VectorStoreClient._render_zilliz_literal(item)
                    for item in normalized_values
                )
                expressions.append(f"{key} in [{rendered_items}]")
            else:
                expressions.append(f"{key} == {VectorStoreClient._render_zilliz_literal(value)}")
        return " and ".join(expressions)

    @staticmethod
    def _render_zilliz_literal(value) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _extract_search_metadata(hit: dict) -> dict:
        entity = hit.get("entity")
        metadata: dict = {}
        if isinstance(entity, dict):
            metadata.update(entity)

        for key, value in hit.items():
            if key in {"entity", "distance", "id", "vector"}:
                continue
            metadata.setdefault(key, value)

        metadata.pop("distance", None)
        metadata.pop("vector", None)
        if metadata.get("chunk_uuid") is None and hit.get("id") is not None:
            metadata["chunk_uuid"] = str(hit["id"])
        return metadata

    @staticmethod
    def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        dot_product = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
        norm_a = sqrt(sum(a * a for a in vec_a))
        norm_b = sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot_product / (norm_a * norm_b)
