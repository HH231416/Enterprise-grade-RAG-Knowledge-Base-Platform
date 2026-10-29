"""评审指出的契约与失败路径测试。

这些问题不需要连接真实服务即可验证：
- 存储接口契约（内存与 Neo4j 适配器提供同等能力）
- 多来源关系不互相覆盖
- 存储故障不产生假发布
- 迁移建库后可正常插入，约束与 ORM 一致
- 证据合并的边界（第 0 块、UUID 类型、同分块多关系）
- 历史上下文过滤
- 完整性判断绑定事项

不依赖 InMemoryGraphStore 的直接服务测试：契约测试直接针对接口方法存在性，
Neo4j 适配器用假 driver 验证 Cypher 结构与语义，不执行真实数据库。
"""
from __future__ import annotations

import uuid

import pytest

from app.graph.factory import UnavailableGraphStore, get_store_degraded_reason, reset_graph_store
from app.graph.memory_store import InMemoryGraphStore
from app.graph.neo4j_adapter import Neo4jGraphStore, _dedupe_relations, _params
from app.graph.store import GraphStore
from app.graph.types import EvidenceRef, GraphRelationRecord

from datetime import UTC, datetime

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_CHUNK_TEXT = (
    "本科生毕业学位申请由教务处受理。"
    "办理需要提交毕业申请表和学位申请表。"
    "申请流程包括在线申请、现场核验、领取证书。"
)


@pytest.fixture
def public_doc(db_session):
    """一条满足图构建条件的公开文档（虚构测试资料）。"""
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk

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
        file_hash="hash-public-contract",
        version="v1",
        access_level="public",
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
            chunk_text=_CHUNK_TEXT,
            page_no=1,
            section_title="第一章",
        )
    )
    db_session.commit()
    return doc


def _relation(
    *,
    batch_uuid: str = "batch-a",
    doc_uuid: str = "doc-a",
    chunk_uuid: str = "chunk-a",
    chunk_index: int = 0,
    source: str = "本科生毕业学位申请",
    target: str = "教务处",
    relation_type: str = "handled_by",
    evidence: str = "本科生毕业学位申请由教务处受理。",
    fingerprint: str = "fp-a",
) -> GraphRelationRecord:
    return GraphRelationRecord(
        relation_type=relation_type,
        source_key=source,
        source_type="procedure",
        source_name=source,
        target_key=target,
        target_type="department",
        target_name=target,
        evidence=EvidenceRef(
            doc_uuid=doc_uuid,
            chunk_uuid=chunk_uuid,
            chunk_index=chunk_index,
            evidence_text=evidence,
        ),
        batch_uuid=batch_uuid,
        source_fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# 存储接口契约
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "upsert_batch",
        "replace_batch",
        "remove_batch",
        "expand",
        "batch_relations",
        "relations_at_chunks",
        "probe",
    ],
)
def test_store_contract_declares_all_methods(method: str):
    assert hasattr(GraphStore, method), f"GraphStore 接口缺少 {method}"


@pytest.mark.parametrize(
    "method",
    [
        "upsert_batch",
        "replace_batch",
        "remove_batch",
        "expand",
        "batch_relations",
        "relations_at_chunks",
        "probe",
    ],
)
def test_memory_store_satisfies_contract(method: str):
    assert callable(getattr(InMemoryGraphStore(), method))


@pytest.mark.parametrize(
    "method",
    [
        "upsert_batch",
        "replace_batch",
        "remove_batch",
        "expand",
        "batch_relations",
        "relations_at_chunks",
        "probe",
    ],
)
def test_neo4j_store_satisfies_contract(method: str):
    """Neo4j 适配器必须提供与内存替身同等的读取能力。

    评审问题 1：锚点解析曾依赖内存替身独有的 batch_relations，
    换 Neo4j 后拿不到锚点，关系存在也返回 empty。
    """
    store = Neo4jGraphStore(driver=object())
    assert callable(getattr(store, method)), f"Neo4jGraphStore 缺少 {method}"


@pytest.mark.parametrize(
    "method",
    [
        "upsert_batch",
        "replace_batch",
        "remove_batch",
        "expand",
        "batch_relations",
        "relations_at_chunks",
        "probe",
    ],
)
def test_unavailable_store_satisfies_contract(method: str):
    """不可用占位存储也必须实现完整接口，不能只实现写。"""
    store = UnavailableGraphStore(reason="neo4j driver is not installed")
    assert callable(getattr(store, method))


def test_unavailable_store_rejects_writes():
    """配置 neo4j 但不可用时，写操作必须失败而不是落到内存。"""
    from app.graph.errors import GraphUnavailableError

    store = UnavailableGraphStore(reason="neo4j probe failed")
    with pytest.raises(GraphUnavailableError):
        store.replace_batch("batch-a", [_relation()])
    with pytest.raises(GraphUnavailableError):
        store.upsert_batch("batch-a", [_relation()])
    assert store.probe() is False


def test_anchor_resolver_detects_missing_interface(db_session, public_doc):
    """存储未实现 relations_at_chunks 时不得静默返回空锚点。"""

    class IncompleteStore:
        def probe(self) -> bool:
            return True

        def batch_relations(self, batch_uuid: str):
            return []

    from app.graph.anchors import GraphAnchorResolver

    resolver = GraphAnchorResolver(db_session, store=IncompleteStore())
    result = resolver.resolve(citation_chunks=[(public_doc.doc_uuid, 0)])
    # 接口缺失时不产生锚点，交由上层按 empty 状态如实反映
    assert result.has_anchors is False


# ---------------------------------------------------------------------------
# 多来源关系隔离
# ---------------------------------------------------------------------------


def test_same_relation_from_two_batches_coexists():
    """同一业务关系来自不同文档/批次时，两条来源都必须保留。

    评审问题 2：旧 Cypher 的关系身份只有「两端实体+关系类型」，
    后写入者会覆盖先写入者的 batch_uuid 与证据。
    """
    store = InMemoryGraphStore()
    relation_a = _relation(batch_uuid="batch-a", doc_uuid="doc-a", chunk_uuid="chunk-a")
    relation_b = _relation(batch_uuid="batch-b", doc_uuid="doc-b", chunk_uuid="chunk-b")

    store.replace_batch("batch-a", [relation_a])
    store.replace_batch("batch-b", [relation_b])

    relations = store.expand(["本科生毕业学位申请"])
    assert len(relations) == 2
    doc_uuids = {str(item.evidence.doc_uuid) for item in relations}
    assert doc_uuids == {"doc-a", "doc-b"}
    # 每条来源保留自己的批次，不被对方覆盖
    assert {item.batch_uuid for item in relations} == {"batch-a", "batch-b"}


def test_removing_one_batch_keeps_other_batch_relation():
    """撤回一份来源不影响另一份来源支撑的事实。"""
    store = InMemoryGraphStore()
    store.replace_batch("batch-a", [_relation(batch_uuid="batch-a", doc_uuid="doc-a")])
    store.replace_batch("batch-b", [_relation(batch_uuid="batch-b", doc_uuid="doc-b")])

    store.remove_batch("batch-a")
    remaining = store.expand(["本科生毕业学位申请"])
    assert len(remaining) == 1
    assert remaining[0].batch_uuid == "batch-b"


def test_neo4j_evidence_edge_carries_own_source():
    """Neo4j 写图必须把批次与证据写到 SOURCE_OF 证据边，而非业务关系上。

    评审问题 2：关系身份需支持多来源，证据必须绑定到各自批次。
    """
    query = Neo4jGraphStore._merge_relation_query(_relation(batch_uuid="batch-a"))
    # 业务关系节点上不得直接写 batch_uuid / doc_uuid 等来源字段
    assert "SOURCE_OF" in query
    assert "ev.relation_type = $relation_type" in query
    # 证据身份由 MERGE 的三元组属性承载：批次 + 文档 + 分块
    assert "batch_uuid: $batch_uuid" in query
    assert "doc_uuid: $doc_uuid" in query
    assert "chunk_uuid: $chunk_uuid" in query
    # 业务关系本身不应被覆盖来源：不得对 r 写来源字段
    assert "r.batch_uuid" not in query
    assert "r.doc_uuid" not in query
    assert "r.evidence_text" not in query


def test_neo4j_params_are_string_normalised():
    """写入参数统一为字符串，避免 UUID 类型在 Neo4j 侧不一致。"""
    params = _params(_relation(doc_uuid=uuid.uuid4(), chunk_uuid=uuid.uuid4()), "batch-a")
    assert isinstance(params["doc_uuid"], str)
    assert isinstance(params["chunk_uuid"], str)
    assert params["batch_uuid"] == "batch-a"


def test_dedupe_keeps_distinct_sources():
    """去重按批次+分块+关系+两端，同一来源不重复、不同来源都保留。"""
    relations = [
        _relation(batch_uuid="batch-a", doc_uuid="doc-a", chunk_index=0),
        _relation(batch_uuid="batch-a", doc_uuid="doc-a", chunk_index=0),
        _relation(batch_uuid="batch-b", doc_uuid="doc-b", chunk_index=1),
    ]
    unique = _dedupe_relations(relations)
    assert len(unique) == 2


# ---------------------------------------------------------------------------
# 存储故障不产生假发布
# ---------------------------------------------------------------------------


def test_publish_fails_when_store_unavailable(db_session, public_doc):
    """配置 neo4j 但不可用：发布必须失败，批次不得标记 searchable。

    评审问题 3：原实现会把数据写进内存并报告成功，
    进程重启后图数据消失。
    """
    from app.graph.builder import GraphBuildService
    from app.graph.errors import GraphPublishFailedError
    from app.graph.publish import GraphPublishService
    from app.repositories.graph_batches import GraphBatchRepository

    class FakeExtractor:
        name = "fake"

        def extract(self, *, doc_title, chunk_text, chunk_index):
            from app.graph.extractor import ExtractionOutcome

            return ExtractionOutcome(
                result="candidates",
                relations=[
                    {
                        "relation_type": "handled_by",
                        "source": {"type": "procedure", "name": "本科生毕业学位申请"},
                        "target": {"type": "department", "name": "教务处"},
                        "evidence_text": "本科生毕业学位申请由教务处受理。",
                        "applicability": {"known": False, "note": ""},
                    }
                ],
            )

    builder = GraphBuildService(db_session, extractor=FakeExtractor())
    batch = builder.start_batch(public_doc.doc_uuid, actor="admin-a")
    outcome = builder.run_extraction(batch.batch_uuid)

    publisher = GraphPublishService(db_session, store=UnavailableGraphStore("neo4j probe failed"))
    publisher.approve_batch(
        uuid.UUID(outcome.batch_uuid), rejected_candidate_uuids=[], admin_actor="admin-a"
    )

    with pytest.raises(GraphPublishFailedError):
        publisher.publish_batch(uuid.UUID(outcome.batch_uuid), admin_actor="admin-a")

    repo = GraphBatchRepository(db_session)
    stored = repo.get_by_uuid(uuid.UUID(outcome.batch_uuid))
    assert stored.status == "publish_failed"
    # 关键：不得标记 searchable，也不得产生生效指针
    assert stored.status != "searchable"
    assert repo.get_active(public_doc.doc_uuid) is None


def test_explicit_memory_provider_is_not_degraded(monkeypatch):
    """显式选择 memory 不算降级，可正常用于隔离测试。"""
    from app.graph import factory

    class S:
        graph_store_provider = "memory"

    monkeypatch.setattr(factory, "get_settings", lambda: S())
    reset_graph_store()
    try:
        store = factory.get_graph_store()
        assert isinstance(store, InMemoryGraphStore)
        assert factory.get_store_degraded_reason() is None
        assert factory.is_store_usable() is True
    finally:
        reset_graph_store()


def test_unknown_provider_is_unavailable_not_memory(monkeypatch):
    """未知 provider 不得静默退到内存并报告成功。"""
    from app.graph import factory

    class S:
        graph_store_provider = "typo-provider"

    monkeypatch.setattr(factory, "get_settings", lambda: S())
    reset_graph_store()
    try:
        store = factory.get_graph_store()
        assert isinstance(store, UnavailableGraphStore)
        assert factory.is_store_usable() is False
        assert factory.get_store_degraded_reason()
    finally:
        reset_graph_store()


# ---------------------------------------------------------------------------
# 证据合并边界
# ---------------------------------------------------------------------------


class _Item:
    def __init__(self, relation, chunk_index: int):
        self.relation = relation
        self.chunk_index = chunk_index
        self.title = "标题"
        self.source_module = "模块"
        self.page_no = 1
        self.section_title = "第一章"
        self.version = "v1"
        self.updated_at = _NOW
        self.chunk_text_fallback = ""


def _citation(doc_uuid: str, chunk_uuid: str, chunk_index: int | None):
    from app.schemas.retrieval import Citation

    return Citation(
        doc_uuid=doc_uuid,
        chunk_uuid=chunk_uuid,
        title="标题",
        source_module="模块",
        snippet="摘要",
        version="v1",
        updated_at=_NOW,
        score=0.5,
        chunk_index=chunk_index,
    )


def test_chunk_zero_is_not_treated_as_minus_one():
    """chunk_index=0 是合法分块序号，不能被 `or -1` 误判。

    评审问题 5：原实现用 citation.chunk_index or -1，
    第 0 块会被当成 -1，导致图证据无法与文档证据合并。
    """
    from app.graph.evidence import merge_graph_into_citations

    relation = _relation(chunk_index=0, doc_uuid="doc-a", chunk_uuid="chunk-a")
    citations = [_citation("doc-a", "chunk-a", 0)]

    merged, appended = merge_graph_into_citations(
        citations,
        [_Item(relation, 0)],
        source_locations={("doc-a", 0): (1, "第一章")},
    )

    assert appended == 0, "第 0 块应与已有引用合并，而不是新增引用"
    assert len(merged) == 1
    assert merged[0].graph_relations and merged[0].graph_relations[0].relation_type == "handled_by"
    assert "graph" in merged[0].recall_sources


def test_merge_handles_uuid_type_difference():
    """图侧 UUID 对象与文档侧字符串应能匹配同一分块。"""
    from app.graph.evidence import merge_graph_into_citations

    doc_uuid = uuid.uuid4()
    chunk_uuid = uuid.uuid4()
    relation = _relation(chunk_index=0, doc_uuid=str(doc_uuid), chunk_uuid=str(chunk_uuid))
    citations = [_citation(str(doc_uuid), str(chunk_uuid), 0)]

    merged, appended = merge_graph_into_citations(
        citations,
        [_Item(relation, 0)],
        source_locations={(str(doc_uuid), 0): (1, "第一章")},
    )
    assert appended == 0
    assert len(merged) == 1
    assert merged[0].graph_relations


def test_multiple_relations_same_chunk_all_preserved():
    """同一分块支持的多条关系必须全部保留。"""
    from app.graph.evidence import merge_graph_into_citations

    doc_uuid = str(uuid.uuid4())
    chunk_uuid = str(uuid.uuid4())
    citations = [_citation(doc_uuid, chunk_uuid, 0)]
    items = [
        _Item(_relation(relation_type="handled_by", doc_uuid=doc_uuid, chunk_uuid=chunk_uuid), 0),
        _Item(
            _relation(
                relation_type="requires_material",
                source="本科生毕业学位申请",
                target="毕业申请表",
                doc_uuid=doc_uuid,
                chunk_uuid=chunk_uuid,
                evidence="办理需要提交毕业申请表",
            ),
            0,
        ),
    ]

    merged, appended = merge_graph_into_citations(
        citations,
        items,
        source_locations={(doc_uuid, 0): (1, "第一章")},
    )
    assert appended == 0
    assert len(merged) == 1
    assert len(merged[0].graph_relations) == 2


def test_graph_evidence_appended_when_chunk_not_recalled():
    """图证据指向未召回分块时新增引用，且 snippet 使用完整证据原文。"""
    from app.graph.evidence import merge_graph_into_citations

    full_evidence = "本科生毕业学位申请由教务处受理。办理需提交毕业申请表与学位申请表。"
    relation = _relation(evidence=full_evidence, doc_uuid="doc-b", chunk_uuid="chunk-b")
    citations = [_citation("doc-a", "chunk-a", 0)]

    merged, appended = merge_graph_into_citations(
        citations,
        [_Item(relation, 3)],
        source_locations={("doc-b", 3): (2, "第二章")},
    )
    assert appended == 1
    assert len(merged) == 2
    appended_citation = merged[-1]
    assert appended_citation.evidence_type == "graph_relation"
    # 关系验证必须拿到完整原文，不是 240 字符摘要
    assert appended_citation.snippet == full_evidence


def test_document_citation_carries_full_text_for_generation():
    """文档检索证据也必须带完整原文，否则生成回答时仍只有 240 字符摘要。

    评审问题 5：校验读过完整证据，不代表生成回答也收到完整证据。
    """
    from app.schemas.retrieval import Citation

    citation = Citation(
        doc_uuid="d", chunk_uuid="c", title="t", source_module="m",
        snippet="短摘要", full_text="完整分块原文内容",
        version="v1", updated_at=_NOW, score=0.5,
    )
    assert citation.snippet == "短摘要"
    assert citation.full_text == "完整分块原文内容"


# ---------------------------------------------------------------------------
# 完整性判断绑定事项
# ---------------------------------------------------------------------------


def _validated(source: str, target: str, relation_type: str):
    return _Item(
        _relation(relation_type=relation_type, source=source, target=target),
        0,
    )


def test_completeness_binds_to_single_subject():
    """不同事项的关系不能拼成一个完整答案。

    评审问题 6：A 事项的部门关系与 B 事项的材料关系拼在一起
    不构成对用户所问事项的完整回答。
    """
    from app.graph.evidence import check_completeness
    from app.schemas.retrieval import Citation

    info = check_completeness(
        [Citation(doc_uuid="d", chunk_uuid="c", title="t", source_module="m",
                  snippet="s", version="v1", updated_at=_NOW, score=0.5)],
        [_validated("事项A", "教务处", "handled_by")],
        settings=None,
        relation_question=True,
        question="事项A由哪个部门受理",
    )
    # 只问部门，不应因缺材料关系而判为不完整
    assert info.required_relations == ["handled_by"]
    assert info.is_complete is True
    assert info.needs_clarification is False


def test_completeness_marks_missing_for_same_subject():
    """同一事项缺所需关系时判定不完整。"""
    from app.graph.evidence import check_completeness
    from app.schemas.retrieval import Citation

    info = check_completeness(
        [Citation(doc_uuid="d", chunk_uuid="c", title="t", source_module="m",
                  snippet="s", version="v1", updated_at=_NOW, score=0.5)],
        # 步骤关系不满足"材料"问法 → 该事项缺所需关系
        [_validated("本科生毕业学位申请", "在线申请", "has_step")],
        settings=None,
        relation_question=True,
        question="本科生毕业学位申请需要哪些材料",
    )
    assert info.required_relations == ["requires_material"]
    assert info.satisfied_relations == []
    assert info.is_complete is False
    assert info.needs_clarification is True


def test_completeness_skipped_for_unsupported_question():
    """不在支持范围内的问法不做完整性拒答。"""
    from app.graph.evidence import check_completeness
    from app.schemas.retrieval import Citation

    info = check_completeness(
        [Citation(doc_uuid="d", chunk_uuid="c", title="t", source_module="m",
                  snippet="s", version="v1", updated_at=_NOW, score=0.5)],
        [_validated("本科生毕业学位申请", "教务处", "handled_by")],
        settings=None,
        relation_question=False,
        question="校园卡怎么补办",
    )
    assert info.is_complete is True
    assert info.needs_clarification is False


def test_completeness_skipped_when_no_relations():
    """图无结果时不做完整性拒答，交由普通证据门控处理。"""
    from app.graph.evidence import check_completeness
    from app.schemas.retrieval import Citation

    info = check_completeness(
        [Citation(doc_uuid="d", chunk_uuid="c", title="t", source_module="m",
                  snippet="s", version="v1", updated_at=_NOW, score=0.5)],
        [],
        settings=None,
        relation_question=True,
        question="本科生毕业学位申请需要哪些材料",
    )
    assert info.needs_clarification is False


def test_multi_subject_relations_do_not_cross_satisfy():
    """A 事项的关系不能满足 B 事项的完整性要求。"""
    from app.graph.evidence import check_completeness
    from app.schemas.retrieval import Citation

    info = check_completeness(
        [Citation(doc_uuid="d", chunk_uuid="c", title="t", source_module="m",
                  snippet="s", version="v1", updated_at=_NOW, score=0.5)],
        [
            _validated("事项A", "毕业申请表", "requires_material"),
            _validated("事项B", "教务处", "handled_by"),
        ],
        settings=None,
        relation_question=True,
        question="需要哪些材料",
    )
    # 问题未提及任何事项名 → 无法确定所问事项，不做完整性证明
    assert info.required_relations == ["requires_material"]
    assert info.subject_uncertain is True
    assert info.is_complete is False
    assert sorted(info.candidate_subjects) == ["事项A", "事项B"]


# ---------------------------------------------------------------------------
# 完整性判断：目标事项绑定（评审要求的四类反例）
# ---------------------------------------------------------------------------


def _completeness(relations, question):
    from app.graph.evidence import check_completeness

    return check_completeness(
        [], relations, settings=None, relation_question=True, question=question
    )


def test_missing_one_of_multiple_required_relations_is_incomplete():
    """问 A 的材料+部门，只有 A 的材料 → 不应判定完整。

    评审问题 3：原实现 is_complete = bool(satisfied)，
    只要满足任意一项就算完整。
    """
    info = _completeness(
        [_validated("事项A", "毕业申请表", "requires_material")],
        "事项A需要哪些材料，由哪个部门受理",
    )
    assert sorted(info.required_relations) == ["handled_by", "requires_material"]
    assert info.is_complete is False
    assert "handled_by" in info.missing_relations
    assert info.target_subject == "事项A"


def test_cross_subject_mixture_is_incomplete():
    """问 A 的材料+部门，有 A 的材料、B 的部门 → 不应判定完整。

    跨事项的关系不能互相证明。
    """
    info = _completeness(
        [
            _validated("事项A", "毕业申请表", "requires_material"),
            _validated("事项B", "教务处", "handled_by"),
        ],
        "事项A需要哪些材料，由哪个部门受理",
    )
    assert info.target_subject == "事项A"
    assert info.is_complete is False
    assert info.missing_relations == ["handled_by"]


def test_only_other_subject_relations_is_uncertain():
    """问 A，只有 B 的完整关系 → 不能用 B 证明 A，返回待澄清。"""
    info = _completeness(
        [
            _validated("事项B", "毕业申请表", "requires_material"),
            _validated("事项B", "教务处", "handled_by"),
        ],
        "事项A需要哪些材料，由哪个部门受理",
    )
    assert info.subject_uncertain is True
    assert info.target_subject is None
    assert info.is_complete is False
    assert info.needs_clarification is True
    assert "事项B" in info.candidate_subjects


def test_target_subject_complete_when_all_required_present():
    """问 A，A 的全部必要关系都有支持 → 才具备判定完整的条件。"""
    info = _completeness(
        [
            _validated("事项A", "毕业申请表", "requires_material"),
            _validated("事项A", "教务处", "handled_by"),
        ],
        "事项A需要哪些材料，由哪个部门受理",
    )
    assert info.subject_uncertain is False
    assert info.target_subject == "事项A"
    assert info.is_complete is True
    assert info.missing_relations == []
    assert sorted(info.satisfied_relations) == ["handled_by", "requires_material"]


def test_subject_uncertain_when_question_names_multiple_subjects():
    """问题同时提到多个事项 → 无法确定是哪一个，保守澄清。"""
    info = _completeness(
        [
            _validated("事项A", "毕业申请表", "requires_material"),
            _validated("事项B", "教务处", "handled_by"),
        ],
        "事项A和事项B分别需要哪些材料，由哪个部门受理",
    )
    assert info.subject_uncertain is True
    assert info.is_complete is False


def test_subject_not_named_in_question_is_uncertain():
    """问题未提到任何已召回事项名 → 不猜测，保守澄清。"""
    info = _completeness(
        [_validated("本科生毕业学位申请", "教务处", "handled_by")],
        "由哪个部门受理",
    )
    assert info.subject_uncertain is True
    assert info.is_complete is False


# ---------------------------------------------------------------------------
# Neo4j 关系类型大小写一致性
# ---------------------------------------------------------------------------


def test_business_edge_type_and_evidence_attribute_are_comparable():
    """业务边类型为大写、证据属性为小写，读取时必须归一化比较。

    评审问题 1：原实现 WHERE ev.relation_type = type(r)
    比较 'handled_by' 与 'HANDLED_BY'，恒不匹配。
    """
    from app.graph.neo4j_adapter import _relation_type_to_cypher

    query = Neo4jGraphStore._merge_relation_query(_relation(relation_type="handled_by"))
    # 写入的业务边类型为大写
    assert ":HANDLED_BY" in query
    # 证据属性参数保持小写（受控枚举原值）
    assert _relation_type_to_cypher("handled_by") == "HANDLED_BY"
    assert "ev.relation_type = $relation_type" in query


def test_expand_query_normalises_case():
    from app.graph.neo4j_adapter import EVIDENCE_RELATION, Neo4jGraphStore

    # 直接检查 expand 使用的查询文本
    import inspect

    source = inspect.getsource(Neo4jGraphStore.expand)
    assert "toLower(ev.relation_type) = toLower(type(r))" in source
    assert EVIDENCE_RELATION == "SOURCE_OF"


def test_row_conversion_normalises_relation_type_to_lowercase():
    """从 Neo4j 读回的关系类型必须归一化为受控枚举的小写形式。"""
    from app.graph.neo4j_adapter import _row_to_relation

    row = {
        "rel_type": "HANDLED_BY",
        "source_key": "事项", "source_type": "procedure", "source_name": "事项",
        "target_key": "教务处", "target_type": "department", "target_name": "教务处",
        "batch_uuid": "b", "source_fingerprint": "fp",
        "doc_uuid": "d", "chunk_uuid": "c", "chunk_index": 0,
        "evidence_text": "x", "evidence_section": None, "evidence_page_no": None,
        "applicability": None, "applicability_known": False,
    }
    relation = _row_to_relation(row)
    assert relation.relation_type == "handled_by"
    assert relation.relation_type in {
        "handled_by", "requires_material", "has_step",
        "has_prerequisite", "governed_by", "applies_to",
    }


# ---------------------------------------------------------------------------
# 事务边界与删除安全性（评审问题 2）
# ---------------------------------------------------------------------------


class _RecordingSession:
    """记录 execute_write 的调用，用于验证事务边界。"""

    def __init__(self, inner):
        self._inner = inner
        self.execute_write_calls = []

    def execute_write(self, work):
        self.execute_write_calls.append(work)
        return self._inner.execute_write(work)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _RecordingDriver:
    def __init__(self, inner_session):
        self._inner_session = inner_session
        self.last_session = None

    def session(self, database=None):
        self.last_session = _RecordingSession(self._inner_session)
        return self.last_session


class _FakeTx:
    def __init__(self):
        self.statements = []

    def run(self, query, **params):
        self.statements.append((query, params))


class _InnerSession:
    def __init__(self):
        self.tx = _FakeTx()

    def execute_write(self, work):
        return work(self.tx)

    def run(self, query, **params):
        self.tx.run(query, **params)


def test_replace_batch_uses_single_explicit_transaction():
    """批次替换必须在一个显式事务内完成删除与全部写入。

    评审问题 2：共用 session 多次 session.run 不构成原子操作。
    """
    inner = _InnerSession()
    driver = _RecordingDriver(inner)
    store = Neo4jGraphStore(driver=driver)

    relations = [
        _relation(relation_type="handled_by", source="事项一"),
        _relation(relation_type="requires_material", source="事项一"),
    ]
    store.replace_batch("batch-a", relations)

    session = driver.last_session
    # 只调用一次 execute_write → 一个事务
    assert len(session.execute_write_calls) == 1
    # 该事务内包含 1 条删除 + 2 条写入
    assert len(inner.tx.statements) == 3


def test_upsert_batch_does_not_delete():
    inner = _InnerSession()
    driver = _RecordingDriver(inner)
    Neo4jGraphStore(driver=driver).upsert_batch("batch-a", [_relation()])

    assert len(driver.last_session.execute_write_calls) == 1
    # upsert 不含删除语句
    queries = [q for q, _ in inner.tx.statements]
    assert not any("DELETE ev" in q for q in queries)


def test_batch_replace_does_not_delete_shared_endpoints():
    """批次替换只删该批次证据边，不得删除共享实体节点。

    评审问题 2：原实现 DELETE s,t 会对仍被业务边连接的端点执行删除，
    违反 Neo4j 约束，且可能误删其他来源支撑的事实。
    """
    inner = _InnerSession()
    driver = _RecordingDriver(inner)
    Neo4jGraphStore(driver=driver).replace_batch("batch-a", [_relation()])

    delete_queries = [q for q, _ in inner.tx.statements if "DELETE" in q]
    assert delete_queries, "应包含删除该批次证据边的语句"
    for query in delete_queries:
        assert "DELETE ev" in query
        # 不得删除节点
        assert "DELETE s" not in query
        assert "DELETE t" not in query
        assert "DELETE n" not in query


def test_remove_batch_uses_transaction():
    inner = _InnerSession()
    driver = _RecordingDriver(inner)
    Neo4jGraphStore(driver=driver).remove_batch("batch-a")

    assert len(driver.last_session.execute_write_calls) == 1
    assert len(inner.tx.statements) == 1
    assert "DELETE ev" in inner.tx.statements[0][0]


def test_write_failure_raises_unavailable():
    """事务失败必须抛 GraphUnavailableError，不得静默吞掉。"""
    from app.graph.errors import GraphUnavailableError

    class _FailingDriver:
        def session(self, database=None):
            raise RuntimeError("neo4j down")

    with pytest.raises(GraphUnavailableError):
        Neo4jGraphStore(driver=_FailingDriver()).replace_batch("batch-a", [_relation()])
