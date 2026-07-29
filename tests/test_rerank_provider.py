from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.integrations.rerank import TeiRerankProvider, create_rerank_provider
from app.retrieval.hybrid import DenseHit, HybridRetriever, SparseHit
from app.schemas.retrieval import SearchRequest


def test_production_search_request_is_hybrid_only():
    assert SearchRequest(query="transfer policy").strategy == "hybrid"

    with pytest.raises(ValidationError):
        SearchRequest(query="transfer policy", strategy="dense")


def test_tei_reranker_uses_native_contract(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"index": 1, "score": 0.33},
                {"index": 0, "score": 0.91},
            ]

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("app.integrations.rerank.httpx.post", fake_post)
    settings = get_settings().model_copy(
        update={
            "rerank_enabled": True,
            "rerank_provider": "tei",
            "rerank_api_base": "http://tei-reranker.internal/rerank",
            "rerank_api_key": "service-token",
            "rerank_model": "BAAI/bge-reranker-v2-m3",
        }
    )

    provider = create_rerank_provider(settings)
    results = provider.rerank("GPA requirement", ["GPA >= 2.0", "library hours"])

    assert isinstance(provider, TeiRerankProvider)
    assert captured["url"] == "http://tei-reranker.internal/rerank"
    assert captured["headers"]["Authorization"] == "Bearer service-token"
    assert captured["json"] == {
        "query": "GPA requirement",
        "texts": ["GPA >= 2.0", "library hours"],
    }
    assert [result.index for result in results] == [0, 1]


def test_hybrid_preserves_single_candidate_channel_evidence():
    hits = HybridRetriever(alpha=0.7).fuse(
        [DenseHit(chunk_uuid="chunk-1", doc_uuid="doc-1", score=0.4)],
        [SparseHit(chunk_uuid="chunk-1", doc_uuid="doc-1", score=1.2)],
    )

    assert len(hits) == 1
    assert hits[0].fusion_score == 1.0
