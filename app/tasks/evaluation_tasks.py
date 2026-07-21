"""Celery 评测任务模块：将评测运行作为异步任务分发到 Celery Worker 执行。

支持：
- 任务自动重试（最多 2 次，指数退避）
- 独立的数据库会话（后台线程不共享请求级会话）
- enqueue_evaluation_run 便捷函数用于提交任务
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from celery import Celery  # Celery 分布式任务队列

from app.core.config import get_settings  # 配置单例
from app.db.runtime import get_session_factory  # 数据库会话工厂


def _create_celery_app() -> Celery:
    """创建 Celery 应用实例，配置 broker 和 result backend。"""
    settings = get_settings()  # 获取配置
    return Celery(
        "knowledge_base_evaluation",  # 应用名称
        broker=settings.celery_broker_url,  # 消息代理（如 Redis/RabbitMQ）
        backend=settings.celery_result_backend or settings.celery_broker_url,  # 结果存储后端
    )


# 模块级 Celery 应用实例
celery_app = _create_celery_app()


@celery_app.task(
    bind=True,                        # 绑定 self 参数（可访问任务实例）
    name="knowledge_base.execute_evaluation_run",  # 全局唯一的任务名
    autoretry_for=(Exception,),       # 任何异常都自动重试
    retry_backoff=True,               # 指数退避重试（1s, 2s, 4s...）
    retry_kwargs={"max_retries": 2},  # 最多重试 2 次
)
def execute_evaluation_run_task(self, run_uuid: str, doc_uuids: list[str]) -> None:
    """Celery 任务：在后台执行评测运行。

    注意：后台线程中需独立创建数据库会话，不能共享 FastAPI 的请求级会话。
    """
    from app.services.evaluation import EvaluationService  # 延迟导入，避免循环引用

    session = get_session_factory()()  # 创建独立的数据库会话
    try:
        # 调用评测服务执行
        EvaluationService(session).execute_run_by_uuid(run_uuid, doc_uuids)
    finally:
        session.close()  # 确保会话关闭，归还连接


def enqueue_evaluation_run(run_uuid: str, doc_uuids: list[str]) -> str:
    """将评测运行任务入队到 Celery，返回任务 ID 供后续追踪。"""
    task = execute_evaluation_run_task.delay(run_uuid, doc_uuids)  # 异步发送任务
    return str(task.id)  # 返回 Celery 任务 ID
