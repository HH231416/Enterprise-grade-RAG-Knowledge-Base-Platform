from __future__ import annotations

from app.core.config import get_settings
from app.integrations.vector_store import VectorStoreClient


def test_local_vector_collections_are_isolated():
    store = VectorStoreClient(get_settings())
    store.upsert_embeddings(
        embeddings=[[1.0, 0.0]],
        metadatas=[{"chunk_uuid": "production", "doc_uuid": "doc-production"}],
        collection_name="production_chunks",
    )
    store.upsert_embeddings(
        embeddings=[[1.0, 0.0]],
        metadatas=[{"chunk_uuid": "evaluation", "doc_uuid": "doc-evaluation"}],
        collection_name="evaluation_run_1",
    )

    production = store.search(
        query_embedding=[1.0, 0.0],
        top_k=10,
        collection_name="production_chunks",
    )
    evaluation = store.search(
        query_embedding=[1.0, 0.0],
        top_k=10,
        collection_name="evaluation_run_1",
    )

    assert [hit.metadata["chunk_uuid"] for hit in production] == ["production"]
    assert [hit.metadata["chunk_uuid"] for hit in evaluation] == ["evaluation"]


def test_delete_collection_does_not_touch_other_collections():
    store = VectorStoreClient(get_settings())
    store.upsert_embeddings(
        embeddings=[[1.0, 0.0]],
        metadatas=[{"chunk_uuid": "keep", "doc_uuid": "doc-keep"}],
        collection_name="keep_collection",
    )
    store.upsert_embeddings(
        embeddings=[[1.0, 0.0]],
        metadatas=[{"chunk_uuid": "remove", "doc_uuid": "doc-remove"}],
        collection_name="remove_collection",
    )

    assert store.drop_collection(collection_name="remove_collection") is True
    assert store.search(
        query_embedding=[1.0, 0.0],
        top_k=10,
        collection_name="remove_collection",
    ) == []
    assert len(
        store.search(
            query_embedding=[1.0, 0.0],
            top_k=10,
            collection_name="keep_collection",
        )
    ) == 1
