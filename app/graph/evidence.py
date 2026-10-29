"""图证据与文档证据的统一合并。

合并规则：
- 图关系不是独立答案来源，必须回落到原文引用。图关系挂在对应原文 Citation 上，
  作为辅助解释，不单独成为一条"看起来像证据"的结论。
- 去重按 (doc_uuid, chunk_uuid)：chunk_uuid 是主键且跨版本稳定；
  不用 chunk_index 做键（第 0 块会被 `or -1` 误判，且重建后会变化）。
- 同一分块支持的不同关系全部保留。
- 图召回分只作排序信号，不与文档检索的原始分相加。
- 关系类问题做完整性检查：所需关系由问法决定，且必须绑定同一事项。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.graph.types import GraphRelationRecord
from app.schemas.retrieval import (
    Citation,
    GraphCompletenessInfo,
    GraphRelationView,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QuestionIntent:
    """一种受支持的问法及其所需的关系类型。

    第一版刻意限定为明确的几类问法。无法匹配到任何问法时不做完整性拒答，
    避免把「未识别的问题类型」误判成「证据不完整」。
    """

    name: str
    markers: tuple[str, ...]
    required_relations: tuple[str, ...]


# 问法 → 所需关系。只列第一版真正支持的类型，不做推断式扩展。
QUESTION_INTENT_RULES: tuple[QuestionIntent, ...] = (
    QuestionIntent(
        name="department",
        markers=("哪个部门", "由哪个部门", "哪个部门受理", "受理部门", "归口部门", "谁受理"),
        required_relations=("handled_by",),
    ),
    QuestionIntent(
        name="material",
        markers=("哪些材料", "需要什么材料", "需要哪些材料", "什么材料", "提交什么"),
        required_relations=("requires_material",),
    ),
    QuestionIntent(
        name="step",
        markers=("哪些步骤", "怎么办理", "办理流程", "流程是什么", "怎么申请"),
        required_relations=("has_step",),
    ),
    QuestionIntent(
        name="prerequisite",
        markers=("前置条件", "前提条件", "有什么前置", "需要先"),
        required_relations=("has_prerequisite",),
    ),
    QuestionIntent(
        name="basis",
        markers=("依据哪", "依据什么", "依据哪项", "依据哪些"),
        required_relations=("governed_by",),
    ),
)


def match_question_intents(query: str) -> list[QuestionIntent]:
    """返回问题命中的受支持问法列表。未命中任何问法时返回空列表。"""
    text = str(query or "")
    if not text:
        return []
    return [intent for intent in QUESTION_INTENT_RULES if any(m in text for m in intent.markers)]


def is_relation_question(query: str) -> bool:
    """是否为受支持的关系类问法。刻意保守：未命中不按关系问题处理。"""
    return bool(match_question_intents(query))


def required_relation_types(settings) -> list[str]:
    """配置项保留为「图关系覆盖度自检」用途，不用于判断单个问题是否完整。"""
    raw = getattr(settings, "graph_required_relations", "") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def to_relation_view(relation: GraphRelationRecord) -> GraphRelationView:
    return GraphRelationView(
        relation_type=relation.relation_type,
        source_entity_name=relation.source_name,
        source_entity_type=relation.source_type,
        target_entity_name=relation.target_name,
        target_entity_type=relation.target_type,
        applicability=relation.applicability,
        applicability_known=relation.applicability_known,
    )


def _citation_chunk_key(doc_uuid, chunk_uuid) -> tuple[str, str]:
    """引用合并键：统一用字符串形式的 (doc_uuid, chunk_uuid)。

    不用 chunk_index 做键，原因有两点：
    1. `or -1` 会把第 0 块（chunk_index=0）误判为 -1；
    2. chunk_uuid 是主键，跨版本稳定；chunk_index 在分块重建后可能变化。
    """
    return (str(doc_uuid or ""), str(chunk_uuid or ""))


def merge_graph_into_citations(
    citations: list[Citation],
    validated_relations: list,
    *,
    source_locations: dict[tuple[str, int], tuple[int | None, str | None]],
    graph_rank: dict[tuple[str, int, str, str, str], int] | None = None,
) -> tuple[list[Citation], int]:
    """把图关系并入已有引用。

    返回 (合并后的引用, 新增的原文引用数)。
    - 命中已有引用所在分块：把关系挂到该引用上，保留 recall_sources 多路信息。
    - 图证据指向未被召回的分块：新增一条 evidence_type=graph_relation 的原文引用，
      snippet 使用完整证据原文，不使用展示用的 240 字符摘要。
    """
    ranks = graph_rank or {}
    by_chunk: dict[tuple[str, str], int] = {}
    for index, citation in enumerate(citations):
        by_chunk[_citation_chunk_key(citation.doc_uuid, citation.chunk_uuid)] = index

    appended = 0
    for item in validated_relations:
        relation: GraphRelationRecord = item.relation
        chunk_index = item.chunk_index
        key = _citation_chunk_key(relation.evidence.doc_uuid, relation.evidence.chunk_uuid)
        location_key = (str(relation.evidence.doc_uuid), chunk_index)
        view = to_relation_view(relation)

        existing_index = by_chunk.get(key)
        if existing_index is not None:
            target = citations[existing_index]
            # 同一分块支持的不同关系都保留
            if view not in target.graph_relations:
                target.graph_relations.append(view)
            if "document" not in target.recall_sources:
                target.recall_sources.append("document")
            if "graph" not in target.recall_sources:
                target.recall_sources.append("graph")
            if target.graph_batch_uuid is None:
                target.graph_batch_uuid = relation.batch_uuid
            continue

        page_no, section_title = source_locations.get(
            location_key, (relation.evidence.page_no, relation.evidence.section_title)
        )
        marker = (
            str(relation.evidence.doc_uuid),
            chunk_index,
            relation.relation_type,
            relation.source_key,
            relation.target_key,
        )
        citation = Citation(
            doc_uuid=str(relation.evidence.doc_uuid),
            chunk_uuid=str(relation.evidence.chunk_uuid),
            title=item.title,
            source_module=item.source_module,
            page_no=page_no,
            section_title=section_title or relation.evidence.section_title,
            # 关系验证使用完整证据原文，不用 240 字符展示摘要
            snippet=relation.evidence.evidence_text or item.chunk_text_fallback,
            version=item.version,
            updated_at=item.updated_at,
            score=0.0,
            evidence_type="graph_relation",
            chunk_index=chunk_index,
            recall_sources=["graph"],
            graph_relations=[view],
            graph_batch_uuid=relation.batch_uuid,
            graph_rank=ranks.get(marker),
        )
        citations.append(citation)
        by_chunk[key] = len(citations) - 1
        appended += 1

    return citations, appended


def check_completeness(
    citations: list[Citation],
    validated_relations: list,
    *,
    settings,
    relation_question: bool,
    question: str | None = None,
) -> GraphCompletenessInfo:
    """关系类问题的证据完整性检查。

第一版限定能力，逐条说明：
1. 只对明确支持的问法生效（QUESTION_INTENT_RULES）。无法判断问法时不做拒答。
2. 所需关系类型由问法决定，而不是对所有关系问题统一要求全部关系类型。
3. 判定以**目标事项**为单位：该事项必须同时具备全部所需关系才算完整。
   - 只满足任意一项 → 不完整（不是「满足任意一项就算完整」）
   - A 事项的材料关系 + B 事项的部门关系 → 不完整（跨事项不能互相证明）
4. 无法从问题中确定目标事项时，返回保守状态（subject_uncertain=True），
   交由上层澄清，**不用任意召回事项代替用户所问事项**。
5. 图无结果或不可用时不做完整性拒答：此时检查文档证据是否已足够，
   避免把图缺失直接等同于答案不完整。
6. 高相似度与图路径存在都不作为完整性证明。
"""
    if not relation_question or not validated_relations:
        return GraphCompletenessInfo(is_complete=True, needs_clarification=False)

    intents = match_question_intents(question or "")
    if not intents:
        # 不在第一版支持的问法范围内：不做完整性判断，也不拒答
        return GraphCompletenessInfo(is_complete=True, needs_clarification=False)

    required: list[str] = []
    for intent in intents:
        for relation_type in intent.required_relations:
            if relation_type not in required:
                required.append(relation_type)
    if not required:
        return GraphCompletenessInfo(is_complete=True, needs_clarification=False)

    # 按事项实体分组（source_name 视为事项主体）
    subjects: dict[str, set[str]] = {}
    unknown_applicability = False
    for item in validated_relations:
        relation: GraphRelationRecord = item.relation
        subject = relation.source_name or relation.source_key
        bucket = subjects.setdefault(subject, set())
        if relation.relation_type in required:
            bucket.add(relation.relation_type)
        if not relation.applicability_known:
            unknown_applicability = True

    if not subjects:
        return GraphCompletenessInfo(is_complete=True, needs_clarification=False)

    # 确定目标事项：优先匹配用户问题中显式提到的事项名；
    # 匹配不到则不假定任意召回事项就是所问事项。
    target_subject = _resolve_target_subject(question or "", subjects)

    if target_subject is None:
        # 无法确定所问事项 → 保守返回，不做完整性证明
        return GraphCompletenessInfo(
            required_relations=required,
            satisfied_relations=[],
            missing_relations=list(required),
            is_complete=False,
            needs_clarification=True,
            unknown_applicability=unknown_applicability,
            subject_uncertain=True,
            candidate_subjects=sorted(subjects.keys()),
        )

    bucket_satisfied = subjects.get(target_subject, set())
    missing = [item for item in required if item not in bucket_satisfied]
    is_complete = not missing

    return GraphCompletenessInfo(
        required_relations=required,
        satisfied_relations=sorted(bucket_satisfied & set(required)),
        missing_relations=missing,
        is_complete=is_complete,
        # 缺任一必要关系即需要澄清，不输出确定结论
        needs_clarification=not is_complete,
        unknown_applicability=unknown_applicability,
        subject_uncertain=False,
        target_subject=target_subject,
    )


def _resolve_target_subject(question: str, subjects: dict[str, set[str]]) -> str | None:
    """从问题文本中确定目标事项。

    第一版采用可解释的字面匹配：问题里直接出现事项名即认为是所问事项。
    不做模糊匹配或语义推断——宁可返回 None（保守澄清），也不猜错事项。
    """
    text = question or ""
    # 归一化：去空白，避免排版差异导致漏匹配
    compact = "".join(text.split())
    matched = [
        subject
        for subject in subjects
        if subject and "".join(str(subject).split()) in compact
    ]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        # 命中多个事项，无法确定是哪一个
        return None
    # 问题未显式提到任何已召回事项：可能是别名或其他表述，不做猜测
    return None
