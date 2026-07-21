"""测试混合检索基准测试的确定性回归行为。

验证 12 个企业规则查询用例的预期指标：
- 稠密检索：Hit@1=0.5833, MRR=0.7917
- 混合检索：Hit@1=0.8333, MRR=0.9167（明显优于纯稠密）
- 第 5、6 个语义改写用例：混合检索排名第 2（暴露局限性）
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from app.retrieval.controlled_benchmark import run_controlled_hybrid_benchmark


def test_controlled_hybrid_benchmark_improves_exact_term_cases():
    """验证混合检索在精确关键词场景下显著优于纯稠密检索。

    稠密检索 Hit@1 仅 7/12（58.33%），混合检索提升至 10/12（83.33%）。
    这证明了 BM25 稀疏检索对精确标识符查询的修复能力。
    """
    report = run_controlled_hybrid_benchmark()  # 执行基准测试

    assert report["query_count"] == 12  # 共 12 个查询
    # 稠密检索基线指标
    assert report["dense"] == {
        "hit_at_1_count": 7,
        "hit_at_1": 0.5833,
        "mrr": 0.7917,
    }
    # 混合检索指标（提升显著）
    assert report["hybrid"] == {
        "hit_at_1_count": 10,
        "hit_at_1": 0.8333,
        "mrr": 0.9167,
    }
    # 第 5、6 个语义改写用例：混合检索排名第 2（无法完美匹配，设计如此）
    assert report["details"][4]["hybrid_rank"] == 2
    assert report["details"][5]["hybrid_rank"] == 2
