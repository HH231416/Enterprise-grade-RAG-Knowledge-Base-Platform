from __future__ import annotations

import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_settings_dep, get_trace_id, get_trusted_user_context
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.metrics import qa_questions_total
from app.core.responses import success_response
from app.graph.history_filter import load_history_for_model
from app.repositories.qa_conversation import QaConversationRepository
from app.schemas.qa_conversation import (
    ConversationListItem,
    ConversationListResponse,
    ConversationResponse,
    CreateConversationRequest,
    MessageResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from app.services.qa import QaService

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/qa/conversations", tags=["qa-conversations"])


def _resolve_permission_checker():
    """取当前权限检查器。不可用时返回 None，历史过滤会因此只按可检索状态判断。"""
    try:
        from app.permissions.loader import load_permission_checker

        return load_permission_checker()
    except Exception:  # noqa: BLE001
        logger.warning("permission checker unavailable for history filtering", exc_info=True)
        return None


def _require_conversation_owner(repo: QaConversationRepository, conversation_id: UUID, owner_user_id: str):
    """读取会话并校验归属。

    未归属的历史会话对任何普通用户都不可见，与不存在返回同样的 404，
    避免通过响应差异探测他人或无主会话是否存在。
    """
    conv = repo.get_owned_conversation(conversation_id, owner_user_id)
    if conv is None:
        raise AppError(
            code=ErrorCode.DOCUMENT_NOT_FOUND,
            message="conversation not found",
            status_code=404,
        )
    return conv


def _require_personal_user(trusted_user_context):
    """个人会话必须绑定可信登录身份，不接受匿名访问或客户端自报身份。"""
    if trusted_user_context is None or not trusted_user_context.user_id:
        raise AppError(
            code=ErrorCode.AUTHENTICATION_FAILED,
            message="authenticated user is required for personal conversations",
            status_code=401,
        )
    return trusted_user_context


@router.post("", response_model=dict)
def create_conversation(
    request: CreateConversationRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    trusted_user_context=Depends(get_trusted_user_context),
):
    user_context = _require_personal_user(trusted_user_context)
    repo = QaConversationRepository(session)
    conversation_id = uuid4()
    title = request.title or "新对话"
    repo.create_conversation(
        conversation_id,
        title=title,
        owner_user_id=user_context.user_id,
    )

    data: dict = {"conversation_id": str(conversation_id), "title": title}
    if request.first_message:
        qa_service = QaService(session)
        result = qa_service.conversation_answer(
            question=request.first_message,
            history=None,
            top_k=8,
            generation_options=None,
            user_context=user_context,
            trusted_user_context=True,
            authenticated_identity_required=settings.retrieval_authenticated_identity_required,
            graph_enabled=settings.graph_retrieval_enabled,
        )
        repo.add_message(
            message_id=uuid4(),
            conversation_id=conversation_id,
            role="user",
            content=request.first_message,
        )
        reply_id = uuid4()
        repo.add_message(
            message_id=reply_id,
            conversation_id=conversation_id,
            role="assistant",
            content=result.answer,
            citations={"citations": [c.model_dump(mode="json") for c in result.citations]},
            metadata={"latency_ms": result.latency_ms.model_dump(mode="json")},
        )
        # 用第一条消息更新标题
        title = request.first_message[:50] + ("..." if len(request.first_message) > 50 else "")
        repo.update_title(conversation_id, title)
        data["title"] = title
        data["answer"] = result.answer
        data["citations"] = [c.model_dump(mode="json") for c in result.citations]

    session.commit()
    return success_response(data, trace_id)


@router.get("", response_model=dict)
def list_conversations(
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    trusted_user_context=Depends(get_trusted_user_context),
):
    user_context = _require_personal_user(trusted_user_context)
    repo = QaConversationRepository(session)
    conversations = repo.list_conversations(owner_user_id=user_context.user_id)
    items: list[ConversationListItem] = []
    for conv in conversations:
        msg_count = repo.get_message_count(conv.conversation_id)
        messages = repo.get_messages(conv.conversation_id)
        last_msg = messages[-1].content[:100] if messages else None
        items.append(
            ConversationListItem(
                conversation_id=conv.conversation_id,
                title=conv.title,
                last_message=last_msg,
                message_count=msg_count,
                created_at=conv.created_at,
                updated_at=conv.updated_at,
            )
        )
    return success_response(
        ConversationListResponse(conversations=items, total=len(items)).model_dump(mode="json"),
        trace_id,
    )


@router.get("/{conversation_id}", response_model=dict)
def get_conversation(
    conversation_id: UUID,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    trusted_user_context=Depends(get_trusted_user_context),
):
    user_context = _require_personal_user(trusted_user_context)
    repo = QaConversationRepository(session)
    conv = _require_conversation_owner(repo, conversation_id, user_context.user_id)
    db_messages = repo.get_messages(conversation_id)
    messages = [
        MessageResponse(
            message_id=m.message_id,
            role=m.role,
            content=m.content,
            citations=m.citations.get("citations", []) if m.citations else None,
            metadata=m.metadata_json,
            created_at=m.created_at,
        )
        for m in db_messages
    ]
    return success_response(
        ConversationResponse(
            conversation_id=conv.conversation_id,
            title=conv.title,
            created_at=conv.created_at,
            updated_at=conv.updated_at,
            messages=messages,
        ).model_dump(mode="json"),
        trace_id,
    )


@router.delete("/{conversation_id}", response_model=dict)
def delete_conversation(
    conversation_id: UUID,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    trusted_user_context=Depends(get_trusted_user_context),
):
    user_context = _require_personal_user(trusted_user_context)
    repo = QaConversationRepository(session)
    _require_conversation_owner(repo, conversation_id, user_context.user_id)
    deleted = repo.delete_conversation(conversation_id)
    session.commit()
    return success_response({"conversation_id": str(conversation_id), "deleted": deleted}, trace_id)


@router.post("/{conversation_id}/messages", response_model=dict)
def send_message(
    conversation_id: UUID,
    request: SendMessageRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    trusted_user_context=Depends(get_trusted_user_context),
):
    user_context = _require_personal_user(trusted_user_context)
    repo = QaConversationRepository(session)
    # 先校验归属，再读取历史与调用检索/模型。
    _require_conversation_owner(repo, conversation_id, user_context.user_id)

    # 构建历史消息（取最近 10 轮即 20 条）。
    # 历史中已失效 / 当前无权访问 / 来源无法确认的助手回答不再传给模型，
    # 但数据库原始记录保持不变——这里只过滤模型输入，不改动已保存内容。
    history, history_filter = load_history_for_model(
        session,
        conversation_id,
        limit=20,
        permission_checker=_resolve_permission_checker(),
        user_context=user_context.model_dump(mode="json"),
    )
    if history_filter.dropped_count:
        logger.info(
            "dropped %d historical answers from model context for conversation %s",
            history_filter.dropped_count,
            conversation_id,
        )

    qa_service = QaService(session)
    result = qa_service.conversation_answer(
        question=request.question,
        history=history,
        top_k=request.top_k,
        generation_options=request.generation_options,
        user_context=user_context,
        trusted_user_context=True,
        authenticated_identity_required=settings.retrieval_authenticated_identity_required,
        graph_enabled=settings.graph_retrieval_enabled,
    )

    # 保存消息
    user_msg_id = uuid4()
    repo.add_message(
        message_id=user_msg_id,
        conversation_id=conversation_id,
        role="user",
        content=request.question,
    )
    reply_msg_id = uuid4()
    repo.add_message(
        message_id=reply_msg_id,
        conversation_id=conversation_id,
        role="assistant",
        content=result.answer,
        citations={"citations": [c.model_dump(mode="json") for c in result.citations]},
        metadata={"latency_ms": result.latency_ms.model_dump(mode="json")},
    )
    session.commit()

    qa_questions_total.inc()
    return success_response(
        SendMessageResponse(
            message_id=reply_msg_id,
            conversation_id=conversation_id,
            answer=result.answer,
            answer_status=result.answer_status,
            citations=result.citations,
            matched_documents=[md.model_dump(mode="json") for md in result.matched_documents],
            latency_ms=result.latency_ms.model_dump(mode="json"),
            graph_status=result.graph_status,
            graph_message=result.graph_message,
            graph_completeness=(
                result.graph_completeness.model_dump(mode="json")
                if result.graph_completeness is not None
                else None
            ),
        ).model_dump(mode="json"),
        trace_id,
    )
