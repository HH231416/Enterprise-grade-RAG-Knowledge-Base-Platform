# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from collections.abc import Generator  # Generator 类型：用于 get_db_session 生成器的类型标注

from sqlalchemy import create_engine   # 创建数据库引擎
from sqlalchemy.engine import Engine   # 引擎类型
from sqlalchemy.orm import Session, sessionmaker  # 会话类型和会话工厂

from app.core.config import get_settings  # 配置单例获取函数

# 模块级全局变量：数据库引擎（懒加载单例）
_engine: Engine | None = None
# 模块级全局变量：会话工厂（懒加载单例）
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """获取（或懒加载创建）SQLAlchemy 数据库引擎。

    支持 SQLite（开发环境）和 PostgreSQL（生产环境），自动配置连接池参数。
    """
    global _engine  # 声明使用模块级全局变量
    if _engine is None:  # 懒加载：未创建时才新建
        settings = get_settings()                        # 获取配置单例
        database_url = settings.sqlalchemy_database_url  # 拼接好的数据库连接 URL
        # SQLite 需要 check_same_thread=False（FastAPI 多线程访问）
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        engine_kwargs = {
            "future": True,               # 使用 SQLAlchemy 2.0 风格
            "pool_pre_ping": True,        # 每次从池中取出连接前先 ping 测试有效性
            "connect_args": connect_args, # 底层驱动连接参数
        }
        if not database_url.startswith("sqlite"):  # PostgreSQL 需要配置连接池
            engine_kwargs.update(
                {
                    "pool_size": settings.db_pool_size,                      # 连接池大小
                    "max_overflow": settings.db_max_overflow,                # 最大溢出连接数
                    "pool_timeout": settings.db_pool_timeout_seconds,        # 获取连接超时
                    "pool_recycle": settings.db_pool_recycle_seconds,        # 连接回收时间
                }
            )
        _engine = create_engine(database_url, **engine_kwargs)  # 创建引擎
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """获取（或懒加载创建）SQLAlchemy 会话工厂。"""
    global _session_factory
    if _session_factory is None:  # 懒加载
        _session_factory = sessionmaker(
            bind=get_engine(),      # 绑定到引擎
            autoflush=False,        # 禁止自动 flush（手动控制事务边界）
            autocommit=False,       # 禁止自动提交
            future=True,            # SQLAlchemy 2.0 风格
            class_=Session,         # 使用标准 Session 类
        )
    return _session_factory


def get_db_session() -> Generator[Session, None, None]:
    """获取数据库会话的生成器函数（用于 FastAPI 依赖注入）。

    使用 yield 模式：请求开始时创建会话，请求结束后自动关闭，
    配合 FastAPI 的 Depends 实现会话生命周期管理。
    """
    session = get_session_factory()()  # 创建新会话
    try:
        yield session                   # 将会话注入到路由函数
    finally:
        session.close()                 # 请求结束后关闭会话，归还连接


def reset_db_runtime() -> None:
    """重置数据库运行时状态（用于测试环境清理）。

    释放引擎的所有连接并清空全局引用，下次调用 get_engine 会重新创建。
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()  # 释放引擎的所有连接池连接
    _engine = None         # 清空引擎引用
    _session_factory = None  # 清空会话工厂引用


def get_db_pool_status() -> dict[str, int | str]:
    """获取数据库连接池状态信息，供健康检查和 Grafana 仪表盘使用。

    返回包含 pool_class、size、checkedout、overflow、available 的字典。
    """
    pool = get_engine().pool                      # 获取连接池对象
    result: dict[str, int | str] = {"pool_class": type(pool).__name__}  # 连接池类型名
    for name in ("size", "checkedout", "overflow"):  # 遍历三个关键指标
        method = getattr(pool, name, None)            # 反射获取方法
        if callable(method):                          # 可调用则执行
            result[name] = int(method())              # 转为整数
    # 计算可用连接数 = 池大小 - 已检出数（确保非负）
    if "size" in result and "checkedout" in result:
        result["available"] = max(0, int(result["size"]) - int(result["checkedout"]))
    return result
