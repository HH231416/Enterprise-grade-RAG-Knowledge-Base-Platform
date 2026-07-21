# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

# 异步上下文管理器，用于管理 FastAPI 应用的生命周期（启动/关闭）
from contextlib import asynccontextmanager

# FastAPI 核心类和 CORS 中间件
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 导入聚合后的 API 路由（包含所有子路由模块）
from app.api.router import api_router
# 配置类和配置单例获取函数
from app.core.config import Settings, get_settings
# 全局异常处理器注册函数
from app.core.exception_handlers import register_exception_handlers
# 日志配置函数
from app.core.logging import configure_logging
# 自定义中间件：MetricsMiddleware 记录 HTTP 请求指标，TraceIdMiddleware 注入链路追踪 ID
from app.core.middleware import MetricsMiddleware, RateLimitMiddleware, TraceIdMiddleware
# 数据库引擎创建函数
from app.db.runtime import get_engine
# 后台任务运行器（线程池），用于异步摄入和评测
from app.services.background_jobs import BackgroundJobRunner


# ============================================================
# FastAPI 生命周期管理
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    管理应用程序的生命周期事件。

    在启动时，它会初始化设置，确保存储目录存在，并创建搜索引擎实例。
    在关闭时，它会妥善关闭后台作业运行器。

    Args:
        app: FastAPI 应用程序实例。
    """
    # 从 app.state 中获取 Settings 实例（在 create_app 中设置）
    settings: Settings = app.state.settings
    # 确保所有存储目录存在（raw/processed/samples）
    settings.ensure_storage_dirs()
    # 创建 SQLAlchemy 数据库引擎并挂载到 app.state
    app.state.engine = get_engine()
    try:
        yield  # 应用运行期间挂起，等待请求处理
    finally:
        # 应用关闭时，优雅地关闭后台线程池
        BackgroundJobRunner.shutdown()


def create_app() -> FastAPI:
    """
    创建并配置 FastAPI 应用程序实例。

    此函数负责：
    - 加载应用程序设置。
    - 配置日志记录。
    - 创建 FastAPI 应用程序实例，并设置标题和生命周期事件。
    - 配置 CORS 中间件以处理跨域请求。
    - 添加跟踪 ID 和指标中间件。
    - 包含主 API 路由。
    - 注册自定义异常处理程序。

    Returns:
        配置完成的 FastAPI 应用程序实例。
    """
    # 获取配置单例（带 lru_cache 缓存）
    settings = get_settings()
    # 按配置的日志级别初始化日志系统
    configure_logging(settings.log_level)

    # 创建 FastAPI 实例，绑定应用名称和生命周期
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    # 将配置实例挂载到 app.state，供 lifespan 和请求中使用
    app.state.settings = settings
    # 生产环境可通过 Redis 启用分布式限流；Trace/Metrics 仍会记录 429 与 503。
    app.add_middleware(RateLimitMiddleware, settings=settings)
    # 解析 CORS 允许的来源列表（逗号分隔），过滤空字符串
    allowed_origins = [item.strip() for item in settings.cors_allow_origins.split(",") if item.strip()]
    # 添加 CORS 中间件，允许跨域请求
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,  # 允许的来源白名单
        allow_credentials=True,         # 允许携带 Cookie 等凭证
        allow_methods=["*"],            # 允许所有 HTTP 方法
        allow_headers=["*"],            # 允许所有请求头
    )
    # 添加链路追踪中间件（在请求头中注入/提取 trace_id）
    app.add_middleware(TraceIdMiddleware)
    # 添加 Prometheus 指标中间件（记录请求数、延迟等）
    app.add_middleware(MetricsMiddleware)
    # 注册聚合后的 API 路由，并加上 /api/v1 前缀
    app.include_router(api_router, prefix=settings.app_api_prefix)
    # 注册全局异常处理器（将业务异常转为标准 JSON 响应）
    register_exception_handlers(app)
    return app


# 模块级应用实例，供 uvicorn 等 ASGI 服务器直接引用
app = create_app()
