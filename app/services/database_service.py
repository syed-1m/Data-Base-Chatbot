"""
app/services/database_service.py
==================================
Business logic for Database Connection Management.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.exceptions import InternalServerException, NotFoundException
from app.logger import get_logger
from app.models.database_connection import DatabaseConnectionRecord
from app.schemas.database import (
    ConnectionMeta,
    ConnectionRequest,
    ConnectionResponse,
    ConnectionStatus,
    DatabaseType,
    DisconnectResponse,
    ValidationResponse,
)
from app.utils.connection_manager import ConnectionEntry, connection_manager
from app.utils.db_drivers import (
    build_mongo_client,
    build_mysql_engine,
    build_postgres_engine,
    dispose_mongo_client,
    dispose_sql_engine,
    ping_connection,
)

logger = get_logger(__name__)


class DatabaseConnectionService:
    async def connect(
        self,
        req: ConnectionRequest,
        db: AsyncSession,
    ) -> ConnectionResponse:
        connection_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        engine = None
        mongo_client = None

        try:
            if req.db_type == DatabaseType.POSTGRESQL:
                engine = await build_postgres_engine(req)
            elif req.db_type == DatabaseType.MYSQL:
                engine = await build_mysql_engine(req)
            elif req.db_type == DatabaseType.MONGODB:
                mongo_client = await build_mongo_client(req)
        except Exception as exc:
            raise InternalServerException(message=f"Could not initialise driver: {exc}") from exc

        try:
            latency_ms = await ping_connection(
                db_type=req.db_type,
                engine=engine,
                mongo_client=mongo_client,
                database_name=req.database_name,
                timeout=req.options.connect_timeout,
            )
        except Exception as exc:
            if engine:
                await dispose_sql_engine(engine)
            if mongo_client:
                await dispose_mongo_client(mongo_client)
            raise InternalServerException(message=f"Cannot connect to database: {exc}") from exc

        entry = ConnectionEntry(
            connection_id=connection_id,
            db_type=req.db_type,
            host=req.host,
            port=req.port,
            database_name=req.database_name,
            username=req.username,
            connected_at=now,
            last_used_at=now,
            label=req.label,
            ssl_enabled=req.options.ssl,
            engine=engine,
            mongo_client=mongo_client,
        )
        await connection_manager.add(entry)

        record = DatabaseConnectionRecord(
            connection_id=connection_id,
            db_type=req.db_type.value,
            host=req.host,
            port=req.port,
            database_name=req.database_name,
            username=req.username,
            label=req.label,
            ssl_enabled=req.options.ssl,
            status="connected",
            pool_size=req.options.pool_size,
            connect_timeout=req.options.connect_timeout,
            last_validated_at=now,
        )
        db.add(record)
        await db.flush()

        meta = ConnectionMeta(
            connection_id=connection_id,
            db_type=req.db_type,
            host=req.host,
            port=req.port,
            database_name=req.database_name,
            username=req.username,
            label=req.label,
            ssl=req.options.ssl,
        )
        return ConnectionResponse(
            connection_id=connection_id,
            status=ConnectionStatus.CONNECTED,
            message=f"Connected to {req.db_type} (latency: {latency_ms:.1f}ms).",
            meta=meta,
            connected_at=now,
        )

    async def validate(
        self,
        connection_id: uuid.UUID,
        db: AsyncSession,
    ) -> ValidationResponse:
        entry = await connection_manager.get(connection_id)
        if entry is None:
            raise NotFoundException(message=f"Connection {connection_id} not found.")

        now = datetime.now(timezone.utc)
        try:
            latency_ms = await ping_connection(
                db_type=entry.db_type,
                engine=entry.engine,
                mongo_client=entry.mongo_client,
                database_name=entry.database_name,
                timeout=10,
            )
            status_val = ConnectionStatus.CONNECTED
            message = f"Connection is alive. Latency: {latency_ms:.1f}ms."
        except Exception as exc:
            latency_ms = 0.0
            status_val = ConnectionStatus.ERROR
            message = f"Ping failed: {exc}"

        result = await db.execute(
            select(DatabaseConnectionRecord).where(
                DatabaseConnectionRecord.connection_id == connection_id
            )
        )
        record = result.scalar_one_or_none()
        if record:
            if status_val == ConnectionStatus.CONNECTED:
                record.mark_validated(now)
            else:
                record.mark_error(message)
            await db.flush()

        meta = ConnectionMeta(
            connection_id=entry.connection_id,
            db_type=entry.db_type,
            host=entry.host,
            port=entry.port,
            database_name=entry.database_name,
            username=entry.username,
            label=entry.label,
            ssl=entry.ssl_enabled,
        )
        return ValidationResponse(
            connection_id=connection_id,
            status=status_val,
            message=message,
            meta=meta,
            last_validated_at=now,
            latency_ms=latency_ms,
        )

    async def disconnect(
        self,
        connection_id: uuid.UUID,
        db: AsyncSession,
    ) -> DisconnectResponse:
        entry = await connection_manager.remove(connection_id)
        if entry is None:
            raise NotFoundException(message=f"Connection {connection_id} not found.")

        if entry.engine is not None:
            await dispose_sql_engine(entry.engine)
        if entry.mongo_client is not None:
            await dispose_mongo_client(entry.mongo_client)

        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(DatabaseConnectionRecord).where(
                DatabaseConnectionRecord.connection_id == connection_id
            )
        )
        record = result.scalar_one_or_none()
        if record:
            record.mark_disconnected()
            await db.flush()

        return DisconnectResponse(
            connection_id=connection_id,
            status=ConnectionStatus.DISCONNECTED,
            message="Connection closed.",
            disconnected_at=now,
        )


def get_database_service() -> DatabaseConnectionService:
    return DatabaseConnectionService()
