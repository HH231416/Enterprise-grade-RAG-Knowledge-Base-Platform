"""混合检索器：通过最小-最大归一化 + Alpha 加权融合，将稠密检索（向量）和稀疏检索（BM25）结果合并为单一排序列表。"""

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
    """通过最小-最大归一化（Min-Max Normalization）和 Alpha 加权融合，将稠密和稀疏检索结果合并排序。

    alpha: 稠密检索分数的权重，取值范围 [0, 1]。
           (1 - alpha) 为稀疏检索分数的权重。
           默认 0.7 表示更信任语义向量检索结果。
    """

    def __init__(self, alpha: float = 0.7) -> None:
        # 将 alpha 钳制在 [0.0, 1.0] 范围内，防止非法输入
        self.alpha = max(0.0, min(1.0, alpha))

    def fuse(
        self,
        dense_hits: list[DenseHit],   # 稠密检索结果列表（按相似度降序）
        sparse_hits: list[SparseHit], # 稀疏检索结果列表（按 BM25 分数降序）
    ) -> list[HybridHit]:             # 返回融合后的结果列表（按 fusion_score 降序）
        """执行混合融合，返回按融合分数降序排列的结果列表。"""

        # === 边界情况处理 ===
        # 情况1：两者都为空，直接返回空列表
        if not dense_hits and not sparse_hits:
            return []
        # 情况2：只有稀疏结果，直接包装为 HybridHit 返回（稠密分数置 None）
        if not dense_hits:
            return [
                HybridHit(
                    chunk_uuid=h.chunk_uuid,
                    doc_uuid=h.doc_uuid,
                    dense_score=None,        # 无稠密命中
                    sparse_score=h.score,     # 保留原始 BM25 分数
                    fusion_score=h.score,     # 融合分数即稀疏分数
                    metadata=h.metadata,
                )
                for h in sparse_hits         # 列表推导式，遍历所有稀疏命中
            ]
        # 情况3：只有稠密结果，直接包装为 HybridHit 返回（稀疏分数置 None）
        if not sparse_hits:
            return [
                HybridHit(
                    chunk_uuid=h.chunk_uuid,
                    doc_uuid=h.doc_uuid,
                    dense_score=h.score,      # 保留原始向量分数
                    sparse_score=None,         # 无稀疏命中
                    fusion_score=h.score,      # 融合分数即稠密分数
                    metadata=h.metadata,
                )
                for h in dense_hits
            ]

        # === 正常融合流程：两种结果都有 ===

        # 提取所有稠密检索分数列表
        dense_scores = [h.score for h in dense_hits]
        # 提取所有稀疏检索分数列表
        sparse_scores = [h.score for h in sparse_hits]

        # 计算稠密分数的最小值和最大值（用于 Min-Max 归一化）
        d_min, d_max = min(dense_scores), max(dense_scores)
        # 计算稀疏分数的最小值和最大值
        s_min, s_max = min(sparse_scores), max(sparse_scores)

        # 计算归一化的分母（极差），若极差为 0（所有分数相同）则设为 1.0 避免除零
        d_range = d_max - d_min if d_max != d_min else 1.0
        s_range = s_max - s_min if s_max != s_min else 1.0

        # 构建以 chunk_uuid 为键的快速查找字典，O(1) 查找
        dense_map: dict[str, DenseHit] = {h.chunk_uuid: h for h in dense_hits}
        sparse_map: dict[str, SparseHit] = {h.chunk_uuid: h for h in sparse_hits}

        # 取两种检索结果的 chunk_uuid 并集（去重），确保两边命中都参与融合
        all_uuids = set(dense_map.keys()) | set(sparse_map.keys())
        fused: list[HybridHit] = []  # 初始化融合结果列表
        for uid in all_uuids:        # 遍历所有唯一的 chunk_uuid
            d = dense_map.get(uid)   # 在稠密结果中查找，不存在则为 None
            s = sparse_map.get(uid)  # 在稀疏结果中查找，不存在则为 None

            # Min-Max 归一化公式：(score - min) / (max - min)，将分数映射到 [0, 1] 区间
            # 若该 chunk 在某侧无命中，归一化分数为 0.0（对融合贡献为零）
            d_norm = (d.score - d_min) / d_range if d else 0.0
            s_norm = (s.score - s_min) / s_range if s else 0.0

            # Alpha 加权融合公式：fusion_score = α × 稠密归一化分数 + (1-α) × 稀疏归一化分数
            fusion_score = self.alpha * d_norm + (1 - self.alpha) * s_norm

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
                    dense_score=d.score if d else None,     # 稠密原始分数
                    sparse_score=s.score if s else None,    # 稀疏原始分数
                    fusion_score=round(fusion_score, 6),    # 融合分数保留 6 位小数
                    metadata=metadata,
                )
            )

        # 按融合分数降序排序，分数最高的排在前面
        fused.sort(key=lambda h: h.fusion_score, reverse=True)
        return fused
