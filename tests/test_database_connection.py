"""
tests/test_database_connection.py
===================================
Async integration tests for the Database Connection Management API.
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
from app.schemas.database import ConnectionStatus, DatabaseType
from app.utils.connection_manager import ConnectionEntry, connection_manager


@pytest_asyncio.fixture(autouse=True)
async def reset_connection_manager():
    async with connection_manager._lock:
        connection_manager._connections.clear()
    yield
    async with connection_manager._lock:
        connection_manager._connections.clear()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


class TestConnectionManager:
    """Unit tests for the in-memory ConnectionManager."""

    @pytest.mark.asyncio
    async def test_add_and_get(self):
        cid = uuid.uuid4()
        entry = ConnectionEntry(
            connection_id=cid,
            db_type=DatabaseType.POSTGRESQL,
            host="localhost",
            port=5432,
            database_name="db",
            username="user",
        )
        await connection_manager.add(entry)
        result = await connection_manager.get(cid)
        assert result is not None
        assert result.connection_id == cid

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self):
        result = await connection_manager.get(uuid.uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_remove_returns_entry(self):
        cid = uuid.uuid4()
        entry = ConnectionEntry(
            connection_id=cid,
            db_type=DatabaseType.MYSQL,
            host="db",
            port=3306,
            database_name="mydb",
            username="admin",
        )
        await connection_manager.add(entry)
        removed = await connection_manager.remove(cid)
        assert removed is not None
        assert removed.connection_id == cid
        assert await connection_manager.get(cid) is None

    @pytest.mark.asyncio
    async def test_exists(self):
        cid = uuid.uuid4()
        assert not await connection_manager.exists(cid)
        entry = ConnectionEntry(
            connection_id=cid,
            db_type=DatabaseType.MONGODB,
            host="mongo",
            port=27017,
            database_name="testdb",
            username="admin",
        )
        await connection_manager.add(entry)
        assert await connection_manager.exists(cid)


class TestConnectionRequestSchema:
    """Pydantic schema validation tests."""

    def test_default_port_postgresql(self):
        from app.schemas.database import ConnectionRequest
        req = ConnectionRequest(
            db_type="postgresql",
            host="localhost",
            database_name="mydb",
            username="user",
            password="pass",
        )
        assert req.port == 5432

    def test_password_is_secret(self):
        from app.schemas.database import ConnectionRequest
        req = ConnectionRequest(
            db_type="postgresql",
            host="localhost",
            database_name="db",
            username="user",
            password="supersecret",
        )
        assert "supersecret" not in repr(req)
        assert req.password.get_secret_value() == "supersecret"


class TestValidateEndpoint:
    """Tests for GET /api/v1/database/validate/{connection_id}."""

    @pytest.mark.asyncio
    async def test_validate_unknown_id_returns_404(self, client: AsyncClient):
        fake_id = uuid.uuid4()
        response = await client.get(f"/api/v1/database/validate/{fake_id}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_validate_invalid_uuid_returns_422(self, client: AsyncClient):
        response = await client.get("/api/v1/database/validate/not-a-valid-uuid")
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
