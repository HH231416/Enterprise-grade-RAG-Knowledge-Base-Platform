from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_trace_id
from app.core.metrics import qa_questions_total
from app.core.responses import success_response
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


router = APIRouter(prefix="/qa/conversations", tags=["qa-conversations"])


@router.post("", response_model=dict)
def create_conversation(
    request: CreateConversationRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    repo = QaConversationRepository(session)
    conversation_id = uuid4()
    title = request.title or "新对话"
    repo.create_conversation(conversation_id, title=title)

    data: dict = {"conversation_id": str(conversation_id), "title": title}
    if request.first_message:
        qa_service = QaService(session)
        result = qa_service.conversation_answer(
            question=request.first_message,
            history=None,
            top_k=8,
            generation_options=None,
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
):
    repo = QaConversationRepository(session)
    conversations = repo.list_conversations()
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
):
    repo = QaConversationRepository(session)
    conv = repo.get_conversation(conversation_id)
    if conv is None:
        from app.core.errors import AppError, ErrorCode
        raise AppError(
            code=ErrorCode.DOCUMENT_NOT_FOUND,
            message="conversation not found",
            status_code=404,
        )
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
):
    repo = QaConversationRepository(session)
    deleted = repo.delete_conversation(conversation_id)
    session.commit()
    return success_response({"conversation_id": str(conversation_id), "deleted": deleted}, trace_id)


@router.post("/{conversation_id}/messages", response_model=dict)
def send_message(
    conversation_id: UUID,
    request: SendMessageRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    repo = QaConversationRepository(session)
    conv = repo.get_conversation(conversation_id)
    if conv is None:
        from app.core.errors import AppError, ErrorCode
        raise AppError(
            code=ErrorCode.DOCUMENT_NOT_FOUND,
            message="conversation not found",
            status_code=404,
        )

    # 构建历史消息（取最近 10 轮即 20 条）
    db_messages = repo.get_messages(conversation_id)
    history = [
        {"role": m.role, "content": m.content}
        for m in db_messages[-20:]
    ]

    qa_service = QaService(session)
    result = qa_service.conversation_answer(
        question=request.question,
        history=history,
        top_k=request.top_k,
        generation_options=request.generation_options,
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
        ).model_dump(mode="json"),
        trace_id,
    )
