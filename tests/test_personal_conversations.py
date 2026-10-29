"""个人会话入口的身份传递与归属隔离测试。

使用可信依赖替身提供身份，不伪造生产信任来源：
通过 FastAPI 依赖覆盖注入 UserContext，而不是伪造签名或信任头。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import deps
from app.main import create_app
from app.models.qa_conversation import QaConversation, QaConversationMessage
from app.repositories.qa_conversation import QaConversationRepository
from app.schemas.retrieval import UserContext


def _client_for(user: UserContext | None) -> TestClient:
    app = create_app()

    def override():
        return user

    app.dependency_overrides[deps.get_trusted_user_context] = override
    return TestClient(app)


def _seed_owned_conversation(session: Session, owner: str) -> str:
    conversation_id = uuid.uuid4()
    QaConversationRepository(session).create_conversation(
        conversation_id, title=f"{owner} 的会话", owner_user_id=owner
    )
    session.commit()
    return str(conversation_id)


def _seed_orphan_conversation(session: Session) -> str:
    """无归属历史会话：迁移不自动分配 owner。"""
    conversation_id = uuid.uuid4()
    session.add(
        QaConversation(
            conversation_id=conversation_id,
            title="历史会话",
            owner_user_id=None,
        )
    )
    session.add(
        QaConversationMessage(
            message_id=uuid.uuid4(),
            conversation_id=conversation_id,
            role="user",
            content="历史提问",
        )
    )
    session.commit()
    return str(conversation_id)


ALICE = UserContext(user_id="alice", roles=["student"], departments=["cs"], is_authenticated=True)
BOB = UserContext(user_id="bob", roles=["student"], departments=["math"], is_authenticated=True)


# ---------------------------------------------------------------------------
# 未认证
# ---------------------------------------------------------------------------


def test_anonymous_cannot_create_conversation():
    client = _client_for(None)
    resp = client.post("/api/v1/qa/conversations", json={"title": "x"})
    assert resp.status_code == 401


def test_anonymous_cannot_list_conversations():
    client = _client_for(None)
    assert client.get("/api/v1/qa/conversations").status_code == 401


def test_anonymous_cannot_send_message(db_session):
    conversation_id = _seed_owned_conversation(db_session, "alice")
    client = _client_for(None)
    resp = client.post(
        f"/api/v1/qa/conversations/{conversation_id}/messages",
        json={"question": "毕业需要哪些材料"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 归属隔离
# ---------------------------------------------------------------------------


def test_list_only_returns_own_conversations(db_session):
    alice_conv = _seed_owned_conversation(db_session, "alice")
    _seed_owned_conversation(db_session, "bob")

    client = _client_for(ALICE)
    data = client.get("/api/v1/qa/conversations").json()["data"]
    ids = {item["conversation_id"] for item in data["conversations"]}
    assert alice_conv in ids
    assert len(ids) == 1


def test_user_b_cannot_list_user_a_conversation(db_session):
    alice_conv = _seed_owned_conversation(db_session, "alice")
    client = _client_for(BOB)
    data = client.get("/api/v1/qa/conversations").json()["data"]
    assert alice_conv not in {item["conversation_id"] for item in data["conversations"]}


def test_user_b_cannot_read_user_a_conversation(db_session):
    alice_conv = _seed_owned_conversation(db_session, "alice")
    client = _client_for(BOB)
    assert client.get(f"/api/v1/qa/conversations/{alice_conv}").status_code == 404


def test_user_b_cannot_delete_user_a_conversation(db_session):
    from sqlalchemy import select

    alice_conv = _seed_owned_conversation(db_session, "alice")
    client = _client_for(BOB)
    assert client.delete(f"/api/v1/qa/conversations/{alice_conv}").status_code == 404

    # 会话仍然存在，未被越权删除
    still_there = db_session.scalar(
        select(QaConversation).where(
            QaConversation.conversation_id == uuid.UUID(alice_conv)
        )
    )
    assert still_there is not None
    assert still_there.owner_user_id == "alice"


def test_user_b_cannot_send_message_to_user_a_conversation(db_session):
    alice_conv = _seed_owned_conversation(db_session, "alice")
    client = _client_for(BOB)
    resp = client.post(
        f"/api/v1/qa/conversations/{alice_conv}/messages",
        json={"question": "毕业需要哪些材料"},
    )
    assert resp.status_code == 404


def test_owner_can_read_own_conversation(db_session):
    alice_conv = _seed_owned_conversation(db_session, "alice")
    client = _client_for(ALICE)
    resp = client.get(f"/api/v1/qa/conversations/{alice_conv}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 历史无主会话
# ---------------------------------------------------------------------------


def test_orphan_conversation_not_listed_for_logged_user(db_session):
    orphan = _seed_orphan_conversation(db_session)
    client = _client_for(ALICE)
    data = client.get("/api/v1/qa/conversations").json()["data"]
    assert orphan not in {item["conversation_id"] for item in data["conversations"]}


def test_orphan_conversation_not_readable(db_session):
    orphan = _seed_orphan_conversation(db_session)
    client = _client_for(ALICE)
    assert client.get(f"/api/v1/qa/conversations/{orphan}").status_code == 404


def test_orphan_conversation_not_writable(db_session):
    orphan = _seed_orphan_conversation(db_session)
    client = _client_for(ALICE)
    resp = client.post(
        f"/api/v1/qa/conversations/{orphan}/messages",
        json={"question": "继续"},
    )
    assert resp.status_code == 404


def test_orphan_conversation_not_deletable_by_user(db_session):
    orphan = _seed_orphan_conversation(db_session)
    client = _client_for(ALICE)
    assert client.delete(f"/api/v1/qa/conversations/{orphan}").status_code == 404


# ---------------------------------------------------------------------------
# 请求体伪造无效
# ---------------------------------------------------------------------------


def test_forged_owner_in_body_is_ignored(db_session):
    client = _client_for(ALICE)
    resp = client.post(
        "/api/v1/qa/conversations",
        json={"title": "forged", "owner_user_id": "bob", "user_id": "bob"},
    )
    assert resp.status_code == 200
    conversation_id = resp.json()["data"]["conversation_id"]

    from sqlalchemy import select

    stored = db_session.scalar(
        select(QaConversation).where(
            QaConversation.conversation_id == uuid.UUID(conversation_id)
        )
    )
    # 所有者来自可信身份，不是请求体
    assert stored.owner_user_id == "alice"


def test_creating_with_first_message_records_owner(db_session):
    client = _client_for(ALICE)
    resp = client.post(
        "/api/v1/qa/conversations",
        json={"first_message": "毕业需要哪些材料"},
    )
    assert resp.status_code == 200
    conversation_id = resp.json()["data"]["conversation_id"]

    from sqlalchemy import select

    stored = db_session.scalar(
        select(QaConversation).where(
            QaConversation.conversation_id == uuid.UUID(conversation_id)
        )
    )
    assert stored.owner_user_id == "alice"


# ---------------------------------------------------------------------------
# 身份透传
# ---------------------------------------------------------------------------


def test_conversation_answer_receives_real_user_context(monkeypatch, db_session):
    """conversation_answer 必须收到真实 user_context，而不是 trusted=True + None。"""
    from app.services.qa import QaService

    captured: dict = {}

    def fake_search(self, request, **kwargs):
        captured["user_context"] = request.user_context
        captured["trusted"] = kwargs.get("trusted_user_context")
        captured["required"] = kwargs.get("authenticated_identity_required")

        from app.schemas.retrieval import SearchResponseData

        return SearchResponseData(
            query=request.query,
            rewritten_query=request.query,
            filters_applied={},
            hits=[],
            latency_ms=1,
        )

    monkeypatch.setattr("app.services.qa.RetrievalService.search", fake_search, raising=True)

    QaService(db_session).conversation_answer(
        question="毕业需要哪些材料",
        user_context=ALICE,
        trusted_user_context=True,
        authenticated_identity_required=True,
    )

    assert captured["user_context"] is not None
    assert captured["user_context"].user_id == "alice"
    assert captured["trusted"] is True
    assert captured["required"] is True


def test_answer_endpoint_unchanged_without_user_context(monkeypatch, db_session):
    """不传 user_context 时保持既有行为，不影响其他调用方。"""
    from app.services.qa import QaService

    captured: dict = {}

    def fake_search(self, request, **kwargs):
        captured["user_context"] = request.user_context
        from app.schemas.retrieval import SearchResponseData

        return SearchResponseData(
            query=request.query,
            rewritten_query=request.query,
            filters_applied={},
            hits=[],
            latency_ms=1,
        )

    monkeypatch.setattr("app.services.qa.RetrievalService.search", fake_search, raising=True)
    QaService(db_session).conversation_answer(question="普通问题")
    assert captured["user_context"] is None


# ---------------------------------------------------------------------------
# Repository 层
# ---------------------------------------------------------------------------


def test_repository_owner_filter(db_session):
    alice_conv = _seed_owned_conversation(db_session, "alice")
    _seed_owned_conversation(db_session, "bob")
    repo = QaConversationRepository(db_session)

    owned = repo.get_owned_conversation(uuid.UUID(alice_conv), "alice")
    assert owned is not None
    assert repo.get_owned_conversation(uuid.UUID(alice_conv), "bob") is None


def test_repository_unfiltered_list_includes_all(db_session):
    _seed_owned_conversation(db_session, "alice")
    _seed_owned_conversation(db_session, "bob")
    repo = QaConversationRepository(db_session)
    assert len(repo.list_conversations()) == 2
    assert len(repo.list_conversations(owner_user_id="alice")) == 1


def test_created_conversation_records_owner(db_session):
    repo = QaConversationRepository(db_session)
    conversation_id = uuid.uuid4()
    repo.create_conversation(conversation_id, owner_user_id="carol")
    db_session.commit()
    assert repo.get_conversation(conversation_id).owner_user_id == "carol"
