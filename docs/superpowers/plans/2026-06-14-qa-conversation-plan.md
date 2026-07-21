# 多轮问答功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为知识库系统新增多轮对话问答功能，支持创建对话、连续提问、历史上下文持久化。

**Architecture:** 后端新增 conversations/conversation_messages 两张表，复用现有 QaService + LlmProvider 检索生成链路，LLM 调用时传入历史消息。前端新增对话列表页和聊天详情页，通过 API 调后端接口。

**Tech Stack:** FastAPI + SQLAlchemy + PostgreSQL + React + TypeScript

---

### Task 1: 数据库迁移

**Files:**
- Create: `migrations/versions/20260614_0011_qa_conversations.py`

- [ ] **Step 1: 创建迁移文件**

新建 `migrations/versions/20260614_0011_qa_conversations.py`：

```python
"""qa conversations

Revision ID: 20260614_0011
Revises: 20260526_0010
Create Date: 2026-06-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260614_0011"
down_revision: Union[str, None] = "20260526_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qa_conversations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(255), server_default="新对话", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_qa_conversations_id", "qa_conversations", ["conversation_id"], unique=True)
    op.create_index("idx_qa_conversations_updated", "qa_conversations", ["updated_at"])

    op.create_table(
        "qa_conversation_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_qa_messages_id", "qa_conversation_messages", ["message_id"], unique=True)
    op.create_index("idx_qa_messages_conversation", "qa_conversation_messages", ["conversation_id"])
    op.create_index("idx_qa_messages_created", "qa_conversation_messages", ["conversation_id", "created_at"])


def downgrade() -> None:
    op.drop_table("qa_conversation_messages")
    op.drop_table("qa_conversations")
```

- [ ] **Step 2: 运行迁移**

```bash
cd e:\xm\rag-knowledge-system-main && python -m alembic upgrade head
```

预期输出：`INFO [alembic.runtime.migration] Running upgrade 20260526_0010 -> 20260614_0011, qa conversations`

- [ ] **Step 3: 提交**

```bash
git add migrations/versions/20260614_0011_qa_conversations.py
git commit -m "feat: add qa_conversations and qa_conversation_messages tables"
```

---

### Task 2: 后端数据模型

**Files:**
- Create: `app/models/qa_conversation.py`
- Modify: `app/models/__init__.py`

- [ ] **Step 1: 创建 ORM 模型**

新建 `app/models/qa_conversation.py`：

```python
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonType, PrimaryKeyBigInt, UuidType


class QaConversation(Base):
    __tablename__ = "qa_conversations"
    __table_args__ = (
        Index("uq_qa_conversations_id", "conversation_id", unique=True),
        Index("idx_qa_conversations_updated", "updated_at"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UuidType, default=uuid.uuid4, nullable=False)
    title: Mapped[str] = mapped_column(String(255), server_default="新对话", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class QaConversationMessage(Base):
    __tablename__ = "qa_conversation_messages"
    __table_args__ = (
        Index("uq_qa_messages_id", "message_id", unique=True),
        Index("idx_qa_messages_conversation", "conversation_id"),
        Index("idx_qa_messages_created", "conversation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyBigInt, primary_key=True, autoincrement=True)
    message_id: Mapped[uuid.UUID] = mapped_column(UuidType, default=uuid.uuid4, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UuidType, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    citations: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JsonType, server_default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
```

- [ ] **Step 2: 注册模型到 __init__.py**

编辑 `app/models/__init__.py`，在两处追加新模型：

在现有 import 块末尾添加：
```python
from app.models.qa_conversation import QaConversation, QaConversationMessage
```

在 `__all__` 列表末尾添加：
```python
    "QaConversation",
    "QaConversationMessage",
```

- [ ] **Step 3: 验证模型可导入**

```bash
cd e:\xm\rag-knowledge-system-main && python -c "from app.models.qa_conversation import QaConversation, QaConversationMessage; print('OK')"
```

- [ ] **Step 4: 提交**

```bash
git add app/models/qa_conversation.py app/models/__init__.py
git commit -m "feat: add QaConversation and QaConversationMessage ORM models"
```

---

### Task 3: Pydantic Schema

**Files:**
- Create: `app/schemas/qa_conversation.py`

- [ ] **Step 1: 创建 Schema 文件**

新建 `app/schemas/qa_conversation.py`：

```python
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.schemas.common import AppBaseModel
from app.schemas.qa import GenerationOptions
from app.schemas.retrieval import Citation


class MessageResponse(AppBaseModel):
    message_id: UUID
    role: str
    content: str
    citations: list[Citation] | None = None
    metadata: dict | None = None
    created_at: datetime


class ConversationResponse(AppBaseModel):
    conversation_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse] = []


class ConversationListItem(AppBaseModel):
    conversation_id: UUID
    title: str
    last_message: str | None = None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(AppBaseModel):
    conversations: list[ConversationListItem]
    total: int


class CreateConversationRequest(AppBaseModel):
    title: str | None = None
    first_message: str | None = None


class SendMessageRequest(AppBaseModel):
    question: str
    top_k: int = 8
    generation_options: GenerationOptions | None = None


class SendMessageResponse(AppBaseModel):
    message_id: UUID
    conversation_id: UUID
    answer: str
    answer_status: str = "grounded"
    citations: list[Citation]
    matched_documents: list[dict]
    latency_ms: dict
```

- [ ] **Step 2: 验证 schema 可导入**

```bash
cd e:\xm\rag-knowledge-system-main && python -c "from app.schemas.qa_conversation import ConversationResponse, SendMessageResponse; print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add app/schemas/qa_conversation.py
git commit -m "feat: add Pydantic schemas for qa conversations"
```

---

### Task 4: Repository 数据访问层

**Files:**
- Create: `app/repositories/qa_conversation.py`

- [ ] **Step 1: 创建 Repository**

新建 `app/repositories/qa_conversation.py`：

```python
from __future__ import annotations

from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models.qa_conversation import QaConversation, QaConversationMessage


class QaConversationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_conversation(self, conversation_id: UUID, title: str = "新对话") -> QaConversation:
        conv = QaConversation(conversation_id=conversation_id, title=title)
        self.session.add(conv)
        self.session.flush()
        return conv

    def get_conversation(self, conversation_id: UUID) -> QaConversation | None:
        stmt = select(QaConversation).where(QaConversation.conversation_id == conversation_id)
        return self.session.scalar(stmt)

    def list_conversations(self) -> list[QaConversation]:
        stmt = (
            select(QaConversation)
            .order_by(desc(QaConversation.updated_at))
        )
        return list(self.session.scalars(stmt).all())

    def delete_conversation(self, conversation_id: UUID) -> bool:
        conv = self.get_conversation(conversation_id)
        if conv is None:
            return False
        # 先删消息再删对话
        msg_stmt = select(QaConversationMessage).where(
            QaConversationMessage.conversation_id == conversation_id
        )
        messages = list(self.session.scalars(msg_stmt).all())
        for msg in messages:
            self.session.delete(msg)
        self.session.delete(conv)
        self.session.flush()
        return True

    def update_title(self, conversation_id: UUID, title: str) -> QaConversation | None:
        conv = self.get_conversation(conversation_id)
        if conv is None:
            return None
        conv.title = title
        self.session.flush()
        return conv

    def add_message(
        self,
        message_id: UUID,
        conversation_id: UUID,
        role: str,
        content: str,
        citations: dict | None = None,
        metadata: dict | None = None,
    ) -> QaConversationMessage:
        msg = QaConversationMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            citations=citations,
            metadata_json=metadata or {},
        )
        self.session.add(msg)
        self.session.flush()
        # 更新对话的 updated_at
        conv = self.get_conversation(conversation_id)
        if conv is not None:
            conv.updated_at = func.now()
        return msg

    def get_messages(self, conversation_id: UUID) -> list[QaConversationMessage]:
        stmt = (
            select(QaConversationMessage)
            .where(QaConversationMessage.conversation_id == conversation_id)
            .order_by(QaConversationMessage.created_at)
        )
        return list(self.session.scalars(stmt).all())

    def get_message_count(self, conversation_id: UUID) -> int:
        stmt = select(func.count()).where(
            QaConversationMessage.conversation_id == conversation_id
        )
        return self.session.scalar(stmt) or 0
```

- [ ] **Step 2: 验证 repository**

```bash
cd e:\xm\rag-knowledge-system-main && python -c "from app.repositories.qa_conversation import QaConversationRepository; print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add app/repositories/qa_conversation.py
git commit -m "feat: add QaConversationRepository"
```

---

### Task 5: 修改 LlmProvider 支持历史上下文

**Files:**
- Modify: `app/integrations/llm.py`

- [ ] **Step 1: 修改 generate_answer 方法**

在 `LlmProvider` 类的 `generate_answer` 方法中，将 `messages` 参数改为支持历史上下文。修改 `generate_answer` 方法的签名和内部 payload 构建：

找到 `app/integrations/llm.py` 第 81-135 行的 `generate_answer` 方法，修改为：

```python
def generate_answer(
    self,
    *,
    question: str,
    citations: list[Citation],
    generation_options: GenerationOptions | None,
    history: list[dict] | None = None,
) -> LlmResult:
    if not self.is_configured():
        raise AppError(
            code=ErrorCode.LLM_GENERATION_FAILED,
            message="llm provider is not configured",
            status_code=422,
        )

    system_prompt = (
        "你是企业知识库问答助手。只能基于给定引用回答，"
        "不要编造制度内容；如果证据不足，要明确说明未找到足够依据；"
        "并且优先使用用户提问所用的语言回答。"
    )
    citation_blocks = "\n\n".join(
        f"[{index}] 标题: {citation.title}\n"
        f"模块: {citation.source_module}\n"
        f"位置: {citation.section_title or citation.sheet_name or citation.page_no or 'chunk'}\n"
        f"内容: {citation.snippet}"
        for index, citation in enumerate(citations, start=1)
    )
    user_prompt = f"问题：{question}\n\n引用片段：\n{citation_blocks}\n\n请基于引用作答。"

    # 构建 messages 列表：system + history + current user
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_prompt})

    payload = {
        "model": self.settings.llm_model,
        "messages": messages,
        "temperature": generation_options.temperature if generation_options else self.settings.llm_temperature,
        "max_tokens": generation_options.max_tokens if generation_options else self.settings.llm_max_tokens,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {self.settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    request_url = build_openai_compatible_url(self.settings.llm_api_base, "/chat/completions")
    started = perf_counter()
    data = post_json_with_retries(
        settings=self.settings,
        url=request_url,
        headers=headers,
        payload=payload,
        timeout=max(60, self.settings.provider_timeout_seconds),
        error_factory=lambda exc: AppError(
            code=ErrorCode.LLM_GENERATION_FAILED,
            message=f"llm provider request failed: {exc}",
            status_code=422,
        ),
    )

    choices = data.get("choices", [])
    if not choices:
        raise AppError(
            code=ErrorCode.LLM_GENERATION_FAILED,
            message="llm provider returned no choices",
            status_code=422,
        )
    message = choices[0].get("message", {})
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise AppError(
            code=ErrorCode.LLM_GENERATION_FAILED,
            message="llm provider returned empty content",
            status_code=422,
        )
    usage = data.get("usage", {})
    latency_ms = int((perf_counter() - started) * 1000)
    return LlmResult(
        answer=content.strip(),
        provider_name=self.settings.llm_api_base,
        model_name=data.get("model") or self.settings.llm_model,
        request_tokens=usage.get("prompt_tokens", len(user_prompt.split())),
        response_tokens=usage.get("completion_tokens"),
        latency_ms=latency_ms,
    )
```

关键改动：新增 `history` 参数，在构建 `messages` 时插入历史消息。

- [ ] **Step 2: 修改 generate_answer_with_fallback 签名**

找到 `generate_answer_with_fallback` 方法，添加 `history` 参数并透传：

```python
def generate_answer_with_fallback(
    self,
    *,
    question: str,
    citations: list[Citation],
    generation_options: GenerationOptions | None,
    fallback_builder,
    history: list[dict] | None = None,
) -> LlmResult | None:
    if not self.is_configured():
        return None
    try:
        return self.generate_answer(
            question=question,
            citations=citations,
            generation_options=generation_options,
            history=history,
        )
    except AppError:
        if not self.settings.allow_provider_fallbacks:
            raise
        logger.warning("LLM provider failed; falling back to local grounded answer builder")
        return LlmResult(
            answer=fallback_builder(citations),
            provider_name="local-fallback",
            model_name="local-grounded-answer",
            request_tokens=0,
            response_tokens=None,
            latency_ms=0,
        )
```

- [ ] **Step 3: 验证**

```bash
cd e:\xm\rag-knowledge-system-main && python -c "from app.integrations.llm import LlmProvider; print('OK')"
```

- [ ] **Step 4: 提交**

```bash
git add app/integrations/llm.py
git commit -m "feat: add history parameter to LlmProvider for multi-turn conversations"
```

---

### Task 6: 修改 QaService 支持对话问答

**Files:**
- Modify: `app/services/qa.py`

- [ ] **Step 1: 添加 conversation_answer 方法**

在 `QaService` 类末尾新增方法：

```python
def conversation_answer(
    self,
    question: str,
    history: list[dict] | None = None,
    top_k: int = 8,
    generation_options=None,
    trusted_user_context: bool = False,
    authenticated_identity_required: bool = False,
) -> AnswerResponseData:
    started = time.perf_counter()
    retrieval_started = time.perf_counter()
    retrieval_result = self.retrieval.search(
        SearchRequest(
            query=question,
            top_k=top_k,
            filters=None,
            user_context=None,
        ),
        trusted_user_context=trusted_user_context,
        authenticated_identity_required=authenticated_identity_required,
    )
    retrieval_latency = int((time.perf_counter() - retrieval_started) * 1000)

    generation_started = time.perf_counter()
    answer_status = "grounded"
    if retrieval_result.hits and self._has_sufficient_evidence(retrieval_result.hits):
        if self.llm_provider.is_configured():
            llm_result = self.llm_provider.generate_answer_with_fallback(
                question=question,
                citations=retrieval_result.hits,
                generation_options=generation_options,
                fallback_builder=self._build_grounded_answer,
                history=history,
            )
            if llm_result is not None:
                answer = llm_result.answer
                self._record_llm_log(llm_result)
            else:
                answer = self._build_grounded_answer(retrieval_result.hits)
        else:
            answer = self._build_grounded_answer(retrieval_result.hits)
    else:
        answer_status = "insufficient_evidence"
        answer = "未找到足够依据，请缩小范围或补充筛选条件后重试。"
    generation_latency = int((time.perf_counter() - generation_started) * 1000)
    total_latency = int((time.perf_counter() - started) * 1000)

    matched_documents = []
    seen_doc_ids = set()
    for citation in retrieval_result.hits:
        if citation.doc_uuid in seen_doc_ids:
            continue
        seen_doc_ids.add(citation.doc_uuid)
        matched_documents.append(
            MatchedDocument(
                doc_uuid=citation.doc_uuid,
                title=citation.title,
                score=citation.score,
            )
        )

    return AnswerResponseData(
        answer=answer,
        answer_status=answer_status,
        citations=retrieval_result.hits,
        matched_documents=matched_documents,
        filters_applied=retrieval_result.filters_applied,
        latency_ms=LatencyBreakdown(
            retrieval=retrieval_latency,
            generation=generation_latency,
            total=total_latency,
        ),
    )
```

- [ ] **Step 2: 验证**

```bash
cd e:\xm\rag-knowledge-system-main && python -c "from app.services.qa import QaService; print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add app/services/qa.py
git commit -m "feat: add conversation_answer method to QaService with history support"
```

---

### Task 7: API 路由

**Files:**
- Create: `app/api/qa_conversation.py`
- Modify: `app/api/router.py`

- [ ] **Step 1: 创建 API 路由文件**

新建 `app/api/qa_conversation.py`：

```python
from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_trace_id
from app.core.config import get_settings
from app.core.responses import success_response
from app.repositories.qa_conversation import QaConversationRepository
from app.schemas.qa_conversation import (
    ConversationListItem,
    ConversationListResponse,
    ConversationResponse,
    CreateConversationRequest,
    MessageResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from app.services.qa import QaService


router = APIRouter(prefix="/qa/conversations", tags=["qa-conversations"])


@router.post("", response_model=dict)
def create_conversation(
    request: CreateConversationRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    repo = QaConversationRepository(session)
    conversation_id = uuid4()
    title = request.title or (
        request.first_message[:50] + ("..." if len(request.first_message or "") > 50 else "")
        if request.first_message
        else "新对话"
    )
    repo.create_conversation(conversation_id, title=title)

    data = {"conversation_id": str(conversation_id), "title": title}
    if request.first_message:
        settings = get_settings()
        qa_service = QaService(session)
        result = qa_service.conversation_answer(
            question=request.first_message,
            history=None,
            top_k=8,
            generation_options=None,
        )
        # 保存消息
        repo.add_message(
            message_id=uuid4(),
            conversation_id=conversation_id,
            role="user",
            content=request.first_message,
        )
        reply_id = uuid4()
        repo.add_message(
            message_id=reply_id,
            conversation_id=conversation_id,
            role="assistant",
            content=result.answer,
            citations={"citations": [c.model_dump(mode="json") for c in result.citations]},
            metadata={"latency_ms": result.latency_ms.model_dump(mode="json")},
        )
        data["answer"] = result.answer
        data["citations"] = [c.model_dump(mode="json") for c in result.citations]

    session.commit()
    return success_response(data, trace_id)


@router.get("", response_model=dict)
def list_conversations(
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    repo = QaConversationRepository(session)
    conversations = repo.list_conversations()
    items: list[ConversationListItem] = []
    for conv in conversations:
        msg_count = repo.get_message_count(conv.conversation_id)
        messages = repo.get_messages(conv.conversation_id)
        last_msg = messages[-1].content[:100] if messages else None
        items.append(
            ConversationListItem(
                conversation_id=conv.conversation_id,
                title=conv.title,
                last_message=last_msg,
                message_count=msg_count,
                created_at=conv.created_at,
                updated_at=conv.updated_at,
            )
        )
    return success_response(
        ConversationListResponse(conversations=items, total=len(items)).model_dump(mode="json"),
        trace_id,
    )


@router.get("/{conversation_id}", response_model=dict)
def get_conversation(
    conversation_id: UUID,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    repo = QaConversationRepository(session)
    conv = repo.get_conversation(conversation_id)
    if conv is None:
        from app.core.errors import AppError, ErrorCode
        raise AppError(
            code=ErrorCode.DOCUMENT_NOT_FOUND,
            message="conversation not found",
            status_code=404,
        )
    db_messages = repo.get_messages(conversation_id)
    messages = [
        MessageResponse(
            message_id=m.message_id,
            role=m.role,
            content=m.content,
            citations=m.citations.get("citations", []) if m.citations else None,
            metadata=m.metadata_json,
            created_at=m.created_at,
        )
        for m in db_messages
    ]
    return success_response(
        ConversationResponse(
            conversation_id=conv.conversation_id,
            title=conv.title,
            created_at=conv.created_at,
            updated_at=conv.updated_at,
            messages=messages,
        ).model_dump(mode="json"),
        trace_id,
    )


@router.delete("/{conversation_id}", response_model=dict)
def delete_conversation(
    conversation_id: UUID,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    repo = QaConversationRepository(session)
    deleted = repo.delete_conversation(conversation_id)
    session.commit()
    return success_response({"conversation_id": str(conversation_id), "deleted": deleted}, trace_id)


@router.post("/{conversation_id}/messages", response_model=dict)
def send_message(
    conversation_id: UUID,
    request: SendMessageRequest,
    trace_id: str = Depends(get_trace_id),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    repo = QaConversationRepository(session)
    conv = repo.get_conversation(conversation_id)
    if conv is None:
        from app.core.errors import AppError, ErrorCode
        raise AppError(
            code=ErrorCode.DOCUMENT_NOT_FOUND,
            message="conversation not found",
            status_code=404,
        )

    # 构建历史消息（取最近 10 轮即 20 条）
    db_messages = repo.get_messages(conversation_id)
    history = [
        {"role": m.role, "content": m.content}
        for m in db_messages[-20:]
    ]

    qa_service = QaService(session)
    result = qa_service.conversation_answer(
        question=request.question,
        history=history,
        top_k=request.top_k,
        generation_options=request.generation_options,
    )

    # 保存消息
    user_msg_id = uuid4()
    repo.add_message(
        message_id=user_msg_id,
        conversation_id=conversation_id,
        role="user",
        content=request.question,
    )
    reply_msg_id = uuid4()
    repo.add_message(
        message_id=reply_msg_id,
        conversation_id=conversation_id,
        role="assistant",
        content=result.answer,
        citations={"citations": [c.model_dump(mode="json") for c in result.citations]},
        metadata={"latency_ms": result.latency_ms.model_dump(mode="json")},
    )
    session.commit()

    return success_response(
        SendMessageResponse(
            message_id=reply_msg_id,
            conversation_id=conversation_id,
            answer=result.answer,
            answer_status=result.answer_status,
            citations=result.citations,
            matched_documents=[md.model_dump(mode="json") for md in result.matched_documents],
            latency_ms=result.latency_ms.model_dump(mode="json"),
        ).model_dump(mode="json"),
        trace_id,
    )
```

- [ ] **Step 2: 注册路由**

编辑 `app/api/router.py`，在现有 import 中添加：

```python
from app.api.qa_conversation import router as qa_conversation_router
```

在路由注册部分（约第 36 行后）添加：

```python
api_router.include_router(qa_conversation_router)
```

- [ ] **Step 3: 验证路由可导入**

```bash
cd e:\xm\rag-knowledge-system-main && python -c "from app.api.qa_conversation import router; print('OK')"
```

- [ ] **Step 4: 验证完整启动**

```bash
cd e:\xm\rag-knowledge-system-main && timeout 5 python -m uvicorn app.main:app --host 127.0.0.1 --port 18080 2>&1 || true
```

确认无 import 错误后 Ctrl+C 退出。

- [ ] **Step 5: 提交**

```bash
git add app/api/qa_conversation.py app/api/router.py
git commit -m "feat: add qa conversation API routes"
```

---

### Task 8: 前端类型定义

**Files:**
- Modify: `frontend/src/types.ts`

- [ ] **Step 1: 添加类型定义**

在 `frontend/src/types.ts` 末尾追加：

```typescript
// QA Conversations
export type Citation = {
  chunk_uuid: string
  doc_uuid: string
  title: string
  source_module: string
  snippet: string
  score: number
  version: string
  page_no: number | null
  sheet_name: string | null
  section_title: string | null
}

export type ConversationMessage = {
  message_id: string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[] | null
  metadata: Record<string, unknown> | null
  created_at: string
}

export type ConversationItem = {
  conversation_id: string
  title: string
  last_message: string | null
  message_count: number
  created_at: string
  updated_at: string
}

export type ConversationDetail = {
  conversation_id: string
  title: string
  created_at: string
  updated_at: string
  messages: ConversationMessage[]
}

export type ConversationListData = {
  conversations: ConversationItem[]
  total: number
}

export type CreateConversationData = {
  conversation_id: string
  title: string
  answer?: string
  citations?: Citation[]
}

export type SendMessageData = {
  message_id: string
  conversation_id: string
  answer: string
  answer_status: string
  citations: Citation[]
  matched_documents: Array<{
    doc_uuid: string
    title: string
    score: number
  }>
  latency_ms: {
    retrieval: number
    generation: number
    total: number
  }
}

export type QaConversationEnvelope<T> = {
  code: number
  message: string
  data: T
  trace_id: string
}
```

- [ ] **Step 2: 提交**

```bash
git add frontend/src/types.ts
git commit -m "feat: add QA conversation types"
```

---

### Task 9: 前端 API 函数

**Files:**
- Modify: `frontend/src/api.ts`

- [ ] **Step 1: 在顶部 import 中追加类型**

找到 `frontend/src/api.ts` 第 1-31 行的 import 块，追加：

```typescript
import type {
  // ...existing types...
  Citation,
  ConversationDetail,
  ConversationItem,
  ConversationListData,
  CreateConversationData,
  SendMessageData,
} from './types'
```

实际上，在现有 import 的 `}` 前添加新类型即可：

```typescript
  ConversationItem,
  ConversationDetail,
  ConversationListData,
  CreateConversationData,
  SendMessageData,
  Citation,
```

- [ ] **Step 2: 在文件末尾添加 API 函数**

在 `frontend/src/api.ts` 文件末尾追加：

```typescript
// QA Conversations
export function createConversation(payload: { title?: string; first_message?: string }) {
  return requestJson<CreateConversationData>('/qa/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function fetchConversations() {
  return requestJson<ConversationListData>('/qa/conversations')
}

export function fetchConversation(conversationId: string) {
  return requestJson<ConversationDetail>(`/qa/conversations/${conversationId}`)
}

export function deleteConversation(conversationId: string) {
  return requestJson<{ conversation_id: string; deleted: boolean }>(
    `/qa/conversations/${conversationId}`,
    { method: 'DELETE' },
  )
}

export function sendMessage(conversationId: string, payload: { question: string; top_k?: number }) {
  return requestJson<SendMessageData>(`/qa/conversations/${conversationId}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}
```

- [ ] **Step 3: 提交**

```bash
git add frontend/src/api.ts
git commit -m "feat: add QA conversation API functions"
```

---

### Task 10: 前端对话列表页

**Files:**
- Create: `frontend/src/pages/QaListPage.tsx`

- [ ] **Step 1: 创建对话列表页**

新建 `frontend/src/pages/QaListPage.tsx`：

```tsx
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createConversation, deleteConversation, fetchConversations } from '../api'
import type { ConversationItem } from '../types'

export function QaListPage() {
  const navigate = useNavigate()
  const [conversations, setConversations] = useState<ConversationItem[]>([])
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    fetchConversations()
      .then((data) => setConversations(data.conversations))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  async function handleCreate() {
    setCreating(true)
    try {
      const data = await createConversation({})
      navigate(`/qa/${data.conversation_id}`)
    } catch { setCreating(false) }
  }

  async function handleDelete(conversationId: string, e: React.MouseEvent) {
    e.stopPropagation()
    try {
      await deleteConversation(conversationId)
      setConversations((prev) => prev.filter((c) => c.conversation_id !== conversationId))
    } catch {}
  }

  return (
    <>
      <section className="panel hero-panel compact-hero">
        <div className="hero-copy">
          <p className="eyebrow">智能问答</p>
          <h2>多轮对话</h2>
          <p className="muted">基于知识库的智能问答，支持多轮上下文对话。</p>
        </div>
        <div className="hero-actions">
          <button className="primary-button" onClick={handleCreate} disabled={creating}>
            {creating ? '创建中...' : '新建对话'}
          </button>
        </div>
      </section>

      <section className="panel">
        {loading ? (
          <div className="empty-debug-state"><strong>加载中...</strong></div>
        ) : conversations.length === 0 ? (
          <div className="empty-debug-state">
            <strong>暂无对话</strong>
            <p>点击「新建对话」开始基于知识库的智能问答。</p>
          </div>
        ) : (
          <div className="conversation-list">
            {conversations.map((conv) => (
              <article
                key={conv.conversation_id}
                className="conversation-card"
                onClick={() => navigate(`/qa/${conv.conversation_id}`)}
                style={{ cursor: 'pointer' }}
              >
                <div className="conversation-card-main">
                  <h4>{conv.title}</h4>
                  <p className="muted">{conv.last_message || '暂无消息'}</p>
                </div>
                <div className="conversation-card-meta">
                  <span className="badge">{conv.message_count} 条消息</span>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {new Date(conv.updated_at).toLocaleString('zh-CN')}
                  </span>
                  <button
                    className="danger-button"
                    style={{ marginTop: 4, fontSize: 12 }}
                    onClick={(e) => handleDelete(conv.conversation_id, e)}
                  >
                    删除
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </>
  )
}
```

- [ ] **Step 2: 提交**

```bash
git add frontend/src/pages/QaListPage.tsx
git commit -m "feat: add QaListPage"
```

---

### Task 11: 前端聊天详情页

**Files:**
- Create: `frontend/src/pages/QaChatPage.tsx`

- [ ] **Step 1: 创建聊天详情页**

新建 `frontend/src/pages/QaChatPage.tsx`：

```tsx
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { API_BASE, fetchConversation, sendMessage } from '../api'
import type { Citation, ConversationMessage } from '../types'

export function QaChatPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [messages, setMessages] = useState<ConversationMessage[]>([])
  const [title, setTitle] = useState('')
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!id) return
    fetchConversation(id).then((data) => {
      setMessages(data.messages)
      setTitle(data.title)
    }).catch(() => navigate('/qa'))
  }, [id, navigate])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function handleSend(e: FormEvent) {
    e.preventDefault()
    if (!input.trim() || sending || !id) return
    const question = input.trim()
    setInput('')
    setSending(true)
    // 乐观更新用户消息
    setMessages((prev) => [
      ...prev,
      {
        message_id: 'temp-' + Date.now(),
        role: 'user',
        content: question,
        citations: null,
        metadata: null,
        created_at: new Date().toISOString(),
      },
    ])
    try {
      const data = await sendMessage(id, { question })
      setMessages((prev) => [
        ...prev,
        {
          message_id: data.message_id,
          role: 'assistant',
          content: data.answer,
          citations: data.citations,
          metadata: data.latency_ms as unknown as Record<string, unknown>,
          created_at: new Date().toISOString(),
        },
      ])
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          message_id: 'err-' + Date.now(),
          role: 'assistant',
          content: '抱歉，请求失败，请重试。',
          citations: null,
          metadata: null,
          created_at: new Date().toISOString(),
        },
      ])
    } finally { setSending(false) }
  }

  return (
    <div className="qa-chat-container">
      <header className="qa-chat-header">
        <button className="text-button" onClick={() => navigate('/qa')}>
          ← 返回列表
        </button>
        <h3>{title}</h3>
        <span style={{ width: 80 }} />
      </header>

      <div className="qa-chat-messages">
        {messages.length === 0 ? (
          <div className="empty-debug-state">
            <strong>开始对话</strong>
            <p>输入问题，基于知识库获取智能回答。</p>
          </div>
        ) : (
          messages.map((msg) => (
            <div
              key={msg.message_id}
              className={`qa-message ${msg.role === 'user' ? 'qa-message-user' : 'qa-message-assistant'}`}
            >
              <div className="qa-message-bubble">
                <div className="qa-message-content">{msg.content}</div>
                {msg.citations && msg.citations.length > 0 && (
                  <details className="qa-citations">
                    <summary>引用来源 ({msg.citations.length})</summary>
                    {msg.citations.map((c: Citation, i: number) => (
                      <div key={i} className="qa-citation-item">
                        <strong>[{i + 1}] {c.title}</strong>
                        <span className="muted">模块: {c.source_module} | 分数: {c.score.toFixed(3)}</span>
                        <p>{c.snippet}</p>
                      </div>
                    ))}
                  </details>
                )}
                {msg.metadata && (
                  <div className="qa-latency muted">
                    {(msg.metadata as Record<string, number>).total != null
                      ? `耗时 ${(msg.metadata as Record<string, number>).total}ms`
                      : ''}
                  </div>
                )}
              </div>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>

      <form className="qa-chat-input" onSubmit={handleSend}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="输入你的问题..."
          disabled={sending}
        />
        <button className="primary-button" type="submit" disabled={sending || !input.trim()}>
          {sending ? '发送中...' : '发送'}
        </button>
      </form>
    </div>
  )
}
```

- [ ] **Step 2: 提交**

```bash
git add frontend/src/pages/QaChatPage.tsx
git commit -m "feat: add QaChatPage"
```

---

### Task 12: 前端路由和导航

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/Layout.tsx`

- [ ] **Step 1: 注册路由**

编辑 `frontend/src/App.tsx`：

在 import 块中添加：
```tsx
import { QaListPage } from './pages/QaListPage'
import { QaChatPage } from './pages/QaChatPage'
```

在 `<Route element={<Layout />}>` 内部添加新路由：
```tsx
<Route path="/qa" element={<QaListPage />} />
<Route path="/qa/:id" element={<QaChatPage />} />
```

- [ ] **Step 2: 添加导航项**

编辑 `frontend/src/components/Layout.tsx`：

在 `NAV_ITEMS` 数组中，`retrieval` 项之前插入：
```tsx
{ to: '/qa', label: '智能问答' },
```

修改后的 NAV_ITEMS 为：
```tsx
const NAV_ITEMS = [
  { to: '/', label: '文档管理' },
  { to: '/chunking', label: '切分配置' },
  { to: '/qa', label: '智能问答' },
  { to: '/retrieval', label: '检索测试' },
  { to: '/evaluation', label: '评测工作台' },
  { to: '/settings', label: '配置管理' },
  { to: '/api-endpoints', label: 'API 接口' },
]
```

- [ ] **Step 3: 验证前端编译**

```bash
cd e:\xm\rag-knowledge-system-main\frontend && npx tsc --noEmit 2>&1
```

- [ ] **Step 4: 提交**

```bash
git add frontend/src/App.tsx frontend/src/components/Layout.tsx
git commit -m "feat: add QA pages to routes and navigation"
```

---

### Task 13: 集成测试

**Files:**
- None

- [ ] **Step 1: 启动完整应用**

终端 1：
```bash
cd e:\xm\rag-knowledge-system-main && python -m uvicorn app.main:app --host 127.0.0.1 --port 18080
```

终端 2：
```bash
cd e:\xm\rag-knowledge-system-main\frontend && npm run dev
```

- [ ] **Step 2: 测试完整流程**

用 curl 验证后端 API：

```bash
# 1. 创建对话
curl -sX POST http://127.0.0.1:18080/api/v1/qa/conversations \
  -H "Content-Type: application/json" \
  -d '{"title": "测试", "first_message": "请假需要提前多久？"}' | python -m json.tool

# 2. 获取对话列表
curl -s http://127.0.0.1:18080/api/v1/qa/conversations | python -m json.tool

# 3. 获取对话详情（替换为实际的 conversation_id）
curl -s http://127.0.0.1:18080/api/v1/qa/conversations/<conversation_id> | python -m json.tool

# 4. 发送后续消息
curl -sX POST http://127.0.0.1:18080/api/v1/qa/conversations/<conversation_id>/messages \
  -H "Content-Type: application/json" \
  -d '{"question": "如果是紧急情况呢？"}' | python -m json.tool

# 5. 删除对话
curl -sX DELETE http://127.0.0.1:18080/api/v1/qa/conversations/<conversation_id>
```

- [ ] **Step 3: 浏览器验证**

打开 `http://localhost:5173/qa`：
- 点击「新建对话」→ 应跳转到聊天页
- 输入问题 → 应看到回答和引用来源
- 继续追问 → 应能结合历史上下文回答
- 返回列表 → 应看到对话卡片
- 删除对话 → 应从列表消失

- [ ] **Step 4: 提交（如无问题）**

```bash
git add -A
git commit -m "test: verify QA conversation feature end-to-end"
```

---

### 自检清单

| 检查项 | 状态 |
|--------|:--:|
| 迁移文件格式与现有模式一致 | ✅ |
| ORM 模型使用项目通用类型（JsonType, UuidType, PrimaryKeyBigInt） | ✅ |
| Repository 遵循现有 Session 管理模式 | ✅ |
| API 路由使用项目通用 deps（get_session, get_trace_id） | ✅ |
| 前端 API 调用复用 requestJson | ✅ |
| 前端页面风格与现有页面一致（panel, hero-panel, muted 等 CSS class） | ✅ |
| history 透传给 LLM，多轮上下文生效 | ✅ |
| 首条消息自动作为对话标题 | ✅ |
