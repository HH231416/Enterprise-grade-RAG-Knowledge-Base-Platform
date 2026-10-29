"""图关系检索。

流程：
    查询 → 实体锚点匹配 → 单跳邻接扩展 → 批次与快照校验 → 证据产出

关键约束：
- 只有业务库有效指针指向的 searchable 批次可进入在线召回。
- 先校验批次与来源快照，再按有效分块标识去重；同一分块支持的不同关系都保留。
- 图召回分只作排序信号，不参与事实成立判断。
- 故障结构化：status 区分 disabled / empty / ok / unavailable，不用 None 混淆。
- 可用性故障可降级；权限与来源完整性失败不可降级，直接抛出。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.graph.builder import GraphBuildService, is_public_document
from app.graph.errors import GraphUnavailableError
from app.graph.types import GraphRelationRecord, GraphRetrievalResult
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.graph import GraphBuildBatch
from app.repositories.graph_batches import GraphBatchRepository
from app.services.eligibility import apply_document_eligibility

logger = logging.getLogger(__name__)


def _uuid_key(value):
    """把图侧字符串 doc_uuid 归一化为 uuid.UUID，用于与数据库主键对齐。

    图证据在 Neo4j 中以字符串保存，而业务库主键是 uuid.UUID；
    不归一化会导致字典查找静默 miss，表现为「公开转非公开后仍能召回」。
    """
    from uuid import UUID

    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class ValidatedRelation:
    """通过批次与快照校验的关系，附带权威文档与分块定位。"""

    relation: GraphRelationRecord
    document: Document
    chunk_index: int
    title: str
    source_module: str
    page_no: int | None
    section_title: str | None
    version: str
    updated_at: object
    # 证据原文缺失时的兜底文本（不使用 240 字符展示摘要）
    chunk_text_fallback: str = ""


class GraphRetrievalService:
    def __init__(self, session: Session, store=None) -> None:
        self.session = session
        self.batches = GraphBatchRepository(session)
        self._store = store

    def _resolve_store(self):
        if self._store is not None:
            return self._store
        from app.graph.factory import get_graph_store

        return get_graph_store()

    def is_enabled(self, settings) -> bool:
        return bool(settings.graph_enabled and settings.graph_retrieval_enabled)

    def search(
        self,
        *,
        query: str,
        settings,
        anchor_keys: list[str] | None = None,
        limit: int | None = None,
    ) -> GraphRetrievalResult:
        """执行图检索。任何一步失败都以结构化状态返回。"""
        if not self.is_enabled(settings):
            return GraphRetrievalResult(status="disabled", message="graph retrieval is disabled")

        try:
            store = self._resolve_store()
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph store resolve failed: %s", exc)
            return GraphRetrievalResult(
                status="unavailable", message=f"graph store resolve failed: {exc}"
            )

        if not store.probe():
            return GraphRetrievalResult(
                status="unavailable", message="graph store probe failed"
            )

        keys = anchor_keys or self._anchor_entity_keys(query)
        if not keys:
            return GraphRetrievalResult(status="empty", message="no anchor entity matched")

        expand_limit = limit or int(getattr(settings, "graph_expand_limit", 50))
        try:
            raw_relations = store.expand(keys, limit=expand_limit)
        except GraphUnavailableError as exc:
            return GraphRetrievalResult(status="unavailable", message=str(exc.message))
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph expand failed: %s", exc)
            return GraphRetrievalResult(status="unavailable", message=f"graph expand failed: {exc}")

        if not raw_relations:
            return GraphRetrievalResult(status="empty", message="no relation matched")

        validated = self._validate_relations(raw_relations, expand_limit)
        if not validated:
            return GraphRetrievalResult(
                status="empty", message="relations exist but none belong to an active batch"
            )

        return GraphRetrievalResult(
            status="ok",
            relations=[item.relation for item in validated],
            validated=validated,
            message=None,
        )

    # -- 校验 ---------------------------------------------------------------

    def _validate_relations(
        self, relations: list[GraphRelationRecord], expand_limit: int
    ) -> list[ValidatedRelation]:
        """只保留属于有效批次且来源仍然有效的关。

        未被生效指针选中的批次一律丢弃，即使图存储中已存在其数据。
        """
        active_map = self.batches.list_searchable_batch_uuids()
        if not active_map:
            return []
        active_batches = {str(item) for item in active_map.values()}

        # 收集候选涉及的分块，批量回填权威文档与分块定位
        # 注意：图侧 evidence.doc_uuid 是字符串形式，而 documents 的键是 uuid.UUID，
        # 必须先归一化再查表，否则会静默 miss 掉全部关系。
        chunk_refs: set[tuple[str, int]] = set()
        for relation in relations:
            chunk_refs.add((relation.evidence.doc_uuid, relation.evidence.chunk_index))

        documents = self._load_documents({doc_uuid for doc_uuid, _ in chunk_refs})
        if not documents:
            return []
        chunk_positions = self._load_chunk_positions(documents.keys(), chunk_refs)

        validated: list[ValidatedRelation] = []
        seen: set[tuple[str, int, str, str, str]] = set()
        truncated = False

        for relation in relations:
            if len(validated) >= expand_limit:
                truncated = True
                break
            if relation.batch_uuid not in active_batches:
                continue

            document = documents.get(_uuid_key(relation.evidence.doc_uuid))
            if document is None:
                continue
            # 公开范围在召回阶段再次校验：公开转非公开后立即阻断，
            # 不等待后台物理清理。
            if not is_public_document(document):
                continue

            active_fingerprint = active_map.get(_uuid_key(relation.evidence.doc_uuid))
            expected_fingerprint = self._active_fingerprint(relation.batch_uuid)
            if expected_fingerprint is None or expected_fingerprint != relation.source_fingerprint:
                # 批次已被替换或来源指纹不一致：不作为当前证据
                continue
            if active_fingerprint is None:
                continue

            chunk_index = self._resolve_chunk_index(relation, chunk_positions)
            if chunk_index is None:
                continue

            # 去重：按 (分块, 关系类型, 两端实体) 保留，同一分块支持的不同关系都保留
            marker = (
                relation.evidence.doc_uuid,
                chunk_index,
                relation.relation_type,
                relation.source_key,
                relation.target_key,
            )
            if marker in seen:
                continue
            seen.add(marker)

            chunk_row = chunk_positions.get(
                (_uuid_key(relation.evidence.doc_uuid), relation.evidence.chunk_index)
            )
            validated.append(
                ValidatedRelation(
                    relation=relation,
                    document=document,
                    chunk_index=chunk_index,
                    title=document.title,
                    source_module=document.source_module or "",
                    page_no=chunk_row[0] if chunk_row else relation.evidence.page_no,
                    section_title=chunk_row[1] if chunk_row else relation.evidence.section_title,
                    version=document.version or "v1",
                    updated_at=document.updated_at,
                    chunk_text_fallback=(chunk_row[2] if chunk_row else "") or "",
                )
            )

        if truncated:
            logger.info("graph relations truncated at expand limit %s", expand_limit)
        return validated

    def _active_fingerprint(self, batch_uuid: str) -> str | None:
        from uuid import UUID

        batch = self.batches.get_by_uuid(UUID(batch_uuid))
        if batch is None or batch.status != "searchable":
            return None
        pointer = self.batches.get_active(batch.doc_uuid)
        if pointer is None or str(pointer.batch_uuid) != str(batch.batch_uuid):
            return None
        return batch.source_fingerprint

    def _load_documents(self, doc_uuids: set) -> dict:
        if not doc_uuids:
            return {}
        # 图侧 doc_uuid 为字符串，统一归一化为 uuid.UUID 再查询
        keys = [key for key in (_uuid_key(item) for item in doc_uuids) if key is not None]
        if not keys:
            return {}
        # 复用统一可检索条件：待审 / 撤回 / 删除 / 未索引文档不得进入回答
        stmt = apply_document_eligibility(select(Document).where(Document.doc_uuid.in_(keys)))
        return {doc.doc_uuid: doc for doc in self.session.scalars(stmt).all()}

    def _load_chunk_positions(
        self, doc_uuids, chunk_refs: set[tuple[str, int]]
    ) -> dict[tuple[str, int], tuple[int | None, str | None, str]]:
        if not doc_uuids:
            return {}
        indexes_by_doc: dict[str, set[int]] = {}
        for doc_uuid, index in chunk_refs:
            indexes_by_doc.setdefault(doc_uuid, set()).add(index)

        # chunk_index 无唯一约束，按 (doc, index) 取最新一条分块定位。
        # 键使用归一化后的 uuid.UUID，与 relation.evidence 保持一致。
        positions: dict[tuple[object, int], tuple[int | None, str | None, str]] = {}
        for doc_uuid, indexes in indexes_by_doc.items():
            key = _uuid_key(doc_uuid)
            if key is None:
                continue
            stmt = (
                select(DocumentChunk)
                .where(
                    DocumentChunk.doc_uuid == key,
                    DocumentChunk.chunk_index.in_(indexes),
                    DocumentChunk.deleted_at.is_(None),
                )
                .order_by(DocumentChunk.chunk_index)
            )
            for chunk in self.session.scalars(stmt).all():
                if chunk.chunk_index is None:
                    continue
                positions[(key, chunk.chunk_index)] = (
                    chunk.page_no,
                    chunk.section_title,
                    chunk.chunk_text or "",
                )
        return positions

    @staticmethod
    def _resolve_chunk_index(
        relation: GraphRelationRecord,
        positions: dict[tuple[object, int], tuple[int | None, str | None, str]],
    ) -> int | None:
        key = (_uuid_key(relation.evidence.doc_uuid), relation.evidence.chunk_index)
        if key in positions:
            return relation.evidence.chunk_index
        # 分块已重建导致 index 不匹配时视为不可用，不用近似匹配掩盖
        return None

    # -- 锚点 ---------------------------------------------------------------

    @staticmethod
    def _anchor_entity_keys(query: str) -> list[str]:
        """从查询中提取候选实体锚点。

        第一版采用与检索层一致的归一化：去空白、转小写。
        这里只做精确子串命中，不做模糊猜测，避免引入未验证的语义匹配。
        """
        from app.graph.extractor import normalise_entity_name

        normalised_query = normalise_entity_name(query)
        if not normalised_query:
            return []
        # 由调用方注入的锚点更准确；此处仅返回归一化查询供上层匹配使用
        return [normalised_query] if len(normalised_query) >= 2 else []
