"""app/schemas/chat.py"""
from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from app.models.chat_session import MessageRole

T = TypeVar("T")


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    connection_id: UUID | None = Field(default=None)
    initial_message: str | None = Field(default=None, min_length=1, max_length=32000)

    @field_validator("title")
    @classmethod
    def strip_title(cls, v: str | None) -> str | None:
        return v.strip() if v else v

    @field_validator("initial_message")
    @classmethod
    def validate_initial_message(cls, v: str | None) -> str | None:
        if v is not None:
            stripped = v.strip()
            if not stripped:
                raise ValueError("initial_message must not be blank.")
            return stripped
        return v


class AddMessageRequest(BaseModel):
    role: MessageRole = Field(default=MessageRole.USER)
    content: str = Field(..., min_length=1, max_length=32000)

    @field_validator("content")
    @classmethod
    def strip_content(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("content must not be blank.")
        return stripped


class ChatMessageResponse(BaseModel):
    message_id: UUID
    session_id: UUID
    role: str
    content: str
    token_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatSessionResponse(BaseModel):
    session_id: UUID
    title: str
    connection_id: UUID | None = None
    is_active: bool
    message_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatSessionDetailResponse(ChatSessionResponse):
    messages: list[ChatMessageResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class PaginationMeta(BaseModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next: bool
    has_prev: bool


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    pagination: PaginationMeta


class DeleteSessionResponse(BaseModel):
    session_id: UUID
    message: str = "Session deleted successfully."
    messages_deleted: int
