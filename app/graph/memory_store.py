"""内存图存储：隔离测试替身与本地降级替身。

不连接任何外部服务。用于本地测试验证发布、失效、单跳扩展与降级语义。
"""
from __future__ import annotations

from threading import RLock

from app.graph.errors import GraphUnavailableError
from app.graph.types import GraphRelationRecord


class InMemoryGraphStore:
    """线程安全的内存图存储。

    按 batch_uuid 分区保存关系，保证 replace/remove 不影响其他有效批次。
    """

    def __init__(self) -> None:
        self._batches: dict[str, list[GraphRelationRecord]] = {}
        self._lock = RLock()
        self.available = True

    # -- 写入 ---------------------------------------------------------------

    def upsert_batch(self, batch_uuid: str, relations: list[GraphRelationRecord]) -> None:
        """幂等写入：同一批次重复写入不产生重复关系。"""
        self._ensure_available()
        with self._lock:
            existing = {r.relation_id: r for r in self._batches.get(batch_uuid, [])}
            for relation in relations:
                existing[relation.relation_id] = relation
            self._batches[batch_uuid] = list(existing.values())

    def replace_batch(self, batch_uuid: str, relations: list[GraphRelationRecord]) -> None:
        self._ensure_available()
        with self._lock:
            deduped: dict[str, GraphRelationRecord] = {}
            for relation in relations:
                deduped[relation.relation_id] = relation
            self._batches[batch_uuid] = list(deduped.values())

    def remove_batch(self, batch_uuid: str) -> None:
        self._ensure_available()
        with self._lock:
            self._batches.pop(batch_uuid, None)

    # -- 读取 ---------------------------------------------------------------

    def expand(self, entity_keys: list[str], limit: int = 50) -> list[GraphRelationRecord]:
        """单跳邻接扩展。只返回端点与传入实体键精确相等的关系。

        实体锚点由调用方通过向量检索确定后传入，此处只做等值匹配，
        不引入模糊匹配，避免无关实体被误召回。
        """
        self._ensure_available()
        keys = set(entity_keys)
        with self._lock:
            found: list[GraphRelationRecord] = []
            seen: set[str] = set()
            for relations in self._batches.values():
                for relation in relations:
                    if relation.source_key in keys or relation.target_key in keys:
                        marker = f"{relation.batch_uuid}|{relation.relation_id}"
                        if marker in seen:
                            continue
                        seen.add(marker)
                        found.append(relation)
                        if len(found) >= limit:
                            return found
            return found

    def batch_relations(self, batch_uuid: str) -> list[GraphRelationRecord]:
        self._ensure_available()
        with self._lock:
            return list(self._batches.get(batch_uuid, []))

    def relations_at_chunks(
        self, batch_uuid: str, chunk_refs: list[tuple[str, int]]
    ) -> list[GraphRelationRecord]:
        """只返回指定分块上的关系，用于把锚点范围收敛到命中分块。"""
        self._ensure_available()
        wanted = {(str(doc_uuid), int(index)) for doc_uuid, index in chunk_refs}
        if not wanted:
            return []
        with self._lock:
            return [
                relation
                for relation in self._batches.get(batch_uuid, [])
                if (str(relation.evidence.doc_uuid), int(relation.evidence.chunk_index)) in wanted
            ]

    def probe(self) -> bool:
        return self.available

    def set_available(self, available: bool) -> None:
        """测试用：模拟图存储故障。"""
        self.available = available

    def clear(self) -> None:
        with self._lock:
            self._batches.clear()

    # -- 内部 ---------------------------------------------------------------

    def _ensure_available(self) -> None:
        if not self.available:
            raise GraphUnavailableError()
