"""QA Agent 服务：基于有限状态机的智能问答系统。

状态机流程：
  plan → retrieve → [rewrite] → answer → finish

核心机制：
- 证据门控 (Evidence Gate)：检索后评估证据是否充分，不足则触发查询改写
- 查询改写 (Query Rewrite)：证据不足时用 LLM 重新表述问题再检索
- 最多 2 轮检索，防止无限循环
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

import logging  # 日志：记录 LLM 降级等异常事件
import re       # 正则：用于提取改写后的查询
import time     # 计时：记录各阶段耗时

logger = logging.getLogger(__name__)

from sqlalchemy.orm import Session

from app.core.config import get_settings  # 配置单例
from app.core.trace import get_trace_id   # 链路追踪 ID
from app.integrations.llm import LlmProvider  # LLM 调用提供者
from app.models.llm_call_log import LlmCallLog  # LLM 调用日志模型
from app.repositories.llm_call_logs import LlmCallLogRepository  # LLM 日志仓库
from app.schemas.qa import MatchedDocument  # 匹配文档 Schema
from app.schemas.qa_agent import (
    AgentAnswerRequest,        # Agent 问答请求
    AgentAnswerResponseData,   # Agent 问答响应
    AgentLatencyBreakdown,     # 耗时明细
    AgentTraceStep,            # 追踪步骤
)
from app.schemas.retrieval import Citation, SearchRequest, SearchResponseData  # 检索相关 Schema
from app.services.retrieval import RetrievalService  # 检索服务


class QaAgentService:
    """有界 QA Agent：检索 → 可选改写一次 → 回答。

    状态机执行顺序：
    1. plan：评估问题，确定检索策略
    2. retrieve：执行检索，获取候选文档
    3. [rewrite]：证据不足时用 LLM 改写查询，再次检索
    4. answer：基于检索结果用 LLM 生成最终回答
    5. finish：汇总结果，返回响应
    """

    def __init__(self, session: Session) -> None:
        self.session = session                        # 数据库会话
        self.settings = get_settings()                # 全局配置
        self.retrieval = RetrievalService(session)    # 检索服务
        self.llm_provider = LlmProvider(self.settings)
        self.llm_logs = LlmCallLogRepository(session)

    def answer(
        self,
        request: AgentAnswerRequest,
        *,
        trusted_user_context: bool = False,
        authenticated_identity_required: bool = False,
    ) -> AgentAnswerResponseData:
        started = time.perf_counter()
        planning_started = time.perf_counter()
        trace: list[AgentTraceStep] = []

        current_query = self._normalize_query(request.question)
        trace.append(
            AgentTraceStep(
                state="plan",
                action="plan_retrieval",
                query=current_query,
                notes="先检索一次，证据不足时最多进行一次查询改写重试。",
            )
        )
        planning_latency = int((time.perf_counter() - planning_started) * 1000)

        total_retrieval_latency = 0
        best_result: SearchResponseData | None = None
        executed_rounds = 0

        for round_index in range(request.max_retrieval_rounds):
            executed_rounds += 1
            retrieval_started = time.perf_counter()
            retrieval_result = self.retrieval.search(
                SearchRequest(
                    query=current_query,
                    top_k=request.top_k,
                    strategy=request.retrieval_strategy,
                    filters=request.filters,
                    user_context=request.user_context,
                ),
                trusted_user_context=trusted_user_context,
                authenticated_identity_required=authenticated_identity_required,
            )
            total_retrieval_latency += int((time.perf_counter() - retrieval_started) * 1000)

            evidence_threshold = self._evidence_threshold(request.retrieval_strategy)
            sufficient = self._has_sufficient_evidence(
                retrieval_result.hits,
                evidence_threshold=evidence_threshold,
            )
            top_score = retrieval_result.hits[0].score if retrieval_result.hits else None
            trace.append(
                AgentTraceStep(
                    state="retrieve",
                    action=f"retrieve_round_{round_index + 1}",
                    query=current_query,
                    hit_count=len(retrieval_result.hits),
                    top_score=top_score,
                    sufficient_evidence=sufficient,
                    evidence_threshold=evidence_threshold,
                    notes=f"使用 {request.retrieval_strategy} 检索策略并应用策略阈值。",
                )
            )

            best_result = self._pick_better_result(best_result, retrieval_result)
            if sufficient:
                break
            if round_index + 1 >= request.max_retrieval_rounds:
                break

            rewritten_query = self._rewrite_query(
                original_question=request.question,
                current_query=current_query,
                retrieval_result=retrieval_result,
            )
            if rewritten_query == current_query:
                trace.append(
                    AgentTraceStep(
                        state="finish",
                        action="skip_rewrite",
                        query=current_query,
                        notes="未生成更有效的改写查询，结束重试。",
                    )
                )
                break
            trace.append(
                AgentTraceStep(
                    state="rewrite",
                    action="rewrite_query",
                    query=rewritten_query,
                    notes="首轮证据不足，执行一次查询改写后重试。",
                )
            )
            current_query = rewritten_query

        generation_started = time.perf_counter()
        result_to_use = best_result or self._empty_result(request.question)
        final_threshold = self._evidence_threshold(request.retrieval_strategy)
        if result_to_use.hits and self._has_sufficient_evidence(
            result_to_use.hits,
            evidence_threshold=final_threshold,
        ):
            try:
                answer = self._generate_grounded_answer(
                    question=request.question,
                    citations=result_to_use.hits,
                    generation_options=request.generation_options,
                )
                answer_status = "grounded"
                trace.append(
                    AgentTraceStep(
                        state="answer",
                        action="generate_grounded_answer",
                        query=current_query,
                        hit_count=len(result_to_use.hits),
                        sufficient_evidence=True,
                        notes="证据充足，生成带引用回答。",
                    )
                )
            except Exception:
                logger.exception("LLM generation failed, degrading to evidence-only response")
                answer_status = "degraded"
                answer = self._build_degraded_response(result_to_use.hits)
                trace.append(
                    AgentTraceStep(
                        state="answer",
                        action="degraded_llm_unavailable",
                        query=current_query,
                        hit_count=len(result_to_use.hits),
                        sufficient_evidence=True,
                        notes="LLM 不可用，降级为仅返回检索证据，拒绝编造。",
                    )
                )
        else:
            answer_status = "insufficient_evidence"
            answer = "未找到足够依据，已执行检索和一次改写重试，建议缩小问题范围或补充筛选条件。"
            trace.append(
                AgentTraceStep(
                    state="answer",
                    action="return_insufficient_evidence",
                    query=current_query,
                    hit_count=len(result_to_use.hits),
                    sufficient_evidence=False,
                    notes="两轮内证据仍不足，拒绝编造答案。",
                )
            )
        generation_latency = int((time.perf_counter() - generation_started) * 1000)

        trace.append(
            AgentTraceStep(
                state="finish",
                action="complete",
                query=current_query,
                hit_count=len(result_to_use.hits),
                sufficient_evidence=answer_status in ("grounded", "degraded"),
                notes="智能体执行结束。",
            )
        )

        total_latency = int((time.perf_counter() - started) * 1000)
        return AgentAnswerResponseData(
            answer=answer,
            answer_status=answer_status,
            final_query=current_query,
            retrieval_rounds=executed_rounds,
            citations=result_to_use.hits,
            matched_documents=self._build_matched_documents(result_to_use.hits),
            filters_applied=result_to_use.filters_applied,
            trace=trace,
            latency_ms=AgentLatencyBreakdown(
                planning=planning_latency,
                retrieval=total_retrieval_latency,
                generation=generation_latency,
                total=total_latency,
            ),
        )

    @staticmethod
    def _normalize_query(question: str) -> str:
        """查询标准化：去除首尾空白并将多空白字符合并为一个空格。

        面试点：为什么要标准化查询？
        答：用户输入可能包含多余换行、空格等噪声，这些会影响
        Embedding 质量和 BM25 分词的一致性。
        """
        return " ".join(question.strip().split())

    def _rewrite_query(
        self,
        *,
        original_question: str,
        current_query: str,
        retrieval_result: SearchResponseData,
    ) -> str:
        """查询改写：当首轮检索证据不足时，尝试用多种策略改写查询。

        面试点：三种改写策略的降级顺序？
        1. LLM 改写（最优）：利用大模型理解语义，生成更精准的查询
        2. 标题追加（启发式）：将首条命中标题追加到查询中
        3. 关键词提取（兜底）：去除停用词，保留核心英文/数字关键词
        4. 标点清理（最后手段）：去除中文标点和语气词

        降级策略：LLM 失败时不抛异常（除非配置为 llm 模式且不允许降级），
        继续尝试启发式方法，保证服务可用性。
        """
        # 策略1：LLM 改写（如果 LLM 已配置且模式不是 heuristic）
        if self.settings.agent_query_rewrite_mode != "heuristic" and self.llm_provider.is_configured():
            try:
                llm_result = self.llm_provider.rewrite_query(
                    original_question=original_question,
                    current_query=current_query,
                    evidence_titles=[citation.title for citation in retrieval_result.hits],
                )
                self._record_llm_log(llm_result, request_type="chat.completions.query_rewrite")
                rewritten = self._normalize_query(llm_result.answer)
                if rewritten and rewritten != current_query:
                    return rewritten  # 有效的改写查询
            except Exception:
                # 如果模式是 llm 且不允许降级，则抛出异常
                if self.settings.agent_query_rewrite_mode == "llm" and not self.settings.allow_provider_fallbacks:
                    raise

        # 策略2：将首条命中标题追加到查询中（如果标题不在查询中）
        if retrieval_result.hits:
            title = retrieval_result.hits[0].title.strip()
            if title and title.lower() not in current_query.lower():
                return f"{current_query} {title}"

        # 策略3：提取英文/数字关键词（去除停用词）
        english_tokens = re.findall(r"[A-Za-z0-9]+", original_question.lower())
        if english_tokens:
            stopwords = {
                "what", "how", "is", "are", "the", "a", "an", "for", "to", "of",
                "please", "tell", "me", "about", "does", "do", "flow",
            }
            condensed = [token for token in english_tokens if token not in stopwords]
            if condensed:
                return " ".join(dict.fromkeys(condensed))  # dict.fromkeys 去重保持顺序

        # 策略4：清理中文标点和语气词
        normalized = re.sub(r"[？?，,。.!！]", " ", original_question)
        normalized = normalized.replace("请问", " ").replace("一下", " ")
        normalized = " ".join(normalized.split())
        return normalized or current_query
 
    def _evidence_threshold(self, strategy: str) -> float:
        """获取证据阈值：根据检索策略返回对应的分数阈值。

        面试点：为什么 hybrid 和 dense 使用不同的阈值？
        答：混合检索的融合得分经过 Min-Max 归一化，分布与纯稠密得分不同，
        因此需要独立的阈值来正确判断证据是否充分。
        """
        return (
            self.settings.evidence_threshold_hybrid
            if strategy == "hybrid"
            else self.settings.evidence_threshold_dense
        )

    @staticmethod
    def _has_sufficient_evidence(
        citations: list[Citation],
        *,
        evidence_threshold: float,
    ) -> bool:
        """证据门控：判断检索结果是否提供了足够的证据来生成可靠回答。

        判断标准（双重检查）：
        1. 首条命中的得分 >= 证据阈值（主条件）
        2. 如果有 BM25 text_score，text_score >= 0.2（辅助条件）

        面试点：为什么还要检查 text_score？
        答：向量得分高可能只是语义相似，但可能不包含精确关键词。
        text_score >= 0.2 确保至少部分关键词匹配，提高回答可靠性。
        """
        if not citations:
            return False  # 无任何命中 → 证据不足
        top = citations[0]
        if top.score < evidence_threshold:  # 主条件：得分低于阈值
            return False
        if top.text_score is not None and top.text_score < 0.2:  # 辅助条件：关键词得分过低
            return False
        return True

    def _generate_grounded_answer(
        self,
        *,
        question: str,
        citations: list[Citation],
        generation_options,
    ) -> str:
        """生成基于证据的回答：优先使用 LLM，降级到模板拼接。

        面试点：为什么需要 fallback_builder？
        答：当 LLM 不可用时（未配置、API 超时、配额耗尽等），
        使用模板拼接引用片段作为兜底回答，保证系统始终可用。
        """
        if self.llm_provider.is_configured():
            llm_result = self.llm_provider.generate_answer_with_fallback(
                question=question,
                citations=citations,
                generation_options=generation_options,
                fallback_builder=self._build_grounded_answer,  # 传入兜底函数
            )
            if llm_result is not None:
                self._record_llm_log(llm_result)
                return llm_result.answer  # LLM 生成的回答
        return self._build_grounded_answer(citations)  # 模板兜底

    @staticmethod
    def _build_grounded_answer(citations: list[Citation]) -> str:
        """模板拼接：基于命中片段构建结构化回答（LLM 不可用时的兜底方案）。

        输出格式：
        基于命中的引用片段，当前可以确认的信息如下：
        - 文档标题（章节位置，score=0.xxx）：片段内容...
        """
        snippets = []
        for citation in citations[:3]:  # 最多展示 3 条
            # 确定位置信息：章节标题 > 工作表名 > 页码 > "chunk"
            location = citation.section_title or citation.sheet_name or (f"page {citation.page_no}" if citation.page_no else "chunk")
            snippets.append(f"- {citation.title}（{location}，score={citation.score:.3f}）：{citation.snippet}")
        return "基于命中的引用片段，当前可以确认的信息如下：\n" + "\n".join(snippets)

    @staticmethod
    def _build_degraded_response(citations: list[Citation]) -> str:
        """LLM 不可用时的降级响应：仅返回检索证据与拒答提示，不拼接为回答格式。

        与 _build_grounded_answer 的关键区别：
        - _build_grounded_answer 会拼接为"当前可以确认的信息如下"格式，容易被误认为完整回答
        - 本方法明确声明"无法生成回答"，仅罗列原始证据，用户知道这是未加工的检索结果
        """
        if not citations:
            return "LLM 服务暂时不可用且未检索到相关文档，请稍后重试。"
        evidence_lines = []
        for c in citations[:5]:
            location = c.section_title or c.sheet_name or (f"第{c.page_no}页" if c.page_no else "chunk")
            evidence_lines.append(f"- {c.title}（{location}）：{c.snippet}")
        return "LLM 服务暂时不可用，无法生成回答。以下为检索到的相关证据，请参考原文确认：\n" + "\n".join(evidence_lines)

    def _record_llm_log(self, llm_result, *, request_type: str = "chat.completions") -> None:
        """记录 LLM 调用日志到数据库。

        面试点：为什么要记录 LLM 调用日志？
        答：
        1. 成本分析：统计 Token 消耗和调用次数
        2. 性能监控：追踪 LLM 延迟
        3. 问题排查：定位 LLM 相关错误（如改写失败、生成异常）
        """
        # 判断状态：local-fallback 表示 LLM 降级
        status = "fallback" if llm_result.provider_name == "local-fallback" else "success"
        self.llm_logs.add(
            LlmCallLog(
                trace_id=get_trace_id(),
                provider_type="llm",
                provider_name=llm_result.provider_name,
                model_name=llm_result.model_name,
                request_type=request_type,
                request_tokens=llm_result.request_tokens,
                response_tokens=llm_result.response_tokens,
                latency_ms=llm_result.latency_ms,
                status=status,
            )
        )
        self.session.commit()

    @staticmethod
    def _pick_better_result(
        current_best: SearchResponseData | None,
        candidate: SearchResponseData,
    ) -> SearchResponseData:
        """从多轮检索中选择最佳结果。

        比较策略：
        1. 无现有最佳结果 → 候选即为最佳
        2. 候选首条得分更高 → 替换
        3. 得分相同但候选命中更多 → 替换（同等质量下信息更丰富）
        4. 否则 → 保持现有最佳
        """
        if current_best is None:
            return candidate  # 首次检索结果
        current_score = current_best.hits[0].score if current_best.hits else -1.0
        candidate_score = candidate.hits[0].score if candidate.hits else -1.0
        if candidate_score > current_score:  # 得分更高
            return candidate
        if candidate_score == current_score and len(candidate.hits) > len(current_best.hits):  # 命中更多
            return candidate
        return current_best

    @staticmethod
    def _build_matched_documents(citations: list[Citation]) -> list[MatchedDocument]:
        """从 Citation 列表中提取去重的匹配文档列表。

        面试点：为什么需要去重？
        答：多个 chunk 可能来自同一文档，前端展示时需要按文档维度聚合，
        避免同一文档重复出现。
        """
        matched_documents = []
        seen_doc_ids: set[str] = set()  # 已见过的文档 UUID 集合
        for citation in citations:
            if citation.doc_uuid in seen_doc_ids:
                continue  # 跳过已出现的文档
            seen_doc_ids.add(citation.doc_uuid)
            matched_documents.append(
                MatchedDocument(
                    doc_uuid=citation.doc_uuid,
                    title=citation.title,
                    score=citation.score,
                )
            )
        return matched_documents

    @staticmethod
    def _empty_result(query: str) -> SearchResponseData:
        """构建空检索结果（当所有检索轮次都无命中时使用）。"""
        return SearchResponseData(
            query=query,
            rewritten_query=query,
            filters_applied={},
            hits=[],
            latency_ms=0,
        )
