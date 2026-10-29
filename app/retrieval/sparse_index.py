"""本地开发与评测使用的内存 BM25 稀疏索引。

生产检索不再依赖独立的稀疏索引服务：Milvus Collection 通过内置 BM25
Function 同时保存原文、稠密向量、稀疏向量和动态标量元数据。本模块只保留
本地运行和隔离评测所需的确定性实现。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock


@dataclass(slots=True)
class SparseHit:
    chunk_uuid: str
    doc_uuid: str
    chunk_text: str
    score: float
    metadata: dict = field(default_factory=dict)


class BM25Index:
    """支持中英文混合分词的内存 BM25 倒排索引。"""

    def __init__(self, k1: float = 1.2, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: dict[str, list[str]] = {}
        self._inverted: dict[str, dict[str, int]] = defaultdict(dict)
        self._metadata: dict[str, dict] = {}
        self._doc_count = 0
        self._total_term_count = 0
        self._avgdl = 0.0

    @property
    def doc_count(self) -> int:
        return self._doc_count

    @property
    def document_ids(self) -> set[str]:
        return set(self._docs)

    def build_index(self, documents: list[dict]) -> None:
        self._docs.clear()
        self._inverted.clear()
        self._metadata.clear()
        self._doc_count = 0
        self._total_term_count = 0
        for document in documents:
            self.add_document(document)

    def add_document(self, document: dict) -> None:
        chunk_uuid = str(document["chunk_uuid"])
        text = str(document.get("chunk_text", ""))
        if chunk_uuid in self._docs:
            self._remove_from_inverted(chunk_uuid)

        tokens = self._tokenize(text)
        self._docs[chunk_uuid] = tokens
        self._metadata[chunk_uuid] = {
            "chunk_uuid": chunk_uuid,
            "doc_uuid": str(document.get("doc_uuid", "")),
            "chunk_text": text,
            **(document.get("metadata") or {}),
        }
        self._doc_count = len(self._docs)
        self._total_term_count += len(tokens)
        self._avgdl = self._total_term_count / max(self._doc_count, 1)
        for term in tokens:
            self._inverted[term][chunk_uuid] = self._inverted[term].get(chunk_uuid, 0) + 1

    def remove_document(self, chunk_uuid: str) -> None:
        if chunk_uuid not in self._docs:
            return
        self._remove_from_inverted(chunk_uuid)
        self._docs.pop(chunk_uuid, None)
        self._metadata.pop(chunk_uuid, None)
        self._doc_count = len(self._docs)
        self._total_term_count = sum(len(tokens) for tokens in self._docs.values())
        self._avgdl = self._total_term_count / max(self._doc_count, 1)

    def search(self, query: str, top_k: int = 20) -> list[SparseHit]:
        query_tokens = self._tokenize(query)
        if not query_tokens or not self._doc_count:
            return []

        scores: dict[str, float] = {}
        for term in query_tokens:
            posting = self._inverted.get(term, {})
            idf = self._idf(term)
            for chunk_uuid, term_frequency in posting.items():
                document_length = len(self._docs[chunk_uuid])
                numerator = term_frequency * (self.k1 + 1)
                denominator = term_frequency + self.k1 * (
                    1 - self.b + self.b * document_length / max(self._avgdl, 1)
                )
                scores[chunk_uuid] = scores.get(chunk_uuid, 0.0) + idf * numerator / denominator

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
        return [
            SparseHit(
                chunk_uuid=chunk_uuid,
                doc_uuid=str(self._metadata[chunk_uuid].get("doc_uuid", "")),
                chunk_text=str(self._metadata[chunk_uuid].get("chunk_text", "")),
                score=round(score, 6),
                metadata=self._metadata[chunk_uuid],
            )
            for chunk_uuid, score in ranked
        ]

    def _remove_from_inverted(self, chunk_uuid: str) -> None:
        tokens = self._docs[chunk_uuid]
        self._total_term_count -= len(tokens)
        for term in tokens:
            posting = self._inverted.get(term)
            if posting is None:
                continue
            posting.pop(chunk_uuid, None)
            if not posting:
                self._inverted.pop(term, None)

    def _idf(self, term: str) -> float:
        document_frequency = len(self._inverted.get(term, {}))
        if not document_frequency:
            return 0.0
        return math.log((self._doc_count - document_frequency + 0.5) / (document_frequency + 0.5) + 1.0)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        # \u4e0e\u751f\u4ea7 Milvus \u7684 jieba \u5206\u6790\u5668\u4fdd\u6301\u540c\u4e00\u5206\u8bcd\u89c4\u8303\uff1ajieba \u8bcd\u7ea7\u5207\u5206 +
        # \u5c0f\u5199\u5f52\u4e00\u3002\u4e2d\u82f1\u6587\u6807\u8bc6\u7b26\uff08\u8bfe\u7a0b\u4ee3\u7801\u3001\u6761\u6b3e\u7f16\u53f7\uff09\u90fd\u7531 jieba \u76f4\u63a5\u5207\u51fa\u3002
        import jieba

        normalized = text.lower().strip()
        return [token for token in jieba.cut(normalized) if token.strip()]


class SparseIndexProvider:
    """本地 BM25 索引的线程安全门面。"""

    _instance: SparseIndexProvider | None = None
    _lock = Lock()

    def __new__(cls) -> SparseIndexProvider:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if not hasattr(self, "_index"):
            self._index = BM25Index()

    @property
    def index(self) -> BM25Index:
        return self._index

    @property
    def doc_count(self) -> int:
        with self._lock:
            return self._index.doc_count

    @property
    def document_ids(self) -> set[str]:
        with self._lock:
            return self._index.document_ids

    def build_index(self, documents: list[dict]) -> None:
        with self._lock:
            self._index.build_index(documents)

    def add_document(self, document: dict) -> None:
        with self._lock:
            self._index.add_document(document)

    def remove_document(self, chunk_uuid: str) -> None:
        with self._lock:
            self._index.remove_document(chunk_uuid)

    def search(self, query: str, top_k: int = 20) -> list[SparseHit]:
        with self._lock:
            return self._index.search(query, top_k)
