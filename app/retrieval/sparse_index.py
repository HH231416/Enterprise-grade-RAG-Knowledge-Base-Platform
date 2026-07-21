"""自实现的 BM25 稀疏索引，支持单例模式。

参数：k1=1.2, b=0.75。支持 build_index（构建索引）、search（搜索）、add_document（添加文档）、remove_document（删除文档）。
提供内存索引和 OpenSearch 两种后端实现。
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

import math                              # 数学函数：log 用于 IDF 计算
import re                                # 正则：中文分词、ASCII 提取
from collections import defaultdict      # 默认字典：倒排索引存储
from dataclasses import dataclass, field # 数据类装饰器
from threading import Lock               # 线程锁：保证单例和索引操作线程安全
from urllib.parse import urlparse        # URL 解析：从 OpenSearch URL 中提取主机和端口

from app.core.config import get_settings # 配置单例获取函数


@dataclass(slots=True)
class SparseHit:
    """稀疏检索命中结果（与 hybrid.py 中的同名类互补，本模块用于 BM25 内部）。"""
    chunk_uuid: str                         # 分块 UUID
    doc_uuid: str                           # 所属文档 UUID
    chunk_text: str                         # 分块文本内容
    score: float                            # BM25 相关性分数
    metadata: dict = field(default_factory=dict)  # 附加元数据


class BM25Index:
    """BM25 倒排索引：基于内存字典实现，支持增删改查。

    BM25 公式参数：
    - k1 (默认 1.2)：控制词频饱和度的参数
    - b (默认 0.75)：控制文档长度归一化程度的参数
    """

    def __init__(self, k1: float = 1.2, b: float = 0.75) -> None:
        self.k1 = k1  # 词频饱和度参数
        self.b = b    # 文档长度归一化参数
        # 正排索引：chunk_uuid → 词条列表
        self._docs: dict[str, list[str]] = {}
        # 倒排索引：词条 → {chunk_uuid → 词频}
        self._inverted: dict[str, dict[str, int]] = defaultdict(dict)
        # 元数据：chunk_uuid → {doc_uuid, chunk_text, ...}
        self._metadata: dict[str, dict] = {}
        self._doc_count = 0         # 文档总数
        self._total_term_count = 0  # 总词条数（所有文档的词条数之和）
        self._avgdl: float = 0.0    # 平均文档长度（avgdl = total_term_count / doc_count）

    @property
    def doc_count(self) -> int:
        """返回已索引的文档数量。"""
        return self._doc_count

    @property
    def document_ids(self) -> set[str]:
        """返回所有已索引的文档 chunk_uuid 集合。"""
        return set(self._docs.keys())

    def build_index(self, documents: list[dict]) -> None:
        """全量构建索引：清空现有索引，逐条添加文档。

        每个文档字典必须包含：chunk_uuid、doc_uuid、chunk_text，可选 metadata。
        """
        # 清空所有现有数据
        self._docs.clear()
        self._inverted.clear()
        self._metadata.clear()
        self._doc_count = 0
        self._total_term_count = 0
        # 逐条添加文档
        for doc in documents:
            self.add_document(doc)

    def add_document(self, doc: dict) -> None:
        """添加或更新单条文档到索引中。"""
        chunk_uuid = doc["chunk_uuid"]           # 分块 UUID
        text = doc.get("chunk_text", "")          # 分块文本
        tokens = self._tokenize(text)             # 分词

        # 如果该 chunk_uuid 已存在（重索引场景），先清除旧条目
        if chunk_uuid in self._docs:
            self._remove_from_inverted(chunk_uuid)

        # 更新正排索引和元数据
        self._docs[chunk_uuid] = tokens
        self._metadata[chunk_uuid] = {
            "doc_uuid": doc.get("doc_uuid", ""),
            "chunk_text": text,
            "chunk_uuid": chunk_uuid,
            **(doc.get("metadata") or {}),       # 合并额外元数据
        }
        # 重新计算统计信息
        self._doc_count = len(self._docs)
        self._total_term_count += len(tokens)
        self._avgdl = self._total_term_count / max(self._doc_count, 1)  # 防除零

        # 更新倒排索引：每个词条的 posting list 中递增该文档的词频
        for term in tokens:
            self._inverted[term].setdefault(chunk_uuid, 0)  # 确保键存在
            self._inverted[term][chunk_uuid] += 1            # 词频 +1

    def remove_document(self, chunk_uuid: str) -> None:
        """从索引中移除指定文档。"""
        if chunk_uuid not in self._docs:  # 不存在则直接返回
            return
        self._remove_from_inverted(chunk_uuid)  # 从倒排索引中清除
        self._docs.pop(chunk_uuid, None)        # 从正排索引中移除
        self._metadata.pop(chunk_uuid, None)    # 从元数据中移除
        self._doc_count = len(self._docs)       # 更新文档计数

        # 重新计算统计信息
        token_count = sum(len(tokens) for tokens in self._docs.values())
        self._total_term_count = token_count
        self._avgdl = self._total_term_count / max(self._doc_count, 1)

    def search(self, query: str, top_k: int = 20) -> list[SparseHit]:
        """BM25 搜索：对查询分词后计算每个候选文档的 BM25 分数，返回 top-k 结果。

        BM25 公式：
            score(D, Q) = Σ IDF(qi) × [tf(qi,D) × (k1+1)] / [tf(qi,D) + k1 × (1-b + b×|D|/avgdl)]
        """
        query_tokens = self._tokenize(query)  # 查询分词
        if not query_tokens or self._doc_count == 0:  # 空查询或空索引
            return []

        scores: dict[str, float] = {}  # chunk_uuid → 累计分数
        for term in query_tokens:      # 遍历查询中的每个词条
            posting = self._inverted.get(term, {})  # 获取该词的 posting list
            idf = self._idf(term)                    # 计算 IDF
            if idf == 0.0:                           # IDF 为 0 表示该词无区分度
                continue
            for doc_id, tf in posting.items():       # 遍历包含该词的所有文档
                doc_len = len(self._docs.get(doc_id, []))  # 文档长度
                # BM25 分子：tf × (k1 + 1)
                numerator = tf * (self.k1 + 1)
                # BM25 分母：tf + k1 × (1 - b + b × doc_len / avgdl)
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / max(self._avgdl, 1))
                # 累加分数：IDF × (分子 / 分母)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * numerator / denominator

        # 按分数降序排序，取 top_k
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [
            SparseHit(
                chunk_uuid=doc_id,
                doc_uuid=self._metadata.get(doc_id, {}).get("doc_uuid", ""),
                chunk_text=self._metadata.get(doc_id, {}).get("chunk_text", ""),
                score=round(score, 6),  # 保留 6 位小数
                metadata=self._metadata.get(doc_id, {}),
            )
            for doc_id, score in ranked
        ]

    def _remove_from_inverted(self, chunk_uuid: str) -> None:
        """从倒排索引中移除指定文档的所有词条记录。"""
        tokens = self._docs.get(chunk_uuid, [])  # 获取该文档的词条列表
        self._total_term_count -= len(tokens)    # 更新总词条数
        for term in tokens:                      # 遍历每个词条
            posting = self._inverted.get(term)
            if posting is None:
                continue
            if chunk_uuid in posting:
                del posting[chunk_uuid]          # 从 posting list 中删除该文档
            if not posting:                      # 如果 posting list 为空
                del self._inverted[term]          # 删除整个词条条目

    def _idf(self, term: str) -> float:
        """计算逆文档频率（IDF）。

        IDF 平滑公式：
            IDF = log((N - df + 0.5) / (df + 0.5) + 1)
        其中 N = 文档总数，df = 包含该词的文档数。
        """
        df = len(self._inverted.get(term, {}))  # 文档频率
        if df == 0:
            return 0.0
        return math.log((self._doc_count - df + 0.5) / (df + 0.5) + 1.0)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """分词器：支持中文 Bigram（二元切词）和 ASCII 字母数字提取。

        中文采用二元切词（如 "知识图谱" → ["知识", "图谱"]），
        英文按字母数字序列提取。
        """
        text = text.lower().strip()              # 转小写、去空白
        tokens: list[str] = []
        # 中文 Bigram：提取所有中文字符，按相邻两字滑动窗口切分
        cjk_chars = "".join(re.findall(r"[\u4e00-\u9fff]", text))
        for i in range(len(cjk_chars) - 1):
            tokens.append(cjk_chars[i : i + 2])  # 连续两个汉字
        # ASCII 分词：先将中文替换为空格，再提取字母数字序列
        ascii_text = re.sub(r"[\u4e00-\u9fff]", " ", text)
        ascii_tokens = re.findall(r"[a-z0-9]+", ascii_text)
        tokens.extend(ascii_tokens)
        return tokens


class SparseIndexProvider:
    """线程安全的稀疏索引门面类，支持内存 BM25 和 OpenSearch 两种后端。

    使用单例模式（__new__ + 双重检查锁）确保全局只有一个实例。
    """

    _instance: SparseIndexProvider | None = None  # 单例实例
    _lock: Lock = Lock()                           # 线程锁

    def __init__(self) -> None:
        # hasattr 检查防止 __init__ 在单例中重复初始化
        if not hasattr(self, "_index"):
            self._index = BM25Index(k1=1.2, b=0.75)  # 内存 BM25 索引
            self._opensearch_client = None             # OpenSearch 客户端（懒加载）

    def __new__(cls) -> SparseIndexProvider:
        """单例模式：双重检查锁定确保线程安全。"""
        if cls._instance is None:         # 第一次检查（无锁，快速路径）
            with cls._lock:                # 获取锁
                if cls._instance is None:  # 第二次检查（有锁，安全路径）
                    cls._instance = super().__new__(cls)  # 创建唯一实例
        return cls._instance

    @property
    def index(self) -> BM25Index:
        """获取内部 BM25 索引实例。"""
        return self._index

    @property
    def is_external(self) -> bool:
        """判断是否使用外部 OpenSearch 作为稀疏索引后端。"""
        return get_settings().sparse_index_provider == "opensearch"

    def build_index(self, documents: list[dict]) -> None:
        """构建索引：根据 provider 类型分发到内存索引或 OpenSearch。"""
        if self.is_external:
            self._build_opensearch_index(documents)  # OpenSearch 批量构建
            return
        with self._lock:                             # 内存模式加锁
            self._index.build_index(documents)

    def search(self, query: str, top_k: int = 20) -> list[SparseHit]:
        """搜索：根据 provider 类型分发到内存 BM25 或 OpenSearch。"""
        if self.is_external:
            return self._search_opensearch(query, top_k)
        with self._lock:                             # 内存模式加锁
            return self._index.search(query, top_k)

    def add_document(self, doc: dict) -> None:
        """添加文档：OpenSearch 通过 REST API，内存模式直接操作索引。"""
        if self.is_external:
            self._opensearch().index(
                index=self._opensearch_index_name(),
                id=doc["chunk_uuid"],               # 以 chunk_uuid 作为文档 ID
                body=self._opensearch_document(doc),
                refresh=False,                       # 不立即刷新（批量导入时性能更好）
            )
            return
        with self._lock:                             # 内存模式加锁
            self._index.add_document(doc)

    def remove_document(self, chunk_uuid: str) -> None:
        """移除文档：OpenSearch 通过 REST API，内存模式直接操作索引。"""
        if self.is_external:
            try:
                self._opensearch().delete(
                    index=self._opensearch_index_name(),
                    id=chunk_uuid,
                    refresh=False,
                )
            except Exception as exc:
                # 忽略 "not_found" 错误（文档可能已被删除）
                if "not_found" not in str(exc).lower():
                    raise
            return
        with self._lock:                             # 内存模式加锁
            self._index.remove_document(chunk_uuid)

    @property
    def doc_count(self) -> int:
        """获取已索引的文档总数。"""
        if self.is_external:
            try:
                return int(self._opensearch().count(index=self._opensearch_index_name())["count"])
            except Exception:
                return 0  # OpenSearch 不可用时返回 0
        with self._lock:
            return self._index.doc_count

    @property
    def document_ids(self) -> set[str]:
        """获取所有已索引的文档 ID 集合（仅内存模式支持）。"""
        if self.is_external:
            return set()  # OpenSearch 模式下不支持全量获取
        with self._lock:
            return self._index.document_ids

    # ── OpenSearch 私有方法 ──

    def _opensearch(self):
        """获取（或懒加载创建）OpenSearch 客户端。"""
        if self._opensearch_client is not None:
            return self._opensearch_client  # 已创建则直接返回
        try:
            from opensearchpy import OpenSearch  # 延迟导入，使其成为可选依赖
        except ImportError as exc:
            raise RuntimeError("opensearch-py is required for SPARSE_INDEX_PROVIDER=opensearch") from exc

        settings = get_settings()
        parsed = urlparse(settings.opensearch_url or "")  # 解析 URL
        if not parsed.hostname:
            raise RuntimeError("OPENSEARCH_URL must include a hostname")
        options = {
            "hosts": [
                {
                    "host": parsed.hostname,
                    "port": parsed.port or (443 if parsed.scheme == "https" else 80),  # 默认端口
                }
            ],
            "use_ssl": parsed.scheme == "https",       # HTTPS 则启用 SSL
            "verify_certs": parsed.scheme == "https",   # HTTPS 则验证证书
            "timeout": settings.provider_timeout_seconds,  # 超时时间
        }
        if settings.opensearch_username:               # 如果配置了用户名密码
            options["http_auth"] = (settings.opensearch_username, settings.opensearch_password or "")
        self._opensearch_client = OpenSearch(**options)  # 创建客户端
        self._ensure_opensearch_index()                  # 确保索引存在
        return self._opensearch_client

    def _ensure_opensearch_index(self) -> None:
        """确保 OpenSearch 索引存在，不存在则自动创建。"""
        client = self._opensearch_client
        assert client is not None
        index_name = self._opensearch_index_name()
        if client.indices.exists(index=index_name):  # 索引已存在
            return
        # 创建索引：1 个分片，1 个副本
        client.indices.create(
            index=index_name,
            body={
                "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 1}},
                "mappings": {
                    "properties": {
                        "chunk_uuid": {"type": "keyword"},     # 精确匹配
                        "doc_uuid": {"type": "keyword"},        # 精确匹配
                        "chunk_text": {"type": "text"},          # 全文搜索
                        "metadata": {"type": "object", "enabled": True},  # 嵌套对象
                    }
                },
            },
        )

    def _build_opensearch_index(self, documents: list[dict]) -> None:
        """全量重建 OpenSearch 索引：删除旧索引 → 创建新索引 → 批量导入。"""
        client = self._opensearch()
        index_name = self._opensearch_index_name()
        if client.indices.exists(index=index_name):    # 删除旧索引
            client.indices.delete(index=index_name)
        self._ensure_opensearch_index()                # 重新创建
        if not documents:
            return
        try:
            from opensearchpy import helpers           # 批量导入工具
        except ImportError as exc:
            raise RuntimeError("opensearch-py is required for SPARSE_INDEX_PROVIDER=opensearch") from exc
        helpers.bulk(
            client,
            [
                {
                    "_index": index_name,
                    "_id": document["chunk_uuid"],
                    "_source": self._opensearch_document(document),
                }
                for document in documents
            ],
            refresh=True,  # 导入后立即刷新，使数据可搜索
        )

    def _search_opensearch(self, query: str, top_k: int) -> list[SparseHit]:
        """通过 OpenSearch 执行全文搜索（multi_match 查询）。"""
        if not query.strip():
            return []
        response = self._opensearch().search(
            index=self._opensearch_index_name(),
            body={
                "size": top_k,
                "query": {
                    "multi_match": {
                        "query": query,
                        "fields": ["chunk_text^2", "metadata.section_title", "metadata.title"],  # chunk_text 权重 ×2
                        "operator": "or",  # 词条之间 OR 关系
                    }
                },
            },
        )
        return [
            SparseHit(
                chunk_uuid=str(hit["_source"]["chunk_uuid"]),
                doc_uuid=str(hit["_source"].get("doc_uuid", "")),
                chunk_text=str(hit["_source"].get("chunk_text", "")),
                score=float(hit.get("_score") or 0.0),  # OpenSearch 返回的 _score
                metadata=dict(hit["_source"].get("metadata") or {}),
            )
            for hit in response.get("hits", {}).get("hits", [])
        ]

    @staticmethod
    def _opensearch_document(doc: dict) -> dict:
        """将内部文档字典转换为 OpenSearch 文档格式。"""
        return {
            "chunk_uuid": str(doc["chunk_uuid"]),
            "doc_uuid": str(doc.get("doc_uuid", "")),
            "chunk_text": str(doc.get("chunk_text", "")),
            "metadata": dict(doc.get("metadata") or {}),
        }

    @staticmethod
    def _opensearch_index_name() -> str:
        """从配置中获取 OpenSearch 索引名称。"""
        return get_settings().opensearch_index
