"""检索服务模块：提供稠密检索、混合检索、调试检索和 Rerank 后处理功能。

核心流程：
  用户查询 → 查询改写 → 权限过滤 → [稠密/混合]检索 → Rerank(可选) → 返回带引用片段的结果

支持的检索策略：
  - dense：纯语义向量检索
  - hybrid：稠密 + BM25 稀疏混合检索（alpha=0.7 融合）

面试点：
  - Rerank 精排的设计模式和降级策略
  - 混合检索中 Min-Max 归一化 + Alpha 加权融合
  - 权限下推到检索层面的实现方式
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

import logging  # 日志记录：记录检索过程和异常
import time     # 计时：精确记录检索延迟
from uuid import UUID  # UUID 类型：用于 chunk/doc 唯一标识

from sqlalchemy import func, select  # SQL 函数和查询构造
from sqlalchemy.orm import Session   # 数据库会话类型

from app.core.config import get_settings       # 配置单例
from app.core.errors import AppError, ErrorCode  # 统一错误处理
from app.core.metrics import retrieval_duration_seconds  # Prometheus 检索耗时直方图
from app.core.trace import get_trace_id        # 链路追踪 ID
from app.integrations.embedding import EmbeddingProvider  # 文本向量化
from app.integrations.rerank import create_rerank_provider, BaseRerankProvider  # Rerank 精排
from app.integrations.vector_store import VectorStoreClient  # 向量数据库客户端
from app.models.document import Document         # 文档 ORM 模型
from app.services.eligibility import apply_document_eligibility  # 统一可检索条件
from app.models.document_chunk import DocumentChunk  # 文档块 ORM 模型
from app.models.retrieval_log import RetrievalLog   # 检索日志 ORM 模型
from app.permissions.loader import load_permission_checker  # 权限检查器工厂
from app.repositories.retrieval_logs import RetrievalLogRepository  # 检索日志仓库
from app.retrieval.hybrid import HybridRetriever, DenseHit as _DenseHit, SparseHit as _SparseHit  # 混合检索器
from app.retrieval.sparse_index import SparseIndexProvider  # BM25 稀疏索引
from app.schemas.retrieval import (
    Citation,                # 检索引用片段
    DebugSearchResponseData,  # 调试检索响应
    DenseHitDebug,            # 稠密命中调试信息
    SparseHitDebug,           # 稀疏命中调试信息
    RetrievalFilters,         # 检索过滤条件
    SearchRequest,            # 检索请求
    SearchResponseData,       # 检索响应
    UserContext,              # 用户上下文
)
from app.services.sparse_indexing import SparseIndexingService  # 稀疏索引构建服务
from app.services.identity import IdentityService


logger = logging.getLogger(__name__)


class RetrievalService:
    """检索服务核心类：封装稠密/混合检索全流程，含权限过滤、Rerank、日志记录。

    面试点：
      - 为什么要做 top_k * 3 扩召回？答：扩大候选集后通过 Rerank 精排，提高召回率。
      - 为什么 search 方法用 trusted_user_context 参数？答：内部服务间调用时可信，
        无需重复验证用户身份，减少权限检查开销。
    """

    def __init__(self, session: Session) -> None:
        """初始化检索服务，注入所有依赖组件。

        Args:
            session: SQLAlchemy 数据库会话，用于查询文档/块/日志。
        """
        self.session = session                             # 数据库会话
        self.settings = get_settings()                     # 全局配置
        self.embedding_provider = EmbeddingProvider(self.settings)  # 文本向量化提供者
        self.vector_store = VectorStoreClient(self.settings)        # 向量数据库客户端
        self.logs = RetrievalLogRepository(session)        # 检索日志仓库
        self.permission_checker = load_permission_checker()  # 权限检查器（插件化）
        self.sparse_index = SparseIndexProvider()           # BM25 稀疏索引
        self.sparse_indexing = SparseIndexingService()      # 稀疏索引构建服务
        self.hybrid = HybridRetriever(alpha=self.settings.hybrid_fusion_alpha)
        self.rerank_provider: BaseRerankProvider | None = None  # Rerank 提供者（延迟初始化）

    def search(
        self,
        request: SearchRequest,
        *,
        trusted_user_context: bool = False,      # 是否信任传入的用户上下文（内部调用）
        authenticated_identity_required: bool = False,  # 是否强制要求认证身份
    ) -> SearchResponseData:
        """执行检索的核心入口方法。

        流程：
        1. 解析用户上下文，必要时做身份验证
        2. 查询改写（标准化空白字符）
        3. 加载文档列表并做权限过滤
        4. 根据策略分发到稠密或混合检索

        Args:
            request: 检索请求（含查询文本、top_k、策略、过滤条件等）
            trusted_user_context: 是否信任用户上下文（内部调用时为 True）
            authenticated_identity_required: 是否要求用户已认证

        Returns:
            SearchResponseData: 检索结果（含命中列表、延迟、改写查询等）

        Raises:
            AppError: 需要认证但用户未认证时返回 401
        """
        started = time.perf_counter()  # 开始计时（高精度性能计数器）

        # 解析用户上下文（简化版，不再从请求头强制提取）
        user_context = request.user_context

        if trusted_user_context and user_context is not None:
            user_context = IdentityService(self.session).resolve_user_context(user_context)
        elif self.settings.permission_mode != "none" or authenticated_identity_required:
            # 生产权限场景下，请求体中的 roles/departments 不是可信身份来源。
            user_context = None

        # 如果要求认证身份但用户未提供或未认证，返回 401 错误
        if authenticated_identity_required and (not user_context or not user_context.is_authenticated):
            raise AppError(
                code=ErrorCode.AUTHENTICATION_FAILED,
                message="authenticated identity is required",
                status_code=401,
            )

        self._last_user_context = user_context  # 保存最后一次用户上下文（供 debug_search 使用）
        rewritten_query = self._rewrite_query(request.query)  # 查询改写：标准化空白字符
        # 无权限模式且未指定结构化过滤时，不先扫描整张 documents 表。
        # 先让向量/稀疏索引召回，再按命中的 doc_uuid 批量补齐元数据，避免百万级
        # 文档库的每次查询都把全部文档加载进 API 进程。
        documents = (
            self._load_documents(filters=request.filters)
            if self.settings.permission_mode != "none" or request.filters
            else []
        )

        # 权限过滤：根据用户上下文过滤可访问的文档
        allowed_documents = self._resolve_allowed_documents(
            documents=documents,
            user_context=user_context,
        )
        if self.settings.permission_mode == "none" and not request.filters:
            allowed_documents = None

        # 如果用户无权访问任何文档，直接返回空结果
        if allowed_documents is not None and not allowed_documents:
            return self._build_empty_response(request, rewritten_query, started)

        # 根据检索策略分发
        request = self._enforce_production_strategy(request)
        return self._search_hybrid(request, rewritten_query, allowed_documents, started)

    def debug_search(
        self,
        request: SearchRequest,
        *,
        trusted_user_context: bool = False,
        authenticated_identity_required: bool = False,
    ) -> DebugSearchResponseData:
        """调试检索接口：返回检索全过程的详细评分分解和调试信息。

        面试点：调试检索在生产环境中为什么重要？
        答：用于排查"为什么这个文档排在第一"的问题，通过观察 vector_score、
        text_score、sparse_score、rerank_score 各维度得分来分析排序偏差。

        Returns:
            DebugSearchResponseData: 包含评分分解、稠密/稀疏命中、Rerank 信息的调试响应。
        """
        # 先执行普通检索
        result = self.search(
            request,
            trusted_user_context=trusted_user_context,
            authenticated_identity_required=authenticated_identity_required,
        )
        request = self._enforce_production_strategy(request)
        strategy = request.strategy

        # 构建评分分解列表：每个命中文档的完整评分明细
        ranking_debug = [
            {
                "chunk_uuid": citation.chunk_uuid,          # 块 UUID
                "doc_uuid": citation.doc_uuid,              # 文档 UUID
                "score": citation.score,                    # 最终得分
                "vector_score": citation.vector_score,      # 向量语义得分
                "text_score": citation.text_score,          # BM25 关键词得分
                "sparse_score": citation.sparse_score,      # 稀疏检索得分
                "rerank_score": citation.rerank_score,      # Rerank 后得分
                "pre_rerank_score": citation.pre_rerank_score,  # Rerank 前得分
                "section_title": citation.section_title,    # 章节标题
            }
            for citation in result.hits
        ]

        # 获取 Rerank 配置和延迟信息
        rerank_info = self._get_rerank_info()

        # 返回增强的调试响应
        return DebugSearchResponseData(
            **result.model_dump(),  # 继承普通检索响应的所有字段
            raw_filters=self._filters_to_dict(request.filters),  # 原始过滤条件
            user_context=(getattr(self, '_last_user_context', None) or request.user_context or UserContext(user_id='anonymous')).model_dump(),  # 实际使用的用户上下文
            ranking_debug=ranking_debug,     # 评分分解详情
            retrieval_strategy=strategy,     # 检索策略标识
            dense_hits=getattr(self, "_last_dense_hits_debug", []),   # 稠密检索命中详情
            sparse_hits=getattr(self, "_last_sparse_hits_debug", []), # 稀疏检索命中详情
            fusion_alpha=self.settings.hybrid_fusion_alpha if strategy == "hybrid" else None,
            rerank_enabled=rerank_info[0],   # Rerank 是否启用
            rerank_model=rerank_info[1],     # Rerank 模型名称
            rerank_latency_ms=rerank_info[2],  # Rerank 耗时（毫秒）
        )

    def _enforce_production_strategy(self, request: SearchRequest) -> SearchRequest:
        if request.strategy != self.settings.retrieval_strategy:
            logger.info(
                "Overriding requested retrieval strategy %s with production strategy %s",
                request.strategy,
                self.settings.retrieval_strategy,
            )
        return request.model_copy(update={"strategy": self.settings.retrieval_strategy})

    # ── dense search ──────────────────────────────────────────────

    def _search_dense(
        self,
        request: SearchRequest,
        rewritten_query: str,
        allowed_documents: dict[str, Document] | None,
        started: float,
    ) -> SearchResponseData:
        """稠密检索：纯语义向量检索流程。

        面试点：为什么 dense search 也查稀疏索引获取 text_score？
        答：text_score 用于排序调试和混合评分的参考，即使纯稠密模式也附带
        BM25 关键词得分作为辅助信息展示，不影响排序结果。

        流程：
        1. 确保稀疏索引已构建
        2. 对查询文本做向量嵌入
        3. 向量数据库 top_k × RERANK_RECALL_FACTOR 召回（扩召回，因子可配置）
        4. 加载对应 chunk 和文档
        5. 计算 BM25 text_score（仅展示用）
        6. 可选 Rerank 精排
        7. 截取 top_k 结果，构建 Citation 列表
        """
        strategy = request.strategy or "dense"
        # 确保数据库中的 chunk 已同步到稀疏索引
        self.sparse_indexing.ensure_database_indexed(self.session)
        # 对改写后的查询做向量嵌入
        query_embedding = self.embedding_provider.embed_texts([rewritten_query])[0].vector
        # 构建向量搜索的过滤条件（限定为允许的文档 UUID 列表）
        vector_filters = self._build_vector_filters(request.filters, allowed_documents)
        # 向量检索：按 RERANK_RECALL_FACTOR 扩召回，为后续 Rerank 提供候选池
        vector_hits = self.vector_store.search(
            query_embedding=query_embedding,
            top_k=request.top_k * self.settings.rerank_recall_factor,  # 面试点：为什么扩召回？扩大候选集，Rerank 后可选出更优结果
            filters=vector_filters,
        )
        # 从向量命中中提取 chunk_uuid
        chunk_ids = [hit.metadata.get("chunk_uuid") for hit in vector_hits if hit.metadata.get("chunk_uuid")]
        # 批量加载 chunk 对象
        chunks_by_uuid = self._load_chunks_by_uuid(chunk_ids)
        if allowed_documents is None:
            allowed_documents = self._load_documents_by_uuid(
                {str(chunk.doc_uuid) for chunk in chunks_by_uuid.values()}
            )
        # 计算 BM25 text_score（仅用于展示和调试，不参与排序）
        sparse_scores = {
            hit.chunk_uuid: hit.score
            for hit in self._search_sparse(
                rewritten_query,
                top_k=request.top_k * self.settings.rerank_recall_factor,
                filters=vector_filters,
            )
        }
        ranked = []  # 排序后的候选列表
        for hit in vector_hits:
            chunk_uuid = hit.metadata.get("chunk_uuid")
            if not chunk_uuid:
                continue  # 跳过无 chunk_uuid 的命中
            chunk = chunks_by_uuid.get(chunk_uuid)
            if chunk is None:
                continue  # 跳过数据库中不存在的 chunk（可能已被删除）
            document = allowed_documents.get(str(chunk.doc_uuid))
            if document is None:
                continue  # 跳过用户无权访问的文档
            vector_score = max(hit.score, 0.0)  # 确保得分非负
            if vector_score < request.min_score:  # 低于最低得分阈值则跳过
                continue
            ranked.append((vector_score, document, chunk))

        # 按向量得分降序排列
        ranked.sort(key=lambda item: item[0], reverse=True)

        # 应用 Rerank 精排（如果启用）
        rerank_latency = 0
        rerank_scores: dict[str, tuple[float, float]] = {}  # chunk_uuid → (rerank_score, pre_rerank)
        if self._is_rerank_enabled():
            rr_started = time.perf_counter()
            pre_rerank_scores = {str(item[2].chunk_uuid): item[0] for item in ranked}
            ranked = self._apply_rerank_dense(ranked, rewritten_query)
            rerank_latency = int((time.perf_counter() - rr_started) * 1000)
            self._last_rerank_latency = rerank_latency
            for item in ranked:
                chunk_uuid = str(item[2].chunk_uuid)
                rerank_scores[chunk_uuid] = (item[0], pre_rerank_scores[chunk_uuid])

        # 截取 top_k 个最终结果
        top_hits = ranked[: request.top_k]

        # 图片文件扩展名集合（用于判断是否返回图片 URL）
        IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"}

        # 构建 Citation 列表（返回给前端的结果）
        citations = [
            Citation(
                doc_uuid=str(document.doc_uuid),
                chunk_uuid=str(chunk.chunk_uuid),
                title=document.title,
                source_module=document.source_module,
                page_no=chunk.page_no,
                sheet_name=chunk.sheet_name,
                section_title=chunk.section_title,
                snippet=chunk.chunk_text[:240],  # 截取前 240 字符作为片段
                version=document.version,
                updated_at=document.updated_at,
                score=vector_score,       # 最终得分
                vector_score=vector_score,  # 向量得分
                text_score=sparse_scores.get(str(chunk.chunk_uuid)),  # BM25 得分（仅展示）
                rerank_score=rerank_scores.get(str(chunk.chunk_uuid), (None, None))[0],
                pre_rerank_score=rerank_scores.get(str(chunk.chunk_uuid), (None, None))[1],
                image_url=f"/api/v1/documents/{document.doc_uuid}/download" if document.file_ext in IMAGE_EXTENSIONS else None,
            )
            for vector_score, document, chunk in top_hits
        ]

        # 存储调试用的稠密命中信息
        self._last_dense_hits_debug = [
            DenseHitDebug(chunk_uuid=str(chunk.chunk_uuid), doc_uuid=str(document.doc_uuid), score=vector_score)
            for vector_score, _, chunk in ranked[: request.top_k * self.settings.rerank_recall_factor]
        ]
        self._last_sparse_hits_debug = []
        self._last_rerank_latency = rerank_latency

        # 计算总延迟并记录日志
        latency_ms = int((time.perf_counter() - started) * 1000)
        filters_applied = self._filters_to_dict(request.filters)
        self._log_search(
            request=request,
            rewritten_query=rewritten_query,
            hit_count=len(citations),
            filters_applied=filters_applied,
            citations=citations,
            latency_ms=latency_ms,
        )
        # 记录 Prometheus 检索耗时指标
        retrieval_duration_seconds.labels(strategy=strategy).observe(latency_ms / 1000.0)
        return SearchResponseData(
            query=request.query,
            rewritten_query=rewritten_query,
            filters_applied=filters_applied,
            hits=citations,
            latency_ms=latency_ms,
        )

    # ── hybrid search ─────────────────────────────────────────────

    def _search_hybrid(
        self,
        request: SearchRequest,
        rewritten_query: str,
        allowed_documents: dict[str, Document] | None,
        started: float,
    ) -> SearchResponseData:
        """混合检索：稠密 + 稀疏双路召回 + Alpha 加权融合。

        面试点：
        1. 为什么混合检索比纯稠密好？
           答：稠密检索擅长语义匹配但弱于精确关键词（如工号、产品编码），
           BM25 稀疏检索正好弥补这一点，两者融合后命中率更高。
        2. 为什么要分别构建 dense_hits 和 sparse_hitlist？
           答：两路召回结果需要先做权限过滤和有效性验证，再传入 HybridRetriever.fuse() 进行 Min-Max 归一化和融合。

        流程：
        1. 稠密路：向量嵌入 → 向量数据库 top_k × RERANK_RECALL_FACTOR 召回
        2. 稀疏路：BM25 索引 top_k × RERANK_RECALL_FACTOR 召回
        3. 权限过滤两路结果
        4. HybridRetriever.fuse() 融合
        5. 可选 Rerank 精排
        6. 构建 Citation 列表
        """
        self.sparse_indexing.ensure_database_indexed(self.session)
        # ── 稠密路召回 ──
        query_embedding = self.embedding_provider.embed_texts([rewritten_query])[0].vector
        vector_filters = self._build_vector_filters(request.filters, allowed_documents)
        vector_hits = self.vector_store.search(
            query_embedding=query_embedding,
            top_k=request.top_k * self.settings.rerank_recall_factor,  # 按配置因子扩召回
            filters=vector_filters,
        )
        # ── 稀疏路召回 ──
        sparse_hits = self._search_sparse(
            rewritten_query,
            top_k=request.top_k * self.settings.rerank_recall_factor,
            filters=vector_filters,
        )

        # ── 合并两路结果的 chunk_uuid，批量加载 chunk ──
        chunk_ids = [hit.metadata.get("chunk_uuid") for hit in vector_hits if hit.metadata.get("chunk_uuid")]
        sparse_chunk_ids = [h.chunk_uuid for h in sparse_hits]
        all_chunk_ids = list(set(chunk_ids + sparse_chunk_ids))  # 去重合并
        chunks_by_uuid = self._load_chunks_by_uuid(all_chunk_ids)
        if allowed_documents is None:
            allowed_documents = self._load_documents_by_uuid(
                {str(chunk.doc_uuid) for chunk in chunks_by_uuid.values()}
            )

        # ── 构建稠密命中列表（带权限过滤） ──
        dense_hits = []
        for hit in vector_hits:
            chunk_uuid = hit.metadata.get("chunk_uuid")
            if not chunk_uuid:
                continue
            chunk = chunks_by_uuid.get(chunk_uuid)
            if chunk is None:
                continue
            doc = allowed_documents.get(str(chunk.doc_uuid))
            if doc is None:
                continue
            dense_hits.append(_DenseHit(
                chunk_uuid=chunk_uuid,
                doc_uuid=str(chunk.doc_uuid),
                score=max(hit.score, 0.0),  # 确保非负
                metadata={"chunk": chunk, "document": doc},  # 附带完整对象供后续使用
            ))

        # ── 构建稀疏命中列表（带权限过滤） ──
        sparse_hitlist = []
        for h in sparse_hits:
            chunk = chunks_by_uuid.get(h.chunk_uuid)
            if chunk is None:
                continue
            doc = allowed_documents.get(str(chunk.doc_uuid))
            if doc is None:
                continue
            sparse_hitlist.append(_SparseHit(
                chunk_uuid=h.chunk_uuid,
                doc_uuid=str(chunk.doc_uuid),
                score=h.score,
                metadata={"chunk": chunk, "document": doc},
            ))

        # ── Min-Max 归一化 + Alpha 加权融合 ──
        fused = self.hybrid.fuse(dense_hits, sparse_hitlist)

        # ── 应用 min_score 过滤并构建排序列表 ──
        ranked = []
        for h in fused:
            chunk = chunks_by_uuid.get(h.chunk_uuid)
            if chunk is None:
                continue
            doc = allowed_documents.get(str(chunk.doc_uuid))
            if doc is None:
                continue
            if h.fusion_score < request.min_score:  # 融合得分低于阈值则跳过
                continue
            # 存储 (融合得分, 稠密得分, 稀疏得分, 文档, 块)
            ranked.append((h.fusion_score, h.dense_score or 0.0, h.sparse_score or 0.0, doc, chunk))

        # 按融合得分 > 稠密得分 > 稀疏得分 降序排列
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)

        # ── 应用 Rerank 精排（如果启用） ──
        rerank_latency = 0
        rerank_scores: dict[str, tuple[float, float]] = {}
        if self._is_rerank_enabled():
            rr_started = time.perf_counter()
            pre_rerank_scores = {str(item[4].chunk_uuid): item[0] for item in ranked}
            ranked = self._apply_rerank_hybrid(ranked, rewritten_query)
            rerank_latency = int((time.perf_counter() - rr_started) * 1000)
            self._last_rerank_latency = rerank_latency
            for score, _, _, _, chunk in ranked:
                rerank_scores[str(chunk.chunk_uuid)] = (score, pre_rerank_scores[str(chunk.chunk_uuid)])

        # 截取 top_k
        top_hits = ranked[: request.top_k]

        # 图片文件扩展名
        IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"}

        # 构建 Citation 列表
        citations = [
            Citation(
                doc_uuid=str(document.doc_uuid),
                chunk_uuid=str(chunk.chunk_uuid),
                title=document.title,
                source_module=document.source_module,
                page_no=chunk.page_no,
                sheet_name=chunk.sheet_name,
                section_title=chunk.section_title,
                snippet=chunk.chunk_text[:240],
                version=document.version,
                updated_at=document.updated_at,
                score=final_score,       # 融合得分
                vector_score=dense_score,  # 稠密路得分
                text_score=sparse_score,
                sparse_score=sparse_score,  # 稀疏路得分
                rerank_score=rerank_scores.get(str(chunk.chunk_uuid), (None, None))[0],
                pre_rerank_score=rerank_scores.get(str(chunk.chunk_uuid), (None, None))[1],
                image_url=f"/api/v1/documents/{document.doc_uuid}/download" if document.file_ext in IMAGE_EXTENSIONS else None,
            )
            for final_score, dense_score, sparse_score, document, chunk in top_hits
        ]

        # 存储调试命中信息
        self._last_dense_hits_debug = [
            DenseHitDebug(chunk_uuid=h.chunk_uuid, doc_uuid=h.doc_uuid, score=h.score)
            for h in dense_hits[:20]  # 最多存 20 条
        ]
        self._last_sparse_hits_debug = [
            SparseHitDebug(chunk_uuid=h.chunk_uuid, doc_uuid=h.doc_uuid, score=h.score)
            for h in sparse_hitlist[:20]  # 最多存 20 条
        ]
        self._last_rerank_latency = rerank_latency

        # 计算总延迟、记录日志和指标
        latency_ms = int((time.perf_counter() - started) * 1000)
        filters_applied = self._filters_to_dict(request.filters)
        self._log_search(
            request=request,
            rewritten_query=rewritten_query,
            hit_count=len(citations),
            filters_applied=filters_applied,
            citations=citations,
            latency_ms=latency_ms,
        )
        retrieval_duration_seconds.labels(strategy="hybrid").observe(latency_ms / 1000.0)
        return SearchResponseData(
            query=request.query,
            rewritten_query=rewritten_query,
            filters_applied=filters_applied,
            hits=citations,
            latency_ms=latency_ms,
        )

    def _search_sparse(
        self,
        query: str,
        *,
        top_k: int,
        filters: dict | None = None,
    ) -> list[_SparseHit]:
        """生产环境从 Milvus 的 sparse_vector 召回；本地和评测保留内存 BM25。"""
        if not self.vector_store.supports_native_sparse_search:
            return self.sparse_index.search(query, top_k=top_k)
        return [
            _SparseHit(
                chunk_uuid=str(hit.metadata.get("chunk_uuid") or hit.vector_id),
                doc_uuid=str(hit.metadata.get("doc_uuid", "")),
                score=hit.score,
                metadata=hit.metadata,
            )
            for hit in self.vector_store.search_sparse(
                query_text=query,
                top_k=top_k,
                filters=filters,
            )
        ]

    # ── rerank ────────────────────────────────────────────────────

    def _is_rerank_enabled(self) -> bool:
        """检查 Rerank 是否已启用。

        需要同时满足三个条件：
        - 配置中 rerank_enabled 为 True
        - rerank_api_base 不为空（有 API 地址）
        - rerank_api_key 不为空（有认证密钥）
        """
        return bool(
            self.settings.rerank_enabled
            and self.settings.rerank_api_base
            and self.settings.rerank_api_key
        )

    def _get_rerank_provider(self) -> BaseRerankProvider:
        """获取 Rerank 提供者实例（延迟初始化，首次调用时创建）。"""
        if self.rerank_provider is None:
            self.rerank_provider = create_rerank_provider(self.settings)
        return self.rerank_provider

    def _get_rerank_info(self) -> tuple[bool, str | None, int | None]:
        """获取 Rerank 配置信息：是否启用、模型名、延迟。

        Returns:
            (enabled, model_name, latency_ms)
        """
        enabled = self._is_rerank_enabled()
        model = self.settings.rerank_model if enabled else None
        latency = getattr(self, "_last_rerank_latency", None)
        return (enabled, model, latency)

    def _apply_rerank_dense(
        self,
        ranked: list[tuple[float, Document, DocumentChunk]],
        rewritten_query: str,
    ) -> list[tuple[float, Document, DocumentChunk]]:
        """对稠密检索结果应用 Rerank 精排，失败时降级返回原始排序。

        面试点：Rerank 失败时的降级策略是什么？
        答：捕获所有异常，记录日志后返回原始排序结果，保证服务可用性。
        这是典型的"优雅降级"设计模式。

        Args:
            ranked: 按向量得分排序的 (score, Document, DocumentChunk) 列表
            rewritten_query: 改写后的查询文本

        Returns:
            Rerank 后的排序列表，或原始列表（失败时）
        """
        try:
            provider = self._get_rerank_provider()
            # 提取每个 chunk 的前 800 字符作为 Rerank 输入
            documents = [item[2].chunk_text[:800] for item in ranked]
            results = provider.rerank(rewritten_query, documents, top_n=len(ranked))
            reranked = []
            for r in results:
                if r.index < len(ranked):
                    original = ranked[r.index]
                    # 用 Rerank 得分替换原始得分，保留文档和 chunk 引用
                    reranked.append((r.score, original[1], original[2]))
            if not reranked:
                return ranked  # Rerank 结果为空则返回原始排序
            return reranked
        except Exception:
            logger.exception("Rerank failed, using original scores")  # 优雅降级
            return ranked

    def _apply_rerank_hybrid(
        self,
        ranked: list[tuple[float, float, float, Document, DocumentChunk]],
        rewritten_query: str,
    ) -> list[tuple[float, float, float, Document, DocumentChunk]]:
        """对混合检索结果应用 Rerank 精排，失败时降级返回原始排序。

        Args:
            ranked: 按融合得分排序的 (fusion, dense, sparse, Document, Chunk) 列表
            rewritten_query: 改写后的查询文本

        Returns:
            Rerank 后的排序列表，或原始列表（失败时）
        """
        try:
            provider = self._get_rerank_provider()
            # 提取 chunk_text 前 800 字符（item[4] 是 DocumentChunk）
            documents = [item[4].chunk_text[:800] for item in ranked]
            results = provider.rerank(rewritten_query, documents, top_n=len(ranked))
            reranked = []
            for r in results:
                if r.index < len(ranked):
                    original = ranked[r.index]
                    # 保留稠密和稀疏得分，仅替换融合得分
                    reranked.append((r.score, original[1], original[2], original[3], original[4]))
            if not reranked:
                return ranked
            return reranked
        except Exception:
            logger.exception("Rerank failed, using original scores")  # 优雅降级
            return ranked

    # ── shared helpers ────────────────────────────────────────────

    def _build_empty_response(self, request: SearchRequest, rewritten_query: str, started: float) -> SearchResponseData:
        """构建空响应：当用户无权访问任何文档时返回。

        面试点：为什么无权限时返回空结果而不是 403？
        答：避免暴露系统中存在哪些文档（信息泄露风险）。返回空结果既保护了安全性，
        又保持了接口的一致性。
        """
        latency_ms = int((time.perf_counter() - started) * 1000)
        filters_applied = self._filters_to_dict(request.filters)
        self._log_search(
            request=request,
            rewritten_query=rewritten_query,
            hit_count=0,
            filters_applied=filters_applied,
            citations=[],
            latency_ms=latency_ms,
        )
        self._last_dense_hits_debug = []
        self._last_sparse_hits_debug = []
        return SearchResponseData(
            query=request.query,
            rewritten_query=rewritten_query,
            filters_applied=filters_applied,
            hits=[],
            latency_ms=latency_ms,
        )

    def _load_documents(self, filters: RetrievalFilters | None) -> list[Document]:
        """加载所有未删除的文档，并应用过滤条件。

        面试点：为什么对 source_module/source_type/file_ext 做大小写不敏感匹配？
        答：企业中模块名可能大小写不一致（如 "HR" vs "hr"），
        大小写不敏感匹配提高了用户体验和检索容错性。

        Args:
            filters: 检索过滤条件（source_module、source_type、file_ext、date 范围等）

        Returns:
            符合条件的 Document 列表
        """
        # 基础查询：应用统一可检索条件
        stmt = apply_document_eligibility(select(Document))
        if filters is not None:
            # 按 source_module 过滤（大小写不敏感）
            if filters.source_module:
                normalized_modules = [item.strip().lower() for item in filters.source_module if item and item.strip()]
                if normalized_modules:
                    stmt = stmt.where(func.lower(Document.source_module).in_(normalized_modules))
            # 按 source_type 过滤（大小写不敏感）
            if filters.source_type:
                normalized_types = [item.strip().lower() for item in filters.source_type if item and item.strip()]
                if normalized_types:
                    stmt = stmt.where(func.lower(Document.source_type).in_(normalized_types))
            # 按文件扩展名过滤（大小写不敏感）
            if filters.file_ext:
                normalized_exts = [item.strip().lower() for item in filters.file_ext if item and item.strip()]
                if normalized_exts:
                    stmt = stmt.where(func.lower(Document.file_ext).in_(normalized_exts))
            # 按访问级别过滤，适合校园公共/校内/院系/角色资料的分层检索。
            if filters.access_level:
                normalized_levels = [item.strip().lower() for item in filters.access_level if item and item.strip()]
                if normalized_levels:
                    stmt = stmt.where(func.lower(Document.access_level).in_(normalized_levels))
            # 按资料归属学院/部门过滤，避免跨院系检索无关资料。
            if filters.owner_dept:
                normalized_departments = [item.strip().lower() for item in filters.owner_dept if item and item.strip()]
                if normalized_departments:
                    stmt = stmt.where(func.lower(Document.owner_dept).in_(normalized_departments))
            # 按创建日期范围过滤
            if filters.date_from:
                stmt = stmt.where(Document.created_at >= filters.date_from)
            if filters.date_to:
                stmt = stmt.where(Document.created_at <= filters.date_to)
        return list(self.session.scalars(stmt))

    def _load_chunks_by_uuid(self, chunk_uuids: list[str]) -> dict[str, DocumentChunk]:
        """根据 UUID 列表批量加载 DocumentChunk。

        安全处理：
        - 空列表直接返回空字典
        - 无效 UUID 格式会被记录警告并跳过（向量数据库中可能存在脏数据）

        Returns:
            {chunk_uuid_str: DocumentChunk} 映射
        """
        if not chunk_uuids:
            return {}
        normalized = []
        for chunk_uuid in chunk_uuids:
            if not chunk_uuid:
                continue
            try:
                normalized.append(UUID(str(chunk_uuid)))  # 字符串转 UUID
            except ValueError:
                logger.warning("Skipping invalid chunk_uuid from retrieval index: %s", chunk_uuid)
        if not normalized:
            return {}
        stmt = select(DocumentChunk).where(DocumentChunk.chunk_uuid.in_(normalized))
        chunks = list(self.session.scalars(stmt))
        return {str(chunk.chunk_uuid): chunk for chunk in chunks}

    def _load_documents_by_uuid(self, doc_uuids: set[str]) -> dict[str, Document]:
        """只加载召回结果涉及的文档，避免无过滤检索扫描全量文档。"""
        normalized: list[UUID] = []
        for doc_uuid in doc_uuids:
            try:
                normalized.append(UUID(str(doc_uuid)))
            except ValueError:
                logger.warning("Skipping invalid doc_uuid from retrieval index: %s", doc_uuid)
        if not normalized:
            return {}
        stmt = apply_document_eligibility(
            select(Document).where(Document.doc_uuid.in_(normalized))
        )
        return {str(document.doc_uuid): document for document in self.session.scalars(stmt)}

    def _resolve_allowed_documents(
        self,
        *,
        documents: list[Document],
        user_context: UserContext | None,
    ) -> dict[str, Document]:
        """使用权限检查器过滤文档：将用户无权访问的文档剔除。

        面试点：权限下推到检索层的设计好处？
        答：
        1. 避免在应用层做二次过滤，减少数据传输和处理开销
        2. 在向量搜索前就限定 doc_uuid 范围，减少无效计算
        3. 与"返回空结果而非 403"配合，防止信息泄露
        """
        # 如果权限模式为 "none"（无权限检查），直接返回所有文档
        if self.settings.permission_mode == "none":
            return {str(doc.doc_uuid): doc for doc in documents}

        # 将 Document 对象转换为字典（权限检查器需要字典格式）
        docs_dict = [self._document_to_dict(doc) for doc in documents]
        user_context_dict = user_context.model_dump() if user_context else None

        # 调用权限检查器过滤
        allowed_docs_dict = self.permission_checker.filter_documents(docs_dict, user_context_dict)

        # 将过滤后的字典转回 Document 对象（按 doc_uuid 匹配）
        allowed_uuids = {d["doc_uuid"] for d in allowed_docs_dict}
        allowed_documents = {}
        for document in documents:
            doc_uuid = str(document.doc_uuid)
            if doc_uuid in allowed_uuids:
                allowed_documents[doc_uuid] = document

        return allowed_documents

    @staticmethod
    def _document_to_dict(document: Document) -> dict:
        """将 Document ORM 对象转换为字典（供权限检查器使用）。

        面试点：为什么不直接用 model_dump()？
        答：手动选择字段可以精确控制传给权限检查器的数据，避免泄露敏感信息，
        同时减少序列化开销。
        """
        return {
            "doc_uuid": str(document.doc_uuid),
            "source_module": document.source_module,
            "source_type": document.source_type,
            "access_level": document.access_level,
            "tags": document.tags,
            "owner_dept": document.owner_dept,
            "owner_role": document.owner_role,
            "created_by": document.created_by,
            "title": document.title,
            "file_ext": document.file_ext,
        }

    @staticmethod
    def _rewrite_query(query: str) -> str:
        """查询改写：将多空白字符规范化为单个空格。

        面试点：为什么需要查询改写？
        答：用户输入可能包含多余空格、换行、制表符等，这些噪声会影响
        Embedding 质量和 BM25 分词结果。规范化后提高检索一致性。
        """
        return " ".join(query.strip().split())

    @staticmethod
    def _filters_to_dict(filters: RetrievalFilters | None) -> dict:
        """将 RetrievalFilters 对象序列化为字典（用于日志和调试）。"""
        return filters.model_dump(mode="json", exclude_none=True) if filters else {}

    def _build_vector_filters(
        self,
        filters: RetrievalFilters | None,
        allowed_documents: dict[str, Document] | None,
    ) -> dict:
        """构建向量搜索的过滤条件：限定为权限允许的文档 UUID 列表。

        面试点：为什么这里把 filters 参数传入了但不使用？
        答：这是预留设计——结构化过滤（source_module 等）已在 _load_documents
        层面完成，向量搜索层面只需传 doc_uuid 白名单即可。保留参数是为
        未来可能在向量数据库层面直接做结构化过滤做准备。
        """
        if allowed_documents is None:
            return {}
        return {"doc_uuid": list(allowed_documents.keys())}

    def _log_search(
        self,
        *,
        request: SearchRequest,
        rewritten_query: str,
        hit_count: int,
        filters_applied: dict,
        citations: list[Citation],
        latency_ms: int,
    ) -> None:
        """记录检索日志到数据库。

        面试点：为什么检索日志要持久化？
        答：
        1. 分析用户搜索行为，优化检索策略
        2. 排查检索质量问题（如为什么某个查询返回空结果）
        3. 作为数据飞轮的基础：用户点击反馈可用于优化排序
        """
        trace_id = get_trace_id()  # 获取链路追踪 ID
        self.logs.add(
            RetrievalLog(
                trace_id=trace_id,
                query_text=request.query,           # 原始查询
                rewritten_query=rewritten_query,    # 改写后查询
                query_intent="search",              # 查询意图
                filters_json=filters_applied,       # 应用的过滤条件
                user_context_json=request.user_context.model_dump() if request.user_context else {},  # 用户上下文
                top_k=request.top_k,                # 请求的 top_k
                hit_count=hit_count,                # 实际命中数
                retrieval_latency_ms=latency_ms,    # 检索延迟
                total_latency_ms=latency_ms,        # 总延迟
                matched_documents_json=[
                    {
                        "doc_uuid": citation.doc_uuid,
                        "chunk_uuid": citation.chunk_uuid,
                        "score": citation.score,
                        "vector_score": citation.vector_score,
                        "text_score": citation.text_score,
                    }
                    for citation in citations
                ],
                response_excerpt=citations[0].snippet if citations else None,  # 第一条片段
            )
        )
        self.session.commit()
