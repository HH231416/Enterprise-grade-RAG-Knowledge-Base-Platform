from datetime import UTC, datetime
from types import SimpleNamespace

from app.integrations.llm import LlmProvider
from app.schemas.retrieval import Citation


def test_answer_judge_parses_strict_json_response(monkeypatch):
    settings = SimpleNamespace(
        llm_api_base="https://llm.example/v1",
        llm_api_key="test-key",
        llm_model="answer-model",
        answer_judge_model="judge-model",
        answer_judge_max_tokens=320,
        provider_timeout_seconds=10,
    )
    monkeypatch.setattr(
        "app.integrations.llm.post_json_with_retries",
        lambda **_: {
            "model": "judge-model",
            "choices": [{"message": {"content": """```json
{"faithfulness_score": 0.92, "answer_relevance_score": 0.88, "citation_coverage_score": 1.0, "verdict": "pass", "reason": "All claims are supported."}
```"""}}],
        },
    )
    citation = Citation(
        doc_uuid="doc",
        chunk_uuid="chunk",
        title="Policy",
        source_module="hr",
        snippet="Employees submit invoices for reimbursement.",
        version="v1",
        updated_at=datetime.now(UTC),
        score=0.9,
    )

    result = LlmProvider(settings).judge_answer(
        question="What is needed for reimbursement?",
        answer="An invoice is required.",
        citations=[citation],
        reference_answer="Submit an invoice.",
    )

    assert result.verdict == "pass"
    assert result.faithfulness_score == 0.92
    assert result.citation_coverage_score == 1.0
