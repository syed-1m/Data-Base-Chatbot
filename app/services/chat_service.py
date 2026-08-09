"""
app/services/chat_service.py
==============================
Business logic for Chat Session Management.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.dependencies import PaginationParams
from app.logger import get_logger
from app.models.chat_session import MessageRole
from app.repositories.chat_repository import ChatMessageRepository, ChatSessionRepository
from app.schemas.chat import (
    AddMessageRequest,
    ChatMessageResponse,
    ChatSessionResponse,
    CreateSessionRequest,
    DeleteSessionResponse,
    PaginatedResponse,
    PaginationMeta,
)

logger = get_logger(__name__)

_session_repo = ChatSessionRepository()
_message_repo = ChatMessageRepository()


def _estimate_token_count(text: str) -> int:
    return max(1, len(text) // 4)


def _build_pagination_meta(page: int, page_size: int, total_items: int) -> PaginationMeta:
    total_pages = max(1, math.ceil(total_items / page_size)) if total_items > 0 else 0
    return PaginationMeta(
        page=page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_prev=page > 1,
    )


def _auto_generate_title(text: str, max_length: int = 60) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return "New Chat"
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[:max_length].rstrip() + "..."


class ChatSessionService:
    async def create_session(
        self,
        db: AsyncSession,
        req: CreateSessionRequest,
    ) -> ChatSessionResponse:
        if req.title:
            title = req.title
        elif req.initial_message:
            title = _auto_generate_title(req.initial_message)
        else:
            ts = datetime.now(timezone.utc).strftime("%b %d, %H:%M")
            title = f"New Chat - {ts}"

        session = await _session_repo.create(db, title=title, connection_id=req.connection_id)

        if req.initial_message:
            token_count = _estimate_token_count(req.initial_message)
            await _message_repo.create(
                db,
                session_id=session.session_id,
                role=MessageRole.USER,
                content=req.initial_message,
                token_count=token_count,
            )

        return ChatSessionResponse.model_validate(session)

    async def list_sessions(
        self,
        db: AsyncSession,
        pagination: PaginationParams,
        connection_id: uuid.UUID | None = None,
    ) -> PaginatedResponse[ChatSessionResponse]:
        sessions, total = await _session_repo.list_active(
            db,
            offset=pagination.offset,
            limit=pagination.limit,
            connection_id=connection_id,
        )
        items = [ChatSessionResponse.model_validate(s) for s in sessions]
        pagination_meta = _build_pagination_meta(
            page=pagination.page,
            page_size=pagination.page_size,
            total_items=total,
        )
        return PaginatedResponse[ChatSessionResponse](items=items, pagination=pagination_meta)

    async def get_session_messages(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
        pagination: PaginationParams,
    ) -> PaginatedResponse[ChatMessageResponse]:
        session = await _session_repo.get_active_by_id(db, session_id)
        if session is None:
            raise NotFoundException(message=f"Chat session {session_id} not found.")

        messages, total = await _message_repo.list_by_session(
            db,
            session_id=session_id,
            offset=pagination.offset,
            limit=pagination.limit,
        )
        items = [ChatMessageResponse.model_validate(m) for m in messages]
        pagination_meta = _build_pagination_meta(
            page=pagination.page,
            page_size=pagination.page_size,
            total_items=total,
        )
        return PaginatedResponse[ChatMessageResponse](items=items, pagination=pagination_meta)

    async def delete_session(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
    ) -> DeleteSessionResponse:
        session = await _session_repo.get_by_id(db, session_id)
        if session is None:
            raise NotFoundException(message=f"Chat session {session_id} not found.")

        messages_deleted = session.message_count
        await _session_repo.hard_delete(db, session_id)
        return DeleteSessionResponse(
            session_id=session_id,
            message="Session deleted.",
            messages_deleted=messages_deleted,
        )


def get_chat_service() -> ChatSessionService:
    return ChatSessionService()
