"""
app/ai/schema_extractor.py
===========================
Live database schema discovery with TTL caching.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import settings
from app.logger import get_logger
from app.schemas.database import DatabaseType
from app.utils.connection_manager import ConnectionEntry, connection_manager

logger = get_logger(__name__)


class _SchemaCache:
    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        self._timestamps: dict[str, datetime] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        ts = self._timestamps.get(key)
        if ts is None:
            return None
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > settings.SCHEMA_CACHE_TTL_SECONDS:
            del self._cache[key]
            del self._timestamps[key]
            return None
        return self._cache.get(key)

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._cache[key] = value
        self._timestamps[key] = datetime.now(timezone.utc)

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)
        self._timestamps.pop(key, None)


_schema_cache = _SchemaCache()


async def _extract_postgres_schema(engine: AsyncEngine, database_name: str) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    async with engine.connect() as conn:
        tables_result = await conn.execute(text("""
            SELECT t.table_name, COALESCE(s.n_live_tup, 0) AS row_count
            FROM information_schema.tables t
            LEFT JOIN pg_stat_user_tables s ON s.relname = t.table_name
            WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
            LIMIT :max_tables
        """), {"max_tables": settings.MAX_SCHEMA_TABLES})
        table_rows = tables_result.fetchall()

        for table_row in table_rows:
            table_name = table_row[0]
            row_count = table_row[1]

            cols_result = await conn.execute(text("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :table_name
            """), {"table_name": table_name})
            col_rows = cols_result.fetchall()

            columns = [
                {
                    "name": col[0],
                    "type": col[1],
                    "nullable": col[2] == "YES",
                    "primary_key": False,
                    "foreign_key": None,
                    "unique": False,
                }
                for col in col_rows
            ]

            tables.append({"name": table_name, "row_count": row_count, "columns": columns, "indexes": []})

    return {
        "database_name": database_name,
        "dialect": "postgresql",
        "tables": tables,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


class SchemaExtractor:
    async def get_schema(self, connection_id: uuid.UUID) -> dict[str, Any]:
        cache_key = str(connection_id)
        cached = _schema_cache.get(cache_key)
        if cached:
            return cached

        entry: ConnectionEntry | None = await connection_manager.get(connection_id)
        if entry is None:
            raise ValueError(f"Connection {connection_id} not found.")

        if entry.db_type == DatabaseType.POSTGRESQL:
            schema = await _extract_postgres_schema(entry.engine, entry.database_name)
        else:
            schema = {
                "database_name": entry.database_name,
                "dialect": str(entry.db_type.value),
                "tables": [],
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            }

        _schema_cache.set(cache_key, schema)
        return schema

    def invalidate_cache(self, connection_id: uuid.UUID) -> None:
        _schema_cache.invalidate(str(connection_id))
