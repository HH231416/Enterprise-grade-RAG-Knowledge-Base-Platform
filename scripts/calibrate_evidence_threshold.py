"""证据门控阈值校准脚本。

在部署了真实评测集的环境里扫描 evidence_threshold_dense，输出每个阈值下的
拒答准确率 / 误拒率 / 误答率 对比表，用于定下最终阈值并回填到配置。

用法：
    python scripts/calibrate_evidence_threshold.py <dataset_uuid>
    python scripts/calibrate_evidence_threshold.py <dataset_uuid> --thresholds 0.20,0.25,0.30,0.35,0.40,0.45

无答案判定：expected_doc_titles 和 expected_terms 都为空的 query 视为"无答案"。
如果你的无答案 query 用其它方式标注（如 notes 字段、独立 dataset），
请相应调整 _is_unanswerable()。
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

# 将项目根目录加入 sys.path，保证 `import app` 能从任意工作目录运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import reset_settings_cache
from app.db.runtime import get_session_factory, reset_db_runtime
from app.models.evaluation_query import EvaluationQuery
from app.schemas.retrieval import Citation, SearchRequest
from app.services.retrieval import RetrievalService


TEXT_SCORE_GATE = 0.2  # 与 qa_agent 证据门控的 text_score 辅助阈值保持一致


def _is_unanswerable(query: EvaluationQuery) -> bool:
    """判断 query 是否为"无答案"：期望文档标题和期望关键词都为空。"""
    return not query.expected_doc_titles and not query.expected_terms


def _gate(hit: Citation | None, threshold: float) -> bool:
    """复刻 qa_agent._has_sufficient_evidence 的证据门控判断。

    返回 True 表示"证据充分，可回答"，False 表示"证据不足，拒答"。
    """
    if hit is None:
        return False
    if hit.vector_score is not None and hit.vector_score < threshold:
        return False
    if hit.text_score is not None and hit.text_score < TEXT_SCORE_GATE:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="证据门控阈值校准")
    parser.add_argument("dataset_uuid", help="评测集 UUID（需包含无答案 query）")
    parser.add_argument("--thresholds", default="0.20,0.25,0.30,0.35,0.40,0.45",
                        help="逗号分隔的候选阈值列表")
    parser.add_argument("--top-k", type=int, default=1,
                        help="每个 query 检索返回数（默认 1，只看首位证据）")
    args = parser.parse_args()

    reset_settings_cache()
    reset_db_runtime()
    session = Session(get_session_factory()())
    try:
        dataset_uuid = uuid.UUID(args.dataset_uuid)
        queries = (
            session.query(EvaluationQuery)
            .filter(EvaluationQuery.dataset_uuid == dataset_uuid)
            .all()
        )
        if not queries:
            print(f"未找到评测集 {dataset_uuid} 的 query")
            return 1

        thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]

        retrieval = RetrievalService(session)
        # 每个 query 跑一次检索，缓存 top-1 分数；不同阈值复用同一批检索结果
        rows: list[tuple[EvaluationQuery, Citation | None]] = []
        for q in queries:
            result = retrieval.search(
                SearchRequest(query=q.query_text, top_k=args.top_k)
            )
            rows.append((q, result.hits[0] if result.hits else None))

        answerable = [(q, h) for q, h in rows if not _is_unanswerable(q)]
        unanswerable = [(q, h) for q, h in rows if _is_unanswerable(q)]

        print(f"评测集 query 总数: {len(rows)}")
        print(f"  有答案: {len(answerable)}")
        print(f"  无答案: {len(unanswerable)}")
        if not unanswerable:
            print("警告: 未发现无答案 query（expected_doc_titles 和 expected_terms 均为空）。")
            print("      请确认无答案 query 的标注方式，必要时调整 _is_unanswerable()。")

        print()
        print(f"{'threshold':>9} | {'拒答准确率':>10} | {'误拒率':>7} | {'误答率':>7}")
        print("-" * 48)
        for t in thresholds:
            correct_refuse = 0
            wrong_refuse = 0
            wrong_answer = 0
            for _, h in unanswerable:
                if _gate(h, t):
                    wrong_answer += 1
                else:
                    correct_refuse += 1
            for _, h in answerable:
                if not _gate(h, t):
                    wrong_refuse += 1
            refuse_acc = correct_refuse / len(unanswerable) if unanswerable else 0.0
            wrong_refuse_rate = wrong_refuse / len(answerable) if answerable else 0.0
            wrong_answer_rate = wrong_answer / len(unanswerable) if unanswerable else 0.0
            print(f"{t:>9.2f} | {refuse_acc:>9.1%} | {wrong_refuse_rate:>6.1%} | {wrong_answer_rate:>6.1%}")

        print()
        print("口径说明:")
        print("  - 拒答准确率 = 无答案 query 中被正确拒答的比例（越高越好）")
        print("  - 误拒率 = 有答案 query 中被误判拒答的比例（越低越好）")
        print("  - 误答率 = 无答案 query 中被误判回答的比例（越低越好）")
        print("  选阈值：在误答率可接受的前提下，找拒答准确率和误拒率的平衡点。")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
