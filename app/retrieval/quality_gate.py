"""Retrieval quality gates for deterministic release benchmarks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RetrievalQualityThresholds:
    """Minimum quality requirements for a release candidate."""

    min_hybrid_hit_at_1: float = 0.80
    min_hybrid_mrr: float = 0.90
    min_hybrid_gain_over_dense: float = 0.0


def evaluate_retrieval_quality_gate(
    report: dict[str, Any],
    thresholds: RetrievalQualityThresholds,
) -> dict[str, Any]:
    """Return a JSON-serializable gate result without hiding benchmark data."""

    dense = report.get("dense") or {}
    hybrid = report.get("hybrid") or {}
    failures: list[str] = []

    dense_hit_at_1 = _metric(dense, "hit_at_1", failures, "dense")
    hybrid_hit_at_1 = _metric(hybrid, "hit_at_1", failures, "hybrid")
    hybrid_mrr = _metric(hybrid, "mrr", failures, "hybrid")

    if hybrid_hit_at_1 is not None and hybrid_hit_at_1 < thresholds.min_hybrid_hit_at_1:
        failures.append(
            "hybrid.hit_at_1 "
            f"{hybrid_hit_at_1:.4f} is below {thresholds.min_hybrid_hit_at_1:.4f}"
        )
    if hybrid_mrr is not None and hybrid_mrr < thresholds.min_hybrid_mrr:
        failures.append(
            f"hybrid.mrr {hybrid_mrr:.4f} is below {thresholds.min_hybrid_mrr:.4f}"
        )
    if (
        dense_hit_at_1 is not None
        and hybrid_hit_at_1 is not None
        and hybrid_hit_at_1 - dense_hit_at_1 < thresholds.min_hybrid_gain_over_dense
    ):
        failures.append(
            "hybrid.hit_at_1 gain "
            f"{hybrid_hit_at_1 - dense_hit_at_1:.4f} is below "
            f"{thresholds.min_hybrid_gain_over_dense:.4f}"
        )

    return {
        "passed": not failures,
        "thresholds": asdict(thresholds),
        "failures": failures,
    }


def _metric(metrics: dict[str, Any], name: str, failures: list[str], section: str) -> float | None:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        failures.append(f"missing numeric metric: {section}.{name}")
        return None
    return float(value)
