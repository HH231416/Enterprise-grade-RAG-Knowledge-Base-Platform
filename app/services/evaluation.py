"""Evaluation service with isolated chunk storage and temporary vector collections.

Key guarantees:
- Writes chunks to evaluation_chunks (never touches document_chunks)
- Temporary vector collection: eval_<run_uuid>
- Serial queue via optimistic locking (status check)
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

import logging  # 日志：记录评测过程和异常
import time     # 计时：精确记录查询延迟
from datetime import datetime, timezone  # 时间处理：记录开始/结束时间
from uuid import UUID, uuid4  # UUID：生成唯一标识

from sqlalchemy import delete, func, select  # SQL 操作：查询、删除、聚合
from sqlalchemy.orm import Session  # 数据库会话类型

from app.core.config import get_settings  # 配置单例
from app.core.errors import AppError
from app.integrations.embedding import EmbeddingProvider  # 文本向量化
from app.integrations.llm import LlmProvider
from app.integrations.vector_store import VectorStoreClient  # 向量数据库客户端
from app.models.document import Document  # 文档 ORM 模型
from app.models.document_chunk import DocumentChunk  # 文档块 ORM 模型
from app.models.evaluation_chunk import EvaluationChunk  # 评测隔离块模型
from app.models.evaluation_dataset import EvaluationDataset  # 评测数据集模型
from app.models.evaluation_query import EvaluationQuery  # 评测查询模型
from app.models.evaluation_result import EvaluationResult  # 评测结果模型
from app.models.evaluation_run import EvaluationRun  # 评测运行模型
from app.retrieval.hybrid import DenseHit as HybridDenseHit
from app.retrieval.hybrid import HybridRetriever, SparseHit as HybridSparseHit
from app.retrieval.sparse_index import BM25Index
from app.schemas.evaluation import (
    DatasetCreateRequest,   # 数据集创建请求
    DatasetDetailResponse,  # 数据集详情响应
    DatasetListResponse,    # 数据集列表响应
    DatasetResponse,        # 数据集响应
    QueryResponse,          # 查询响应
    RunCreateRequest,       # 运行创建请求
    RunDetailResponse,      # 运行详情响应
    RunListResponse,        # 运行列表响应
    RunResponse,            # 运行响应
    RunResultResponse,      # 运行结果响应
    RunSummary,             # 运行摘要
)
from app.schemas.retrieval import Citation  # 检索引用片段

logger = logging.getLogger(__name__)


class EvaluationService:
    """评测服务：管理评测数据集 CRUD 和评测运行的完整生命周期。

    核心设计：
    - 数据隔离：评测使用 evaluation_chunks 表（不污染 document_chunks）
    - 向量隔离：临时集合 eval_<run_uuid>（不影响生产索引）
    - 乐观锁：只有 pending 状态的运行才能开始执行
    - 三种执行模式：sync（同步）/ background（线程池）/ celery（任务队列）

    面试点：为什么评测需要数据隔离？
    答：避免评测过程污染生产数据（如修改 chunk 的 vector_id），
    同时保证评测的可复现性——每次运行都在独立的向量集合中。
    """

    def __init__(self, session: Session) -> None:
        """初始化评测服务，注入依赖。

        Args:
            session: SQLAlchemy 数据库会话
        """
        self.session = session                        # 数据库会话
        self.settings = get_settings()                # 全局配置
        self.embedding_provider = EmbeddingProvider(self.settings)  # Embedding 提供者
        self.llm_provider = LlmProvider(self.settings)
        self.vector_store = VectorStoreClient(self.settings)        # 向量数据库客户端
        # 每个评测 Run 都使用独立 BM25 索引，不能复用生产或其他 Run 的稀疏数据。
        self._eval_sparse_indexes: dict[UUID, BM25Index] = {}

    # ── Dataset CRUD ─────────────────────────────────────────────

    def create_dataset(self, req: DatasetCreateRequest) -> DatasetResponse:
        """创建评测数据集：包含数据集本身和关联的查询列表。

        面试点：为什么要用 flush() 而不是直接 commit？
        答：flush() 将 SQL 发送到数据库但不提交事务，此时可以获取
        自增 ID 或 UUID，后续在同一事务中继续操作。如果后续操作失败，
        整个事务回滚，保证数据一致性。
        """
        # 创建数据集记录
        dataset = EvaluationDataset(
            dataset_uuid=uuid4(),
            name=req.name,
            description=req.description,
        )
        self.session.add(dataset)
        self.session.flush()  # 刷新以获取 dataset_uuid（用于关联查询）

        # 创建关联的查询列表
        for q in req.queries:
            eq = EvaluationQuery(
                query_uuid=uuid4(),
                dataset_uuid=dataset.dataset_uuid,  # 关联数据集
                query_text=q.query_text,            # 查询文本
                expected_doc_titles=q.expected_doc_titles,  # 期望命中的文档标题
                expected_terms=q.expected_terms,    # 期望包含的关键词
                reference_answer=q.reference_answer,
                notes=q.notes,                      # 备注
            )
            self.session.add(eq)

        self.session.commit()  # 提交事务
        count = self._count_queries(dataset.dataset_uuid)  # 统计查询数
        return DatasetResponse(
            dataset_uuid=str(dataset.dataset_uuid),
            name=dataset.name,
            description=dataset.description,
            query_count=count,
            created_at=dataset.created_at,
        )

    def list_datasets(self) -> DatasetListResponse:
        """列出所有评测数据集（按创建时间倒序）。"""
        stmt = select(EvaluationDataset).order_by(EvaluationDataset.created_at.desc())
        datasets = list(self.session.scalars(stmt))
        items = []
        for d in datasets:
            count = self._count_queries(d.dataset_uuid)  # 统计每个数据集的查询数
            items.append(DatasetResponse(
                dataset_uuid=str(d.dataset_uuid),
                name=d.name,
                description=d.description,
                query_count=count,
                created_at=d.created_at,
            ))
        return DatasetListResponse(datasets=items)

    def get_dataset(self, dataset_uuid: str) -> DatasetDetailResponse:
        """获取数据集详情（含查询列表）。"""
        ds = self._get_dataset(dataset_uuid)
        queries = self._get_queries(ds.dataset_uuid)
        return DatasetDetailResponse(
            dataset_uuid=str(ds.dataset_uuid),
            name=ds.name,
            description=ds.description,
            query_count=len(queries),
            created_at=ds.created_at,
            queries=[
                QueryResponse(
                    query_uuid=str(q.query_uuid),
                    query_text=q.query_text,
                    expected_doc_titles=q.expected_doc_titles,
                    expected_terms=q.expected_terms,
                    reference_answer=q.reference_answer,
                    notes=q.notes,
                )
                for q in queries
            ],
        )

    def delete_dataset(self, dataset_uuid: str) -> None:
        """删除数据集（级联删除关联的查询）。"""
        ds = self._get_dataset(dataset_uuid)
        self.session.delete(ds)
        self.session.commit()

    # ── Run CRUD ──────────────────────────────────────────────────

    def create_run(self, req: RunCreateRequest) -> RunResponse:
        """创建评测运行，根据配置选择执行模式。

        面试点：三种执行模式的设计考量？
        1. sync（同步）：适合小规模评测，请求-响应模型，简单直接
        2. background（线程池）：适合中等规模，不阻塞 HTTP 请求，但服务重启会丢失
        3. celery（任务队列）：适合大规模，支持重试、持久化、分布式执行

        乐观锁：只有 pending 状态才能开始执行，防止并发重复执行。
        """
        ds = self._get_dataset(req.dataset_uuid)  # 验证数据集存在
        run = EvaluationRun(
            run_uuid=uuid4(),
            dataset_uuid=ds.dataset_uuid,
            chunking_strategy=req.chunking_strategy,    # 分块策略
            chunking_params=req.chunking_params or {},   # 分块参数
            retrieval_strategy=req.retrieval_strategy,   # 检索策略
            retrieval_params=req.retrieval_params or {}, # 检索参数
            status="pending",  # 初始状态：等待执行
        )
        self.session.add(run)
        self.session.commit()

        # 根据配置选择执行模式
        if self.settings.evaluation_execution_mode == "sync":
            # 同步模式：直接在请求线程中执行
            self._execute_run(run, req.doc_uuids)
        elif self.settings.evaluation_execution_mode == "background":
            # 后台线程池模式：提交到 BackgroundJobRunner
            from app.services.background_jobs import BackgroundJobRunner

            BackgroundJobRunner.submit_evaluation_run(
                run_uuid=run.run_uuid,
                doc_uuids=req.doc_uuids,
            )
        else:
            # Celery 任务队列模式：入队到 Celery
            from app.tasks.evaluation_tasks import enqueue_evaluation_run

            enqueue_evaluation_run(str(run.run_uuid), req.doc_uuids)

        return RunResponse(
            run_uuid=str(run.run_uuid),
            dataset_uuid=str(run.dataset_uuid),
            chunking_strategy=run.chunking_strategy,
            chunking_params=run.chunking_params,
            retrieval_strategy=run.retrieval_strategy,
            retrieval_params=run.retrieval_params,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
        )

    def execute_run_by_uuid(self, run_uuid: str, doc_uuids: list[str]) -> None:
        """Worker 入口点：由后台任务调用，不创建新运行，幂等可重试。"""
        run = self._get_run(run_uuid)
        self._execute_run(run, doc_uuids)

    def list_runs(self, dataset_uuid: str | None = None) -> RunListResponse:
        """列出评测运行记录，支持按数据集过滤。

        Args:
            dataset_uuid: 可选，按数据集 UUID 过滤
        """
        stmt = select(EvaluationRun).order_by(EvaluationRun.created_at.desc())
        if dataset_uuid:
            stmt = stmt.where(EvaluationRun.dataset_uuid == UUID(dataset_uuid))
        runs = list(self.session.scalars(stmt))

        # 获取所有关联的评测集名称（用于展示）
        dataset_uuids = list(set(r.dataset_uuid for r in runs))
        datasets = {}
        if dataset_uuids:
            ds_stmt = select(EvaluationDataset).where(EvaluationDataset.dataset_uuid.in_(dataset_uuids))
            for ds in self.session.scalars(ds_stmt):
                datasets[str(ds.dataset_uuid)] = ds.name

        return RunListResponse(
            runs=[
                RunResponse(
                    run_uuid=str(r.run_uuid),
                    dataset_uuid=str(r.dataset_uuid),
                    dataset_name=datasets.get(str(r.dataset_uuid)),  # 附加数据集名称
                    chunking_strategy=r.chunking_strategy,
                    chunking_params=r.chunking_params,
                    retrieval_strategy=r.retrieval_strategy,
                    retrieval_params=r.retrieval_params,
                    status=r.status,          # 运行状态
                    started_at=r.started_at,   # 开始时间
                    finished_at=r.finished_at, # 结束时间
                    created_at=r.created_at,   # 创建时间
                )
                for r in runs
            ]
        )

    def get_run(self, run_uuid: str) -> RunDetailResponse:
        """获取评测运行详情：包含结果列表和汇总指标。

        面试点：汇总指标的计算方式
        - Hit@k 率：所有查询中命中期望文档的比例
        - Mean MRR：所有查询 MRR 的平均值
        - Mean Term Hit Rate：所有查询关键词命中率的平均值
        """
        run = self._get_run(run_uuid)
        results = self._get_results(run.run_uuid)  # 获取所有评测结果
        queries = {str(q.query_uuid): q for q in self._get_queries(run.dataset_uuid)}  # 查询索引

        # 构建结果项列表
        result_items = []
        for r in results:
            q = queries.get(str(r.query_uuid))
            result_items.append(RunResultResponse(
                query_uuid=str(r.query_uuid),
                query_text=q.query_text if q else "",
                hit_at_1=r.hit_at_1,     # 第一位是否命中
                hit_at_3=r.hit_at_3,     # 前三是否命中
                hit_at_5=r.hit_at_5,     # 前五是否命中
                mrr=r.mrr,               # 平均倒数排名
                expected_term_hit_rate=r.expected_term_hit_rate,  # 关键词命中率
                avg_latency_ms=r.avg_latency_ms,  # 平均延迟
                answer_text=r.answer_text,
                answer_faithfulness_score=r.answer_faithfulness_score,
                answer_relevance_score=r.answer_relevance_score,
                citation_coverage_score=r.citation_coverage_score,
                answer_judge_verdict=r.answer_judge_verdict,
                answer_judge_reason=r.answer_judge_reason,
                answer_judge_latency_ms=r.answer_judge_latency_ms,
                top_hits=r.top_hits,     # 命中详情
                debug_info=r.debug_info, # 调试信息
            ))

        # 计算汇总指标（如果有结果）
        summary = None
        if result_items:
            n = len(result_items)
            summary = RunSummary(
                total_queries=n,
                hit_at_1_rate=round(sum(1 for x in result_items if x.hit_at_1) / n, 4),
                hit_at_3_rate=round(sum(1 for x in result_items if x.hit_at_3) / n, 4),
                hit_at_5_rate=round(sum(1 for x in result_items if x.hit_at_5) / n, 4),
                mean_mrr=round(sum(x.mrr for x in result_items) / n, 4),
                mean_term_hit_rate=round(sum(x.expected_term_hit_rate for x in result_items) / n, 4),
                mean_latency_ms=int(sum(x.avg_latency_ms for x in result_items) / n),
                mean_answer_faithfulness=self._mean_optional_metric([x.answer_faithfulness_score for x in result_items]),
                mean_answer_relevance=self._mean_optional_metric([x.answer_relevance_score for x in result_items]),
                mean_citation_coverage=self._mean_optional_metric([x.citation_coverage_score for x in result_items]),
            )

        return RunDetailResponse(
            run_uuid=str(run.run_uuid),
            dataset_uuid=str(run.dataset_uuid),
            chunking_strategy=run.chunking_strategy,
            chunking_params=run.chunking_params,
            retrieval_strategy=run.retrieval_strategy,
            retrieval_params=run.retrieval_params,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
            results=result_items,
            summary=summary,
        )

    def delete_run(self, run_uuid: str) -> None:
        """删除评测运行：清理临时向量集合和隔离块数据。

        面试点：为什么删除运行时要清理向量集合？
        答：评测运行使用临时向量集合 eval_<run_uuid>，运行结束后如果不清理，
        会积累大量无用的向量数据，浪费存储和内存资源。
        """
        run = self._get_run(run_uuid)
        self._eval_sparse_indexes.pop(run.run_uuid, None)
        # 删除临时向量集合
        self.vector_store.drop_collection(collection_name=self._eval_collection_name(run.run_uuid))
        # 删除隔离的评测块数据
        self.session.execute(
            delete(EvaluationChunk).where(EvaluationChunk.run_uuid == run.run_uuid)
        )
        self.session.delete(run)
        self.session.commit()

    # ── Run execution (isolation) ────────────────────────────────

    def _execute_run(self, run: EvaluationRun, doc_uuids: list[str]) -> None:
        """执行评测运行的核心方法：隔离环境下的三步骤流程。

        面试点：乐观锁如何实现？
        答：检查 run.status != "pending" 则跳过，确保只有 pending 状态才能执行。
        如果状态已变更为 running/completed/failed，说明已有其他进程在执行或已完成。

        流程：
        1. 将生产 chunk 复制到 evaluation_chunks（数据隔离）
        2. 构建临时向量索引（向量隔离）
        3. 逐个执行查询并计算指标
        """
        # 乐观锁：只处理 pending 状态的运行
        if run.status != "pending":
            logger.warning("Run %s is not pending, skipping", run.run_uuid)
            return

        # 状态变更：pending → running
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        self.session.commit()

        try:
            # Step 1: 将生产 chunk 复制到评测隔离表
            self._copy_chunks_to_eval(run, doc_uuids)

            # Step 2: 构建临时向量索引
            self._build_eval_vector_index(run)
            if run.retrieval_strategy == "hybrid":
                self._build_eval_sparse_index(run)

            # Step 3: 逐个执行评测查询
            queries = self._get_queries(run.dataset_uuid)
            eval_collection = self._eval_collection_name(run.run_uuid)

            for q in queries:
                self._evaluate_query(run, q, eval_collection)

            run.status = "completed"  # 执行成功
        except Exception as exc:
            logger.exception("Run %s failed", run.run_uuid)
            run.status = "failed"  # 执行失败
            # 失败时清理临时向量集合，避免残留
            self.vector_store.drop_collection(collection_name=self._eval_collection_name(run.run_uuid))
        finally:
            self._eval_sparse_indexes.pop(run.run_uuid, None)
            run.finished_at = datetime.now(timezone.utc)  # 记录结束时间
            self.session.commit()

    def _copy_chunks_to_eval(self, run: EvaluationRun, doc_uuids: list[str]) -> None:
        """将生产 chunk 复制到评测隔离表 evaluation_chunks。

        面试点：为什么复制而非直接引用？
        答：
        1. 数据隔离：评测不修改生产数据
        2. 可复现：评测运行结束后生产数据可能已变更
        3. 独立管理：评测块有独立的 vector_id 和 collection 名称
        """
        # 根据 doc_uuids 筛选要复制的 chunk
        if doc_uuids:
            uuids = [UUID(u) for u in doc_uuids]
            stmt = select(DocumentChunk).where(DocumentChunk.doc_uuid.in_(uuids))
        else:
            stmt = select(DocumentChunk)  # 无指定则复制全部

        chunks = list(self.session.scalars(stmt))
        for chunk in chunks:
            eval_chunk = EvaluationChunk(
                chunk_uuid=uuid4(),  # 新的 UUID（与生产 chunk 不同）
                run_uuid=run.run_uuid,  # 关联到当前运行
                doc_uuid=chunk.doc_uuid,  # 保持文档引用
                chunk_index=chunk.chunk_index,
                chunk_type=chunk.chunk_type,
                section_title=chunk.section_title,
                page_no=chunk.page_no,
                sheet_name=chunk.sheet_name,
                row_start=chunk.row_start,
                row_end=chunk.row_end,
                token_count=chunk.token_count,
                char_count=chunk.char_count,
                chunk_text=chunk.chunk_text,  # 复制文本内容
                chunk_summary=chunk.chunk_summary,
                vector_id=None,  # 稍后由 _build_eval_vector_index 填充
                zilliz_collection=f"eval_{run.run_uuid}",  # 临时集合名
                metadata_json={**chunk.metadata_json},  # 浅拷贝元数据
            )
            self.session.add(eval_chunk)

        self.session.commit()
        logger.info("Copied %s chunks to eval run %s", len(chunks), run.run_uuid)

    def _build_eval_vector_index(self, run: EvaluationRun) -> None:
        """为评测块构建临时向量索引。

        流程：
        1. 读取所有评测块
        2. 批量向量化
        3. 写入临时向量集合 eval_<run_uuid>
        4. 将返回的 vector_id 回写到评测块
        """
        stmt = select(EvaluationChunk).where(EvaluationChunk.run_uuid == run.run_uuid)
        chunks = list(self.session.scalars(stmt))
        if not chunks:
            return

        # 提取文本并批量向量化
        texts = [c.chunk_text for c in chunks]
        embeddings = self.embedding_provider.embed_texts(texts)

        # 构建向量元数据
        metadatas = [
            {
                "chunk_uuid": str(c.chunk_uuid),
                "doc_uuid": str(c.doc_uuid),
                "run_uuid": str(c.run_uuid),
                "chunk_index": c.chunk_index,
                "section_title": c.section_title,
            }
            for c in chunks
        ]

        # 写入临时向量集合
        eval_collection = self._eval_collection_name(run.run_uuid)
        vector_records = self.vector_store.upsert_embeddings(
            embeddings=[e.vector for e in embeddings],
            metadatas=metadatas,
            collection_name=eval_collection,  # 临时集合：eval_<run_uuid>
        )

        # 将返回的 vector_id 回写到评测块（用于后续删除）
        chunk_uuid_to_record = {
            record.metadata["chunk_uuid"]: record for record in vector_records
        }
        for chunk in chunks:
            record = chunk_uuid_to_record.get(str(chunk.chunk_uuid))
            if record is None:
                continue
            chunk.vector_id = record.vector_id
            chunk.zilliz_collection = record.collection

        self.session.commit()
        logger.info("Built vector index for eval run %s (%s chunks)", run.run_uuid, len(chunks))

    def _build_eval_sparse_index(self, run: EvaluationRun) -> None:
        """Build a BM25 index from the current Run's isolated chunks only."""
        chunks = list(
            self.session.scalars(
                select(EvaluationChunk).where(EvaluationChunk.run_uuid == run.run_uuid)
            )
        )
        index = BM25Index()
        index.build_index(
            [
                {
                    "chunk_uuid": str(chunk.chunk_uuid),
                    "doc_uuid": str(chunk.doc_uuid),
                    "chunk_text": chunk.chunk_text,
                    "metadata": {"run_uuid": str(run.run_uuid)},
                }
                for chunk in chunks
            ]
        )
        self._eval_sparse_indexes[run.run_uuid] = index
        logger.info("Built isolated sparse index for eval run %s (%s chunks)", run.run_uuid, len(chunks))

    def _evaluate_query(
        self,
        run: EvaluationRun,
        query: EvaluationQuery,
        eval_collection: str,
    ) -> None:
        """执行单个评测查询并计算指标。

        计算的指标：
        - Hit@1/3/5：期望文档是否出现在前 1/3/5 名中
        - MRR (Mean Reciprocal Rank)：期望文档的倒数排名
        - Expected Term Hit Rate：期望关键词在命中片段中的出现比例
        """
        started = time.perf_counter()
        # 在隔离集合中检索
        hits = self._search_eval_hits(
            run=run,
            query_text=query.query_text,
            eval_collection=eval_collection,
            top_k=5,  # 固定 top_k=5
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # 提取命中文档标题
        hit_titles = [h.title for h in hits]

        # 计算 Hit@k 指标
        hit_at_1 = self._hit_at_k(hit_titles, query.expected_doc_titles, 1)
        hit_at_3 = self._hit_at_k(hit_titles, query.expected_doc_titles, 3)
        hit_at_5 = self._hit_at_k(hit_titles, query.expected_doc_titles, 5)

        # 计算 MRR
        mrr = self._compute_mrr(hit_titles, query.expected_doc_titles)

        # 计算关键词命中率
        term_hit_rate = self._compute_term_hit_rate(hits, query.expected_terms)

        # 构建命中结果 JSON
        top_hits_json = [
            {
                "doc_uuid": h.doc_uuid,
                "chunk_uuid": h.chunk_uuid,
                "title": h.title,
                "score": h.score,
                "vector_score": h.vector_score,
                "sparse_score": h.sparse_score,
                "snippet": h.snippet[:120],  # 截取前 120 字符
            }
            for h in hits
        ]
        answer_evaluation = self._evaluate_answer_layer(query=query, hits=hits)

        # 保存评测结果
        eval_result = EvaluationResult(
            run_uuid=run.run_uuid,
            query_uuid=query.query_uuid,
            hit_at_1=hit_at_1,
            hit_at_3=hit_at_3,
            hit_at_5=hit_at_5,
            mrr=mrr,
            expected_term_hit_rate=term_hit_rate,
            avg_latency_ms=elapsed_ms,
            answer_text=answer_evaluation["answer_text"],
            answer_faithfulness_score=answer_evaluation["faithfulness_score"],
            answer_relevance_score=answer_evaluation["relevance_score"],
            citation_coverage_score=answer_evaluation["coverage_score"],
            answer_judge_verdict=answer_evaluation["verdict"],
            answer_judge_reason=answer_evaluation["reason"],
            answer_judge_latency_ms=answer_evaluation["latency_ms"],
            top_hits=top_hits_json,
            debug_info={
                "retrieval_strategy": run.retrieval_strategy,
                "executed_strategy": run.retrieval_strategy,
                "vector_collection": eval_collection,
                "latency_ms": elapsed_ms,
                "hit_count": len(hits),
                "answer_judge_status": answer_evaluation["status"],
                "answer_generation_provider": answer_evaluation["answer_provider"],
            },
        )
        self.session.add(eval_result)
        self.session.commit()

    def _evaluate_answer_layer(self, *, query: EvaluationQuery, hits: list[Citation]) -> dict:
        """Generate and judge the answer against the exact isolated retrieval evidence."""
        skipped = {
            "answer_text": None,
            "faithfulness_score": None,
            "relevance_score": None,
            "coverage_score": None,
            "verdict": None,
            "reason": None,
            "latency_ms": None,
            "answer_provider": None,
        }
        if not self.settings.answer_judge_enabled:
            return {**skipped, "status": "disabled"}
        if not self.llm_provider.is_configured():
            return {**skipped, "status": "not_configured"}
        if not hits:
            return {**skipped, "status": "no_evidence"}

        try:
            generated = self.llm_provider.generate_answer_with_fallback(
                question=query.query_text,
                citations=hits,
                generation_options=None,
                fallback_builder=self._build_grounded_answer,
            )
            if generated is None:
                return {**skipped, "status": "answer_generation_unavailable"}
            judged = self.llm_provider.judge_answer(
                question=query.query_text,
                answer=generated.answer,
                citations=hits,
                reference_answer=query.reference_answer,
            )
        except AppError as exc:
            logger.warning("Answer judge skipped for evaluation query %s: %s", query.query_uuid, exc.message)
            return {**skipped, "status": "failed", "reason": exc.message}

        threshold_passed = (
            judged.faithfulness_score >= self.settings.answer_judge_min_faithfulness
            and judged.answer_relevance_score >= self.settings.answer_judge_min_relevance
        )
        verdict = "pass" if judged.verdict == "pass" and threshold_passed else "fail"
        reason = judged.reason
        if not threshold_passed:
            reason = f"{reason} (below configured answer quality threshold)"
        return {
            "answer_text": generated.answer,
            "faithfulness_score": judged.faithfulness_score,
            "relevance_score": judged.answer_relevance_score,
            "coverage_score": judged.citation_coverage_score,
            "verdict": verdict,
            "reason": reason,
            "latency_ms": judged.latency_ms,
            "answer_provider": generated.provider_name,
            "status": "completed",
        }

    @staticmethod
    def _build_grounded_answer(citations: list[Citation]) -> str:
        lines = [f"- {citation.title}: {citation.snippet}" for citation in citations[:3]]
        return "基于检索到的引用，当前可确认的信息如下：\n" + "\n".join(lines)

    @staticmethod
    def _mean_optional_metric(values: list[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return round(sum(present) / len(present), 4) if present else None

    # ── Metrics helpers ──────────────────────────────────────────

    @staticmethod
    def _hit_at_k(hit_titles: list[str], expected_titles: list[str], k: int) -> bool:
        """计算 Hit@k：期望文档标题是否出现在前 k 个结果中。

        面试点：Hit@k vs Recall@k 的区别？
        - Hit@k：至少一个期望文档出现在 top-k 中 → True/False
        - Recall@k：top-k 中命中的期望文档数 / 总期望文档数 → 0~1
        本系统使用 Hit@k，更适用于单文档检索场景（每个查询期望一个文档）。

        Args:
            hit_titles: 检索命中的文档标题列表
            expected_titles: 期望命中的文档标题列表
            k: 前 k 个结果

        Returns:
            是否命中（大小写不敏感匹配）
        """
        if not expected_titles:
            return False
        top_k = hit_titles[:k]  # 截取前 k 个
        return any(et.lower() in [t.lower() for t in top_k] for et in expected_titles)

    @staticmethod
    def _compute_mrr(hit_titles: list[str], expected_titles: list[str]) -> float:
        """计算 MRR (Mean Reciprocal Rank)：期望文档首次出现排名的倒数。

        面试点：MRR 的优势和局限？
        优势：简单直观，反映检索系统将相关文档排在前面的能力
        局限：只关注第一个相关文档，忽略后续相关文档的排名

        例如：期望文档排第 2 位 → MRR = 1/2 = 0.5
             期望文档排第 1 位 → MRR = 1/1 = 1.0
             未命中 → MRR = 0.0
        """
        if not expected_titles:
            return 0.0
        for rank, title in enumerate(hit_titles, start=1):  # 从第 1 位开始
            for et in expected_titles:
                if et.lower() == title.lower():
                    return round(1.0 / rank, 4)  # 倒数排名
        return 0.0

    @staticmethod
    def _compute_term_hit_rate(hits: list, expected_terms: list[str]) -> float:
        """计算关键词命中率：期望关键词在命中片段中的出现比例。

        面试点：为什么需要关键词命中率？
        答：向量检索可能返回语义相关但不含关键词的结果，
        关键词命中率从精确匹配角度评估检索质量。
        """
        if not expected_terms or not hits:
            return 0.0
        total_hits = 0
        for term in expected_terms:
            term_lower = term.lower()
            for hit in hits:
                if term_lower in hit.snippet.lower():  # 在片段中查找关键词
                    total_hits += 1
                    break  # 一个关键词只需命中一次
        return round(total_hits / len(expected_terms), 4)

    # ── Internal helpers ─────────────────────────────────────────

    def _get_dataset(self, dataset_uuid: str) -> EvaluationDataset:
        """根据 UUID 获取数据集，不存在则抛出 404 错误。"""
        stmt = select(EvaluationDataset).where(EvaluationDataset.dataset_uuid == UUID(dataset_uuid))
        ds = self.session.scalar(stmt)
        if ds is None:
            from app.core.errors import AppError, ErrorCode
            raise AppError(code=ErrorCode.DOCUMENT_NOT_FOUND, message="dataset not found", status_code=404)
        return ds

    def _get_run(self, run_uuid: str) -> EvaluationRun:
        """根据 UUID 获取评测运行，不存在则抛出 404 错误。"""
        stmt = select(EvaluationRun).where(EvaluationRun.run_uuid == UUID(run_uuid))
        run = self.session.scalar(stmt)
        if run is None:
            from app.core.errors import AppError, ErrorCode
            raise AppError(code=ErrorCode.DOCUMENT_NOT_FOUND, message="run not found", status_code=404)
        return run

    def _get_queries(self, dataset_uuid: UUID) -> list[EvaluationQuery]:
        """获取数据集的所有查询。"""
        stmt = select(EvaluationQuery).where(EvaluationQuery.dataset_uuid == dataset_uuid)
        return list(self.session.scalars(stmt))

    def _get_results(self, run_uuid: UUID) -> list[EvaluationResult]:
        """获取评测运行的所有结果记录。"""
        stmt = select(EvaluationResult).where(EvaluationResult.run_uuid == run_uuid)
        return list(self.session.scalars(stmt))

    def _count_queries(self, dataset_uuid: UUID) -> int:
        """统计数据集的查询数量。"""
        stmt = select(func.count(EvaluationQuery.id)).where(EvaluationQuery.dataset_uuid == dataset_uuid)
        return self.session.scalar(stmt) or 0

    @staticmethod
    def _eval_collection_name(run_uuid: UUID) -> str:
        """生成评测专用的临时向量集合名称。

        格式：eval_<run_uuid>
        确保与生产集合隔离，评测完成后可安全删除。
        """
        return f"eval_{run_uuid}"

    @staticmethod
    def _rewrite_query(query: str) -> str:
        """查询标准化：将多空白字符规范化为单个空格。"""
        return " ".join(query.strip().split())

    def _search_eval_hits(
        self,
        *,
        run: EvaluationRun,
        query_text: str,
        eval_collection: str,
        top_k: int,
    ) -> list[Citation]:
        """Search one isolated Run with dense or hybrid retrieval.

        Hybrid evaluation never uses the production sparse index. A BM25 index
        is built from the same ``evaluation_chunks`` snapshot as the temporary
        vector collection, so both recall paths observe identical test data.
        """
        rewritten_query = self._rewrite_query(query_text)
        strategy = run.retrieval_strategy or "dense"
        if strategy not in {"dense", "hybrid"}:
            raise ValueError(f"Unsupported isolated evaluation strategy: {strategy}")

        candidate_k = top_k * 3 if strategy == "hybrid" else top_k
        query_embedding = self.embedding_provider.embed_texts([rewritten_query])[0].vector
        vector_hits = self.vector_store.search(
            query_embedding=query_embedding,
            top_k=candidate_k,
            filters={"run_uuid": str(run.run_uuid)},
            collection_name=eval_collection,
        )

        chunk_ids = [
            str(hit.metadata.get("chunk_uuid"))
            for hit in vector_hits
            if hit.metadata.get("chunk_uuid")
        ]
        raw_sparse_hits = []
        if strategy == "hybrid":
            index = self._eval_sparse_indexes.get(run.run_uuid)
            if index is None:
                raise RuntimeError(f"Missing isolated sparse index for evaluation run {run.run_uuid}")
            raw_sparse_hits = index.search(rewritten_query, top_k=candidate_k)
            chunk_ids.extend(hit.chunk_uuid for hit in raw_sparse_hits)

        chunks_by_uuid = self._load_eval_chunks_by_uuid(run.run_uuid, chunk_ids)
        documents = self._load_documents_by_uuid([chunk.doc_uuid for chunk in chunks_by_uuid.values()])

        dense_hits: list[HybridDenseHit] = []
        for hit in vector_hits:
            chunk_uuid = str(hit.metadata.get("chunk_uuid") or "")
            chunk = chunks_by_uuid.get(chunk_uuid)
            if chunk is None:
                continue
            document = documents.get(str(chunk.doc_uuid))
            if document is None:
                continue
            dense_hits.append(
                HybridDenseHit(
                    chunk_uuid=chunk_uuid,
                    doc_uuid=str(chunk.doc_uuid),
                    score=max(hit.score, 0.0),
                    metadata={"chunk": chunk, "document": document},
                )
            )

        if strategy == "dense":
            ranked = [
                (hit.score, hit.score, None, hit.metadata["document"], hit.metadata["chunk"])
                for hit in dense_hits
            ]
        else:
            sparse_hits: list[HybridSparseHit] = []
            for hit in raw_sparse_hits:
                chunk = chunks_by_uuid.get(hit.chunk_uuid)
                if chunk is None:
                    continue
                document = documents.get(str(chunk.doc_uuid))
                if document is None:
                    continue
                sparse_hits.append(
                    HybridSparseHit(
                        chunk_uuid=hit.chunk_uuid,
                        doc_uuid=str(chunk.doc_uuid),
                        score=hit.score,
                        metadata={"chunk": chunk, "document": document},
                    )
                )
            alpha = run.retrieval_params.get("alpha", 0.7) if run.retrieval_params else 0.7
            if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
                raise ValueError("retrieval_params.alpha must be numeric")
            fused_hits = HybridRetriever(alpha=float(alpha)).fuse(dense_hits, sparse_hits)
            ranked = []
            for hit in fused_hits:
                chunk = chunks_by_uuid.get(hit.chunk_uuid)
                document = documents.get(str(chunk.doc_uuid)) if chunk is not None else None
                if chunk is None or document is None:
                    continue
                ranked.append((hit.fusion_score, hit.dense_score, hit.sparse_score, document, chunk))

        ranked.sort(key=lambda item: (item[0], item[1] or 0.0, item[2] or 0.0), reverse=True)
        return [
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
                score=score,
                vector_score=dense_score,
                text_score=None,
                sparse_score=sparse_score,
                rerank_score=None,
                pre_rerank_score=None,
                image_url=None,
            )
            for score, dense_score, sparse_score, document, chunk in ranked[:top_k]
        ]

    def _load_eval_chunks_by_uuid(
        self,
        run_uuid: UUID,
        chunk_uuids: list[str],
    ) -> dict[str, EvaluationChunk]:
        """从评测隔离表中批量加载 chunk。

        Args:
            run_uuid: 评测运行 UUID（用于限定范围）
            chunk_uuids: 要加载的 chunk UUID 列表

        Returns:
            {chunk_uuid_str: EvaluationChunk} 映射
        """
        if not chunk_uuids:
            return {}
        normalized: list[UUID] = []
        for chunk_uuid in chunk_uuids:
            try:
                normalized.append(UUID(str(chunk_uuid)))
            except ValueError:
                logger.warning("Skipping invalid eval chunk uuid: %s", chunk_uuid)
        if not normalized:
            return {}
        # 同时限定 run_uuid 和 chunk_uuid，确保数据隔离
        stmt = (
            select(EvaluationChunk)
            .where(EvaluationChunk.run_uuid == run_uuid)
            .where(EvaluationChunk.chunk_uuid.in_(normalized))
        )
        chunks = list(self.session.scalars(stmt))
        return {str(chunk.chunk_uuid): chunk for chunk in chunks}

    def _load_documents_by_uuid(self, doc_uuids: list[UUID]) -> dict[str, Document]:
        """根据 UUID 列表批量加载文档（去重）。

        Returns:
            {doc_uuid_str: Document} 映射
        """
        normalized = list({doc_uuid for doc_uuid in doc_uuids if doc_uuid})  # 去重
        if not normalized:
            return {}
        stmt = select(Document).where(Document.doc_uuid.in_(normalized))
        documents = list(self.session.scalars(stmt))
        return {str(document.doc_uuid): document for document in documents}
