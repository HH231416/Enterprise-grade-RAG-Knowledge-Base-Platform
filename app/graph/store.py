"""图谱存储接口。

GraphStore 只负责「已批准批次的图数据写入与关系扩展读取」，不负责审核与发布决策。
发布可见性由业务库的生效批次指针决定，不由存储层自证。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.graph.types import GraphRelationRecord


@runtime_checkable
class GraphStore(Protocol):
    """图存储适配接口。

    实现方：Neo4jAdapter（生产）、InMemoryGraphStore（隔离测试替身）。
    """

    def upsert_batch(self, batch_uuid: str, relations: list[GraphRelationRecord]) -> None:
        """将一个已批准批次的全部关系写入图存储。

        必须幂等：同一 batch_uuid 重复写入不产生重复关系。
        """
        ...

    def replace_batch(self, batch_uuid: str, relations: list[GraphRelationRecord]) -> None:
        """以批次为单位整体替换，先清除该批次既有关系再写入。"""
        ...

    def remove_batch(self, batch_uuid: str) -> None:
        """移除整个批次的图数据。仅用于物理清理，不改变业务库发布状态。"""
        ...

    def expand(self, entity_keys: list[str], limit: int = 50) -> list[GraphRelationRecord]:
        """按实体键做单跳邻接扩展。

        只返回调用方显式传入的 active_batches 范围内的关系由实现方自行约束；
        调用方必须先校验批次有效性。
        """
        ...

    def batch_relations(self, batch_uuid: str) -> list[GraphRelationRecord]:
        """读取某个批次已写入的全部关系。

        锚点解析需要「某批次包含哪些实体」这一读取能力。必须由接口正式声明，
        不能依赖具体实现的专有方法——否则换用其他存储实现时锚点会静默为空。
        """
        ...

    def relations_at_chunks(
        self, batch_uuid: str, chunk_refs: list[tuple[str, int]]
    ) -> list[GraphRelationRecord]:
        """读取某批次中指定分块（doc_uuid, chunk_index）上的关系。

        用于把锚点范围收敛到检索命中的具体分块，而不是整篇文档的所有实体。
        """
        ...

    def probe(self) -> bool:
        """可用性探测。False 表示不可用，调用方应降级到纯文档检索。"""
        ...
