"""
app/utils/connection_manager.py
================================
In-memory singleton that manages all active database connections.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.logger import get_logger
from app.schemas.database import DatabaseType

logger = get_logger(__name__)


@dataclass
class ConnectionEntry:
    connection_id: uuid.UUID
    db_type: DatabaseType
    host: str
    port: int
    database_name: str
    username: str
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    label: str | None = None
    ssl_enabled: bool = False

    engine: Any = None
    mongo_client: Any = None

    def touch(self) -> None:
        self.last_used_at = datetime.now(timezone.utc)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, ConnectionEntry] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def add(self, entry: ConnectionEntry) -> None:
        async with self._lock:
            if entry.connection_id in self._connections:
                raise ValueError(f"Connection {entry.connection_id} is already registered.")
            self._connections[entry.connection_id] = entry

    async def get(self, connection_id: uuid.UUID) -> ConnectionEntry | None:
        async with self._lock:
            entry = self._connections.get(connection_id)
            if entry:
                entry.touch()
            return entry

    async def remove(self, connection_id: uuid.UUID) -> ConnectionEntry | None:
        async with self._lock:
            return self._connections.pop(connection_id, None)

    async def exists(self, connection_id: uuid.UUID) -> bool:
        async with self._lock:
            return connection_id in self._connections

    async def count(self) -> int:
        async with self._lock:
            return len(self._connections)

    async def get_all(self) -> list[ConnectionEntry]:
        async with self._lock:
            return list(self._connections.values())


connection_manager: ConnectionManager = ConnectionManager()
