"""图存储工厂。

按配置选择实现：
- graph_store_provider = "memory" → InMemoryGraphStore（显式选择：隔离测试或本地演示）
- graph_store_provider = "neo4j"  → Neo4jGraphStore（需 neo4j 驱动与可用实例）

关键约束：配置为 neo4j 但驱动缺失或实例不可用时，**不得**退到内存并报告成功。
那样会产生「批次标记 searchable、进程重启后图数据消失」的假发布。
因此这种情况返回 UnavailableGraphStore：写入直接失败并记录原因，
问答侧读取则得到 unavailable 从而降级到纯文档检索。
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.graph.errors import GraphUnavailableError
from app.graph.memory_store import InMemoryGraphStore
from app.graph.types import GraphRelationRecord

logger = logging.getLogger(__name__)


class UnavailableGraphStore:
    """占位存储：表示「期望的持久存储当前不可用」。

    行为准则：
    - probe() 返回 False，检索侧据此返回 unavailable 并降级；
    - 所有写操作抛 GraphUnavailableError，发布路径据此置 publish_failed，
      绝不把数据写进进程内存后报告成功。
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def upsert_batch(self, batch_uuid: str, relations: list[GraphRelationRecord]) -> None:
        raise GraphUnavailableError(self.reason)

    def replace_batch(self, batch_uuid: str, relations: list[GraphRelationRecord]) -> None:
        raise GraphUnavailableError(self.reason)

    def remove_batch(self, batch_uuid: str) -> None:
        raise GraphUnavailableError(self.reason)

    def expand(self, entity_keys: list[str], limit: int = 50) -> list[GraphRelationRecord]:
        raise GraphUnavailableError(self.reason)

    def batch_relations(self, batch_uuid: str) -> list[GraphRelationRecord]:
        raise GraphUnavailableError(self.reason)

    def relations_at_chunks(
        self, batch_uuid: str, chunk_refs: list[tuple[str, int]]
    ) -> list[GraphRelationRecord]:
        raise GraphUnavailableError(self.reason)

    def probe(self) -> bool:
        return False


_instance: Any | None = None
_degraded_reason: str | None = None


def _build(settings) -> tuple[Any, str | None]:
    provider = (getattr(settings, "graph_store_provider", "memory") or "memory").lower()

    if provider == "memory":
        # 显式选择内存：用于隔离测试或明确的本地演示，不算降级
        return InMemoryGraphStore(), None

    if provider == "neo4j":
        try:
            from neo4j import GraphDatabase  # type: ignore[import-not-found]
        except ImportError as exc:
            reason = f"neo4j driver is not installed: {exc}"
            return UnavailableGraphStore(reason), reason
        try:
            driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            store = _build_adapter(driver, settings.neo4j_database)
        except Exception as exc:  # noqa: BLE001
            reason = f"neo4j init failed: {exc}"
            return UnavailableGraphStore(reason), reason
        if not store.probe():
            reason = "neo4j probe failed"
            return UnavailableGraphStore(reason), reason
        return store, None

    reason = f"unknown graph_store_provider: {provider}"
    return UnavailableGraphStore(reason), reason


def _build_adapter(driver, database: str):
    from app.graph.neo4j_adapter import Neo4jGraphStore

    return Neo4jGraphStore(driver, database=database)


def get_graph_store() -> Any:
    """返回进程级图存储实例。"""
    global _instance, _degraded_reason
    if _instance is None:
        settings = get_settings()
        _instance, _degraded_reason = _build(settings)
        if _degraded_reason:
            logger.error("graph store unavailable: %s", _degraded_reason)
    return _instance


def get_store_degraded_reason() -> str | None:
    """不可用原因。非 None 时不得进行图发布。"""
    return _degraded_reason


def is_store_usable() -> bool:
    """图存储是否真正可用（不降级到内存）。"""
    store = get_graph_store()
    try:
        return bool(store.probe())
    except Exception:  # noqa: BLE001
        return False


def reset_graph_store() -> None:
    """测试用：重置单例。"""
    global _instance, _degraded_reason
    _instance = None
    _degraded_reason = None


def set_graph_store(store: Any) -> None:
    """测试用：注入替身。"""
    global _instance, _degraded_reason
    _instance = store
    _degraded_reason = None


__all__ = [
    "get_graph_store",
    "get_store_degraded_reason",
    "is_store_usable",
    "reset_graph_store",
    "set_graph_store",
    "UnavailableGraphStore",
]
