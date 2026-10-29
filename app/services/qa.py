from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.trace import get_trace_id
from app.graph.evidence import is_relation_question
from app.integrations.llm import LlmProvider
from app.models.llm_call_log import LlmCallLog
from app.repositories.llm_call_logs import LlmCallLogRepository
from app.schemas.qa import AnswerRequest, AnswerResponseData, LatencyBreakdown, MatchedDocument
from app.schemas.retrieval import SearchRequest, UserContext
from app.services.retrieval import RetrievalService

logger = logging.getLogger(__name__)


def _build_incomplete_answer(completeness) -> str:
    """证据不完整时的有边界回答：说明缺失什么，不输出确定结论。"""
    if completeness.subject_uncertain:
        candidates = "、".join(completeness.candidate_subjects[:3])
        parts = [
            "当前检索到多个办事事项，无法确定你所问的是哪一个，因此不能给出确定结论。",
            f"本次召回的事项包括：{candidates}。请补充具体事项名称后重试。",
        ]
        if candidates and len(completeness.candidate_subjects) > 3:
            parts[1] += " 等"
        return "\n".join(parts)

    missing = "、".join(completeness.missing_relations)
    parts = [
        f"当前检索到的资料未覆盖「{completeness.target_subject}」的完整信息，缺少：{missing}。",
        "建议补充具体事项名称或限定查询范围后重试；在依据补齐前不作确定结论。",
    ]
    if completeness.unknown_applicability:
        parts.append("注意：部分依据的适用条件未知，是否适用于当前情况需以原文条款为准。")
    return "\n".join(parts)


class QaService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.retrieval = RetrievalService(session)
        self.llm_provider = LlmProvider(self.settings)
        self.llm_logs = LlmCallLogRepository(session)

    def answer(
        self,
        request: AnswerRequest,
        *,
        trusted_user_context: bool = False,
        authenticated_identity_required: bool = False,
    ) -> AnswerResponseData:
        started = time.perf_counter()
        retrieval_started = time.perf_counter()
        retrieval_result = self.retrieval.search(
            SearchRequest(
                query=request.question,
                top_k=request.top_k,
                filters=request.filters,
                user_context=request.user_context,
            ),
            trusted_user_context=trusted_user_context,
            authenticated_identity_required=authenticated_identity_required,
        )
        retrieval_latency = int((time.perf_counter() - retrieval_started) * 1000)

        generation_started = time.perf_counter()
        answer_status = "grounded"
        if retrieval_result.hits and self._has_sufficient_evidence(
            retrieval_result.hits,
            evidence_threshold=self.settings.evidence_threshold_dense,
        ):
            if self.llm_provider.is_configured():
                llm_result = self.llm_provider.generate_answer_with_fallback(
                    question=request.question,
                    citations=retrieval_result.hits,
                    generation_options=request.generation_options,
                    fallback_builder=self._build_grounded_answer,
                )
                if llm_result is not None:
                    answer = llm_result.answer
                    self._record_llm_log(llm_result)
                else:
                    answer = self._build_grounded_answer(retrieval_result.hits)
            else:
                answer = self._build_grounded_answer(retrieval_result.hits)
        else:
            answer_status = "insufficient_evidence"
            answer = "未找到足够依据，请缩小范围或补充筛选条件后重试。"
        generation_latency = int((time.perf_counter() - generation_started) * 1000)
        total_latency = int((time.perf_counter() - started) * 1000)

        matched_documents = []
        seen_doc_ids = set()
        for citation in retrieval_result.hits:
            if citation.doc_uuid in seen_doc_ids:
                continue
            seen_doc_ids.add(citation.doc_uuid)
            matched_documents.append(
                MatchedDocument(
                    doc_uuid=citation.doc_uuid,
                    title=citation.title,
                    score=citation.score,
                )
            )

        return AnswerResponseData(
            answer=answer,
            answer_status=answer_status,
            citations=retrieval_result.hits,
            matched_documents=matched_documents,
            filters_applied=retrieval_result.filters_applied,
            latency_ms=LatencyBreakdown(
                retrieval=retrieval_latency,
                generation=generation_latency,
                total=total_latency,
            ),
        )

    @staticmethod
    def _build_search_params(question: str, top_k: int) -> dict:
        """检索策略参数透传通道。键名与 SearchRequest.strategy_params 对齐。"""
        return {"top_k": top_k}

    @staticmethod
    def _build_grounded_answer(citations) -> str:
        snippets = []
        for citation in citations[:3]:
            location = citation.section_title or citation.sheet_name or (f"page {citation.page_no}" if citation.page_no else "chunk")
            snippets.append(f"- {citation.title}（{location}，score={citation.score:.3f}）：{citation.snippet}")
        return "基于命中的引用片段，当前可确认的信息如下：\n" + "\n".join(snippets)

    @staticmethod
    def _has_sufficient_evidence(citations, *, evidence_threshold: float) -> bool:
        if not citations:
            return False
        top = citations[0]
        # RRF 融合分是排名信号，门控改用 Dense 余弦分（vector_score）
        if top.vector_score is not None and top.vector_score < evidence_threshold:
            return False
        # text_score 为 None 时跳过检查（纯向量检索模式）
        if top.text_score is not None and top.text_score < 0.2:
            return False
        return True

    def _record_llm_log(self, llm_result) -> None:
        status = "fallback" if llm_result.provider_name == "local-fallback" else "success"
        self.llm_logs.add(
            LlmCallLog(
                trace_id=get_trace_id(),
                provider_type="llm",
                provider_name=llm_result.provider_name,
                model_name=llm_result.model_name,
                request_type="chat.completions",
                request_tokens=llm_result.request_tokens,
                response_tokens=llm_result.response_tokens,
                latency_ms=llm_result.latency_ms,
                status=status,
            )
        )
        self.session.commit()

    def conversation_answer(
        self,
        question: str,
        history: list[dict] | None = None,
        top_k: int = 8,
        generation_options=None,
        user_context: UserContext | None = None,
        *,
        trusted_user_context: bool = False,
        authenticated_identity_required: bool = False,
        graph_enabled: bool | None = None,
    ) -> AnswerResponseData:
        started = time.perf_counter()
        retrieval_started = time.perf_counter()
        search_params = self._build_search_params(question, top_k)
        if graph_enabled is not None:
            search_params["graph_enabled"] = graph_enabled
        retrieval_result = self.retrieval.search(
            SearchRequest(
                query=question,
                top_k=top_k,
                filters=None,
                user_context=user_context,
                strategy_params=search_params,
            ),
            trusted_user_context=trusted_user_context,
            authenticated_identity_required=authenticated_identity_required,
        )
        retrieval_latency = int((time.perf_counter() - retrieval_started) * 1000)

        graph_status: str | None = None
        graph_message: str | None = None
        citations = retrieval_result.hits
        completeness = None

        # 图增强检索：按需启用。图存储不可用时降级为纯文档检索；
        # 身份、权限或来源完整性失败不在此吞掉，会由检索层直接抛出。
        if graph_enabled and self.settings.graph_enabled:
            from app.graph.anchors import (
                GraphAnchorResolver,
                load_chunk_indexes,
            )
            from app.graph.evidence import check_completeness, merge_graph_into_citations
            from app.graph.retrieval import GraphRetrievalService

            # 实体锚点复用既有向量召回结果：问句→实体由已上线的 Dense 检索承担，
            # 图检索只做「实体→关系」的图遍历。
            # 优先用 Citation.chunk_index；缺失时按 chunk_uuid 从业务库补齐，
            # 使锚点收敛到命中的具体分块，而不是整篇文档的全部实体。
            chunk_refs = [
                (citation.doc_uuid, citation.chunk_index)
                for citation in citations
                if citation.chunk_index is not None
            ]
            if not chunk_refs:
                chunk_refs = load_chunk_indexes(
                    self.session,
                    [(c.doc_uuid, c.chunk_uuid) for c in citations],
                )

            anchors = GraphAnchorResolver(self.session).resolve(
                citation_chunks=chunk_refs,
            )
            if anchors.is_degraded:
                # 存储不可用/读取失败：如实标记 unavailable，回答降级为纯文档检索
                graph_status = "unavailable"
                graph_message = anchors.message or "graph anchor resolution unavailable"
                logger.warning("graph anchor resolution degraded: %s", graph_message)
            elif not anchors.has_anchors:
                # 确实没有可用锚点（无已发布关系等），与故障区分
                graph_status = anchors.status if anchors.status != "ok" else "empty"
                graph_message = anchors.message or "no anchor entity resolved from retrieval hits"
            else:
                graph_service = GraphRetrievalService(self.session)
                graph_result = graph_service.search(
                    query=question,
                    settings=self.settings,
                    anchor_keys=anchors.entity_keys,
                )
                graph_status = graph_result.status
                graph_message = graph_result.message

                if graph_result.status == "ok" and graph_result.validated:
                    # search() 已完成批次与来源快照校验，此处直接使用校验结果
                    validated = graph_result.validated
                    source_locations = {
                        (item.relation.evidence.doc_uuid, item.chunk_index): (
                            item.page_no,
                            item.section_title,
                        )
                        for item in validated
                    }
                    citations, _ = merge_graph_into_citations(
                        citations,
                        validated,
                        source_locations=source_locations,
                    )
                    completeness = check_completeness(
                        citations,
                        validated,
                        settings=self.settings,
                        relation_question=is_relation_question(question),
                        question=question,
                    )
                elif graph_result.status == "unavailable":
                    # 降级：明确记录状态，回答仍基于文档检索证据
                    logger.warning("graph retrieval degraded: %s", graph_result.message)

        hits = citations

        generation_started = time.perf_counter()
        answer_status = "grounded"

        # 关系类问题证据不完整时，不输出确定结论
        if completeness is not None and completeness.needs_clarification:
            answer_status = "insufficient_evidence"
            answer = _build_incomplete_answer(completeness)
        elif hits and self._has_sufficient_evidence(
            hits,
            evidence_threshold=self.settings.evidence_threshold_dense,
        ):
            if self.llm_provider.is_configured():
                llm_result = self.llm_provider.generate_answer_with_fallback(
                    question=question,
                    citations=hits,
                    generation_options=generation_options,
                    fallback_builder=self._build_grounded_answer,
                    history=history,
                )
                if llm_result is not None:
                    answer = llm_result.answer
                    self._record_llm_log(llm_result)
                else:
                    answer = self._build_grounded_answer(hits)
            else:
                answer = self._build_grounded_answer(hits)
        else:
            answer_status = "insufficient_evidence"
            answer = "未找到足够依据，请缩小范围或补充筛选条件后重试。"
        generation_latency = int((time.perf_counter() - generation_started) * 1000)
        total_latency = int((time.perf_counter() - started) * 1000)

        matched_documents = []
        seen_doc_ids = set()
        for citation in hits:
            if citation.doc_uuid in seen_doc_ids:
                continue
            seen_doc_ids.add(citation.doc_uuid)
            matched_documents.append(
                MatchedDocument(
                    doc_uuid=citation.doc_uuid,
                    title=citation.title,
                    score=citation.score,
                )
            )

        return AnswerResponseData(
            answer=answer,
            answer_status=answer_status,
            citations=hits,
            matched_documents=matched_documents,
            filters_applied=retrieval_result.filters_applied,
            latency_ms=LatencyBreakdown(
                retrieval=retrieval_latency,
                generation=generation_latency,
                total=total_latency,
            ),
            graph_status=graph_status,
            graph_message=graph_message,
            graph_completeness=completeness,
        )
