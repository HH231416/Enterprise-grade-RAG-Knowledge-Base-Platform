from __future__ import annotations

from app.retrieval.quality_gate import RetrievalQualityThresholds, evaluate_retrieval_quality_gate


def test_quality_gate_accepts_baseline_hybrid_metrics():
    result = evaluate_retrieval_quality_gate(
        {
            "dense": {"hit_at_1": 0.5833},
            "hybrid": {"hit_at_1": 0.8333, "mrr": 0.9167},
        },
        RetrievalQualityThresholds(),
    )

    assert result["passed"] is True
    assert result["failures"] == []


def test_quality_gate_rejects_metric_regression():
    result = evaluate_retrieval_quality_gate(
        {
            "dense": {"hit_at_1": 0.58},
            "hybrid": {"hit_at_1": 0.50, "mrr": 0.72},
        },
        RetrievalQualityThresholds(),
    )

    assert result["passed"] is False
    assert "hybrid.hit_at_1 0.5000 is below 0.8000" in result["failures"]
    assert "hybrid.mrr 0.7200 is below 0.9000" in result["failures"]
