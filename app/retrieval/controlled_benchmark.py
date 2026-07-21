"""确定性回归基准测试：验证混合检索（Hybrid）排序行为。

设计原则：
- 小而受控的测试集（12 个企业规则查询），使用冻结的稠密基线分数
- 验证 BM25 + 融合层能否修正纯语义检索的排序缺陷
- 非生产流量模拟，不替代标注业务数据集

评估指标：Hit@1（首位命中率）、MRR（平均倒数排名）
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from dataclasses import dataclass

from app.retrieval.hybrid import DenseHit, HybridRetriever, SparseHit  # 混合检索组件
from app.retrieval.sparse_index import BM25Index  # BM25 稀疏索引


@dataclass(frozen=True, slots=True)  # frozen=True 创建不可变实例，适合测试用例
class BenchmarkCase:
    """单个基准测试用例。

    - case_id：用例唯一标识
    - query：查询文本
    - expected_chunk_uuid：期望的正确答案 chunk_uuid
    - dense_scores：冻结的稠密检索分数（模拟向量检索结果）
    """
    case_id: str
    query: str
    expected_chunk_uuid: str
    dense_scores: dict[str, float]  # chunk_uuid → 冻结的相似度分数


# 12 个模拟的企业规则文档片段（chunk_uuid, doc_uuid, 文本内容）
_DOCUMENTS: tuple[tuple[str, str, str], ...] = (
    ("chunk-01", "doc-01", "HR-TRF-2025 employee transfer requires manager approval and HR approval."),
    ("chunk-02", "doc-02", "FIN-EXP-2024 travel expense reimbursement requires original receipts."),
    ("chunk-03", "doc-03", "IT-VPN-OPS remote access VPN account application and approval procedure."),
    ("chunk-04", "doc-04", "SEC-DLP-07 data loss prevention policy for confidential files."),
    ("chunk-05", "doc-05", "HR-LEAVE-2025 annual leave request and manager approval rules."),
    ("chunk-06", "doc-06", "FIN-INV-2024 supplier invoice verification workflow."),
    ("chunk-07", "doc-07", "IT-ACC-2025 system account access and role assignment guide."),
    ("chunk-08", "doc-08", "SEC-INC-2025 security incident reporting escalation process."),
    ("chunk-09", "doc-09", "HR-PERF-2025 performance review and calibration process."),
    ("chunk-10", "doc-10", "FIN-BUD-2025 annual budget submission and adjustment policy."),
    ("chunk-11", "doc-11", "IT-CHG-2025 production change approval and rollback procedure."),
    ("chunk-12", "doc-12", "SEC-AUD-2025 access audit evidence retention requirements."),
)

# 12 个对应的查询（与文档一一对应）
_QUERIES: tuple[str, ...] = (
    "HR-TRF-2025 transfer approval",
    "FIN-EXP-2024 reimbursement receipt",
    "IT-VPN-OPS VPN access",
    "SEC-DLP-07 confidential file policy",
    "holiday balance remaining days",
    "vendor billing proof",
    "system account role assignment",
    "security incident escalation",
    "performance review calibration",
    "annual budget adjustment",
    "production change rollback",
    "access audit evidence retention",
)


def controlled_benchmark_cases() -> list[BenchmarkCase]:
    """生成 12 个固定企业规则查询用例，使用冻结的稠密基线分数。

    用例设计：
    - 前 3 个：精确标识符查询（稠密检索可能不如关键词匹配），测试稀疏检索的修复能力
    - 第 5、6 个：语义改写查询（不包含策略标识符），故意暴露混合检索的局限性
    - 其余：正常查询，稠密检索应排在首位
    """
    
    cases: list[BenchmarkCase] = []
    for index, query in enumerate(_QUERIES, start=1):  # 遍历 12 个查询
        correct = f"chunk-{index:02d}"                  # 正确答案
        first_distractor = f"chunk-{(index % len(_DOCUMENTS)) + 1:02d}"     # 干扰项 1
        second_distractor = f"chunk-{((index + 3) % len(_DOCUMENTS)) + 1:02d}"  # 干扰项 2

        if index <= 3:
            # 精确标识符查询：稠密语义基线故意将正确结果排在第二
            # 测试稀疏检索能否将其提升到首位
            dense_scores = {
                first_distractor: 0.960,
                correct: 0.945,
                second_distractor: 0.900,
            }
        elif index in {5, 6}:
            # 语义改写查询：既无标识符也无关键词精确匹配
            # 保持稠密检索的局限性可见，而非人为制造完美分数
            dense_scores = {
                first_distractor: 0.960,
                correct: 0.945,
                second_distractor: 0.900,
            }
        else:
            # 正常查询：稠密检索正确排在首位
            dense_scores = {
                correct: 0.960,
                first_distractor: 0.940,
                second_distractor: 0.900,
            }
        cases.append(
            BenchmarkCase(
                case_id=f"rule-query-{index:02d}",
                query=query,
                expected_chunk_uuid=correct,
                dense_scores=dense_scores,
            )
        )
    return cases


def run_controlled_hybrid_benchmark() -> dict:
    """执行确定性稠密 vs 混合检索回归基准测试。

    流程：
    1. 构建 BM25 索引（12 条文档）
    2. 对每个用例：生成稠密命中 + 稀疏命中 → 混合融合
    3. 计算 Hit@1 和 MRR 指标
    4. 返回稠密和混合的对比结果
    """
    index = BM25Index()  # 创建 BM25 索引
    documents = [
        {
            "chunk_uuid": chunk_uuid,
            "doc_uuid": doc_uuid,
            "chunk_text": text,
            "metadata": {"title": doc_uuid},
        }
        for chunk_uuid, doc_uuid, text in _DOCUMENTS
    ]
    index.build_index(documents)  # 构建索引
    document_ids = {chunk_uuid: doc_uuid for chunk_uuid, doc_uuid, _ in _DOCUMENTS}  # chunk_uuid → doc_uuid 映射
    hybrid = HybridRetriever(alpha=0.7)  # 混合检索器（稠密权重 0.7）

    details: list[dict] = []               # 每个用例的详细结果
    dense_rankings: list[list[str]] = []   # 每个用例的稠密排序
    hybrid_rankings: list[list[str]] = []  # 每个用例的混合排序

    for case in controlled_benchmark_cases():  # 遍历每个用例
        # 构建稠密命中列表（使用冻结的分数）
        dense_hits = [
            DenseHit(
                chunk_uuid=chunk_uuid,
                doc_uuid=document_ids[chunk_uuid],
                score=score,
            )
            for chunk_uuid, score in case.dense_scores.items()
        ]
        dense_hits.sort(key=lambda hit: hit.score, reverse=True)  # 按分数降序
        # 构建稀疏命中列表（BM25 搜索结果）
        sparse_hits = [
            SparseHit(
                chunk_uuid=hit.chunk_uuid,
                doc_uuid=hit.doc_uuid,
                score=hit.score,
            )
            for hit in index.search(case.query, top_k=3)
        ]
        # 执行混合融合
        fused_hits = hybrid.fuse(dense_hits, sparse_hits)
        # 记录排序结果
        dense_ranking = [hit.chunk_uuid for hit in dense_hits]
        hybrid_ranking = [hit.chunk_uuid for hit in fused_hits]
        dense_rankings.append(dense_ranking)
        hybrid_rankings.append(hybrid_ranking)
        # 记录详情
        details.append(
            {
                "case_id": case.case_id,
                "query": case.query,
                "expected_chunk_uuid": case.expected_chunk_uuid,
                "dense_top_1": dense_ranking[0],    # 稠密检索首位
                "hybrid_top_1": hybrid_ranking[0],  # 混合检索首位
                "dense_rank": _rank_of(case.expected_chunk_uuid, dense_ranking),   # 稠密检索中的排名
                "hybrid_rank": _rank_of(case.expected_chunk_uuid, hybrid_ranking), # 混合检索中的排名
            }
        )

    return {
        "dataset": "controlled-enterprise-rule-regression-v1",
        "query_count": len(details),
        "alpha": hybrid.alpha,
        "dense": _metrics(controlled_benchmark_cases(), dense_rankings),    # 稠密指标
        "hybrid": _metrics(controlled_benchmark_cases(), hybrid_rankings),  # 混合指标
        "details": details,
    }


def _metrics(cases: list[BenchmarkCase], rankings: list[list[str]]) -> dict[str, float | int]:
    """计算评估指标：Hit@1（首位命中率）和 MRR（平均倒数排名）。

    Hit@1：正确结果排在首位的比例
    MRR (Mean Reciprocal Rank)：1/排名的平均值（排名越靠前越接近 1）
    """
    hit_count = 0
    reciprocal_rank_total = 0.0
    for case, ranking in zip(cases, rankings, strict=True):
        rank = _rank_of(case.expected_chunk_uuid, ranking)  # 获取正确结果在排序中的位置
        if rank == 1:         # 排在首位
            hit_count += 1
        if rank is not None:  # 在结果中
            reciprocal_rank_total += 1.0 / rank  # 累加倒数排名
    query_count = len(cases)
    return {
        "hit_at_1_count": hit_count,                             # 首位命中次数
        "hit_at_1": round(hit_count / query_count, 4),          # 首位命中率
        "mrr": round(reciprocal_rank_total / query_count, 4),   # 平均倒数排名
    }


def _rank_of(chunk_uuid: str, ranking: list[str]) -> int | None:
    """获取指定 chunk_uuid 在排序列表中的排名（1-based），不存在则返回 None。"""
    try:
        return ranking.index(chunk_uuid) + 1  # index 是 0-based，+1 转为 1-based
    except ValueError:                        # 不在列表中
        return None
