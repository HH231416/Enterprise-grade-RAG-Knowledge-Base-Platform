"""候选关系程序校验。

校验范围（只覆盖可程序化判定的部分）：
- 受控实体类型与关系类型白名单
- 关系两端类型是否匹配该关系类型的允许形状
- 必需字段存在性与长度上限
- 实体名在原文分块中确实出现（原文锚点）
- evidence_text 逐字来自该分块
- 关系两端实体引用一致性（不得自环、不得同名不同类型歧义）
- 适用条件：未知保持 unknown，不接受模型补写

明确不能证明的：关系语义正确性。程序校验通过 ≠ 事实成立，
语义正确性由人工批次审核负责。
"""
from __future__ import annotations

import re

from app.graph.extractor import clamp_text, normalise_entity_name
from app.graph.types import (
    ALLOWED_RELATION_SHAPE,
    ENTITY_TYPES,
    MAX_EVIDENCE_LENGTH,
    MAX_NAME_LENGTH,
    RELATION_TYPES,
    CandidateValidationResult,
    EvidenceRef,
    GraphRelationRecord,
)

_WHITESPACE = re.compile(r"\s+")


def validate_chunk_relations(
    raw_relations: list[dict],
    *,
    doc_uuid: str,
    chunk_uuid: str,
    chunk_index: int,
    chunk_text: str,
    batch_uuid: str,
    source_fingerprint: str,
) -> CandidateValidationResult:
    """校验单个分块的抽取结果。"""
    result = CandidateValidationResult()
    normalised_chunk = _normalise_for_match(chunk_text)

    for raw in raw_relations:
        relation, errors = _validate_one(
            raw,
            doc_uuid=doc_uuid,
            chunk_uuid=chunk_uuid,
            chunk_index=chunk_index,
            normalised_chunk=normalised_chunk,
            chunk_text=chunk_text,
            batch_uuid=batch_uuid,
            source_fingerprint=source_fingerprint,
        )
        if errors:
            if relation is not None:
                result.rejected.append((relation, errors))
            else:
                result.errors.extend(errors)
            continue
        result.accepted.append(relation)

    return result


def _validate_one(
    raw: object,
    *,
    doc_uuid: str,
    chunk_uuid: str,
    chunk_index: int,
    normalised_chunk: str,
    chunk_text: str,
    batch_uuid: str,
    source_fingerprint: str,
) -> tuple[GraphRelationRecord | None, list[str]]:
    errors: list[str] = []

    if not isinstance(raw, dict):
        return None, ["relation item must be an object"]

    relation_type = str(raw.get("relation_type") or "").strip()
    if relation_type not in RELATION_TYPES:
        return None, [f"unsupported relation_type: {relation_type!r}"]

    source = raw.get("source")
    target = raw.get("target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        return None, ["source and target must be objects"]

    source_type = str(source.get("type") or "").strip()
    target_type = str(target.get("type") or "").strip()
    source_name = clamp_text(str(source.get("name") or "").strip(), MAX_NAME_LENGTH)
    target_name = clamp_text(str(target.get("name") or "").strip(), MAX_NAME_LENGTH)

    if source_type not in ENTITY_TYPES:
        errors.append(f"unsupported source entity type: {source_type!r}")
    if target_type not in ENTITY_TYPES:
        errors.append(f"unsupported target entity type: {target_type!r}")

    allowed_source, allowed_target = ALLOWED_RELATION_SHAPE.get(relation_type, (frozenset(), frozenset()))
    if source_type in ENTITY_TYPES and source_type not in allowed_source:
        errors.append(f"relation {relation_type} does not accept source type {source_type!r}")
    if target_type in ENTITY_TYPES and target_type not in allowed_target:
        errors.append(f"relation {relation_type} does not accept target type {target_type!r}")

    if not source_name:
        errors.append("source entity name is required")
    if not target_name:
        errors.append("target entity name is required")

    source_key = normalise_entity_name(source_name)
    target_key = normalise_entity_name(target_name)
    if source_key and source_key == target_key:
        # 自环关系在本域无业务意义，且通常是模型抄写错误
        errors.append("source and target entity must differ")

    evidence_text = clamp_text(str(raw.get("evidence_text") or "").strip(), MAX_EVIDENCE_LENGTH)
    if not evidence_text:
        errors.append("evidence_text is required: a relation without source evidence is rejected")
    elif _normalise_for_match(evidence_text) not in normalised_chunk:
        # 原文锚点校验：证据必须逐字来自该分块，否则不允许入库
        errors.append("evidence_text is not a verbatim excerpt of the source chunk")

    # 原文锚点：两个实体名都必须在该分块中出现
    for label, name in (("source", source_name), ("target", target_name)):
        if name and _normalise_for_match(name) not in normalised_chunk:
            errors.append(f"{label} entity name {name!r} not found in source chunk")

    applicability_raw = raw.get("applicability")
    applicability_known = False
    applicability_note = None
    if isinstance(applicability_raw, dict):
        known_flag = applicability_raw.get("known")
        note = str(applicability_raw.get("note") or "").strip()
        if known_flag is True and note:
            applicability_known = True
            applicability_note = clamp_text(note, MAX_EVIDENCE_LENGTH)
        elif known_flag is True and not note:
            # 声称已知但没有内容，按未知处理，不补写
            applicability_known = False
        elif known_flag not in (True, False, None):
            errors.append("applicability.known must be boolean")
    elif applicability_raw is not None:
        errors.append("applicability must be an object")

    relation = GraphRelationRecord(
        relation_type=relation_type,
        source_key=source_key,
        source_type=source_type,
        source_name=source_name,
        target_key=target_key,
        target_type=target_type,
        target_name=target_name,
        evidence=EvidenceRef(
            doc_uuid=doc_uuid,
            chunk_uuid=chunk_uuid,
            chunk_index=chunk_index,
            evidence_text=evidence_text,
            section_title=None,
            page_no=None,
        ),
        batch_uuid=batch_uuid,
        source_fingerprint=source_fingerprint,
        applicability=applicability_note,
        applicability_known=applicability_known,
    )

    if errors:
        return relation, errors
    return relation, []


def _normalise_for_match(text: str) -> str:
    """归一化用于原文包含判断：去除所有空白，避免排版差异导致误判。"""
    return _WHITESPACE.sub("", str(text or "")).lower()
