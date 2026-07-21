# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

import logging
# 线程池执行器：用于在后台线程中异步执行耗时任务（摄入、评测）
from concurrent.futures import ThreadPoolExecutor
# 线程锁：保证线程池的创建和关闭是线程安全的
from threading import Lock
from uuid import UUID

from app.core.config import Settings
# 数据库会话工厂：后台线程中创建独立会话，不依赖 FastAPI 的请求级会话
from app.db.runtime import get_session_factory
# 摄入服务：后台线程中执行文档的解析→分块→向量化全流程
from app.services.ingestion import IngestionService

# 模块级日志记录器
logger = logging.getLogger(__name__)


class BackgroundJobRunner:
    """后台任务运行器：基于线程池的异步任务调度器。

    使用类级别（classmethod）设计，全局共享一个线程池实例。
    负责摄入任务和评测任务的后台执行。
    """
    # 类级别线程池实例，初始为 None（懒加载）
    _executor: ThreadPoolExecutor | None = None
    # 线程锁，保证 _executor 的创建和关闭操作是原子性的
    _lock = Lock()

    @classmethod
    def submit_ingest_job(cls, *, doc_uuid: UUID, job_uuid: UUID, settings: Settings) -> None:
        """提交文档摄入任务到后台线程池执行。

        使用关键字参数强制（*），防止调用时参数顺序错误。

        Args:
            doc_uuid: 文档 UUID
            job_uuid: 任务 UUID（用于状态追踪）
            settings: 全局配置（含 ingestion_mode 等）
        """
        executor = cls._get_executor()                        # 获取（或创建）线程池
        executor.submit(cls._run_ingest_job, doc_uuid, job_uuid, settings)  # 提交任务

    @classmethod
    def submit(cls, fn, *args, **kwargs) -> None:
        """通用任务提交方法：提交任意可调用对象到后台线程池。"""
        executor = cls._get_executor()
        executor.submit(fn, *args, **kwargs)

    @classmethod
    def submit_evaluation_run(cls, *, run_uuid: UUID, doc_uuids: list[str]) -> None:
        """提交评测任务到后台线程池执行。

        Args:
            run_uuid: 评测运行 UUID
            doc_uuids: 参与评测的文档 UUID 列表
        """
        executor = cls._get_executor()
        executor.submit(cls._run_evaluation_run, str(run_uuid), doc_uuids)

    @classmethod
    def shutdown(cls) -> None:
        """优雅关闭后台线程池。

        wait=True：等待所有已提交任务完成后再关闭。
        cancel_futures=False：不取消已提交但未开始的任务（让其完成）。
        """
        with cls._lock:  # 加锁，防止与 _get_executor 并发操作
            if cls._executor is not None:
                cls._executor.shutdown(wait=True, cancel_futures=False)
                cls._executor = None  # 置空，下次调用会重新创建

    @classmethod
    def _get_executor(cls) -> ThreadPoolExecutor:
        """获取（或懒加载创建）线程池实例。

        线程安全：使用 _lock 确保多线程环境下只创建一个实例。
        """
        with cls._lock:  # 加锁
            if cls._executor is None:  # 双重检查：未创建则新建
                # 最大 4 个工作线程，线程名前缀 "oa-rag-bg" 便于日志追踪
                cls._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="oa-rag-bg")
            return cls._executor

    @staticmethod
    def _run_ingest_job(doc_uuid: UUID, job_uuid: UUID, settings: Settings) -> None:
        """后台线程中执行的实际摄入逻辑。

        关键点：后台线程中需要独立创建数据库会话（不共享请求级会话），
        避免会话跨线程使用导致的 SQLAlchemy 错误。
        """
        # 创建独立的数据库会话（SessionFactory 是线程安全的）
        session = get_session_factory()()
        try:
            # 调用摄入服务恢复/执行任务
            IngestionService(session, settings).resume_job(doc_uuid=doc_uuid, job_uuid=job_uuid)
            logger.info("Completed background ingestion job %s for document %s", job_uuid, doc_uuid)
        except Exception:
            # logger.exception 自动附带完整的堆栈跟踪
            logger.exception("Background ingestion job %s failed", job_uuid)
        finally:
            session.close()  # 确保会话关闭，归还连接到连接池

    @staticmethod
    def _run_evaluation_run(run_uuid: str, doc_uuids: list[str]) -> None:
        """后台线程中执行的实际评测逻辑。"""
        from app.services.evaluation import EvaluationService  # 延迟导入，避免循环引用

        # 创建独立的数据库会话
        session = get_session_factory()()
        try:
            # 调用评测服务按 UUID 执行评测运行
            EvaluationService(session).execute_run_by_uuid(run_uuid, doc_uuids)
            logger.info("Completed background evaluation run %s", run_uuid)
        except Exception:
            logger.exception("Background evaluation run %s failed", run_uuid)
        finally:
            session.close()  # 确保会话关闭
