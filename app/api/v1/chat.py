"""
app/api/v1/chat.py
==================
FastAPI router for Chat Session Management.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.dependencies import PaginationParams, get_pagination
from app.schemas.chat import (
    ChatMessageResponse,
    ChatSessionResponse,
    CreateSessionRequest,
    DeleteSessionResponse,
    PaginatedResponse,
)
from app.services.chat_service import ChatSessionService, get_chat_service

router = APIRouter(prefix="/chat", tags=["Chat Sessions"])


@router.post("/sessions", response_model=ChatSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    req: CreateSessionRequest,
    service: ChatSessionService = Depends(get_chat_service),
    db: AsyncSession = Depends(get_db_session),
) -> ChatSessionResponse:
    return await service.create_session(db, req)


@router.get("/sessions", response_model=PaginatedResponse[ChatSessionResponse], status_code=status.HTTP_200_OK)
async def list_sessions(
    connection_id: Optional[uuid.UUID] = Query(default=None),
    pagination: PaginationParams = Depends(get_pagination),
    service: ChatSessionService = Depends(get_chat_service),
    db: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[ChatSessionResponse]:
    return await service.list_sessions(db, pagination, connection_id=connection_id)


@router.get("/sessions/{session_id}/messages", response_model=PaginatedResponse[ChatMessageResponse], status_code=status.HTTP_200_OK)
async def get_session_messages(
    session_id: uuid.UUID,
    pagination: PaginationParams = Depends(get_pagination),
    service: ChatSessionService = Depends(get_chat_service),
    db: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[ChatMessageResponse]:
    return await service.get_session_messages(db, session_id, pagination)


@router.delete("/sessions/{session_id}", response_model=DeleteSessionResponse, status_code=status.HTTP_200_OK)
async def delete_session(
    session_id: uuid.UUID,
    service: ChatSessionService = Depends(get_chat_service),
    db: AsyncSession = Depends(get_db_session),
) -> DeleteSessionResponse:
    return await service.delete_session(db, session_id)
