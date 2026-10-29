from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.schemas.common import AppBaseModel
from app.schemas.qa import GenerationOptions
from app.schemas.retrieval import Citation, GraphCompletenessInfo


class MessageResponse(AppBaseModel):
    message_id: UUID
    role: str
    content: str
    citations: list[Citation] | None = None
    metadata: dict | None = None
    created_at: datetime


class ConversationResponse(AppBaseModel):
    conversation_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse] = []


class ConversationListItem(AppBaseModel):
    conversation_id: UUID
    title: str
    last_message: str | None = None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(AppBaseModel):
    conversations: list[ConversationListItem]
    total: int


class CreateConversationRequest(AppBaseModel):
    title: str | None = None
    first_message: str | None = None


class SendMessageRequest(AppBaseModel):
    question: str
    top_k: int = 8
    generation_options: GenerationOptions | None = None


class SendMessageResponse(AppBaseModel):
    message_id: UUID
    conversation_id: UUID
    answer: str
    answer_status: str = "grounded"
    citations: list[Citation]
    matched_documents: list[dict]
    latency_ms: dict
    # 图增强状态，图功能关闭时为 "disabled"
    graph_status: str | None = None
    graph_message: str | None = None
    graph_completeness: GraphCompletenessInfo | None = None
