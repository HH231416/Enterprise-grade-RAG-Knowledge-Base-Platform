"""QA Agent 单元测试：验证智能问答的状态机行为和端点集成。

测试覆盖：
- Agent 端点返回完整的 trace 信息
- 首轮检索无结果时触发查询改写（rewrite）
- LLM 可用时优先使用结构化改写
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from datetime import datetime  # 时间：构造测试用的 Citation 时间戳

from app.schemas.qa_agent import AgentAnswerRequest  # Agent 问答请求 Schema
from app.integrations.llm import LlmResult  # LLM 结果模型
from app.schemas.retrieval import Citation, SearchResponseData  # 检索相关 Schema
from app.services.qa_agent import QaAgentService  # 被测试的服务


def _upload_sample_document(client, sample_pdf_bytes):
    """辅助函数：上传示例 PDF 文档并返回 doc_uuid。
    
    面试点：为什么测试中上传文档用 assert response.status_code == 200？
    答：确保测试前置条件成立，如果上传失败则测试无法进行，
    快速失败（fail-fast）原则。
    """
    response = client.post(
        "/api/v1/documents/upload",
        data={"source_type": "rule_doc", "source_module": "hr", "owner_dept": "hr"},
        files={"file": ("policy.pdf", sample_pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 200
    return response.json()["data"]["doc_uuid"]


def test_agent_answer_endpoint_returns_trace(client, sample_pdf_bytes):
    """测试 Agent 端点返回完整的 trace（追踪链路）。

    验证点：
    - 回答状态为 grounded 或 insufficient_evidence
    - 至少执行了 1 轮检索
    - trace 中第一步是 plan，最后一步是 finish
    - 返回 final_query 和 latency_ms
    """
    _upload_sample_document(client, sample_pdf_bytes)  # 前置：上传文档
    response = client.post(
        "/api/v1/qa/agent-answer",
        headers={"X-Internal-Token": "test-internal-token"},  # 内部服务令牌
        json={
            "question": "What is the approval flow for transfer requests?",
            "top_k": 5,
            "retrieval_strategy": "dense",
            "filters": {"source_module": ["hr"]},  # 限定 HR 模块
            "user_context": {"user_id": "u1001"},  # 用户上下文
        },
    )
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["answer"]  # 有回答内容
    assert payload["answer_status"] in {"grounded", "insufficient_evidence"}  # 状态合法
    assert payload["retrieval_rounds"] >= 1  # 至少一轮检索
    assert payload["trace"]  # 有追踪信息
    assert payload["trace"][0]["state"] == "plan"     # trace 第一步是 plan
    assert payload["trace"][-1]["state"] == "finish"   # trace 最后一步是 finish
    assert "final_query" in payload  # 有最终查询
    assert "latency_ms" in payload  # 有延迟统计


def test_agent_service_rewrites_once_when_first_retrieval_is_weak(db_session, monkeypatch):
    """测试首轮检索无结果时，Agent 触发一次查询改写并成功检索。

    面试点：monkeypatch 在测试中的作用？
    答：monkeypatch 允许在测试中替换依赖组件的行为，这里用它来
    模拟 RetrievalService.search 的返回值，测试 Agent 的改写逻辑
    而不依赖真实的向量数据库和 Embedding 服务。

    测试场景：
    - 第 1 次检索返回空结果 → 触发改写
    - 第 2 次检索返回有效结果 → 回答状态为 grounded
    """
    service = QaAgentService(db_session)  # 创建 Agent 服务实例
    calls = {"count": 0}  # 闭包计数器：追踪 search 调用次数

    def fake_search(self, request, *, trusted_user_context=False, authenticated_identity_required=False):
        """伪造的 search 方法：第 1 次返回空，第 2 次返回有效结果。"""
        calls["count"] += 1
        if calls["count"] == 1:
            # 第 1 次：返回空结果，触发改写
            return SearchResponseData(
                query=request.query,
                rewritten_query=request.query,
                filters_applied={},
                hits=[],  # 空命中
                latency_ms=1,
            )
        # 第 2 次：返回有效结果
        return SearchResponseData(
            query=request.query,
            rewritten_query=request.query,
            filters_applied={},
            hits=[
                Citation(
                    doc_uuid="doc-1",
                    chunk_uuid="chunk-1",
                    title="HR Transfer Policy",
                    source_module="hr",
                    snippet="Transfer approval now requires two levels.",
                    version="v1",
                    updated_at=datetime.now(),
                    score=0.92,  # 高得分
                    vector_score=0.92,
                    text_score=None,
                )
            ],
            latency_ms=1,
        )

    # 用 monkeypatch 替换真实 search 方法
    monkeypatch.setattr("app.services.retrieval.RetrievalService.search", fake_search)

    result = service.answer(
        AgentAnswerRequest(
            question="What is the approval flow for transfer requests?",
            top_k=5,
            max_retrieval_rounds=2,  # 最多 2 轮
            retrieval_strategy="dense",
        )
    )

    assert result.answer_status == "grounded"  # 最终获得有效回答
    assert result.retrieval_rounds == 2  # 执行了 2 轮
    assert any(step.state == "rewrite" for step in result.trace)  # trace 包含 rewrite 步骤
    assert result.final_query != "What is the approval flow for transfer requests?"  # 查询被改写


def test_agent_prefers_structured_llm_rewrite_when_provider_is_available(db_session, monkeypatch):
    """测试 LLM 可用时，Agent 优先使用结构化改写而非启发式方法。

    面试点：为什么要验证 final_query 的具体值？
    答：这确保 LLM 改写的查询确实被使用了，而不是降级到启发式方法。
    如果 LLM 返回 "employee transfer manager HR approval workflow"，
    说明改写有效且被 Agent 采纳。

    测试场景：
    - LLM 已配置（is_configured = True）
    - 第 1 次检索返回空结果
    - LLM 改写查询为 "employee transfer manager HR approval workflow"
    - 第 2 次检索用改写后的查询获得有效结果
    """
    service = QaAgentService(db_session)
    calls = {"count": 0}

    def fake_search(self, request, *, trusted_user_context=False, authenticated_identity_required=False):
        """伪造 search：第 1 次空结果，第 2 次有效结果。"""
        calls["count"] += 1
        if calls["count"] == 1:
            return SearchResponseData(
                query=request.query,
                rewritten_query=request.query,
                filters_applied={},
                hits=[],
                latency_ms=1,
            )
        return SearchResponseData(
            query=request.query,
            rewritten_query=request.query,
            filters_applied={},
            hits=[
                Citation(
                    doc_uuid="doc-1",
                    chunk_uuid="chunk-1",
                    title="HR Transfer Policy",
                    source_module="hr",
                    snippet="Transfer approval requires manager and HR approval.",
                    version="v1",
                    updated_at=datetime.now(),
                    score=0.92,
                    vector_score=0.92,
                    text_score=None,
                )
            ],
            latency_ms=1,
        )

    monkeypatch.setattr("app.services.retrieval.RetrievalService.search", fake_search)
    # 模拟 LLM 已配置
    monkeypatch.setattr(service.llm_provider, "is_configured", lambda: True)
    # 模拟 LLM 改写查询返回特定结果
    monkeypatch.setattr(
        service.llm_provider,
        "rewrite_query",
        lambda **kwargs: LlmResult(
            answer="employee transfer manager HR approval workflow",  # 改写后的查询
            provider_name="test-llm",
            model_name="test-model",
            request_tokens=8,
            response_tokens=6,
            latency_ms=2,
        ),
    )
    # 模拟 LLM 生成回答返回 None（使用 fallback）
    monkeypatch.setattr(
        service.llm_provider,
        "generate_answer_with_fallback",
        lambda **kwargs: None,
    )
    # 跳过 LLM 日志记录（避免数据库操作）
    monkeypatch.setattr(service, "_record_llm_log", lambda *args, **kwargs: None)

    result = service.answer(
        AgentAnswerRequest(
            question="How does employee transfer approval work?",
            max_retrieval_rounds=2,
        )
    )

    assert result.answer_status == "grounded"
    # 验证 LLM 改写结果被采纳
    assert result.final_query == "employee transfer manager HR approval workflow"
    rewrite_step = next(step for step in result.trace if step.state == "rewrite")
    assert rewrite_step.query == result.final_query  # rewrite 步骤记录了改写后的查询
