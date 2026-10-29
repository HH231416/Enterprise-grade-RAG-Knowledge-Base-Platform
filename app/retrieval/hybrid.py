"""混合检索器：通过 RRF（Reciprocal Rank Fusion，倒数排名融合）将稠密检索（向量）和稀疏检索（BM25）结果合并为单一排序列表。"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

# 导入 dataclass 装饰器和 field 函数，用于定义轻量级数据容器
from dataclasses import dataclass, field


# ============================================================
# 数据模型：三种命中（Hit）类型
# ============================================================

@dataclass(slots=True)  # slots=True 禁用 __dict__，减少内存占用，适合大量实例场景
class DenseHit:
    """稠密检索命中结果（向量相似度搜索返回的单条结果）。"""
    chunk_uuid: str                         # 分块 UUID
    doc_uuid: str                           # 所属文档 UUID
    score: float                            # 向量相似度分数（如余弦相似度）
    metadata: dict = field(default_factory=dict)  # 附加元数据，default_factory 确保每个实例独立的空字典


@dataclass(slots=True)
class SparseHit:
    """稀疏检索命中结果（BM25 关键词搜索返回的单条结果）。"""
    chunk_uuid: str                         # 分块 UUID
    doc_uuid: str                           # 所属文档 UUID
    score: float                            # BM25 相关性分数
    metadata: dict = field(default_factory=dict)  # 附加元数据


@dataclass(slots=True)
class HybridHit:
    """融合后的命中结果，包含两种检索的原始分数和加权融合分数。"""
    chunk_uuid: str                         # 分块 UUID
    doc_uuid: str                           # 所属文档 UUID
    dense_score: float | None               # 稠密检索分数（仅稠密命中时有值）
    sparse_score: float | None              # 稀疏检索分数（仅稀疏命中时有值）
    fusion_score: float                     # 融合后的最终排序分数
    metadata: dict = field(default_factory=dict)  # 合并后的元数据


# ============================================================
# 混合检索器
# ============================================================

class HybridRetriever:
    """通过 RRF（Reciprocal Rank Fusion，倒数排名融合）将稠密和稀疏检索结果合并排序。

    rrf_k: RRF 平滑常数，默认 60。融合分 = Σ 1/(rrf_k + 排名)，只用排名不用原始分数，
           因此天然免疫 Dense 余弦分与 BM25 词频分的量纲差异，也不受单批候选分数漂移影响。
           与 Min-Max 归一化的区别：Min-Max 产出"绝对相关性"分数，RRF 只产出"相对排名"分数。
    """

    def __init__(self, rrf_k: float = 60.0) -> None:
        # 平滑常数钳制在 >= 1，避免分母为 0 或负值
        self.rrf_k = max(1.0, rrf_k)

    def fuse(
        self,
        dense_hits: list[DenseHit],   # 稠密检索结果列表（按相似度降序）
        sparse_hits: list[SparseHit], # 稀疏检索结果列表（按 BM25 分数降序）
    ) -> list[HybridHit]:             # 返回融合后的结果列表（按 fusion_score 降序）
        """执行 RRF 融合，返回按融合分降序排列的结果列表。

        RRF 公式：score(chunk) = Σ 1 / (rrf_k + rank)
        - rank 从 1 开始；输入列表已按各自分数降序，故 index + 1 即排名；
        - 某一路未命中该 chunk 时，该项贡献为 0。
        """
        # 两者都为空，直接返回空列表
        if not dense_hits and not sparse_hits:
            return []

        # 排名映射：chunk_uuid → 排名（从 1 开始）
        dense_rank: dict[str, int] = {h.chunk_uuid: i + 1 for i, h in enumerate(dense_hits)}
        sparse_rank: dict[str, int] = {h.chunk_uuid: i + 1 for i, h in enumerate(sparse_hits)}
        # 原始命中映射：chunk_uuid → Hit（用于回填原始分数和元数据）
        dense_map: dict[str, DenseHit] = {h.chunk_uuid: h for h in dense_hits}
        sparse_map: dict[str, SparseHit] = {h.chunk_uuid: h for h in sparse_hits}

        # 取两种检索结果的 chunk_uuid 并集（去重），确保两边命中都参与融合
        all_uuids = set(dense_map.keys()) | set(sparse_map.keys())
        fused: list[HybridHit] = []  # 初始化融合结果列表
        for uid in all_uuids:        # 遍历所有唯一的 chunk_uuid
            d = dense_map.get(uid)   # 在稠密结果中查找，不存在则为 None
            s = sparse_map.get(uid)  # 在稀疏结果中查找，不存在则为 None

            # RRF：只用排名不用原始分数；某一路未命中该项贡献为 0
            d_rrf = 1.0 / (self.rrf_k + dense_rank[uid]) if d else 0.0
            s_rrf = 1.0 / (self.rrf_k + sparse_rank[uid]) if s else 0.0
            fusion_score = d_rrf + s_rrf

            # 合并元数据：稠密和稀疏的 metadata 合并（后者覆盖前者同名键）
            metadata = {}
            if d:
                metadata.update(d.metadata)  # 先合并稠密侧的元数据
            if s:
                metadata.update(s.metadata)  # 再合并稀疏侧的元数据（同名键覆盖）

            fused.append(
                HybridHit(
                    chunk_uuid=uid,
                    # 优先取稠密结果的 doc_uuid，否则取稀疏结果的，都没有则为空字符串
                    doc_uuid=(d and d.doc_uuid) or (s and s.doc_uuid) or "",
                    dense_score=d.score if d else None,     # 保留稠密原始分数（供证据门控）
                    sparse_score=s.score if s else None,    # 保留稀疏原始分数（供证据门控）
                    fusion_score=round(fusion_score, 6),    # RRF 融合分（仅用于排序）
                    metadata=metadata,
                )
            )

        # 按融合分数降序排序，分数最高的排在前面
        fused.sort(key=lambda h: h.fusion_score, reverse=True)
        return fused
