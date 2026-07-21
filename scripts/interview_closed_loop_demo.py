from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_import_path() -> None:
    root = _project_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _configure_demo_environment(run_id: str) -> None:
    storage_root = Path(f"/private/tmp/oa_rag_demo_{run_id}")
    db_path = Path(f"/private/tmp/oa_rag_demo_{run_id}.sqlite3")

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["STORAGE_ROOT"] = str(storage_root)
    os.environ["RAW_DATA_DIR"] = str(storage_root / "raw")
    os.environ["PROCESSED_DATA_DIR"] = str(storage_root / "processed")
    os.environ["SAMPLE_DATA_DIR"] = str(storage_root / "samples")
    os.environ["APP_API_KEY"] = "demo-api-key"
    os.environ["INTERNAL_TOKEN"] = "demo-internal-token"
    os.environ["INGESTION_MODE"] = "sync"
    os.environ["VECTOR_STORE_PROVIDER"] = "local"
    os.environ["EMBEDDING_PROVIDER"] = "local"
    os.environ["EMBEDDING_API_BASE"] = ""
    os.environ["EMBEDDING_API_KEY"] = ""
    os.environ["EMBEDDING_MODEL"] = ""
    os.environ["LLM_API_BASE"] = ""
    os.environ["LLM_API_KEY"] = ""
    os.environ["LLM_MODEL"] = ""
    os.environ["ZILLIZ_URI"] = ""
    os.environ["ZILLIZ_TOKEN"] = ""


def _build_client() -> TestClient:
    from app.core.config import get_settings, reset_settings_cache
    from app.db.base import Base
    from app.db.runtime import get_engine, reset_db_runtime
    from app.main import create_app
    from app.integrations.vector_store import VectorStoreClient
    from app.retrieval.sparse_index import SparseIndexProvider

    reset_settings_cache()
    reset_db_runtime()
    VectorStoreClient._local_store.clear()
    SparseIndexProvider().build_index([])

    settings = get_settings()
    settings.ensure_storage_dirs()
    Base.metadata.create_all(bind=get_engine())
    return TestClient(create_app())


def _upload_text_document(
    client: TestClient,
    *,
    title: str,
    text: str,
    source_module: str = "hr",
) -> str:
    response = client.post(
        "/api/v1/documents/upload",
        data={
            "title": title,
            "source_type": "rule_doc",
            "source_module": source_module,
            "owner_dept": "hr",
        },
        files={"file": (f"{title}.txt", text.encode("utf-8"), "text/plain")},
    )
    response.raise_for_status()
    return response.json()["data"]["doc_uuid"]


def main() -> int:
    _ensure_import_path()
    run_id = uuid4().hex[:8]
    _configure_demo_environment(run_id)
    client = _build_client()

    relevant_doc_uuid = _upload_text_document(
        client,
        title="HR Transfer Policy",
        text=(
            "Transfer approval now requires two levels. "
            "The department manager approves first, then HR approves second."
        ),
    )
    _upload_text_document(
        client,
        title="Travel Expense Guide",
        text="Travel reimbursement requires receipts and finance review.",
    )

    search_response = client.post(
        "/api/v1/retrieval/search",
        json={
            "query": "department manager approves first then HR approves second",
            "top_k": 3,
            "strategy": "hybrid",
            "filters": {"source_module": ["hr"]},
            "user_context": {"user_id": "demo-user"},
        },
    )
    search_response.raise_for_status()
    search_payload = search_response.json()["data"]

    qa_response = client.post(
        "/api/v1/qa/answer",
        headers={"X-Internal-Token": "demo-internal-token"},
        json={
            "question": "What is the approval flow for transfer requests?",
            "top_k": 3,
            "filters": {"source_module": ["hr"]},
            "user_context": {"user_id": "demo-user"},
        },
    )
    qa_response.raise_for_status()
    qa_payload = qa_response.json()["data"]

    agent_response = client.post(
        "/api/v1/qa/agent-answer",
        headers={"X-Internal-Token": "demo-internal-token"},
        json={
            "question": "What is the approval flow for transfer requests?",
            "top_k": 3,
            "retrieval_strategy": "dense",
            "filters": {"source_module": ["hr"]},
            "user_context": {"user_id": "demo-user"},
        },
    )
    agent_response.raise_for_status()
    agent_payload = agent_response.json()["data"]

    dataset_response = client.post(
        "/api/v1/evaluation/datasets",
        json={
            "name": f"demo-eval-{run_id}",
            "queries": [
                {
                    "query_text": "transfer approval two levels",
                    "expected_doc_titles": ["HR Transfer Policy"],
                    "expected_terms": ["two levels", "HR approves second"],
                }
            ],
        },
    )
    dataset_response.raise_for_status()
    dataset_uuid = dataset_response.json()["data"]["dataset_uuid"]

    eval_run_response = client.post(
        "/api/v1/evaluation/runs",
        json={
            "dataset_uuid": dataset_uuid,
            "doc_uuids": [relevant_doc_uuid],
            "retrieval_strategy": "dense",
        },
    )
    eval_run_response.raise_for_status()
    eval_run = eval_run_response.json()["data"]

    eval_detail_response = client.get(f"/api/v1/evaluation/runs/{eval_run['run_uuid']}")
    eval_detail_response.raise_for_status()
    eval_detail = eval_detail_response.json()["data"]

    result = {
        "demo_run_id": run_id,
        "uploaded_documents": 2,
        "search_top_hit": search_payload["hits"][0] if search_payload["hits"] else None,
        "qa_answer_status": qa_payload["answer_status"],
        "qa_first_citation": qa_payload["citations"][0] if qa_payload["citations"] else None,
        "agent_answer_status": agent_payload["answer_status"],
        "agent_final_query": agent_payload["final_query"],
        "agent_trace": agent_payload["trace"],
        "evaluation_summary": eval_detail.get("summary"),
        "evaluation_first_result": eval_detail["results"][0] if eval_detail["results"] else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
