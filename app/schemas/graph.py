"""图谱管理接口的请求与响应模型。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.schemas.common import AppBaseModel


class BuildBatchRequest(AppBaseModel):
    doc_uuid: str


class ApproveBatchRequest(AppBaseModel):
    # 被管理员剔除的候选；未列出的 pending 候选视为批准
    rejected_candidate_uuids: list[UUID] = []


class GraphBatchSummaryResponse(AppBaseModel):
    batch_uuid: str
    doc_uuid: str
    doc_version: str
    status: str
    extraction_result: str | None = None
    access_scope: str
    source_fingerprint: str
    chunk_count: int
    candidate_count: int
    approved_count: int
    rejected_count: int
    retry_count: int
    extractor_name: str | None = None
    prompt_template_version: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    published_at: datetime | None = None


class GraphCandidateResponse(AppBaseModel):
    candidate_uuid: str
    doc_uuid: str
    chunk_uuid: str
    chunk_index: int
    relation_type: str
    source_entity_name: str
    source_entity_type: str
    target_entity_name: str
    target_entity_type: str
    # 原文证据，供人工核验；不是无来源三元组
    evidence_text: str
    evidence_section: str | None = None
    evidence_page_no: int | None = None
    applicability: str | None = None
    applicability_known: bool = False
    review_status: str


class GraphBatchDetailResponse(AppBaseModel):
    found: bool
    batch: GraphBatchSummaryResponse | None = None
    candidates: list[GraphCandidateResponse] = []


class PublishBatchResponse(AppBaseModel):
    batch_uuid: str
    published: bool
    status: str
    approved_count: int = 0
    rejected_count: int = 0
    message: str | None = None
