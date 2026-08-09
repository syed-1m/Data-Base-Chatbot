"""
tests/test_chat_sessions.py
=============================
Async tests for the Chat Session Management API.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import status
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.chat_session import MessageRole
from app.schemas.chat import (
    CreateSessionRequest,
    PaginationMeta,
)
from app.services.chat_service import _auto_generate_title, _build_pagination_meta, _estimate_token_count


@pytest_asyncio.fixture
async def client():
    """Provide an httpx AsyncClient bound to the FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


def _make_mock_session(
    session_id: uuid.UUID | None = None,
    title: str = "Test Session",
    message_count: int = 0,
    is_active: bool = True,
    connection_id: uuid.UUID | None = None,
) -> MagicMock:
    s = MagicMock()
    s.session_id = session_id or uuid.uuid4()
    s.title = title
    s.message_count = message_count
    s.is_active = is_active
    s.connection_id = connection_id
    s.created_at = datetime.now(timezone.utc)
    s.updated_at = datetime.now(timezone.utc)
    return s


def _make_mock_message(
    session_id: uuid.UUID | None = None,
    role: str = "user",
    content: str = "Hello",
) -> MagicMock:
    m = MagicMock()
    m.message_id = uuid.uuid4()
    m.session_id = session_id or uuid.uuid4()
    m.role = role
    m.content = content
    m.token_count = len(content) // 4
    m.created_at = datetime.now(timezone.utc)
    m.updated_at = datetime.now(timezone.utc)
    return m


class TestHelpers:
    """Unit tests for pure helper functions in chat_service.py."""

    def test_estimate_token_count_basic(self):
        assert _estimate_token_count("abcd") == 1
        assert _estimate_token_count("a" * 400) == 100
        assert _estimate_token_count("") == 1

    def test_auto_generate_title_short(self):
        assert _auto_generate_title("Hello world") == "Hello world"

    def test_auto_generate_title_truncates(self):
        long_text = "A" * 100
        result = _auto_generate_title(long_text, max_length=60)
        assert len(result) <= 63
        assert result.endswith("...")

    def test_auto_generate_title_collapses_whitespace(self):
        text = "Show me\nall tables\n  in the database"
        result = _auto_generate_title(text)
        assert "\n" not in result
        assert "  " not in result

    def test_auto_generate_title_empty_returns_default(self):
        assert _auto_generate_title("   ") == "New Chat"

    def test_build_pagination_meta_first_page(self):
        meta = _build_pagination_meta(page=1, page_size=10, total_items=25)
        assert meta.page == 1
        assert meta.total_pages == 3
        assert meta.has_next is True
        assert meta.has_prev is False

    def test_build_pagination_meta_last_page(self):
        meta = _build_pagination_meta(page=3, page_size=10, total_items=25)
        assert meta.has_next is False
        assert meta.has_prev is True

    def test_build_pagination_meta_empty_result(self):
        meta = _build_pagination_meta(page=1, page_size=20, total_items=0)
        assert meta.total_items == 0
        assert meta.total_pages == 0
        assert meta.has_next is False
        assert meta.has_prev is False


class TestCreateSessionSchema:
    """Schema validation tests for CreateSessionRequest."""

    def test_minimal_request_is_valid(self):
        req = CreateSessionRequest()
        assert req.title is None
        assert req.connection_id is None
        assert req.initial_message is None

    def test_title_stripped(self):
        req = CreateSessionRequest(title="  My Session  ")
        assert req.title == "My Session"

    def test_title_too_long_raises(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CreateSessionRequest(title="A" * 256)

    def test_initial_message_empty_raises(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CreateSessionRequest(initial_message="")

    def test_initial_message_blank_raises(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CreateSessionRequest(initial_message="   ")


class TestAddMessageSchema:
    """Schema validation tests for AddMessageRequest."""

    def test_default_role_is_user(self):
        from app.schemas.chat import AddMessageRequest
        req = AddMessageRequest(content="Hello")
        assert req.role == MessageRole.USER

    def test_content_stripped(self):
        from app.schemas.chat import AddMessageRequest
        req = AddMessageRequest(content="  Hello world  ")
        assert req.content == "Hello world"

    def test_blank_content_raises(self):
        from pydantic import ValidationError
        from app.schemas.chat import AddMessageRequest
        with pytest.raises(ValidationError):
            AddMessageRequest(content="   ")


class TestChatSessionAPI:
    """Tests for the HTTP layer."""

    @pytest.mark.asyncio
    async def test_get_messages_invalid_uuid_returns_422(self, client: AsyncClient):
        response = await client.get("/api/v1/chat/sessions/not-a-uuid/messages")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_delete_invalid_uuid_returns_422(self, client: AsyncClient):
        response = await client.delete("/api/v1/chat/sessions/not-a-uuid")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_list_sessions_invalid_page_returns_422(self, client: AsyncClient):
        response = await client.get("/api/v1/chat/sessions?page=0")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestMessageRoleEnum:
    """Tests for the MessageRole enum."""

    def test_all_roles_defined(self):
        assert MessageRole.USER.value == "user"
        assert MessageRole.ASSISTANT.value == "assistant"
        assert MessageRole.SYSTEM.value == "system"
