"""Neo4j 图存储适配器。

实现 GraphStore 接口。依赖 neo4j 官方驱动；驱动未安装或连接失败时由
GraphStoreFactory 降级处理，并在探针中暴露不可用状态。

图模型
------
业务关系与来源证据分离，避免不同批次的同一关系互相覆盖：

- 业务关系：`(s:GraphEntity)-[r:REL_TYPE]->(t:GraphEntity)`
  身份只有两端实体 + 关系类型，共享同一业务事实。
- 来源证据：`(s)-[ev:SOURCE_OF {batch_uuid, doc_uuid, chunk_uuid}]->(t)`
  每条证据边携带批次与原文定位。同一业务关系可挂多条来源证据。

关系类型大小写约定
------------------
业务边的类型为**大写**（如 `HANDLED_BY`），证据边的 `relation_type` 属性与
受控枚举一致保持**小写**（如 `handled_by`）。读取时用
`toLower(ev.relation_type) = toLower(type(r))` 归一化比较，
避免两侧大小写不一致导致恒不匹配、扩展查询返回空结果。

事务边界
--------
批次替换必须原子完成：删除该批次证据边与写入新证据边在**同一个显式事务**内。
Neo4j 驱动提供两种方式，本实现用 `session.execute_write()`（托管式事务，
由驱动负责重试与提交/回滚），不使用「多次 session.run 共用一个 session」
——那不构成原子操作。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.graph.errors import GraphUnavailableError
from app.graph.types import (
    ALLOWED_RELATION_SHAPE,
    GraphRelationRecord,
)

logger = logging.getLogger(__name__)

# 证据边的固定类型。证据身份 = batch + doc + chunk，同一业务关系可挂多条。
EVIDENCE_RELATION = "SOURCE_OF"

# 删除某批次证据边的语句。
#
# 只删除该批次的证据边，**不清理共享端点**：一个业务关系可能由多个批次共同支持，
# 删除证据后端点仍被业务边连接，此时 DELETE 节点会违反 Neo4j 约束。
# 孤立实体的清理由独立的清理流程处理，不在批次替换中顺带执行。
_DELETE_BATCH_EVIDENCE = (
    "MATCH ()-[ev:SOURCE_OF]->() WHERE ev.batch_uuid = $batch_uuid DELETE ev"
)

# 读取单批次全部证据边的公共返回列。
_EVIDENCE_RETURN_CLAUSE = """
    RETURN ev.relation_type AS rel_type,
           s.key AS source_key,
           s.entity_type AS source_type,
           s.name AS source_name,
           t.key AS target_key,
           t.entity_type AS target_type,
           t.name AS target_name,
           ev.batch_uuid AS batch_uuid,
           ev.source_fingerprint AS source_fingerprint,
           ev.doc_uuid AS doc_uuid,
           ev.chunk_uuid AS chunk_uuid,
           ev.chunk_index AS chunk_index,
           ev.evidence_text AS evidence_text,
           ev.evidence_section AS evidence_section,
           ev.evidence_page_no AS evidence_page_no,
           ev.applicability AS applicability,
           ev.applicability_known AS applicability_known
"""


def _relation_type_to_cypher(relation_type: str) -> str:
    """受控枚举内的关系类型才允许拼接进 Cypher，并统一为大写业务边类型。

    严格白名单校验，杜绝把模型输出直接拼进查询。
    """
    if relation_type not in ALLOWED_RELATION_SHAPE:
        raise ValueError(f"unsupported relation type: {relation_type}")
    return relation_type.upper()


class Neo4jGraphStore:
    """Neo4j 适配器。"""

    def __init__(self, driver: Any, database: str = "neo4j") -> None:
        self._driver = driver
        self._database = database

    # -- 写入 ---------------------------------------------------------------

    def upsert_batch(self, batch_uuid: str, relations: list[GraphRelationRecord]) -> None:
        """幂等写入：MERGE 语义，重复执行不产生重复证据边。"""
        self._write_transaction(batch_uuid, relations, delete_existing=False)

    def replace_batch(self, batch_uuid: str, relations: list[GraphRelationRecord]) -> None:
        """以批次为单位整体替换。删除与写入在同一显式事务内完成。"""
        self._write_transaction(batch_uuid, relations, delete_existing=True)

    def _write_transaction(
        self, batch_uuid: str, relations: list[GraphRelationRecord], *, delete_existing: bool
    ) -> None:
        """在单个托管式事务内执行「（可选）删除 + 全部写入」。

        使用 session.execute_write：驱动保证整段回调要么全部提交、要么全部回滚，
        避免出现「旧证据已删、新证据未写」的中间态被并发检索读到。
        """
        queries = [
            (self._merge_relation_query(relation), _params(relation, batch_uuid))
            for relation in relations
        ]

        def _work(tx):
            if delete_existing:
                tx.run(_DELETE_BATCH_EVIDENCE, batch_uuid=batch_uuid)
            for query, params in queries:
                tx.run(query, **params)

        try:
            with self._driver.session(database=self._database) as session:
                session.execute_write(_work)
        except Exception as exc:  # noqa: BLE001 - 统一转为可降级的可用性故障
            logger.warning("neo4j write_transaction failed: %s", exc)
            raise GraphUnavailableError(f"neo4j write failed: {exc}") from exc

    def remove_batch(self, batch_uuid: str) -> None:
        """移除整个批次的证据数据。仅用于物理清理，不改变业务库发布状态。"""
        try:
            with self._driver.session(database=self._database) as session:
                session.execute_write(
                    lambda tx: tx.run(_DELETE_BATCH_EVIDENCE, batch_uuid=batch_uuid)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("neo4j remove_batch failed: %s", exc)
            raise GraphUnavailableError(f"neo4j remove failed: {exc}") from exc

    # -- 读取 ---------------------------------------------------------------

    def expand(self, entity_keys: list[str], limit: int = 50) -> list[GraphRelationRecord]:
        """单跳邻接扩展。

        同一业务关系可能有多条来源证据，按 (批次, 分块) 去重后返回，
        避免同一事实在结果中出现重复行；多来源互相印证则各占一行。
        """
        if not entity_keys:
            return []
        query = (
            "MATCH (s:GraphEntity)-[r]-(t:GraphEntity) "
            "WHERE type(r) <> $evidence_relation "
            "AND (s.key IN $keys OR t.key IN $keys) "
            "MATCH (s)-[ev:SOURCE_OF]->(t) "
            # 大小写归一化比较：业务边类型为大写，证据属性为小写
            "WHERE toLower(ev.relation_type) = toLower(type(r)) "
            + _EVIDENCE_RETURN_CLAUSE
            + " LIMIT $limit"
        )
        return self._read(
            query,
            {
                "keys": [str(key) for key in entity_keys],
                "limit": int(limit),
                "evidence_relation": EVIDENCE_RELATION,
            },
            dedupe=True,
        )

    def batch_relations(self, batch_uuid: str) -> list[GraphRelationRecord]:
        """读取某批次的全部证据边。锚点解析的正式接口。"""
        query = (
            "MATCH (s:GraphEntity)-[ev:SOURCE_OF]->(t:GraphEntity) "
            "WHERE ev.batch_uuid = $batch_uuid "
            + _EVIDENCE_RETURN_CLAUSE
        )
        return self._read(query, {"batch_uuid": batch_uuid}, dedupe=False)

    def relations_at_chunks(
        self, batch_uuid: str, chunk_refs: list[tuple[str, int]]
    ) -> list[GraphRelationRecord]:
        """只读取指定分块上的证据边，把锚点范围收敛到命中分块。"""
        if not chunk_refs:
            return []
        query = (
            "MATCH (s:GraphEntity)-[ev:SOURCE_OF]->(t:GraphEntity) "
            "WHERE ev.batch_uuid = $batch_uuid "
            "AND ev.doc_uuid IN $doc_uuids "
            "AND ev.chunk_index IN $chunk_indexes "
            + _EVIDENCE_RETURN_CLAUSE
        )
        return self._read(
            query,
            {
                "batch_uuid": batch_uuid,
                "doc_uuids": [str(doc_uuid) for doc_uuid, _ in chunk_refs],
                "chunk_indexes": [int(index) for _, index in chunk_refs],
            },
            dedupe=False,
        )

    def _read(self, query: str, params: dict, *, dedupe: bool) -> list[GraphRelationRecord]:
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run(query, **params)
                relations = [_row_to_relation(record) for record in result]
                return _dedupe_relations(relations) if dedupe else relations
        except Exception as exc:  # noqa: BLE001
            logger.warning("neo4j read failed: %s", exc)
            raise GraphUnavailableError(f"neo4j read failed: {exc}") from exc

    def probe(self) -> bool:
        try:
            with self._driver.session(database=self._database) as session:
                session.run("RETURN 1").consume()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("neo4j probe failed: %s", exc)
            return False

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _merge_relation_query(relation: GraphRelationRecord) -> str:
        rel_type = _relation_type_to_cypher(relation.relation_type)
        return f"""
            MERGE (s:GraphEntity {{key: $source_key}})
              ON CREATE SET s.entity_type = $source_type, s.name = $source_name
            MERGE (t:GraphEntity {{key: $target_key}})
              ON CREATE SET t.entity_type = $target_type, t.name = $target_name
            MERGE (s)-[r:{rel_type}]->(t)
            MERGE (s)-[ev:SOURCE_OF {{
              batch_uuid: $batch_uuid,
              doc_uuid: $doc_uuid,
              chunk_uuid: $chunk_uuid
            }}]->(t)
            SET ev.relation_type = $relation_type,
                ev.source_fingerprint = $source_fingerprint,
                ev.chunk_index = $chunk_index,
                ev.evidence_text = $evidence_text,
                ev.evidence_section = $evidence_section,
                ev.evidence_page_no = $evidence_page_no,
                ev.applicability = $applicability,
                ev.applicability_known = $applicability_known
        """


def _dedupe_relations(relations: list[GraphRelationRecord]) -> list[GraphRelationRecord]:
    """按 (批次, 分块, 关系类型, 两端实体) 去重。

    同一业务关系可能有多条来源证据；多来源互相印证时各占一行是合理的，
    但同一来源不应重复出现。
    """
    seen: set[tuple[str, str, int, str, str, str]] = set()
    unique: list[GraphRelationRecord] = []
    for relation in relations:
        marker = (
            relation.batch_uuid,
            str(relation.evidence.doc_uuid),
            int(relation.evidence.chunk_index),
            relation.relation_type,
            relation.source_key,
            relation.target_key,
        )
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(relation)
    return unique


def _params(relation: GraphRelationRecord, batch_uuid: str) -> dict[str, Any]:
    evidence = relation.evidence
    return {
        "batch_uuid": batch_uuid,
        "relation_type": relation.relation_type,
        "source_key": relation.source_key,
        "source_type": relation.source_type,
        "source_name": relation.source_name,
        "target_key": relation.target_key,
        "target_type": relation.target_type,
        "target_name": relation.target_name,
        "source_fingerprint": relation.source_fingerprint,
        "doc_uuid": str(evidence.doc_uuid),
        "chunk_uuid": str(evidence.chunk_uuid),
        "chunk_index": int(evidence.chunk_index),
        "evidence_text": evidence.evidence_text,
        "evidence_section": evidence.section_title,
        "evidence_page_no": evidence.page_no,
        "applicability": relation.applicability,
        "applicability_known": bool(relation.applicability_known),
    }


def _row_to_relation(record: Any) -> GraphRelationRecord:
    from app.graph.types import EvidenceRef

    applicability = record.get("applicability")
    if isinstance(applicability, str) and applicability:
        try:
            decoded = json.loads(applicability)
            applicability = decoded if isinstance(decoded, str) else None
        except json.JSONDecodeError:
            pass

    evidence = EvidenceRef(
        doc_uuid=record.get("doc_uuid") or "",
        chunk_uuid=record.get("chunk_uuid") or "",
        chunk_index=int(record.get("chunk_index") or 0),
        evidence_text=record.get("evidence_text") or "",
        section_title=record.get("evidence_section"),
        page_no=record.get("evidence_page_no"),
    )
    return GraphRelationRecord(
        # Cypher 业务边类型为大写，这里归一化回受控枚举的小写形式
        relation_type=(record.get("rel_type") or "").lower(),
        source_key=record.get("source_key") or "",
        source_type=record.get("source_type") or "",
        source_name=record.get("source_name") or "",
        target_key=record.get("target_key") or "",
        target_type=record.get("target_type") or "",
        target_name=record.get("target_name") or "",
        evidence=evidence,
        batch_uuid=record.get("batch_uuid") or "",
        source_fingerprint=record.get("source_fingerprint") or "",
        applicability=applicability,
        applicability_known=bool(record.get("applicability_known")),
    )
