"""图检索的实体锚点确定。

第一版采用「复用既有向量检索结果」的路线：
问句 → Dense 检索命中文档片段 → 从命中的**分块**所属已发布批次关系中取实体 →
单跳扩展。这样把「问句→实体」的语义匹配交给已上线的向量检索，
图检索只负责「实体→关系」的图遍历，职责清晰且不重复造轮子。

为什么不自己做问句匹配：纯字符串启发式（子串/字符二元组）在中文短实体名上
误召回与漏召回都明显，属于未验证的做法，因此不采用。

读取能力来自 GraphStore 正式接口（relations_at_chunks），不依赖任何
具体存储实现的专有方法。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.repositories.graph_batches import GraphBatchRepository
from app.services.eligibility import apply_document_eligibility

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AnchorContext:
    """一次图检索可用的实体锚点上下文。

    status 明确区分四种情况，避免上层把存储故障当成「没有匹配关系」：
    - ok: 正常解析
    - empty: 已启用且可读，但确实没有可用锚点
    - disabled: 图功能未启用
    - unavailable: 存储不可用或读取失败（可降级）
    """

    entity_keys: list[str] = field(default_factory=list)
    doc_uuids: set = field(default_factory=set)
    chunk_refs: list[tuple[str, int]] = field(default_factory=list)
    status: str = "ok"
    message: str | None = None

    @property
    def has_anchors(self) -> bool:
        return bool(self.entity_keys)

    @property
    def is_degraded(self) -> bool:
        return self.status == "unavailable"

    @classmethod
    def empty_result(cls, *, status: str = "empty", message: str | None = None, **kwargs):
        return cls(status=status, message=message, **kwargs)


class GraphAnchorResolver:
    """从向量检索命中的分块出发，收集可用于图扩展的实体锚点。

    只读取已发布（searchable）批次的关系所涉及的实体，
    未审核、已失效批次不会成为锚点来源。
    """

    def __init__(self, session: Session, store=None) -> None:
        self.session = session
        self.batches = GraphBatchRepository(session)
        self._store = store

    def _resolve_store(self):
        if self._store is not None:
            return self._store
        from app.graph.factory import get_graph_store

        return get_graph_store()

    def resolve(
        self,
        *,
        citation_chunks: list[tuple] | None = None,
        anchor_keys: list[str] | None = None,
        settings=None,
    ) -> AnchorContext:
        """确定本次图扩展的实体锚点。

        参数
        ----
        citation_chunks: 既有文档检索命中的 (doc_uuid, chunk_uuid, chunk_index) 列表。
                         chunk_index 可能为 None，此时退化为按文档取全部关系。
        anchor_keys: 调用方显式提供的实体键（可选，优先级最高）
        """
        if anchor_keys:
            return AnchorContext(
                entity_keys=list(dict.fromkeys(anchor_keys)),
                status="ok",
            )

        refs = self._normalise_chunk_refs(citation_chunks or [])
        if not refs:
            return AnchorContext.empty_result(
                status="empty", message="no retrievable chunk hit for anchor resolution"
            )

        # 只保留当前仍可检索的文档，撤回/删除/待审文档不提供锚点
        eligible = self._eligible_doc_uuids({doc_uuid for doc_uuid, _ in refs})
        if not eligible:
            return AnchorContext.empty_result(
                status="empty", message="no retrievable document in retrieval hits"
            )

        usable_refs = [
            (str(doc_uuid), index)
            for doc_uuid, index in refs
            if doc_uuid in eligible
        ]
        if not usable_refs:
            return AnchorContext.empty_result(
                status="empty",
                message="no retrievable chunk in retrieval hits",
                doc_uuids=eligible,
            )

        active_map = self.batches.list_searchable_batch_uuids()
        active_batch_uuids = {str(batch_uuid) for batch_uuid in active_map.values()}
        if not active_batch_uuids:
            return AnchorContext.empty_result(
                status="empty",
                message="no published graph batch available",
                doc_uuids=eligible,
                chunk_refs=usable_refs,
            )

        try:
            store = self._resolve_store()
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph anchor store resolve failed: %s", exc)
            return AnchorContext.empty_result(
                status="unavailable",
                message=f"graph store resolve failed: {exc}",
                doc_uuids=eligible,
                chunk_refs=usable_refs,
            )

        try:
            usable = store.probe()
        except Exception as exc:  # noqa: BLE001
            usable = False
            probe_error: str | None = str(exc)
        else:
            probe_error = None
        if not usable:
            # 存储不可用：透传为 unavailable，不能与「没有匹配关系」混淆
            reason = (
                f"graph store probe failed: {probe_error}"
                if probe_error
                else "graph store probe failed"
            )
            return AnchorContext.empty_result(
                status="unavailable",
                message=reason,
                doc_uuids=eligible,
                chunk_refs=usable_refs,
            )

        relations_reader = getattr(store, "relations_at_chunks", None)
        if not callable(relations_reader):
            # 接口未实现属契约缺陷，不静默返回空锚点
            logger.error("graph store does not implement relations_at_chunks")
            return AnchorContext.empty_result(
                status="unavailable",
                message="graph store does not implement relations_at_chunks",
                doc_uuids=eligible,
                chunk_refs=usable_refs,
            )

        # index=-1 表示退化模式：该文档未提供分块信息，按文档取全部关系
        has_degraded = any(index < 0 for _, index in usable_refs)
        if has_degraded:
            fallback_reader = getattr(store, "batch_relations", None)
            if not callable(fallback_reader):
                logger.error("graph store does not implement batch_relations")
                return AnchorContext.empty_result(
                    status="unavailable",
                    message="graph store does not implement batch_relations",
                    doc_uuids=eligible,
                    chunk_refs=usable_refs,
                )
            reader = lambda batch_uuid, _refs: fallback_reader(batch_uuid)  # noqa: E731
        else:
            reader = relations_reader

        keys: list[str] = []
        # eligible 存 UUID 对象，比较时统一归一化为字符串
        eligible_str = {str(item) for item in eligible}
        exact_refs = {
            (str(doc_uuid), int(index))
            for doc_uuid, index in usable_refs
            if index >= 0
        }
        read_failures = 0
        for batch_uuid in sorted(active_batch_uuids):
            try:
                relations = reader(batch_uuid, usable_refs)
            except Exception as exc:  # noqa: BLE001
                # 读取失败必须被记录为故障，不能静默当作「该批次没有关系」
                logger.warning("relations_at_chunks failed for %s: %s", batch_uuid, exc)
                read_failures += 1
                continue
            for relation in relations:
                rel_doc = str(relation.evidence.doc_uuid)
                if rel_doc not in eligible_str:
                    continue
                if not has_degraded:
                    # 精确分块模式：只保留命中分块上的关系
                    if (rel_doc, int(relation.evidence.chunk_index)) not in exact_refs:
                        continue
                for key in (relation.source_key, relation.target_key):
                    if key and key not in keys:
                        keys.append(key)

        # 读取失败时不返回空锚点：如实标记为 unavailable，由上层降级
        if read_failures:
            return AnchorContext(
                status="unavailable",
                message=f"failed to read graph relations for {read_failures} batch(es)",
                doc_uuids=eligible,
                chunk_refs=usable_refs,
            )

        status = "ok" if keys else "empty"
        message = None if keys else "no entity anchor found in published batches"
        return AnchorContext(
            entity_keys=keys,
            doc_uuids=eligible,
            chunk_refs=usable_refs,
            status=status,
            message=message,
        )

    # -- 内部 ---------------------------------------------------------------

    def _normalise_chunk_refs(self, citation_chunks: list[tuple]) -> list[tuple]:
        """把 citation 分块信息规整为 (doc_uuid, chunk_index)。

        兼容两种入参：
        - (doc_uuid, chunk_index) 二元组
        - (doc_uuid, chunk_uuid, chunk_index) 三元组

        chunk_index 缺失时无法收敛到具体分块，退化为「整篇文档取全部关系」
        （用 index=-1 标记），并保留可在调用方观察的退化信号。
        """
        from uuid import UUID

        refs: list[tuple] = []
        saw_missing_index = False
        docs: set = set()

        for item in citation_chunks:
            if not item:
                continue
            raw_doc = item[0]
            raw_index = item[1] if len(item) == 2 else (item[2] if len(item) > 2 else None)
            try:
                doc_uuid = raw_doc if isinstance(raw_doc, UUID) else UUID(str(raw_doc))
            except (TypeError, ValueError):
                continue
            docs.add(doc_uuid)
            if raw_index is None:
                saw_missing_index = True
                continue
            refs.append((doc_uuid, int(raw_index)))

        if saw_missing_index and not refs:
            # 全部缺失 chunk_index：退化为按文档维度取锚点
            return [(doc_uuid, -1) for doc_uuid in docs]
        return refs

    def _eligible_doc_uuids(self, doc_uuids: set) -> set:
        from uuid import UUID

        keys = []
        for item in doc_uuids:
            keys.append(item if isinstance(item, UUID) else UUID(str(item)))
        if not keys:
            return set()
        stmt = apply_document_eligibility(select(Document).where(Document.doc_uuid.in_(keys)))
        return {doc.doc_uuid for doc in self.session.scalars(stmt).all()}

    @staticmethod
    def load_citation_chunks(citations: list) -> list[tuple]:
        """从 Citation 列表提取 (doc_uuid, chunk_uuid, chunk_index)。

        文档检索构造的 Citation 目前不填 chunk_index，因此这里通常得到
        (doc_uuid, chunk_uuid, None)，锚点会退化为文档维度。
        该退化是有意为之并已在文档中说明，后续应让文档检索填充 chunk_index。
        """
        refs: list[tuple] = []
        for citation in citations:
            doc_uuid = getattr(citation, "doc_uuid", None)
            if not doc_uuid:
                continue
            refs.append(
                (
                    doc_uuid,
                    getattr(citation, "chunk_uuid", None),
                    getattr(citation, "chunk_index", None),
                )
            )
        return refs


def load_chunk_indexes(session: Session, refs: list[tuple]) -> list[tuple]:
    """按 (doc_uuid, chunk_uuid) 批量补齐 chunk_index。

    文档检索的 Citation 未携带 chunk_index，但 chunk_uuid 可唯一定位分块，
    因此可以在锚点解析阶段从业务库补齐，从而把锚点收敛到真实分块。
    """
    from uuid import UUID

    pairs: list[tuple] = []
    for item in refs:
        if len(item) < 2 or not item[1]:
            continue
        try:
            doc_uuid = item[0] if isinstance(item[0], UUID) else UUID(str(item[0]))
        except (TypeError, ValueError):
            continue
        try:
            chunk_uuid = item[1] if isinstance(item[1], UUID) else UUID(str(item[1]))
        except (TypeError, ValueError):
            continue
        pairs.append((doc_uuid, chunk_uuid))

    if not pairs:
        return []

    doc_uuids = {doc_uuid for doc_uuid, _ in pairs}
    chunk_uuids = {chunk_uuid for _, chunk_uuid in pairs}
    rows = session.execute(
        select(DocumentChunk.doc_uuid, DocumentChunk.chunk_uuid, DocumentChunk.chunk_index).where(
            DocumentChunk.doc_uuid.in_(doc_uuids),
            DocumentChunk.chunk_uuid.in_(chunk_uuids),
            DocumentChunk.deleted_at.is_(None),
        )
    ).all()
    index_map = {(row[0], row[1]): row[2] for row in rows}

    result: list[tuple] = []
    for doc_uuid, chunk_uuid in pairs:
        chunk_index = index_map.get((doc_uuid, chunk_uuid))
        if chunk_index is not None:
            result.append((doc_uuid, chunk_index))
    return result
