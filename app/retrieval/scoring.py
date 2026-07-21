# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

# 正则表达式模块，用于文本归一化和分词
import re


def _normalize_text(value: str) -> str:
    """文本归一化：去除首尾空白、转小写、将连续空白压缩为单个空格。"""
    # r"\s+" 匹配任意连续空白字符（空格、制表符、换行等），替换为单个空格
    return re.sub(r"\s+", " ", value.strip().lower())


def _extract_query_terms(query: str) -> list[str]:
    """从查询文本中提取有意义的检索词条列表（去重）。

    处理逻辑：
    1. 先归一化文本
    2. 按空格分词
    3. 对每个词段，分别提取 ASCII 字母数字串（如 "openai"）和中文词（≥2 个汉字）
    4. 去重后返回
    """
    # 归一化：去空白、转小写、压缩空格
    normalized = _normalize_text(query)
    if not normalized:          # 空查询直接返回空列表
        return []

    # 按空格分词，过滤空字符串
    split_terms = [term for term in normalized.split(" ") if term]
    extracted: list[str] = []   # 最终提取的词条列表
    seen: set[str] = set()      # 已见过的词条集合，用于去重

    for term in split_terms:    # 遍历每个分词段
        # 提取 ASCII 字母数字串：[a-z0-9]+ 匹配连续的字母或数字（如 "gpt4"、"2024"）
        ascii_segments = re.findall(r"[a-z0-9]+", term)
        # 提取中文词段：[\u4e00-\u9fff]{2,} 匹配 2 个及以上连续汉字（过滤单字）
        cjk_segments = re.findall(r"[\u4e00-\u9fff]{2,}", term)

        # 合并 ASCII 和中文候选词
        candidates = ascii_segments + cjk_segments
        # 如果正则没匹配到任何候选，但原词段非空，则原样保留（兜底策略）
        if not candidates and term:
            candidates = [term]

        for candidate in candidates:     # 遍历候选词
            if candidate not in seen:    # 去重：只添加未见过的词
                extracted.append(candidate)
                seen.add(candidate)

    return extracted


def _expand_terms(terms: list[str]) -> list[str]:
    """对中文长词进行 Bigram 切分扩展，提升召回率。

    例如：四字中文词 "知识图谱" 扩展为 ["知识图谱", "知识", "图谱"]
    这样短查询也能命中长文档中的关键词。
    """
    expanded: list[str] = []   # 扩展后的词条列表
    seen: set[str] = set()     # 已见过的词条集合，用于去重

    for term in terms:         # 遍历原始词条
        candidates = [term]    # 候选列表，至少包含原词条本身

        # 若词条为 4 个及以上纯汉字，则生成 Bigram（连续 2 字组合）
        # re.fullmatch 确保整个字符串都是汉字
        if re.fullmatch(r"[\u4e00-\u9fff]{4,}", term):
            # 滑动窗口生成 Bigram：从索引 0 到 len-2，每次取 2 个字符
            candidates.extend(
                term[index:index + 2]              # 取连续 2 个汉字
                for index in range(len(term) - 1)  # 窗口从 0 滑到 len-2
            )

        for candidate in candidates:     # 遍历候选词（原词 + Bigram 子串）
            if candidate and candidate not in seen:  # 过滤空串并去重
                expanded.append(candidate)
                seen.add(candidate)

    return expanded


def score_text_match(query: str, text: str) -> float:
    """计算查询与文本的关键词匹配分数。

    返回值范围 [0.0, 1.0]，表示查询词条在目标文本中的命中比例。

    流程：
    1. 提取查询中的关键词条
    2. 对中文长词进行 Bigram 扩展（提升召回）
    3. 统计有多少词条出现在归一化后的目标文本中
    4. 返回 命中数 / 总词条数
    """
    # 提取查询词条并做 Bigram 扩展
    query_terms = _expand_terms(_extract_query_terms(query))
    if not query_terms:        # 无有效词条则返回 0.0
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
