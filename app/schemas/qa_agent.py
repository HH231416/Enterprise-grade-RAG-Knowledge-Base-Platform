# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from typing import Literal  # Literal 类型：限制字段只能取特定值

from pydantic import Field  # Field：为 Pydantic 字段添加校验约束和默认值

from app.schemas.common import AppBaseModel  # 项目统一的 Pydantic 基类
from app.schemas.qa import GenerationOptions, MatchedDocument  # 生成选项、匹配文档
from app.schemas.retrieval import Citation, RetrievalFilters, UserContext  # 引用、检索过滤、用户上下文


class AgentAnswerRequest(AppBaseModel):
    """QA Agent 问答请求 Schema。"""
    question: str                                           # 用户问题（必填）
    top_k: int = Field(default=8, ge=1, le=20)              # 返回结果数量，默认 8，范围 1~20
    filters: RetrievalFilters | None = None                 # 检索过滤条件（可选）
    user_context: UserContext | None = None                 # 用户上下文（用户ID、部门等）
    generation_options: GenerationOptions | None = None     # LLM 生成选项（温度、最大 Token 等）
    retrieval_strategy: Literal["hybrid"] = "hybrid"
    max_retrieval_rounds: int = Field(default=2, ge=1, le=2)  # 最大检索轮次，默认 2（目前限制 ≤2）


class AgentTraceStep(AppBaseModel):
    """Agent 执行追踪步骤：记录有限状态机每个状态的操作和结果。"""
    state: Literal["plan", "retrieve", "rewrite", "answer", "finish"]  # 状态名称
    action: str                                              # 执行的动作描述
    query: str | None = None                                 # 当前步骤使用的查询文本
    hit_count: int | None = None                             # 检索命中数
    top_score: float | None = None                           # 最高相似度分数
    sufficient_evidence: bool | None = None                  # 证据是否充分（用于证据门控判断）
    evidence_threshold: float | None = None                  # 证据阈值
    notes: str | None = None                                 # 备注信息


class AgentLatencyBreakdown(AppBaseModel):
    """Agent 各阶段耗时明细。"""
    planning: int       # 规划阶段耗时（毫秒）
    retrieval: int      # 检索阶段耗时（毫秒）
    generation: int     # 生成阶段耗时（毫秒）
    total: int          # 总耗时（毫秒）


class AgentAnswerResponseData(AppBaseModel):
    """QA Agent 问答响应 Schema。"""
    answer: str                              # 生成的回答文本
    answer_status: str = "grounded"          # 回答状态：grounded（有依据）/ ungrounded（无依据）
    final_query: str                         # 最终使用的检索查询
    retrieval_rounds: int                    # 实际执行的检索轮次
    citations: list[Citation]                # 引用来源列表（每个引用指向一个文档分块）
    matched_documents: list[MatchedDocument] # 匹配到的文档列表
    filters_applied: dict                    # 实际应用的过滤条件
    trace: list[AgentTraceStep]              # 完整的状态机执行追踪
    latency_ms: AgentLatencyBreakdown        # 各阶段耗时明细
