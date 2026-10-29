"""历史上下文证据过滤测试。

对应评审结论：过滤模型输入与修改已保存内容是两件事。
本测试只验证「失权 / 失效 / 来源不明的历史知识不进入下一轮模型输入」，
并验证数据库原始记录保持不变。
"""
from __future__ import annotations

import uuid

import pytest

from app.graph.history_filter import (
    filter_history_for_model,
    load_history_for_model,
)
from app.repositories.qa_conversation import QaConversationRepository


def _create_document(session, *, access_level: str = "public", doc_uuid=None, version="v1"):
    """创建文档并返回 (doc_uuid_str, chunk_uuid_str)。

    历史引用必须指向真实存在的分块，否则按当前语义会被正确判定为已失效。
    """
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk

    identifier = doc_uuid or uuid.uuid4()
    session.add(
        Document(
            doc_uuid=identifier,
            title=f"资料 {identifier}",
            source_type="manual",
            source_module="campus-guide",
            file_name="f.pdf",
            file_ext=".pdf",
            mime_type="application/pdf",
            file_path=f"/tmp/{identifier}.pdf",
            file_size=1,
            file_hash=f"hash-{identifier}",
            version=version,
            access_level=access_level,
            status="active",
            review_status="approved",
            parse_status="success",
            index_status="success",
        )
    )
    session.flush()
    chunk = DocumentChunk(
        chunk_uuid=uuid.uuid4(),
        doc_uuid=identifier,
        chunk_index=0,
        chunk_text="原文内容",
        page_no=1,
    )
    session.add(chunk)
    session.commit()
    return str(identifier), str(chunk.chunk_uuid)


def _seed_conversation_with_answers(session, conversation_id, answers: list[dict]):
    """answers: [{doc_uuids: [...], has_citations: bool, chunk_uuids: [...] | None}]

    传入的 chunk_uuids 必须是文档下真实存在的分块，否则引用会被判定为已失效。
    """
    repo = QaConversationRepository(session)
    repo.create_conversation(conversation_id, title="会话", owner_user_id="alice")

    repo.add_message(
        message_id=uuid.uuid4(),
        conversation_id=conversation_id,
        role="user",
        content="毕业需要哪些材料",
    )
    message_ids = []
    for item in answers:
        citations = None
        if item.get("has_citations", True):
            chunk_uuids = item.get("chunk_uuids")
            citations = {
                "citations": [
                    {
                        "doc_uuid": doc_uuid,
                        "chunk_uuid": (
                            chunk_uuids[index]
                            if chunk_uuids and index < len(chunk_uuids)
                            else str(uuid.uuid4())
                        ),
                        "version": "v1",
                        "title": "标题",
                    }
                    for index, doc_uuid in enumerate(item["doc_uuids"])
                ]
            }
        message_id = uuid.uuid4()
        repo.add_message(
            message_id=message_id,
            conversation_id=conversation_id,
            role="assistant",
            content=item.get("content", "历史回答"),
            citations=citations,
        )
        message_ids.append(message_id)
    session.commit()
    return message_ids


def _history(message_ids) -> list[dict]:
    history = [{"role": "user", "content": "毕业需要哪些材料"}]
    for message_id in message_ids:
        history.append({"role": "assistant", "content": "历史回答", "message_id": str(message_id)})
    return history


# ---------------------------------------------------------------------------
# 有效来源的历史回答保留
# ---------------------------------------------------------------------------


def test_history_with_accessible_source_is_kept(db_session):
    conversation_id = uuid.uuid4()
    doc_uuid, doc_uuid_chunk = _create_document(db_session)
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [doc_uuid], "chunk_uuids": [doc_uuid_chunk]}]
    )

    result = filter_history_for_model(
        db_session,
        conversation_id,
        _history(message_ids),
        permission_checker=None,
    )
    assert result.dropped_count == 0
    assert any(item.get("message_id") == str(message_ids[0]) for item in result.kept)


def test_user_question_is_always_kept(db_session):
    """用户自己的提问不是知识证据，始终保留以维持会话连贯性。"""
    conversation_id = uuid.uuid4()
    doc_uuid, doc_uuid_chunk = _create_document(db_session)
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [doc_uuid], "chunk_uuids": [doc_uuid_chunk]}]
    )
    result = filter_history_for_model(db_session, conversation_id, _history(message_ids))
    assert result.kept[0]["role"] == "user"


# ---------------------------------------------------------------------------
# 失效来源被过滤
# ---------------------------------------------------------------------------


def test_withdrawn_source_is_dropped(db_session):
    """来源被撤回后，历史回答不再作为可用知识传给模型。"""
    conversation_id = uuid.uuid4()
    doc_uuid, doc_uuid_chunk = _create_document(db_session)
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [doc_uuid], "chunk_uuids": [doc_uuid_chunk]}]
    )

    from app.models.document import Document
    from sqlalchemy import select

    document = db_session.scalar(select(Document).where(Document.doc_uuid == uuid.UUID(doc_uuid)))
    document.review_status = "revoked"
    document.status = "inactive"
    db_session.commit()

    result = filter_history_for_model(db_session, conversation_id, _history(message_ids))
    assert result.dropped_count == 1
    assert str(message_ids[0]) in result.dropped_message_ids
    # 用户提问仍在
    assert result.kept[0]["role"] == "user"


def test_deleted_source_is_dropped(db_session):
    conversation_id = uuid.uuid4()
    doc_uuid, doc_uuid_chunk = _create_document(db_session)
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [doc_uuid], "chunk_uuids": [doc_uuid_chunk]}]
    )

    from sqlalchemy import select

    from app.models.document import Document

    document = db_session.scalar(select(Document).where(Document.doc_uuid == uuid.UUID(doc_uuid)))
    document.deleted_at = __import__("datetime").datetime.now(__import__("datetime").UTC)
    db_session.commit()

    result = filter_history_for_model(db_session, conversation_id, _history(message_ids))
    assert result.dropped_count == 1


def test_history_without_structured_citations_is_dropped(db_session):
    """来源无法确认的历史回答不当作权威知识。"""
    conversation_id = uuid.uuid4()
    _doc_uuid, _chunk_uuid = _create_document(db_session)
    message_ids = _seed_conversation_with_answers(
        db_session,
        conversation_id,
        [{"doc_uuids": [], "has_citations": False}],
    )

    result = filter_history_for_model(db_session, conversation_id, _history(message_ids))
    assert result.dropped_count == 1


def test_partially_invalid_source_drops_whole_answer(db_session):
    """引用中只要有一个来源失效，整条历史回答即不可用。"""
    conversation_id = uuid.uuid4()
    valid_doc, valid_doc_chunk = _create_document(db_session)
    invalid_doc, invalid_doc_chunk = _create_document(db_session)
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [valid_doc, invalid_doc],
          "chunk_uuids": [valid_doc_chunk, invalid_doc_chunk]}]
    )

    from sqlalchemy import select

    from app.models.document import Document

    document = db_session.scalar(select(Document).where(Document.doc_uuid == uuid.UUID(invalid_doc)))
    document.review_status = "revoked"
    db_session.commit()

    result = filter_history_for_model(db_session, conversation_id, _history(message_ids))
    assert result.dropped_count == 1


# ---------------------------------------------------------------------------
# 权限维度
# ---------------------------------------------------------------------------


def test_permission_denied_source_is_dropped(db_session):
    """当前用户无权访问的来源，历史回答不再进入模型上下文。"""
    conversation_id = uuid.uuid4()
    internal_doc, internal_doc_chunk = _create_document(db_session, access_level="private")
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [internal_doc], "chunk_uuids": [internal_doc_chunk]}]
    )

    from app.permissions.campus import CampusPermissionChecker

    checker = CampusPermissionChecker()
    # 非创建者、无权角色 → 访问被拒
    result = filter_history_for_model(
        db_session,
        conversation_id,
        _history(message_ids),
        permission_checker=checker,
        user_context={"user_id": "bob", "roles": [], "departments": [], "is_authenticated": True},
    )
    assert result.dropped_count == 1


def test_permission_granted_source_is_kept(db_session):
    """公开资料对已认证用户可访问，历史回答保留。"""
    conversation_id = uuid.uuid4()
    public_doc, public_doc_chunk = _create_document(db_session, access_level="public")
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [public_doc], "chunk_uuids": [public_doc_chunk]}]
    )

    from app.permissions.campus import CampusPermissionChecker

    result = filter_history_for_model(
        db_session,
        conversation_id,
        _history(message_ids),
        permission_checker=CampusPermissionChecker(),
        user_context={"user_id": "bob", "roles": [], "departments": [], "is_authenticated": True},
    )
    assert result.dropped_count == 0


def test_permission_checker_failure_drops_history(db_session):
    """权限检查器异常时，状态判为未知 → 不把历史证据当权威知识。

    不可降级为「全部放行」。
    """

    class BrokenChecker:
        def filter_documents(self, documents, user_context):
            raise RuntimeError("permission backend down")

    conversation_id = uuid.uuid4()
    doc_uuid, doc_uuid_chunk = _create_document(db_session)
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [doc_uuid], "chunk_uuids": [doc_uuid_chunk]}]
    )

    result = filter_history_for_model(
        db_session,
        conversation_id,
        _history(message_ids),
        permission_checker=BrokenChecker(),
        user_context={"user_id": "alice", "roles": [], "departments": []},
    )
    assert result.dropped_count == 1


# ---------------------------------------------------------------------------
# 原始记录不被修改
# ---------------------------------------------------------------------------


def test_original_history_records_unchanged(db_session):
    """过滤只作用于模型输入，数据库原始记录保持不变。"""
    from sqlalchemy import select

    from app.models.qa_conversation import QaConversationMessage

    conversation_id = uuid.uuid4()
    doc_uuid, doc_uuid_chunk = _create_document(db_session)
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [doc_uuid], "chunk_uuids": [doc_uuid_chunk]}]
    )

    from sqlalchemy import select as sa_select

    from app.models.document import Document

    document = db_session.scalar(sa_select(Document).where(Document.doc_uuid == uuid.UUID(doc_uuid)))
    document.review_status = "revoked"
    db_session.commit()

    filter_history_for_model(db_session, conversation_id, _history(message_ids))

    stored = db_session.scalar(
        select(QaConversationMessage).where(
            QaConversationMessage.message_id == message_ids[0]
        )
    )
    assert stored is not None
    assert stored.content == "历史回答"
    assert stored.citations is not None
    assert stored.citations["citations"]


def test_load_history_for_model_returns_kept_history(db_session):
    conversation_id = uuid.uuid4()
    doc_uuid, doc_uuid_chunk = _create_document(db_session)
    message_ids = _seed_conversation_with_answers(
        db_session, conversation_id,
        [{"doc_uuids": [doc_uuid], "chunk_uuids": [doc_uuid_chunk]}]
    )

    kept, result = load_history_for_model(db_session, conversation_id)
    assert result.dropped_count == 0
    assert len(kept) >= 2
    # 用户提问 + 助手回答
    assert kept[0]["role"] == "user"
    assert kept[1]["message_id"] == str(message_ids[0])


def test_load_history_respects_limit(db_session):
    conversation_id = uuid.uuid4()
    doc_uuid, doc_uuid_chunk = _create_document(db_session)
    repo = QaConversationRepository(db_session)
    repo.create_conversation(conversation_id, title="会话", owner_user_id="alice")
    for index in range(5):
        repo.add_message(
            message_id=uuid.uuid4(),
            conversation_id=conversation_id,
            role="user",
            content=f"问题{index}",
        )
    db_session.commit()

    kept, _ = load_history_for_model(db_session, conversation_id, limit=2)
    assert len(kept) == 2


# ---------------------------------------------------------------------------
# 分块与版本有效性（评审问题 5）
# ---------------------------------------------------------------------------


def _seed_with_chunks(session, *, access_level="public", version="v1"):
    from app.models.document import Document
    from app.models.document_chunk import DocumentChunk

    doc = Document(
        doc_uuid=uuid.uuid4(),
        title="政策文件",
        source_type="manual",
        source_module="campus-guide",
        file_name="p.pdf",
        file_ext=".pdf",
        mime_type="application/pdf",
        file_path=f"/tmp/{uuid.uuid4()}.pdf",
        file_size=1,
        file_hash=f"hash-{uuid.uuid4()}",
        version=version,
        access_level=access_level,
        status="active",
        review_status="approved",
        parse_status="success",
        index_status="success",
    )
    session.add(doc)
    session.flush()
    chunk = DocumentChunk(
        chunk_uuid=uuid.uuid4(),
        doc_uuid=doc.doc_uuid,
        chunk_index=0,
        chunk_text="原文内容",
        page_no=1,
    )
    session.add(chunk)
    session.commit()
    return doc, chunk


def _seed_answer_citing(session, conversation_id, *, doc_uuid, chunk_uuid, version):
    """构造一条历史回答，其 citations 记录了当时的分块与版本。"""
    repo = QaConversationRepository(session)
    repo.create_conversation(conversation_id, title="会话", owner_user_id="alice")
    repo.add_message(
        message_id=uuid.uuid4(), conversation_id=conversation_id,
        role="user", content="原问题",
    )
    message_id = uuid.uuid4()
    repo.add_message(
        message_id=message_id,
        conversation_id=conversation_id,
        role="assistant",
        content="历史回答",
        citations={
            "citations": [
                {
                    "doc_uuid": str(doc_uuid),
                    "chunk_uuid": str(chunk_uuid),
                    "version": version,
                    "title": "政策文件",
                }
            ]
        },
    )
    session.commit()
    return message_id


def _history_for(message_id):
    return [
        {"role": "user", "content": "原问题"},
        {"role": "assistant", "content": "历史回答", "message_id": str(message_id)},
    ]


def test_history_kept_when_chunk_and_version_match(db_session):
    """分块仍存在且版本一致 → 历史回答保留。"""
    doc, chunk = _seed_with_chunks(db_session, version="v1")
    cid = uuid.uuid4()
    mid = _seed_answer_citing(
        db_session, cid, doc_uuid=doc.doc_uuid, chunk_uuid=chunk.chunk_uuid, version="v1"
    )
    result = filter_history_for_model(db_session, cid, _history_for(mid))
    assert result.dropped_count == 0


def test_history_dropped_when_document_version_changed(db_session):
    """文档内容改版（version 变化）→ 旧回答的依据不再对应当前原文。

    文档仍处于 active/approved，仅版本号变化——这正是只按 doc_uuid 判断会漏掉的情况。
    """
    doc, chunk = _seed_with_chunks(db_session, version="v1")
    cid = uuid.uuid4()
    mid = _seed_answer_citing(
        db_session, cid, doc_uuid=doc.doc_uuid, chunk_uuid=chunk.chunk_uuid, version="v1"
    )

    # 政策改版，但文档仍可检索
    doc.version = "v2"
    db_session.commit()

    result = filter_history_for_model(db_session, cid, _history_for(mid))
    assert result.dropped_count == 1
    assert result.kept[0]["role"] == "user"


def test_history_dropped_when_chunk_deleted(db_session):
    """引用分块已被删除（重建后旧 chunk 不再存在）→ 丢弃。"""
    doc, chunk = _seed_with_chunks(db_session, version="v1")
    cid = uuid.uuid4()
    mid = _seed_answer_citing(
        db_session, cid, doc_uuid=doc.doc_uuid, chunk_uuid=chunk.chunk_uuid, version="v1"
    )

    from datetime import UTC, datetime

    chunk.deleted_at = datetime.now(UTC)
    db_session.commit()

    result = filter_history_for_model(db_session, cid, _history_for(mid))
    assert result.dropped_count == 1


def test_history_dropped_when_chunk_rebuilt(db_session):
    """分块重建后 chunk_uuid 变化，旧引用失效。"""
    doc, _chunk = _seed_with_chunks(db_session, version="v1")
    cid = uuid.uuid4()
    old_chunk_uuid = uuid.uuid4()
    mid = _seed_answer_citing(
        db_session, cid, doc_uuid=doc.doc_uuid, chunk_uuid=old_chunk_uuid, version="v1"
    )
    result = filter_history_for_model(db_session, cid, _history_for(mid))
    assert result.dropped_count == 1


def test_history_kept_when_citation_has_no_chunk_uuid(db_session):
    """引用未记录分块（仅文档级）时，只按文档可访问性判断。"""
    doc, _chunk = _seed_with_chunks(db_session, version="v2")
    cid = uuid.uuid4()
    repo = QaConversationRepository(db_session)
    repo.create_conversation(cid, title="会话", owner_user_id="alice")
    repo.add_message(message_id=uuid.uuid4(), conversation_id=cid,
                     role="user", content="原问题")
    mid = uuid.uuid4()
    repo.add_message(
        message_id=mid, conversation_id=cid, role="assistant", content="历史回答",
        citations={"citations": [{"doc_uuid": str(doc.doc_uuid), "version": "v2"}]},
    )
    db_session.commit()

    result = filter_history_for_model(db_session, cid, _history_for(mid))
    assert result.dropped_count == 0


def test_permission_payload_includes_owner_role(db_session):
    """历史过滤构造的权限字段必须包含 owner_role，与检索层语义一致。"""
    from app.graph.history_filter import _accessible_doc_map
    from app.services.permission_fields import document_to_permission_dict

    doc = Document_owner_role_case()
    payload = document_to_permission_dict(doc)
    for field in ("doc_uuid", "access_level", "owner_dept", "owner_role", "created_by"):
        assert field in payload, f"权限字段缺少 {field}"

    assert _accessible_doc_map is not None


def Document_owner_role_case():
    from app.models.document import Document

    return Document(
        doc_uuid=uuid.uuid4(),
        title="角色级资料",
        source_type="manual",
        source_module="campus",
        file_name="r.pdf",
        file_ext=".pdf",
        mime_type="application/pdf",
        file_path="/tmp/r.pdf",
        file_size=1,
        file_hash="hash-role",
        version="v1",
        access_level="role",
        owner_role="campus_admin",
        created_by="admin",
        status="active",
        review_status="approved",
        parse_status="success",
        index_status="success",
    )


def test_role_scoped_document_requires_matching_role(db_session):
    """角色级资料的历史证据：角色不匹配时不得进入模型上下文。"""
    from app.permissions.campus import CampusPermissionChecker

    doc = Document_owner_role_case()
    db_session.add(doc)
    db_session.commit()

    cid = uuid.uuid4()
    repo = QaConversationRepository(db_session)
    repo.create_conversation(cid, title="会话", owner_user_id="alice")
    repo.add_message(message_id=uuid.uuid4(), conversation_id=cid, role="user", content="问题")
    mid = uuid.uuid4()
    repo.add_message(
        message_id=mid, conversation_id=cid, role="assistant", content="历史回答",
        citations={"citations": [{"doc_uuid": str(doc.doc_uuid)}]},
    )
    db_session.commit()

    # 角色不匹配 → 丢弃
    result = filter_history_for_model(
        db_session, cid, _history_for(mid),
        permission_checker=CampusPermissionChecker(),
        user_context={"user_id": "alice", "roles": ["student"], "departments": [],
                      "is_authenticated": True},
    )
    assert result.dropped_count == 1

    # 角色匹配 → 保留（证明 owner_role 确实参与了判断）
    result_ok = filter_history_for_model(
        db_session, cid, _history_for(mid),
        permission_checker=CampusPermissionChecker(),
        user_context={"user_id": "admin", "roles": ["campus_admin"], "departments": [],
                      "is_authenticated": True},
    )
    assert result_ok.dropped_count == 0
