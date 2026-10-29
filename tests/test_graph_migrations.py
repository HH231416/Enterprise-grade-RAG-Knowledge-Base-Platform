"""经 Alembic 建库后的插入验证。

评审问题 4：迁移中三张新表的 id 缺少 primary_key=True、外键未声明，
与 ORM 元数据创建的表不一致。原来的迁移测试只检查迁移能执行，
未验证新表能否真正插入。

本测试用独立的临时库执行 alembic upgrade head，然后经 SQLAlchemy ORM
向新表插入批次、候选与生效指针，验证主键自增、外键与约束行为。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session


@pytest.fixture
def migrated_engine(tmp_path, monkeypatch):
    """执行 alembic upgrade head 并返回指向该库的 engine。

    SQLite 默认不强制外键（PRAGMA foreign_keys=OFF），会掩盖外键定义缺陷。
    这里通过事件钩子开启，使外键验证真实有效。
    PostgreSQL 始终强制外键，不受影响。
    """
    db_path = tmp_path / "migrated.sqlite3"
    storage_root = tmp_path / "storage"

    from app.core.config import reset_settings_cache
    from app.db.runtime import reset_db_runtime

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("RAW_DATA_DIR", str(storage_root / "raw"))
    monkeypatch.setenv("PROCESSED_DATA_DIR", str(storage_root / "processed"))
    monkeypatch.setenv("SAMPLE_DATA_DIR", str(storage_root / "samples"))

    reset_settings_cache()
    reset_db_runtime()

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")

    @sqlalchemy.event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        yield engine
    finally:
        engine.dispose()
        reset_settings_cache()
        reset_db_runtime()


def _insert_document(session: Session) -> str:
    """插入一条满足外键前提的文档。"""
    import uuid

    from app.models.document import Document

    doc_uuid = uuid.uuid4()
    session.add(
        Document(
            doc_uuid=doc_uuid,
            title="校园办事指南（虚构测试资料）",
            source_type="manual",
            source_module="campus-guide",
            file_name="guide.pdf",
            file_ext=".pdf",
            mime_type="application/pdf",
            file_path="/tmp/graph-test/guide.pdf",
            file_size=1024,
            file_hash="hash-migrated",
            version="v1",
            access_level="public",
            status="active",
            review_status="approved",
            parse_status="success",
            index_status="success",
        )
    )
    session.commit()
    return str(doc_uuid)


def test_graph_tables_created_by_migration(migrated_engine):
    inspector = inspect(migrated_engine)
    tables = set(inspector.get_table_names())
    assert {
        "graph_build_batches",
        "graph_candidates",
        "graph_active_batches",
    }.issubset(tables)


def test_graph_tables_have_primary_keys(migrated_engine):
    """三张新表必须有主键，且 id 列确实被识别为 primary key。"""
    inspector = inspect(migrated_engine)
    for table in ("graph_build_batches", "graph_candidates", "graph_active_batches"):
        pk = inspector.get_pk_constraint(table)
        assert pk is not None, f"{table} 缺少主键约束"
        assert pk["constrained_columns"] == ["id"], f"{table} 主键应为 id"


def test_graph_tables_have_foreign_keys(migrated_engine):
    """外键必须在迁移建库后存在，与 ORM 声明一致。"""
    inspector = inspect(migrated_engine)

    batch_fks = {fk["referred_table"] for fk in inspector.get_foreign_keys("graph_build_batches")}
    assert "documents" in batch_fks, "graph_build_batches.doc_uuid 应外键到 documents"

    candidate_fks = {
        (fk["referred_table"], tuple(fk["constrained_columns"]))
        for fk in inspector.get_foreign_keys("graph_candidates")
    }
    assert ("documents", ("doc_uuid",)) in candidate_fks
    assert ("graph_build_batches", ("batch_uuid",)) in candidate_fks

    active_fks = {
        (fk["referred_table"], tuple(fk["constrained_columns"]))
        for fk in inspector.get_foreign_keys("graph_active_batches")
    }
    assert ("graph_build_batches", ("batch_uuid",)) in active_fks


def test_qa_conversations_has_owner_column(migrated_engine):
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("qa_conversations")}
    assert "owner_user_id" in columns


def test_insert_graph_batch_candidate_and_pointer(migrated_engine):
    """迁移建库后，ORM 能正常插入批次、候选与生效指针并拿到自增主键。"""
    import uuid

    from app.models.graph import GraphActiveBatch, GraphBuildBatch, GraphCandidate

    with Session(migrated_engine) as session:
        doc_uuid = _insert_document(session)
        batch_uuid = uuid.uuid4()
        candidate_uuid = uuid.uuid4()
        doc_key = uuid.UUID(doc_uuid)

        batch = GraphBuildBatch(
            batch_uuid=batch_uuid,
            doc_uuid=doc_key,
            doc_version="v1",
            source_fingerprint="fp-migrated",
            chunk_count=1,
            access_scope="public",
            status="pending_review",
            candidate_count=1,
        )
        session.add(batch)
        session.commit()

        # 主键自增生效
        assert batch.id is not None

        candidate = GraphCandidate(
            candidate_uuid=candidate_uuid,
            batch_uuid=batch_uuid,
            doc_uuid=doc_key,
            chunk_uuid=uuid.uuid4(),
            chunk_index=0,
            relation_type="handled_by",
            source_entity_key="本科生毕业学位申请",
            source_entity_type="procedure",
            source_entity_name="本科生毕业学位申请",
            target_entity_key="教务处",
            target_entity_type="department",
            target_entity_name="教务处",
            evidence_text="本科生毕业学位申请由教务处受理。",
            review_status="pending",
        )
        session.add(candidate)
        session.commit()
        assert candidate.id is not None
        # 自增主键在各自表内独立计数，两张表都是首个插入 → 均为 1
        assert candidate.id == 1

        pointer = GraphActiveBatch(
            doc_uuid=doc_key,
            batch_uuid=batch_uuid,
            source_fingerprint="fp-migrated",
        )
        session.add(pointer)
        session.commit()
        assert pointer.id is not None


def test_candidate_batch_foreign_key_enforced(migrated_engine):
    """候选引用不存在的批次必须失败，证明外键真实生效。"""
    import uuid

    import sqlalchemy.exc

    from app.models.graph import GraphCandidate

    with Session(migrated_engine) as session:
        doc_uuid = _insert_document(session)
        session.add(
            GraphCandidate(
                candidate_uuid=uuid.uuid4(),
                batch_uuid=uuid.uuid4(),  # 不存在的批次
                doc_uuid=uuid.UUID(doc_uuid),
                chunk_uuid=uuid.uuid4(),
                chunk_index=0,
                relation_type="handled_by",
                source_entity_key="事项",
                source_entity_type="procedure",
                source_entity_name="事项",
                target_entity_key="部门",
                target_entity_type="department",
                target_entity_name="部门",
                evidence_text="原文证据",
                review_status="pending",
            )
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            session.commit()
        session.rollback()


def test_batch_doc_foreign_key_enforced(migrated_engine):
    """批次引用不存在的文档必须失败，证明外键真实生效。"""
    import uuid

    import sqlalchemy.exc

    from app.models.graph import GraphBuildBatch

    with Session(migrated_engine) as session:
        session.add(
            GraphBuildBatch(
                batch_uuid=uuid.uuid4(),
                doc_uuid=uuid.uuid4(),  # 不存在的文档
                doc_version="v1",
                source_fingerprint="fp-orphan",
                chunk_count=0,
                access_scope="public",
                status="pending",
            )
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            session.commit()
        session.rollback()


def test_migrated_schema_matches_orm_metadata(migrated_engine):
    """迁移建库的列集合必须与 ORM 声明一致。

    只比对图相关表，避免因既有表的历史差异导致误报。
    """
    import app.models.graph  # noqa: F401  确保模型已注册
    from app.db.base import Base

    inspector = inspect(migrated_engine)
    orm_tables = Base.metadata.tables

    for table in ("graph_build_batches", "graph_candidates", "graph_active_batches"):
        migrated_columns = {col["name"] for col in inspector.get_columns(table)}
        orm_columns = {col.name for col in orm_tables[table].columns}
        assert migrated_columns == orm_columns, f"{table} 列集合与 ORM 不一致"


def test_active_pointer_unique_per_document(migrated_engine):
    """每文档只能有一个生效指针（唯一约束生效）。"""
    import uuid

    import sqlalchemy.exc

    from app.models.graph import GraphActiveBatch, GraphBuildBatch

    with Session(migrated_engine) as session:
        doc_uuid = uuid.UUID(_insert_document(session))
        batch_a = GraphBuildBatch(
            batch_uuid=uuid.uuid4(), doc_uuid=doc_uuid, doc_version="v1",
            source_fingerprint="fp-a", chunk_count=0,
            access_scope="public", status="searchable",
        )
        session.add(batch_a)
        session.commit()

        session.add(GraphActiveBatch(doc_uuid=doc_uuid, batch_uuid=batch_a.batch_uuid,
                                     source_fingerprint="fp-a"))
        session.commit()

        batch_b = GraphBuildBatch(
            batch_uuid=uuid.uuid4(), doc_uuid=doc_uuid, doc_version="v1",
            source_fingerprint="fp-b", chunk_count=0,
            access_scope="public", status="searchable",
        )
        session.add(batch_b)
        session.commit()

        session.add(GraphActiveBatch(doc_uuid=doc_uuid, batch_uuid=batch_b.batch_uuid,
                                     source_fingerprint="fp-b"))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            session.commit()
        session.rollback()


def test_batch_snapshot_unique_per_document(migrated_engine):
    """同一文档同一来源快照只允许一个批次（幂等约束生效）。"""
    import uuid

    import sqlalchemy.exc

    from app.models.graph import GraphBuildBatch

    with Session(migrated_engine) as session:
        doc_uuid = uuid.UUID(_insert_document(session))
        session.add(
            GraphBuildBatch(
                batch_uuid=uuid.uuid4(), doc_uuid=doc_uuid, doc_version="v1",
                source_fingerprint="fp-same", chunk_count=1,
                access_scope="public", status="pending",
            )
        )
        session.commit()

        session.add(
            GraphBuildBatch(
                batch_uuid=uuid.uuid4(), doc_uuid=doc_uuid, doc_version="v1",
                source_fingerprint="fp-same", chunk_count=1,
                access_scope="public", status="pending",
            )
        )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            session.commit()
        session.rollback()


def test_qa_conversation_owner_nullable_for_legacy_rows(migrated_engine):
    """历史会话 owner 必须可空，否则迁移无法兼容既有数据。

    注意：既有迁移 20260614_0012 的 qa_conversations.id 使用裸 sa.BigInteger，
    在 SQLite 下不自增（SQLAlchemy 要求 INTEGER PRIMARY KEY 才 ROWID 自增）。
    该缺陷属既有迁移范围，本轮不修，因此此处显式提供 id 值，
    只验证 owner_user_id 的可空性。
    """
    import uuid as uuid_module

    from sqlalchemy import text as sa_text

    with Session(migrated_engine) as engine_session:
        conversation_id = uuid_module.uuid4()
        engine_session.execute(
            sa_text(
                "INSERT INTO qa_conversations (id, conversation_id, title) "
                "VALUES (:id, :cid, '历史会话')"
            ),
            {"id": 987654, "cid": str(conversation_id)},
        )
        engine_session.commit()

        result = engine_session.execute(
            sa_text("SELECT owner_user_id FROM qa_conversations WHERE title = '历史会话'")
        ).first()
        assert result is not None
        assert result[0] is None
