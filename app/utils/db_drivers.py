"""
app/utils/db_drivers.py
========================
Database driver factory and connectivity utilities.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.logger import get_logger
from app.schemas.database import ConnectionOptions, ConnectionRequest, DatabaseType

logger = get_logger(__name__)


from urllib.parse import quote_plus


def _build_postgres_url(req: ConnectionRequest) -> str:
    password = quote_plus(req.password.get_secret_value())
    return f"postgresql+asyncpg://{req.username}:{password}@{req.host}:{req.port}/{req.database_name}"


def _build_mysql_url(req: ConnectionRequest) -> str:
    password = quote_plus(req.password.get_secret_value())
    return f"mysql+aiomysql://{req.username}:{password}@{req.host}:{req.port}/{req.database_name}"


async def build_postgres_engine(req: ConnectionRequest) -> AsyncEngine:
    opts: ConnectionOptions = req.options
    url = _build_postgres_url(req)
    try:
        connect_args: dict[str, Any] = {
            "timeout": opts.connect_timeout,
            "server_settings": {"application_name": "db-chatbot"},
        }
        if opts.ssl:
            connect_args["ssl"] = "require"

        return create_async_engine(
            url=url,
            pool_size=opts.pool_size,
            max_overflow=opts.max_overflow,
            pool_timeout=opts.connect_timeout,
            pool_recycle=opts.pool_recycle,
            pool_pre_ping=True,
            connect_args=connect_args,
            echo=False,
        )
    except Exception as exc:
        raise ConnectionError(f"Failed to create PostgreSQL engine: {exc}") from exc


async def build_mysql_engine(req: ConnectionRequest) -> AsyncEngine:
    opts: ConnectionOptions = req.options
    url = _build_mysql_url(req)
    try:
        connect_args: dict[str, Any] = {"connect_timeout": opts.connect_timeout}
        if opts.ssl:
            connect_args["ssl"] = {"ssl_ca": None}

        return create_async_engine(
            url=url,
            pool_size=opts.pool_size,
            max_overflow=opts.max_overflow,
            pool_timeout=opts.connect_timeout,
            pool_recycle=opts.pool_recycle,
            pool_pre_ping=True,
            connect_args=connect_args,
            echo=False,
        )
    except Exception as exc:
        raise ConnectionError(f"Failed to create MySQL engine: {exc}") from exc


async def build_mongo_client(req: ConnectionRequest) -> Any:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError as exc:
        raise ImportError("motor is required for MongoDB support.") from exc

    opts: ConnectionOptions = req.options
    password = req.password.get_secret_value()

    if req.username and password:
        mongo_uri = f"mongodb://{req.username}:{password}@{req.host}:{req.port}/{req.database_name}"
    else:
        mongo_uri = f"mongodb://{req.host}:{req.port}/{req.database_name}"

    if opts.ssl:
        mongo_uri += "?tls=true"

    try:
        return AsyncIOMotorClient(
            mongo_uri,
            serverSelectionTimeoutMS=opts.server_selection_timeout_ms,
            connectTimeoutMS=opts.connect_timeout * 1000,
            socketTimeoutMS=opts.query_timeout * 1000,
            maxPoolSize=opts.pool_size + opts.max_overflow,
            minPoolSize=1,
            appname="db-chatbot",
        )
    except Exception as exc:
        raise ConnectionError(f"Failed to create MongoDB client: {exc}") from exc


async def ping_sql_engine(engine: AsyncEngine, timeout: int = 10) -> float:
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return round((time.perf_counter() - start) * 1000, 2)
    except Exception as exc:
        raise ConnectionError(f"SQL engine ping failed: {exc}") from exc


async def ping_mongo_client(client: Any, database_name: str, timeout: int = 10) -> float:
    start = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            db = client[database_name]
            await db.command("ping")
        return round((time.perf_counter() - start) * 1000, 2)
    except Exception as exc:
        raise ConnectionError(f"MongoDB ping failed: {exc}") from exc


async def dispose_sql_engine(engine: AsyncEngine) -> None:
    try:
        await engine.dispose()
    except Exception:
        pass


async def dispose_mongo_client(client: Any) -> None:
    try:
        client.close()
    except Exception:
        pass


async def ping_connection(
    db_type: DatabaseType,
    engine: AsyncEngine | None,
    mongo_client: Any | None,
    database_name: str,
    timeout: int = 10,
) -> float:
    if db_type in (DatabaseType.POSTGRESQL, DatabaseType.MYSQL):
        if engine is None:
            raise ValueError("engine is required for SQL ping.")
        return await ping_sql_engine(engine, timeout=timeout)
    elif db_type == DatabaseType.MONGODB:
        if mongo_client is None:
            raise ValueError("mongo_client is required for Mongo ping.")
        return await ping_mongo_client(mongo_client, database_name, timeout=timeout)
    else:
        raise ValueError(f"Unsupported database type: {db_type}")
