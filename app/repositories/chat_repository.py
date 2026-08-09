"""
app/repositories/chat_repository.py
=====================================
Repository pattern implementation for Chat Session Management.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.logger import get_logger
from app.models.chat_session import ChatMessage, ChatSession, MessageRole

logger = get_logger(__name__)


class ChatSessionRepository:
    async def create(
        self,
        db: AsyncSession,
        *,
        title: str = "New Chat",
        connection_id: uuid.UUID | None = None,
    ) -> ChatSession:
        session_id = uuid.uuid4()
        session = ChatSession(
            session_id=session_id,
            title=title,
            connection_id=connection_id,
            is_active=True,
            message_count=0,
        )
        db.add(session)
        await db.flush()
        return session

    async def get_by_id(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
        *,
        include_messages: bool = False,
    ) -> ChatSession | None:
        stmt = select(ChatSession).where(ChatSession.session_id == session_id)
        if include_messages:
            stmt = stmt.options(selectinload(ChatSession.messages))
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_by_id(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
        *,
        include_messages: bool = False,
    ) -> ChatSession | None:
        stmt = select(ChatSession).where(
            ChatSession.session_id == session_id,
            ChatSession.is_active.is_(True),
        )
        if include_messages:
            stmt = stmt.options(selectinload(ChatSession.messages))
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active(
        self,
        db: AsyncSession,
        *,
        offset: int = 0,
        limit: int = 20,
        connection_id: uuid.UUID | None = None,
    ) -> tuple[list[ChatSession], int]:
        filters = [ChatSession.is_active.is_(True)]
        if connection_id is not None:
            filters.append(ChatSession.connection_id == connection_id)

        count_stmt = select(func.count()).select_from(ChatSession).where(*filters)
        total: int = (await db.execute(count_stmt)).scalar_one()

        data_stmt = (
            select(ChatSession)
            .where(*filters)
            .order_by(ChatSession.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = await db.execute(data_stmt)
        sessions = list(rows.scalars().all())
        return sessions, total

    async def update_title(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
        title: str,
    ) -> ChatSession | None:
        stmt = (
            update(ChatSession)
            .where(ChatSession.session_id == session_id, ChatSession.is_active.is_(True))
            .values(title=title, updated_at=datetime.now(timezone.utc))
            .returning(ChatSession)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def increment_message_count(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
    ) -> None:
        stmt = (
            update(ChatSession)
            .where(ChatSession.session_id == session_id)
            .values(
                message_count=ChatSession.message_count + 1,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await db.execute(stmt)

    async def soft_delete(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
    ) -> ChatSession | None:
        stmt = (
            update(ChatSession)
            .where(ChatSession.session_id == session_id)
            .values(is_active=False, updated_at=datetime.now(timezone.utc))
            .returning(ChatSession)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def hard_delete(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
    ) -> int:
        stmt = delete(ChatSession).where(ChatSession.session_id == session_id)
        result = await db.execute(stmt)
        return result.rowcount


class ChatMessageRepository:
    async def create(
        self,
        db: AsyncSession,
        *,
        session_id: uuid.UUID,
        role: MessageRole,
        content: str,
        token_count: int = 0,
    ) -> ChatMessage:
        message_id = uuid.uuid4()
        message = ChatMessage(
            message_id=message_id,
            session_id=session_id,
            role=role.value,
            content=content,
            token_count=token_count,
        )
        db.add(message)
        await db.flush()

        session_repo = ChatSessionRepository()
        await session_repo.increment_message_count(db, session_id)
        return message

    async def list_by_session(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[ChatMessage], int]:
        count_stmt = (
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.session_id == session_id)
        )
        total: int = (await db.execute(count_stmt)).scalar_one()

        data_stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
            .offset(offset)
            .limit(limit)
        )
        rows = await db.execute(data_stmt)
        messages = list(rows.scalars().all())
        return messages, total

    async def count_by_session(self, db: AsyncSession, session_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(ChatMessage)
            .where(ChatMessage.session_id == session_id)
        )
        return (await db.execute(stmt)).scalar_one()

    async def get_first_user_message(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
    ) -> ChatMessage | None:
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.session_id == session_id,
                ChatMessage.role == MessageRole.USER.value,
            )
            .order_by(ChatMessage.created_at.asc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_by_session(self, db: AsyncSession, session_id: uuid.UUID) -> int:
        stmt = delete(ChatMessage).where(ChatMessage.session_id == session_id)
        result = await db.execute(stmt)
        return result.rowcount
