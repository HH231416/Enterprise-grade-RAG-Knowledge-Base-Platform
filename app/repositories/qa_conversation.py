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
        stmt = select(QaConversation).order_by(desc(QaConversation.updated_at))
        return list(self.session.scalars(stmt).all())

    def delete_conversation(self, conversation_id: UUID) -> bool:
        conv = self.get_conversation(conversation_id)
        if conv is None:
            return False
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
