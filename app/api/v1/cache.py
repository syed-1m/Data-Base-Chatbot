"""
app/api/v1/cache.py
====================
Cache management REST endpoints.

Endpoints
---------
GET  /api/v1/cache/metrics/{connection_id}
     Return hit/miss statistics for a given database connection.

DELETE /api/v1/cache/{connection_id}
     Flush ALL cache entries for a connection (full invalidation).

DELETE /api/v1/cache/{connection_id}/{cache_key}
     Invalidate a single cache entry by its key.

GET  /api/v1/cache/health
     Report whether Redis is reachable and embedding model is loaded.
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.cache_service import CacheMetrics, cache_service
from app.db.session import get_db_session
from app.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/cache", tags=["Query Cache"])


# ---------------------------------------------------------------------------
# Schemas (inline — simple enough not to warrant a separate file)
# ---------------------------------------------------------------------------

from pydantic import BaseModel


class CacheMetricsResponse(BaseModel):
    connection_id: str
    total_queries: int
    cache_hits: int
    cache_misses: int
    hit_rate: float
    tokens_saved: int
    entries_stored: int


class CacheInvalidateResponse(BaseModel):
    connection_id: str
    invalidated: int
    message: str


class CacheHealthResponse(BaseModel):
    redis_available: bool
    embedding_model: str
    embedding_model_loaded: bool
    similarity_threshold: float
    cache_ttl_seconds: int
    cache_enabled: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/metrics/{connection_id}",
    response_model=CacheMetricsResponse,
    summary="Get cache hit/miss metrics for a connection",
)
async def get_cache_metrics(
    connection_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> CacheMetricsResponse:
    """
    Return cache performance statistics for the specified database connection.

    Metrics include:
    - **total_queries**: Total number of queries processed
    - **cache_hits**: Queries served from cache (LLM not called)
    - **cache_misses**: Queries that required a full LLM pipeline run
    - **hit_rate**: Fraction of queries served from cache [0.0, 1.0]
    - **tokens_saved**: Estimated LLM tokens saved by cache hits
    - **entries_stored**: Number of active cache entries
    """
    metrics: CacheMetrics = await cache_service.get_metrics(connection_id, db)
    return CacheMetricsResponse(
        connection_id=metrics.connection_id,
        total_queries=metrics.total_queries,
        cache_hits=metrics.cache_hits,
        cache_misses=metrics.cache_misses,
        hit_rate=metrics.hit_rate,
        tokens_saved=metrics.tokens_saved,
        entries_stored=metrics.entries_stored,
    )


@router.delete(
    "/{connection_id}",
    response_model=CacheInvalidateResponse,
    summary="Flush all cache entries for a connection",
)
async def invalidate_connection_cache(
    connection_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> CacheInvalidateResponse:
    """
    Invalidate (soft-delete) ALL cache entries for the specified connection.

    Use this when:
    - The underlying database schema has changed
    - You want to force fresh LLM generation for all queries
    - A previous cache entry returned incorrect results
    """
    count = await cache_service.invalidate(connection_id, db)
    return CacheInvalidateResponse(
        connection_id=connection_id,
        invalidated=count,
        message=f"Flushed {count} cache entries for connection {connection_id}.",
    )


@router.delete(
    "/{connection_id}/{cache_key}",
    response_model=CacheInvalidateResponse,
    summary="Invalidate a single cache entry",
)
async def invalidate_cache_entry(
    connection_id: str,
    cache_key: str,
    db: AsyncSession = Depends(get_db_session),
) -> CacheInvalidateResponse:
    """
    Invalidate a specific cache entry by its 32-character SHA-256 key.

    The ``cache_key`` is returned in the ``cache_key`` field of every
    ``complete`` SSE event when a query is served from cache.
    """
    count = await cache_service.invalidate(connection_id, db, cache_key=cache_key)
    return CacheInvalidateResponse(
        connection_id=connection_id,
        invalidated=count,
        message=f"Cache entry {cache_key} invalidated.",
    )


@router.get(
    "/health",
    response_model=CacheHealthResponse,
    summary="Cache subsystem health check",
)
async def cache_health() -> CacheHealthResponse:
    """
    Check the health of the cache subsystem:

    - Whether Redis is reachable
    - Whether the sentence-transformer model is loaded
    - Current configuration values
    """
    from app.cache.embedding_service import _model, _model_loaded, _MODEL_NAME
    from app.cache.cache_service import _redis_available, _get_redis
    from app.config import settings

    # Probe Redis (don't rely on cached _redis_available)
    redis_ok = False
    try:
        r = await _get_redis()
        if r is not None:
            await r.ping()
            redis_ok = True
    except Exception:
        redis_ok = False

    return CacheHealthResponse(
        redis_available=redis_ok,
        embedding_model=_MODEL_NAME,
        embedding_model_loaded=_model_loaded and _model is not None,
        similarity_threshold=settings.CACHE_SIMILARITY_THRESHOLD,
        cache_ttl_seconds=settings.CACHE_TTL_SECONDS,
        cache_enabled=settings.CACHE_ENABLED,
    )
