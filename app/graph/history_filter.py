"""历史上下文证据过滤。

会话历史消息持久化了生成当时的结构化 citations。若其中某份资料事后被撤回、
删除或改为无权访问，历史回答正文仍可能包含这些知识。

本模块在**构造新一轮模型上下文时**核对历史回答所关联的来源：
- 原始历史记录保持不变，不删除、不改写；
- 已失效 / 当前无权访问 / 来源无法确认的历史回答，不作为可用知识传给模型。

这样既满足「不能将失权历史证据继续传给模型」，也不涉及页面展示策略。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.repositories.qa_conversation import QaConversationRepository
from app.services.eligibility import apply_document_eligibility
from app.services.permission_fields import document_to_permission_dict

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HistoryFilterResult:
    """过滤结果，供调用方与测试观察。"""

    kept: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    # 被丢弃的历史消息 id，便于排查而不影响原始数据
    dropped_message_ids: list[str] = field(default_factory=list)

    @property
    def kept_count(self) -> int:
        return len(self.kept)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


def filter_history_for_model(
    session: Session,
    conversation_id,
    history: list[dict],
    *,
    permission_checker=None,
    user_context: dict | None = None,
) -> HistoryFilterResult:
    """构造模型上下文前的历史过滤。

    只保留当前仍可作为知识依据的历史回答：其引用来源全部仍处于可检索状态，
    且当前用户仍有访问权限。原始数据库记录不变。
    """
    result = HistoryFilterResult()
    if not history:
        return result

    # 历史结构化 citations 只在 DB 中；按 message_id 批量取
    citations_by_message = _load_citations(session, conversation_id)

    # 预先判定当前可访问的文档集合，避免逐条重复检查
    accessible = _accessible_doc_map(
        session,
        permission_checker=permission_checker,
        user_context=user_context,
    )
    # 分块与版本状态用于判断历史引用是否仍对应当前内容
    chunk_versions = _load_chunk_state(session)

    for item in history:
        role = item.get("role")
        # 用户自己的提问始终保留：它不是知识证据，且是会话连贯性所需
        if role != "assistant":
            result.kept.append(item)
            continue

        message_id = str(item.get("message_id") or "")
        citations = citations_by_message.get(message_id) or []
        verdict = _assess(citations, accessible, chunk_versions)
        if verdict["usable"]:
            result.kept.append(item)
        else:
            result.dropped.append(item)
            if message_id:
                result.dropped_message_ids.append(message_id)

    return result


def _assess(
    citations: list[dict],
    accessible: dict[str, bool] | None,
    _chunk_versions: dict | None,
) -> dict[str, Any]:
    """判断一条历史回答的证据是否仍可作为知识使用。

    - 无结构化引用：来源无法确认 → 不可用（不把来源不明的知识当权威证据）
    - 任一引用来源已失效或当前无权访问 → 不可用
    """
    if not citations:
        return {"usable": False, "reason": "no_structured_citations"}

    if accessible is None:
        # 无法判定可访问集合时，不把历史证据当作权威知识
        return {"usable": False, "reason": "access_state_unknown"}

    for citation in citations:
        doc_uuid = str(citation.get("doc_uuid") or "")
        if not doc_uuid:
            return {"usable": False, "reason": "citation_missing_doc_uuid"}
        if doc_uuid not in accessible:
            return {"usable": False, "reason": "source_not_accessible"}
        if not accessible[doc_uuid]:
            return {"usable": False, "reason": "access_denied"}

    # 分块与版本有效性核对：
    # 同一 doc_uuid 的政策内容可能已更新且仍处于 active 状态。
    # 历史回答引用的是旧分块/旧版本时，不能继续当作当前权威知识。
    validity = _chunk_versions
    if validity is None:
        return {"usable": False, "reason": "chunk_state_unknown"}
    for citation in citations:
        doc_uuid = str(citation.get("doc_uuid") or "")
        chunk_uuid = str(citation.get("chunk_uuid") or "")
        doc_version = str(citation.get("version") or "")

        if chunk_uuid:
            if chunk_uuid not in validity:
                # 引用的分块已被重建或删除，历史引用不再是当前内容的依据
                return {"usable": False, "reason": "chunk_no_longer_exists"}
            live_doc_uuids, live_version = validity[chunk_uuid]
            # live_doc_uuids 是该分块当前所属的文档集合：
            # 分块仍在，但已不属于引用的那份文档 → 引用关系失效
            if doc_uuid not in live_doc_uuids:
                return {"usable": False, "reason": "chunk_not_in_document"}
            # 版本变化说明内容已改写，旧回答的依据不再对应当前原文
            if doc_version and live_version and doc_version != live_version:
                return {"usable": False, "reason": "document_version_changed"}

    return {"usable": True, "reason": None}


def _load_chunk_state(session: Session) -> dict[str, tuple[set[str], str]] | None:
    """返回 chunk_uuid → (所属文档集合, 当前文档版本)。

    用于判断历史引用指向的分块是否仍属于该文档、文档版本是否已变化。
    无法确定时返回 None，调用方据此保守丢弃。
    """
    try:
        rows = session.execute(
            select(
                DocumentChunk.chunk_uuid,
                DocumentChunk.doc_uuid,
                Document.version,
            ).join(
                Document,
                Document.doc_uuid == DocumentChunk.doc_uuid,
            ).where(DocumentChunk.deleted_at.is_(None))
        ).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("history filter chunk state query failed: %s", exc)
        return None

    mapping: dict[str, tuple[set[str], str]] = {}
    for chunk_uuid, doc_uuid, version in rows:
        key = str(chunk_uuid)
        doc_key = str(doc_uuid)
        current = mapping.get(key)
        if current is None:
            mapping[key] = ({doc_key}, str(version or ""))
        else:
            current[0].add(doc_key)
    return mapping


def _load_citations(session: Session, conversation_id) -> dict[str, list[dict]]:
    from app.models.qa_conversation import QaConversationMessage

    rows = session.execute(
        select(
            QaConversationMessage.message_id,
            QaConversationMessage.role,
            QaConversationMessage.citations,
        ).where(QaConversationMessage.conversation_id == conversation_id)
    ).all()

    mapping: dict[str, list[dict]] = {}
    for message_id, role, citations in rows:
        if role != "assistant":
            continue
        payload = citations or {}
        items = payload.get("citations") if isinstance(payload, dict) else None
        if isinstance(items, list):
            mapping[str(message_id)] = [c for c in items if isinstance(c, dict)]
    return mapping


def _accessible_doc_map(
    session: Session,
    *,
    permission_checker=None,
    user_context: dict | None = None,
) -> dict[str, bool] | None:
    """返回 doc_uuid → 当前是否仍可访问。

    判定 = 统一可检索条件（未撤回/未删除/已审核/已索引）AND 权限检查通过。
    任一环节无法判定时返回 None，表示状态未知。
    """
    stmt = apply_document_eligibility(select(Document))
    documents = list(session.scalars(stmt).all())
    if not documents:
        return {}

    # 复用检索层的权限字段构造，避免漏 owner_role 等字段导致语义不一致
    payload = [document_to_permission_dict(doc) for doc in documents]

    if permission_checker is None:
        # 无权限检查器时只按可检索状态判断，不做访问控制
        return {item["doc_uuid"]: True for item in payload}

    try:
        allowed = permission_checker.filter_documents(payload, user_context)
    except Exception as exc:  # noqa: BLE001
        logger.warning("history filter permission check failed: %s", exc)
        return None

    allowed_ids = {str(item.get("doc_uuid")) for item in allowed or []}
    return {item["doc_uuid"]: item["doc_uuid"] in allowed_ids for item in payload}


def load_history_for_model(
    session: Session,
    conversation_id,
    *,
    limit: int = 20,
    permission_checker=None,
    user_context: dict | None = None,
) -> tuple[list[dict], HistoryFilterResult]:
    """读取历史并过滤，返回 (可用于模型的 history, 过滤结果)。"""
    repo = QaConversationRepository(session)
    messages = repo.get_messages(conversation_id)

    raw = [
        {
            "role": item.role,
            "content": item.content,
            "message_id": str(item.message_id),
        }
        for item in messages[-limit:]
    ]
    result = filter_history_for_model(
        session,
        conversation_id,
        raw,
        permission_checker=permission_checker,
        user_context=user_context,
    )
    return result.kept, result


__all__ = [
    "HistoryFilterResult",
    "filter_history_for_model",
    "load_history_for_model",
]
