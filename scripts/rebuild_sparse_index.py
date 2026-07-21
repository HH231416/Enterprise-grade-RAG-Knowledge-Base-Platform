from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from app.core.config import get_settings
    from app.db.runtime import get_session_factory
    from app.services.sparse_indexing import SparseIndexingService

    settings = get_settings()
    if settings.sparse_index_provider != "opensearch":
        raise SystemExit("Set SPARSE_INDEX_PROVIDER=opensearch before rebuilding the external sparse index.")

    session = get_session_factory()()
    try:
        count = SparseIndexingService().rebuild_from_database(session)
    finally:
        session.close()
    print(f"Rebuilt OpenSearch sparse index with {count} chunks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
