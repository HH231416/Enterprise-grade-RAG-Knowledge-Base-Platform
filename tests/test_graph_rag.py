"""图增强检索隔离测试。

全部使用 SQLite + InMemoryGraphStore + 假抽取器：
不连接 Neo4j、PostgreSQL 或任何真实模型服务。

这些是机制测试（mock 验证），不等同于真实集成验收。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.graph.builder import GraphBuildService, is_public_document
from app.graph.errors import (
    GraphAdminRequiredError,
    GraphScopeError,
    GraphSourceChangedError,
)
from app.graph.extractor import ExtractionOutcome
from app.graph.memory_store import InMemoryGraphStore
from app.graph.publish import GraphPublishService
from app.graph.retrieval import GraphRetrievalService
from app.graph.types import SourceSnapshot
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.repositories.graph_batches import GraphBatchRepository


CHUNK_TEXT = (
    "本科生毕业学位申请由教务处受理。"
    "办理需要提交毕业申请表和学位申请表。"
    "申请流程包括在线申请、现场核验、领取证书。"
)


class FakeExtractor:
    """假抽取器：按分块内容返回预设候选，不调用模型。"""

    def __init__(self, relations=None, outcome: ExtractionOutcome | None = None) -> None:
        self.name = "fake-extractor"
        self._outcome = outcome
        self._relations = relations or []

    def extract(self, *, doc_title, chunk_text, chunk_index) -> ExtractionOutcome:
        if self._outcome is not None:
            return self._outcome
        if not chunk_text.strip():
            return ExtractionOutcome(result="empty")
        return ExtractionOutcome(result="candidates", relations=list(self._relations))


def _relation(
    relation_type: str,
    source_name: str,
    source_type: str,
    target_name: str,
    target_type: str,
    evidence: str,
    applicability_known: bool = False,
):
    return {
        "relation_type": relation_type,
        "source": {"type": source_type, "name": source_name},
        "target": {"type": target_type, "name": target_name},
        "evidence_text": evidence,
        "applicability": {"known": applicability_known, "note": "在校本科生" if applicability_known else ""},
    }


DEFAULT_RELATIONS = [
    _relation("handled_by", "本科生毕业学位申请", "procedure", "教务处", "department",
              "本科生毕业学位申请由教务处受理。"),
    _relation("requires_material", "本科生毕业学位申请", "procedure", "毕业申请表", "material",
              "办理需要提交毕业申请表和学位申请表。"),
]


@pytest.fixture
def store() -> InMemoryGraphStore:
    return InMemoryGraphStore()


@pytest.fixture
def public_doc(db_session) -> Document:
    doc = Document(
        doc_uuid=uuid.uuid4(),
        title="校园办事指南（虚构测试资料）",
        source_type="manual",
        source_module="campus-guide",
        file_name="guide.pdf",
        file_ext=".pdf",
        mime_type="application/pdf",
        file_path="/tmp/graph-test/guide.pdf",
        file_size=1024,
        file_hash="hash-public-1",
        version="v1",
        access_level="public",
        status="active",
        review_status="approved",
        parse_status="success",
        index_status="success",
    )
    db_session.add(doc)
    db_session.flush()
    chunk = DocumentChunk(
        chunk_uuid=uuid.uuid4(),
        doc_uuid=doc.doc_uuid,
        chunk_index=0,
        chunk_text=CHUNK_TEXT,
        page_no=1,
        section_title="第一章",
    )
    db_session.add(chunk)
    db_session.commit()
    return doc


@pytest.fixture
def internal_doc(db_session) -> Document:
    doc = Document(
        doc_uuid=uuid.uuid4(),
        title="内部审批规定（虚构测试资料）",
        source_type="manual",
        source_module="campus-internal",
        file_name="internal.pdf",
        file_ext=".pdf",
        mime_type="application/pdf",
        file_path="/tmp/graph-test/internal.pdf",
        file_size=1024,
        file_hash="hash-internal-1",
        version="v1",
        access_level="internal",
        status="active",
        review_status="approved",
        parse_status="success",
        index_status="success",
    )
    db_session.add(doc)
    db_session.flush()
    db_session.add(
        DocumentChunk(
            chunk_uuid=uuid.uuid4(),
            doc_uuid=doc.doc_uuid,
            chunk_index=0,
            chunk_text=CHUNK_TEXT,
            page_no=1,
        )
    )
    db_session.commit()
    return doc


def _build_and_publish(db_session, doc, store, extractor=None, rejected=None):
    builder = GraphBuildService(db_session, extractor=extractor or FakeExtractor(DEFAULT_RELATIONS))
    batch = builder.start_batch(doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)
    publisher = GraphPublishService(db_session, store=store)
    published = publisher.approve_and_publish(
        uuid.UUID(outcome.batch_uuid),
        rejected_candidate_uuids=rejected or [],
        admin_actor="admin-a",
    )
    return outcome, published


# ---------------------------------------------------------------------------
# 公开范围约束
# ---------------------------------------------------------------------------


def test_public_document_enters_graph_build(db_session, public_doc, store):
    outcome, published = _build_and_publish(db_session, public_doc, store)
    assert published.published is True
    assert published.status == "searchable"


def test_non_public_document_rejected_at_build(db_session, internal_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    with pytest.raises(GraphScopeError):
        builder.start_batch(internal_doc.doc_uuid, actor="admin-a")


def test_internal_document_still_usable_by_document_retrieval(db_session, internal_doc):
    """非公开文档不得进图，但不应被本次改造影响原有检索语义。"""
    assert is_public_document(internal_doc) is False


def test_withdrawn_document_cannot_enter_graph_build(db_session, public_doc, store):
    public_doc.review_status = "revoked"
    public_doc.status = "inactive"
    db_session.commit()
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    with pytest.raises(GraphScopeError):
        builder.start_batch(public_doc.doc_uuid, actor="admin-a")


# ---------------------------------------------------------------------------
# 候选校验
# ---------------------------------------------------------------------------


def test_relation_without_evidence_is_rejected(db_session, public_doc, store):
    bad = [_relation("handled_by", "本科生毕业学位申请", "procedure", "教务处", "department", "")]
    builder = GraphBuildService(db_session, extractor=FakeExtractor(bad))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)
    assert outcome.candidate_count == 0
    assert outcome.extraction_result == "evidence_rejected"


def test_evidence_not_in_source_chunk_is_rejected(db_session, public_doc, store):
    fabricated = [
        _relation(
            "handled_by", "本科生毕业学位申请", "procedure", "教务处", "department",
            "该事项由财务处受理并收取费用。",
        )
    ]
    builder = GraphBuildService(db_session, extractor=FakeExtractor(fabricated))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)
    assert outcome.candidate_count == 0


def test_illegal_relation_type_is_rejected(db_session, public_doc, store):
    bad = [_relation("unknown_relation", "本科生毕业学位申请", "procedure", "教务处", "department",
                     "本科生毕业学位申请由教务处受理。")]
    builder = GraphBuildService(db_session, extractor=FakeExtractor(bad))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)
    assert outcome.candidate_count == 0


def test_wrong_relation_direction_is_rejected(db_session, public_doc, store):
    """handled_by 的方向必须是 procedure → department，反向应被拒绝。"""
    bad = [_relation("handled_by", "教务处", "department", "本科生毕业学位申请", "procedure",
                     "本科生毕业学位申请由教务处受理。")]
    builder = GraphBuildService(db_session, extractor=FakeExtractor(bad))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)
    assert outcome.candidate_count == 0


# ---------------------------------------------------------------------------
# 抽取失败分类：不静默兜底
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome,expected_result",
    [
        (ExtractionOutcome(result="llm_failed", error_code="llm_call_failed", error_message="timeout"), "llm_failed"),
        (ExtractionOutcome(result="invalid_output", error_code="invalid_json", error_message="bad"), "invalid_output"),
    ],
)
def test_extraction_failure_is_recorded_not_fallback(
    db_session, public_doc, store, outcome, expected_result
):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(outcome=outcome))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    result = builder.run_extraction(batch.batch_uuid)
    assert result.status == "failed"
    assert result.extraction_result == expected_result
    assert result.candidate_count == 0
    # 失败批次不得成为生效批次
    repo = GraphBatchRepository(db_session)
    assert repo.get_active(public_doc.doc_uuid) is None


def test_empty_extraction_is_distinguished_from_failure(db_session, public_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor([]))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    result = builder.run_extraction(batch.batch_uuid)
    assert result.extraction_result == "evidence_rejected"
    assert result.status == "failed"


# ---------------------------------------------------------------------------
# 批次审核
# ---------------------------------------------------------------------------


def test_pending_batch_not_searchable_before_approval(db_session, public_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    builder.run_extraction(batch.batch_uuid)

    retrieval = GraphRetrievalService(db_session, store=store)
    result = retrieval.search(query="本科毕业", settings=_settings(), anchor_keys=["教务处"])
    assert result.status in ("empty", "disabled")
    assert not result.has_relations


def test_approved_batch_becomes_searchable(db_session, public_doc, store):
    _build_and_publish(db_session, public_doc, store)
    retrieval = GraphRetrievalService(db_session, store=store)
    result = retrieval.search(
        query="本科生毕业学位申请", settings=_settings(),
        anchor_keys=["本科生毕业学位申请", "教务处"],
    )
    assert result.status == "ok"
    assert result.has_relations


def test_rejected_candidate_not_retrievable(db_session, public_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)
    batch_uuid = uuid.UUID(outcome.batch_uuid)

    repo = GraphBatchRepository(db_session)
    candidates = repo.list_candidates(batch_uuid)
    assert len(candidates) == 2
    rejected_uuid = candidates[1].candidate_uuid

    publisher = GraphPublishService(db_session, store=store)
    publisher.approve_batch(
        batch_uuid, rejected_candidate_uuids=[rejected_uuid], admin_actor="admin-a"
    )
    published = publisher.publish_batch(batch_uuid, admin_actor="admin-a")
    assert published.approved_count == 1

    retrieval = GraphRetrievalService(db_session, store=store)
    result = retrieval.search(
        query="本科生毕业学位申请", settings=_settings(),
        anchor_keys=["本科生毕业学位申请", "教务处"],
    )
    assert result.status == "ok"
    assert len(result.validated) == 1
    assert result.validated[0].relation.relation_type == "handled_by"


def test_trusted_admin_required_for_approval(db_session, public_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)
    publisher = GraphPublishService(db_session, store=store)

    with pytest.raises(GraphAdminRequiredError):
        publisher.approve_batch(uuid.UUID(outcome.batch_uuid), rejected_candidate_uuids=[], admin_actor=None)
    with pytest.raises(GraphAdminRequiredError):
        publisher.approve_batch(uuid.UUID(outcome.batch_uuid), rejected_candidate_uuids=[], admin_actor="")


def test_trusted_admin_required_for_publish(db_session, public_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)
    batch_uuid = uuid.UUID(outcome.batch_uuid)
    publisher = GraphPublishService(db_session, store=store)
    publisher.approve_batch(batch_uuid, rejected_candidate_uuids=[], admin_actor="admin-a")

    with pytest.raises(GraphAdminRequiredError):
        publisher.publish_batch(batch_uuid, admin_actor=None)


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_repeated_build_same_snapshot_reuses_batch(db_session, public_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    first = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    second = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    assert first.batch_uuid == second.batch_uuid
    assert second.created is False


def test_repeated_publish_does_not_duplicate_relations(db_session, public_doc, store):
    _build_and_publish(db_session, public_doc, store)
    repo = GraphBatchRepository(db_session)
    batch_uuid = str(repo.get_active(public_doc.doc_uuid).batch_uuid)

    publisher = GraphPublishService(db_session, store=store)
    again = publisher.publish_batch(uuid.UUID(batch_uuid), admin_actor="admin-a")
    assert again.published is True
    assert len(store.batch_relations(batch_uuid)) == 2


def test_repeated_approval_is_idempotent(db_session, public_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)
    batch_uuid = uuid.UUID(outcome.batch_uuid)

    publisher = GraphPublishService(db_session, store=store)
    first = publisher.approve_batch(batch_uuid, rejected_candidate_uuids=[], admin_actor="admin-a")
    repo = GraphBatchRepository(db_session)
    approved_after_first = repo.count_candidates(batch_uuid, "approved")

    # 重复批准不应重复计数
    repo.set_status(batch_uuid, "publish_failed")
    db_session.commit()
    publisher.approve_batch(batch_uuid, rejected_candidate_uuids=[], admin_actor="admin-a")
    assert repo.count_candidates(batch_uuid, "approved") == approved_after_first == first.approved_count


# ---------------------------------------------------------------------------
# 来源快照变化
# ---------------------------------------------------------------------------


def test_changed_source_blocks_approval(db_session, public_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    builder.run_extraction(batch.batch_uuid)

    # 文档内容变化 → 来源快照变化
    public_doc.file_hash = "hash-public-2"
    db_session.commit()

    publisher = GraphPublishService(db_session, store=store)
    with pytest.raises(GraphSourceChangedError):
        publisher.approve_batch(uuid.UUID(batch.batch_uuid), rejected_candidate_uuids=[], admin_actor="admin-a")


def test_changed_source_requires_rebuild(db_session, public_doc, store):
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    first = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    builder.run_extraction(first.batch_uuid)

    public_doc.file_hash = "hash-public-3"
    db_session.commit()

    second = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    assert second.batch_uuid != first.batch_uuid
    assert second.created is True
    # 旧批次已失效
    repo = GraphBatchRepository(db_session)
    old = repo.get_by_uuid(uuid.UUID(first.batch_uuid))
    assert old.status == "superseded"
    assert repo.get_active(public_doc.doc_uuid) is None


def test_rebuild_same_snapshot_keeps_previous_active_batch(db_session, public_doc, store):
    """同一来源快照重新构图：旧批次继续生效，不因开始重建就被替换。"""
    _build_and_publish(db_session, public_doc, store)
    repo = GraphBatchRepository(db_session)
    original_active = str(repo.get_active(public_doc.doc_uuid).batch_uuid)

    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    again = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    assert again.created is False  # 同一快照命中幂等，不新建批次
    assert str(repo.get_active(public_doc.doc_uuid).batch_uuid) == original_active


# ---------------------------------------------------------------------------
# 失效
# ---------------------------------------------------------------------------


def test_invalidate_blocks_retrieval_immediately(db_session, public_doc, store):
    _build_and_publish(db_session, public_doc, store)
    retrieval = GraphRetrievalService(db_session, store=store)
    assert retrieval.search(
        query="本科生毕业学位申请", settings=_settings(),
        anchor_keys=["本科生毕业学位申请", "教务处"],
    ).status == "ok"

    publisher = GraphPublishService(db_session, store=store)
    publisher.invalidate_document(public_doc.doc_uuid, actor="admin-a")

    result = retrieval.search(
        query="本科生毕业学位申请", settings=_settings(),
        anchor_keys=["本科生毕业学位申请", "教务处"],
    )
    assert result.status == "empty"
    assert not result.has_relations


def test_visibility_change_to_non_public_blocks_graph_evidence(db_session, public_doc, store):
    _build_and_publish(db_session, public_doc, store)
    retrieval = GraphRetrievalService(db_session, store=store)
    assert retrieval.search(
        query="本科生毕业学位申请", settings=_settings(),
        anchor_keys=["本科生毕业学位申请", "教务处"],
    ).status == "ok"

    # 公开 → 非公开，立即阻止继续使用
    public_doc.access_level = "internal"
    db_session.commit()

    result = retrieval.search(
        query="本科生毕业学位申请", settings=_settings(),
        anchor_keys=["本科生毕业学位申请", "教务处"],
    )
    assert not result.has_relations


def test_one_source_invalidation_keeps_other_document_relations(db_session, public_doc, store):
    """一份来源失效不误删其他来源支撑的事实。"""
    _build_and_publish(db_session, public_doc, store)

    # 第二个公开文档支撑同一实体
    other = Document(
        doc_uuid=uuid.uuid4(),
        title="补充办事指南（虚构测试资料）",
        source_type="manual",
        source_module="campus-guide",
        file_name="guide2.pdf",
        file_ext=".pdf",
        mime_type="application/pdf",
        file_path="/tmp/graph-test/guide2.pdf",
        file_size=1024,
        file_hash="hash-public-other",
        version="v1",
        access_level="public",
        status="active",
        review_status="approved",
        parse_status="success",
        index_status="success",
    )
    db_session.add(other)
    db_session.flush()
    db_session.add(
        DocumentChunk(
            chunk_uuid=uuid.uuid4(),
            doc_uuid=other.doc_uuid,
            chunk_index=0,
            chunk_text=CHUNK_TEXT,
            page_no=1,
        )
    )
    db_session.commit()
    _build_and_publish(db_session, other, store)

    repo = GraphBatchRepository(db_session)
    assert len(repo.list_active()) == 2

    publisher = GraphPublishService(db_session, store=store)
    publisher.invalidate_document(public_doc.doc_uuid, actor="admin-a")

    active = repo.list_active()
    assert len(active) == 1
    assert active[0].doc_uuid == other.doc_uuid
    # 另一份来源的关系仍在图存储中
    assert len(store.batch_relations(str(active[0].batch_uuid))) == 2


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------


def test_graph_store_failure_degrades_without_breaking_document_search(db_session, public_doc, store):
    _build_and_publish(db_session, public_doc, store)
    store.set_available(False)

    retrieval = GraphRetrievalService(db_session, store=store)
    result = retrieval.search(
        query="本科生毕业学位申请", settings=_settings(),
        anchor_keys=["本科生毕业学位申请", "教务处"],
    )
    assert result.status == "unavailable"
    assert result.is_degraded


def test_graph_disabled_returns_disabled_not_unavailable(db_session, store):
    class OffSettings:
        graph_enabled = False
        graph_retrieval_enabled = False

    retrieval = GraphRetrievalService(db_session, store=store)
    result = retrieval.search(query=" anything ", settings=OffSettings())
    assert result.status == "disabled"
    assert not result.is_degraded


def test_graph_disabled_leaves_citation_schema_unchanged():
    """图功能关闭时既有字段语义不变，向后兼容。"""
    from app.schemas.retrieval import Citation

    citation = Citation(
        doc_uuid="d", chunk_uuid="c", title="t", source_module="m",
        snippet="s", version="v1",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC), score=0.5,
    )
    assert citation.evidence_type == "document"
    assert citation.graph_relations == []
    assert citation.recall_sources == []


def test_unapproved_batch_in_graph_store_not_retrievable(db_session, public_doc, store):
    """即使图数据已写入，未被生效指针选中的批次也不进入在线回答。"""
    builder = GraphBuildService(db_session, extractor=FakeExtractor(DEFAULT_RELATIONS))
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)

    # 手工写入图存储，模拟 Neo4j 写入成功但指针未切换
    repo = GraphBatchRepository(db_session)
    candidates = repo.list_candidates(uuid.UUID(outcome.batch_uuid), "approved")
    store.upsert_batch(outcome.batch_uuid, [])

    retrieval = GraphRetrievalService(db_session, store=store)
    result = retrieval.search(
        query="本科生毕业学位申请", settings=_settings(),
        anchor_keys=["本科生毕业学位申请", "教务处"],
    )
    assert result.status == "empty"
    assert candidates == [] or all(c.review_status == "pending" for c in candidates)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _settings():
    class S:
        graph_enabled = True
        graph_retrieval_enabled = True
        graph_expand_limit = 50
        graph_required_relations = "handled_by,requires_material"

    return S()


def test_snapshot_comparison_detects_change():
    base = SourceSnapshot(
        doc_uuid="d", doc_version="v1", source_fingerprint="fp1",
        chunk_count=3, access_scope="public",
    )
    same = SourceSnapshot(
        doc_uuid="d", doc_version="v1", source_fingerprint="fp1",
        chunk_count=3, access_scope="public",
    )
    changed = SourceSnapshot(
        doc_uuid="d", doc_version="v1", source_fingerprint="fp2",
        chunk_count=3, access_scope="public",
    )
    assert base.is_same_source(same) is True
    assert base.is_same_source(changed) is False


# ---------------------------------------------------------------------------
# 实体锚点解析（复用向量召回结果）
# ---------------------------------------------------------------------------


def test_anchor_resolver_uses_retrieval_hits(db_session, public_doc, store):
    """锚点来自向量召回命中的文档，而不是对问句做字符串猜测。"""
    from app.graph.anchors import GraphAnchorResolver

    _build_and_publish(db_session, public_doc, store)
    anchors = GraphAnchorResolver(db_session, store=store).resolve(
        citation_chunks=[(public_doc.doc_uuid, 0)],
    )
    assert anchors.has_anchors
    assert "教务处" in anchors.entity_keys
    assert "本科生毕业学位申请" in anchors.entity_keys


def test_anchor_resolver_empty_without_retrieval_hits(db_session, public_doc, store):
    from app.graph.anchors import GraphAnchorResolver

    _build_and_publish(db_session, public_doc, store)
    anchors = GraphAnchorResolver(db_session, store=store).resolve(citation_chunks=[])
    assert not anchors.has_anchors


def test_anchor_resolver_skips_withdrawn_document(db_session, public_doc, store):
    """已撤回文档不提供锚点，避免通过图分支绕过权威状态约束。"""
    from app.graph.anchors import GraphAnchorResolver

    _build_and_publish(db_session, public_doc, store)
    public_doc.review_status = "revoked"
    public_doc.status = "inactive"
    db_session.commit()

    anchors = GraphAnchorResolver(db_session, store=store).resolve(
        citation_chunks=[(public_doc.doc_uuid, 0)],
    )
    assert not anchors.has_anchors


def test_explicit_anchor_keys_take_priority(db_session, public_doc, store):
    from app.graph.anchors import GraphAnchorResolver

    _build_and_publish(db_session, public_doc, store)
    anchors = GraphAnchorResolver(db_session, store=store).resolve(
        citation_chunks=[], anchor_keys=["教务处"],
    )
    assert anchors.entity_keys == ["教务处"]
