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
from dataclasses import dataclass, field  # 数据类：承载状态机运行上下文
from enum import Enum                    # 枚举：定义显式状态集合

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


class AgentState(str, Enum):
    """QA Agent 有限状态机的显式状态集合。

    两条合法路径：
      证据充分： plan → retrieve → answer → finish
      证据不足： plan → retrieve → rewrite → retrieve → answer → finish
                 （改写无变化时 rewrite 直接进入 answer，并记录 skip_rewrite 追踪）

    面试点：为什么把状态收敛成枚举，而不是散落的字符串字面量？
    答：枚举把"合法状态"约束到一个集合，转移逻辑里写错状态名会在
    静态检查/测试阶段暴露，而不是运行期产生一个拼写错误的 trace state。
    """
    PLAN = "plan"
    RETRIEVE = "retrieve"
    REWRITE = "rewrite"
    ANSWER = "answer"
    FINISH = "finish"


@dataclass
class _AgentRuntime:
    """状态机运行上下文：承载跨状态共享的可变状态。

    这是"业务动作层"与"编排层"解耦的关键：
    - 业务动作（检索/改写/生成/证据门控）是 service 上的纯方法，输入输出确定；
    - 编排层（状态转移）只负责读写 runtime 字段、决定下一个状态。
    将来迁图框架时，业务动作方法可直接复用为图节点，只有编排层需要重写。
    """
    question: str
    strategy: str
    max_rounds: int
    current_query: str
    round_index: int = 0                       # 当前检索轮次（从 0 起，改写成功递增）
    executed_rounds: int = 0                   # 实际执行的检索次数
    best_result: SearchResponseData | None = None       # 多轮中的最佳检索结果
    last_retrieval: SearchResponseData | None = None    # 最近一轮检索结果（供改写使用）
    result_to_use: SearchResponseData | None = None     # 最终用于生成的结果
    answer: str = ""                           # 最终回答文本
    answer_status: str = "insufficient_evidence"        # grounded / degraded / insufficient_evidence
    trace: list[AgentTraceStep] = field(default_factory=list)  # 状态机执行追踪
    planning_latency: int = 0                  # 规划阶段耗时（毫秒）
    total_retrieval_latency: int = 0           # 检索阶段累计耗时（毫秒）
    generation_latency: int = 0                # 生成阶段耗时（毫秒）


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
        """有界 QA Agent：检索 → 可选改写一次 → 回答。

        状态机执行顺序（显式状态循环，替代原 for/if/break 硬编码）：
          1. plan：标准化查询，确定检索策略
          2. retrieve：执行检索 + 证据门控，决定进入改写还是回答
          3. [rewrite]：证据不足时用 LLM 改写查询，回到检索
          4. answer：生成 / 降级 / 拒答
          5. finish：汇总结果，返回响应

        面试点：为什么用状态循环 + handler，而不是把流转写死在 answer 里？
        答：把"编排"（状态转移）和"业务动作"（检索/改写/生成/门控）拆开，
        新增状态只需加一个 handler 并调整返回值，不动业务实现。
        """
        started = time.perf_counter()
        runtime = _AgentRuntime(
            question=request.question,
            strategy=request.retrieval_strategy,
            max_rounds=request.max_retrieval_rounds,
            current_query=request.question,
        )

        state: AgentState | None = AgentState.PLAN
        while state is not None:
            state = self._advance(
                state,
                runtime,
                request,
                trusted_user_context=trusted_user_context,
                authenticated_identity_required=authenticated_identity_required,
            )

        total_latency = int((time.perf_counter() - started) * 1000)
        return AgentAnswerResponseData(
            answer=runtime.answer,
            answer_status=runtime.answer_status,
            final_query=runtime.current_query,
            retrieval_rounds=runtime.executed_rounds,
            citations=runtime.result_to_use.hits,
            matched_documents=self._build_matched_documents(runtime.result_to_use.hits),
            filters_applied=runtime.result_to_use.filters_applied,
            trace=runtime.trace,
            latency_ms=AgentLatencyBreakdown(
                planning=runtime.planning_latency,
                retrieval=runtime.total_retrieval_latency,
                generation=runtime.generation_latency,
                total=total_latency,
            ),
        )

    def _advance(
        self,
        state: AgentState,
        runtime: _AgentRuntime,
        request: AgentAnswerRequest,
        *,
        trusted_user_context: bool,
        authenticated_identity_required: bool,
    ) -> AgentState | None:
        """状态转移：执行当前状态的动作，返回下一个状态。

        面试点：为什么把转移抽成 _advance 而不是写死在 answer 里？
        答：显式转移表让状态机和业务动作解耦——新增状态（如 clarify、escalate）
        只需增加一个 handler 分支并调整返回值，不动检索/改写/生成的业务实现；
        将来迁图框架时，每个 handler 就是一个现成的图节点。
        """
        if state is AgentState.PLAN:
            return self._run_plan(runtime, request)
        if state is AgentState.RETRIEVE:
            return self._run_retrieve(
                runtime,
                request,
                trusted_user_context=trusted_user_context,
                authenticated_identity_required=authenticated_identity_required,
            )
        if state is AgentState.REWRITE:
            return self._run_rewrite(runtime, request)
        if state is AgentState.ANSWER:
            return self._run_answer(runtime, request)
        if state is AgentState.FINISH:
            return self._run_finish(runtime)
        raise RuntimeError(f"未知状态: {state}")

    def _run_plan(self, runtime: _AgentRuntime, request: AgentAnswerRequest) -> AgentState:
        """plan 状态：标准化查询并记录规划步骤。"""
        planning_started = time.perf_counter()
        runtime.current_query = self._normalize_query(request.question)
        runtime.trace.append(
            AgentTraceStep(
                state=AgentState.PLAN.value,
                action="plan_retrieval",
                query=runtime.current_query,
                notes="先检索一次，证据不足时最多进行一次查询改写重试。",
            )
        )
        runtime.planning_latency = int((time.perf_counter() - planning_started) * 1000)
        return AgentState.RETRIEVE

    def _run_retrieve(
        self,
        runtime: _AgentRuntime,
        request: AgentAnswerRequest,
        *,
        trusted_user_context: bool,
        authenticated_identity_required: bool,
    ) -> AgentState:
        """retrieve 状态：执行检索 + 证据门控，决定下一步。

        转移规则：
          - 证据充分 → answer
          - 证据不足且还有改写轮次 → rewrite
          - 证据不足且已用尽轮次 → answer（将走拒答）
        """
        retrieval_started = time.perf_counter()
        retrieval_result = self.retrieval.search(
            SearchRequest(
                query=runtime.current_query,
                top_k=request.top_k,
                strategy=request.retrieval_strategy,
                filters=request.filters,
                user_context=request.user_context,
            ),
            trusted_user_context=trusted_user_context,
            authenticated_identity_required=authenticated_identity_required,
        )
        runtime.total_retrieval_latency += int((time.perf_counter() - retrieval_started) * 1000)
        runtime.executed_rounds += 1
        runtime.last_retrieval = retrieval_result

        evidence_threshold = self._evidence_threshold(request.retrieval_strategy)
        sufficient = self._has_sufficient_evidence(
            retrieval_result.hits,
            evidence_threshold=evidence_threshold,
        )
        top_score = retrieval_result.hits[0].score if retrieval_result.hits else None
        runtime.trace.append(
            AgentTraceStep(
                state=AgentState.RETRIEVE.value,
                action=f"retrieve_round_{runtime.round_index + 1}",
                query=runtime.current_query,
                hit_count=len(retrieval_result.hits),
                top_score=top_score,
                sufficient_evidence=sufficient,
                evidence_threshold=evidence_threshold,
                notes=f"使用 {request.retrieval_strategy} 检索策略并应用策略阈值。",
            )
        )
        runtime.best_result = self._pick_better_result(runtime.best_result, retrieval_result)

        if sufficient:
            return AgentState.ANSWER
        if runtime.round_index + 1 >= request.max_retrieval_rounds:
            return AgentState.ANSWER
        return AgentState.REWRITE

    def _run_rewrite(self, runtime: _AgentRuntime, request: AgentAnswerRequest) -> AgentState:
        """rewrite 状态：查询改写，改写成功回到检索，无变化则进入回答。"""
        # REWRITE 只能由 RETRIEVE 进入，此时 last_retrieval 必已设置
        assert runtime.last_retrieval is not None
        rewritten_query = self._rewrite_query(
            original_question=request.question,
            current_query=runtime.current_query,
            retrieval_result=runtime.last_retrieval,
        )
        if rewritten_query == runtime.current_query:
            runtime.trace.append(
                AgentTraceStep(
                    state=AgentState.FINISH.value,
                    action="skip_rewrite",
                    query=runtime.current_query,
                    notes="未生成更有效的改写查询，结束重试。",
                )
            )
            return AgentState.ANSWER
        runtime.trace.append(
            AgentTraceStep(
                state=AgentState.REWRITE.value,
                action="rewrite_query",
                query=rewritten_query,
                notes="首轮证据不足，执行一次查询改写后重试。",
            )
        )
        runtime.current_query = rewritten_query
        runtime.round_index += 1
        return AgentState.RETRIEVE

    def _run_answer(self, runtime: _AgentRuntime, request: AgentAnswerRequest) -> AgentState:
        """answer 状态：生成回答 / LLM 降级 / 证据不足拒答。"""
        generation_started = time.perf_counter()
        runtime.result_to_use = runtime.best_result or self._empty_result(request.question)
        final_threshold = self._evidence_threshold(request.retrieval_strategy)
        if runtime.result_to_use.hits and self._has_sufficient_evidence(
            runtime.result_to_use.hits,
            evidence_threshold=final_threshold,
        ):
            try:
                runtime.answer = self._generate_grounded_answer(
                    question=request.question,
                    citations=runtime.result_to_use.hits,
                    generation_options=request.generation_options,
                )
                runtime.answer_status = "grounded"
                runtime.trace.append(
                    AgentTraceStep(
                        state=AgentState.ANSWER.value,
                        action="generate_grounded_answer",
                        query=runtime.current_query,
                        hit_count=len(runtime.result_to_use.hits),
                        sufficient_evidence=True,
                        notes="证据充足，生成带引用回答。",
                    )
                )
            except Exception:
                logger.exception("LLM generation failed, degrading to evidence-only response")
                runtime.answer_status = "degraded"
                runtime.answer = self._build_degraded_response(runtime.result_to_use.hits)
                runtime.trace.append(
                    AgentTraceStep(
                        state=AgentState.ANSWER.value,
                        action="degraded_llm_unavailable",
                        query=runtime.current_query,
                        hit_count=len(runtime.result_to_use.hits),
                        sufficient_evidence=True,
                        notes="LLM 不可用，降级为仅返回检索证据，拒绝编造。",
                    )
                )
        else:
            runtime.answer_status = "insufficient_evidence"
            runtime.answer = "未找到足够依据，已执行检索和一次改写重试，建议缩小问题范围或补充筛选条件。"
            runtime.trace.append(
                AgentTraceStep(
                    state=AgentState.ANSWER.value,
                    action="return_insufficient_evidence",
                    query=runtime.current_query,
                    hit_count=len(runtime.result_to_use.hits),
                    sufficient_evidence=False,
                    notes="两轮内证据仍不足，拒绝编造答案。",
                )
            )
        runtime.generation_latency = int((time.perf_counter() - generation_started) * 1000)
        return AgentState.FINISH

    def _run_finish(self, runtime: _AgentRuntime) -> AgentState | None:
        """finish 状态：汇总执行结果，返回 None 终止状态循环。"""
        runtime.trace.append(
            AgentTraceStep(
                state=AgentState.FINISH.value,
                action="complete",
                query=runtime.current_query,
                hit_count=len(runtime.result_to_use.hits),
                sufficient_evidence=runtime.answer_status in ("grounded", "degraded"),
                notes="智能体执行结束。",
            )
        )
        return None

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
        """获取证据阈值：RRF 融合分是排名信号，门控改用 Dense 余弦分（vector_score）。

        面试点：为什么 hybrid 和 dense 现在共用同一阈值？
        答：RRF 融合分只反映排名、不反映相关性强度，不能当门控门槛；
        门控改用 vector_score（Dense 余弦分），hybrid 与 dense 是同一量纲，
        因此共用 evidence_threshold_dense 一个阈值。
        """
        return self.settings.evidence_threshold_dense

    @staticmethod
    def _has_sufficient_evidence(
        citations: list[Citation],
        *,
        evidence_threshold: float,
    ) -> bool:
        """证据门控：判断检索结果是否提供了足够的证据来生成可靠回答。

        判断标准（双重检查）：
        1. 首条命中的 vector_score（Dense 余弦分）>= 证据阈值（主条件）
        2. 如果有 BM25 text_score，text_score >= 0.2（辅助条件）

        面试点：为什么不用 RRF 融合分（top.score）当门控门槛？
        答：RRF 分是排名信号（约 0.016~0.033），不反映相关性强度；
        门控需要"绝对相关性"，所以改用 vector_score（余弦分 [0,1]）。
        """
        if not citations:
            return False  # 无任何命中 → 证据不足
        top = citations[0]
        if top.vector_score is not None and top.vector_score < evidence_threshold:  # 主条件：余弦分低于阈值
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
