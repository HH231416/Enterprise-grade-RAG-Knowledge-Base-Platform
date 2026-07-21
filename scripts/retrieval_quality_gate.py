"""Run the deterministic retrieval benchmark as a CI quality gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-hybrid-hit-at-1", type=float, default=0.80)
    parser.add_argument("--min-hybrid-mrr", type=float, default=0.90)
    parser.add_argument("--min-hybrid-gain-over-dense", type=float, default=0.0)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from app.retrieval.controlled_benchmark import run_controlled_hybrid_benchmark
    from app.retrieval.quality_gate import RetrievalQualityThresholds, evaluate_retrieval_quality_gate

    report = run_controlled_hybrid_benchmark()
    gate = evaluate_retrieval_quality_gate(
        report,
        RetrievalQualityThresholds(
            min_hybrid_hit_at_1=args.min_hybrid_hit_at_1,
            min_hybrid_mrr=args.min_hybrid_mrr,
            min_hybrid_gain_over_dense=args.min_hybrid_gain_over_dense,
        ),
    )
    print(json.dumps({**report, "quality_gate": gate}, ensure_ascii=False, indent=2))
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
