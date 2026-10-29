from __future__ import annotations

from app.schemas.common import AppBaseModel
from app.schemas.retrieval import (
    Citation,
    GraphCompletenessInfo,
    RetrievalFilters,
    UserContext,
)


class GenerationOptions(AppBaseModel):
    temperature: float = 0.1
    max_tokens: int = 1200


class AnswerRequest(AppBaseModel):
    question: str
    top_k: int = 8
    filters: RetrievalFilters | None = None
    user_context: UserContext | None = None
    generation_options: GenerationOptions | None = None


class MatchedDocument(AppBaseModel):
    doc_uuid: str
    title: str
    score: float


class LatencyBreakdown(AppBaseModel):
    retrieval: int
    generation: int
    total: int


class AnswerResponseData(AppBaseModel):
    answer: str
    answer_status: str = "grounded"
    citations: list[Citation]
    matched_documents: list[MatchedDocument]
    filters_applied: dict
    latency_ms: LatencyBreakdown
    # 图增强状态。关闭或无结果时为 None / "disabled" / "empty"，
    # 与既有接口兼容：不改变既有字段语义。
    graph_status: str | None = None
    graph_message: str | None = None
    graph_completeness: GraphCompletenessInfo | None = None
