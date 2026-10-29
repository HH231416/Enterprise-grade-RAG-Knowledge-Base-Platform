from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas.common import AppBaseModel


class UserContext(AppBaseModel):
    user_id: str
    roles: list[str] = []
    departments: list[str] = []
    is_super_admin: bool = False
    is_authenticated: bool = False
    is_trusted_identity: bool = False
    is_session_identity: bool = False
    is_external_identity: bool = False
    is_jit_user: bool = False


class RetrievalFilters(AppBaseModel):
    source_module: list[str] | None = None
    source_type: list[str] | None = None
    file_ext: list[str] | None = None
    access_level: list[str] | None = None
    owner_dept: list[str] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


class SearchRequest(AppBaseModel):
    query: str
    top_k: int = 8
    min_score: float = 0.2
    strategy: Literal["hybrid"] = "hybrid"
    strategy_params: dict | None = None
    filters: RetrievalFilters | None = None
    user_context: UserContext | None = None


class GraphRelationView(AppBaseModel):
    """图关系路径。仅作辅助解释，事实成立以原文引用为准。"""

    relation_type: str
    source_entity_name: str
    source_entity_type: str
    target_entity_name: str
    target_entity_type: str
    applicability: str | None = None
    applicability_known: bool = False


class Citation(AppBaseModel):
    doc_uuid: str
    chunk_uuid: str
    title: str
    source_module: str
    page_no: int | None = None
    sheet_name: str | None = None
    section_title: str | None = None
    snippet: str
    version: str
    updated_at: datetime
    score: float
    vector_score: float | None = None
    text_score: float | None = None
    sparse_score: float | None = None
    rerank_score: float | None = None
    pre_rerank_score: float | None = None
    image_url: str | None = None
    # 完整分块原文。snippet 是 240 字符展示摘要，仅供 UI；
    # 生成回答与关系验证必须使用本字段，避免用摘要代替原文证据。
    full_text: str | None = None

    # ---- 图增强字段（向后兼容：不改变既有字段语义，图功能关闭时全为空）----
    # document = 原有文档检索证据；graph_relation = 由图关系补充的原文支撑
    evidence_type: Literal["document", "graph_relation"] = "document"
    chunk_index: int | None = None
    # 该证据由哪些召回路径命中（document / graph），保留多路命中信息
    recall_sources: list[str] = []
    graph_relations: list[GraphRelationView] = []
    graph_batch_uuid: str | None = None
    # 召回来源与排序信息。graph_rank 是排序信号，不是事实正确概率。
    graph_rank: int | None = None
    graph_expand_limit_hit: bool = False


class GraphCompletenessInfo(AppBaseModel):
    """关系类问题的证据完整性检查结果。

    缺失必要关系时不得假装完整回答，应澄清或给出有边界的部分回答。
    """

    required_relations: list[str] = []
    satisfied_relations: list[str] = []
    missing_relations: list[str] = []
    is_complete: bool = True
    needs_clarification: bool = False
    # 未知适用条件提示。条件未知时不擅自下结论。
    unknown_applicability: bool = False
    # 无法从问题中确定所问事项。不猜测，保守返回待澄清。
    subject_uncertain: bool = False
    # 判定所依据的目标事项；subject_uncertain 时为空
    target_subject: str | None = None
    # 已召回但未能确定所问的事项，供澄清提示使用
    candidate_subjects: list[str] = []


class SearchResponseData(AppBaseModel):
    query: str
    rewritten_query: str
    filters_applied: dict
    hits: list[Citation]
    latency_ms: int
    # 图检索状态：disabled / empty / ok / unavailable
    graph_status: str | None = None
    graph_message: str | None = None
    graph_completeness: GraphCompletenessInfo | None = None


class DenseHitDebug(AppBaseModel):
    chunk_uuid: str
    doc_uuid: str
    score: float


class SparseHitDebug(AppBaseModel):
    chunk_uuid: str
    doc_uuid: str
    score: float


class DebugSearchResponseData(SearchResponseData):
    raw_filters: dict
    user_context: dict
    ranking_debug: list[dict]
    retrieval_strategy: str = "hybrid"
    dense_hits: list[DenseHitDebug] = []
    sparse_hits: list[SparseHitDebug] = []
    rrf_k: float | None = None
    rerank_enabled: bool = False
    rerank_model: str | None = None
    rerank_latency_ms: int | None = None


class StrategyInfo(AppBaseModel):
    name: str
    label: str
    description: str
    requires: list[str]


class RerankConfig(AppBaseModel):
    enabled: bool = False
    model: str | None = None
    api_base: str | None = None


class StrategiesResponse(AppBaseModel):
    strategies: list[StrategyInfo]
    rerank: RerankConfig
