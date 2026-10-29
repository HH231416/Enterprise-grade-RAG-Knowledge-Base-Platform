# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

# 正则表达式模块，用于文本归一化和分词
import re


def _normalize_text(value: str) -> str:
    """文本归一化：去除首尾空白、转小写、将连续空白压缩为单个空格。"""
    # r"\s+" 匹配任意连续空白字符（空格、制表符、换行等），替换为单个空格
    return re.sub(r"\s+", " ", value.strip().lower())


def _extract_query_terms(query: str) -> list[str]:
    """从查询文本中提取检索词条列表（jieba 词级分词，去重）。

    处理逻辑：
    1. 先归一化文本（去空白、转小写、压缩空格）
    2. 用 jieba 词级切分——与生产 Milvus 的 jieba 分析器保持一致
    3. 去重后返回
    """
    import jieba

    normalized = _normalize_text(query)
    if not normalized:  # 空查询直接返回空列表
        return []

    extracted: list[str] = []  # 最终提取的词条列表
    seen: set[str] = set()     # 已见过的词条集合，用于去重
    for token in jieba.cut(normalized):
        token = token.strip()
        if token and token not in seen:
            extracted.append(token)
            seen.add(token)
    return extracted


def score_text_match(query: str, text: str) -> float:
    """计算查询与文本的关键词匹配分数。

    返回值范围 [0.0, 1.0]，表示查询词条在目标文本中的命中比例。

    流程：
    1. 用 jieba 提取查询关键词条（与生产分析器一致）
    2. 统计有多少词条出现在归一化后的目标文本中
    3. 返回 命中数 / 总词条数
    """
    query_terms = _extract_query_terms(query)
    if not query_terms:  # 无有效词条则返回 0.0
        return 0.0

    # 归一化目标文本（与查询相同的归一化处理，保证匹配一致性）
    normalized_text = _normalize_text(text)
    # 统计命中数：词条是否出现在归一化文本中（子串匹配）
    hits = sum(1 for term in query_terms if term in normalized_text)
    # 返回命中比例，保留 4 位小数
    return round(hits / len(query_terms), 4)

def blend_scores(*, vector_score: float, text_score: float) -> float:
    """混合打分：将向量相似度分数和文本匹配分数加权融合。

    使用关键字参数（* 后的参数必须命名传参），避免位置混淆。

    权重分配：
    - vector_score 权重 0.7（更信任语义相似度）
    - text_score   权重 0.3（关键词匹配作为补充）
    """
    # 加权求和并保留 6 位小数
    return round((vector_score * 0.7) + (text_score * 0.3), 6)
