# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from redis import Redis                           # Redis 客户端，用于健康检查 ping
from sqlalchemy import text                       # text：执行原生 SQL 语句
from sqlalchemy.orm import Session                # 数据库会话类型

from app.core.config import Settings              # 全局配置
from app.db.runtime import get_db_pool_status     # 获取数据库连接池状态
from app.integrations.embedding import EmbeddingProvider  # Embedding 服务探针
from app.integrations.llm import LlmProvider             # LLM 服务探针
from app.integrations.vector_store import VectorStoreClient  # 向量库服务探针
from app.schemas.health import HealthStatus       # 健康状态响应 Schema


def _status_from_result(ok: bool) -> str:
    """将布尔检测结果转换为 'up'/'down' 字符串。"""
    return "up" if ok else "down"


class HealthService:
    """健康检查服务：检测各依赖服务的连通性和配置状态。

    检查项包括：PostgreSQL、Redis、向量库、Embedding、LLM、数据库连接池。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings  # 保存配置引用

    def check(self, db_session: Session) -> HealthStatus:
        """执行全量健康检查，返回 HealthStatus 响应模型。"""
        postgres_up = False  # PostgreSQL 连通状态，默认失败
        redis_up = False     # Redis 连通状态，默认失败

        # === PostgreSQL 检查：执行 SELECT 1 测试连接 ===
        try:
            db_session.execute(text("SELECT 1"))  # 执行轻量级查询
            postgres_up = True                    # 成功则标记为 up
        except Exception:
            postgres_up = False                   # 任何异常标记为 down

        # === Redis 检查：PING 命令测试连接 ===
        try:
            Redis.from_url(
                self.settings.resolved_redis_url,        # 从配置构建 Redis URL
                socket_connect_timeout=1,                 # 连接超时 1 秒
                socket_timeout=1,                         # 读写超时 1 秒
            ).ping()                                     # 发送 PING 命令
            redis_up = True                              # PONG 返回则标记为 up
        except Exception:
            redis_up = False

        # === 向量库状态：根据 provider 类型判断配置状态 ===
        provider = self.settings.vector_store_provider
        if provider == "local":
            zilliz_status = "not_configured"             # 本地模式无需外部配置
        elif provider == "milvus":
            zilliz_status = "configured" if self.settings.is_external_service_configured("milvus") else "not_configured"
        else:
            zilliz_status = "configured" if self.settings.is_external_service_configured("zilliz") else "not_configured"

        # === Embedding 服务配置状态 ===
        embedding_status = (
            "configured"
            if self.settings.is_external_service_configured("embedding_provider")
            else "not_configured"
        )

        # === LLM 服务配置状态 ===
        llm_status = (
            "configured"
            if self.settings.is_external_service_configured("llm_provider")
            else "not_configured"
        )

        # === 外部服务深度探针（仅在 health_probe_external_services=true 时执行） ===
        probes = None  # 默认不探测
        if self.settings.health_probe_external_services:
            probes = {
                "zilliz": "up" if VectorStoreClient(self.settings).probe() else "down",    # 向量库实际连通性
                "embedding": "up" if EmbeddingProvider(self.settings).probe() else "down", # Embedding 实际连通性
                "llm_provider": "up" if LlmProvider(self.settings).probe() else "down",    # LLM 实际连通性
            }

        # 组装健康状态响应
        return HealthStatus(
            app="up",                                                      # 应用本身始终为 up
            postgres=_status_from_result(postgres_up),                     # PostgreSQL 状态
            redis=_status_from_result(redis_up),                           # Redis 状态
            zilliz=zilliz_status,                                          # 向量库配置状态
            embedding=embedding_status,                                    # Embedding 配置状态
            llm_provider=llm_status,                                       # LLM 配置状态
            probes=probes,                                                 # 深度探针结果（可选）
            database_pool=get_db_pool_status(),                            # 数据库连接池状态
            provider_fallbacks_enabled=self.settings.allow_provider_fallbacks,  # 是否允许降级
            ingestion_mode=self.settings.ingestion_mode,                   # 摄入模式
            evaluation_execution_mode=self.settings.evaluation_execution_mode,  # 评测执行模式
        )
