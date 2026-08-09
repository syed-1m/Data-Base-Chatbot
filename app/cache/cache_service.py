"""
app/cache/cache_service.py
===========================
Intelligent Query Cache — dual-tier Redis + PostgreSQL backend.

Architecture
------------

  ┌──────────────────────────────────────────────────────────────────────┐
  │                        CacheService                                  │
  │                                                                      │
  │  lookup(question, connection_id)                                     │
  │     1. Embed the question → 384-dim vector                           │
  │     2. Load all vectors for this connection_id from Redis (or PG)    │
  │     3. Run cosine similarity → best match                            │
  │     4. If sim ≥ threshold → fetch full entry → return CacheHit       │
  │     5. Else → return CacheMiss                                       │
  │                                                                      │
  │  store(question, sql, results, metadata, connection_id)              │
  │     1. Embed the question                                            │
  │     2. Write to Redis (JSON, with TTL)                               │
  │     3. Write to PostgreSQL (for persistence + analytics)             │
  │     4. Record hit/miss metrics                                       │
  └──────────────────────────────────────────────────────────────────────┘

Storage layout
--------------

Redis key schema:
  ``qcache:{connection_id}:index``       → JSON list of {key, vector} pairs
  ``qcache:{connection_id}:{cache_key}`` → JSON full cache entry
  ``qcache:metrics:{connection_id}``     → JSON {hits, misses, total_queries}

PostgreSQL tables (see migration):
  ``query_cache``         — main entries table
  ``query_cache_metrics`` — aggregate metrics per connection

TTL
---
Redis entries expire after ``CACHE_TTL_SECONDS`` (default 24 h).
PostgreSQL records have ``expires_at`` column; a background task or the
store() method prunes expired rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    _NUMPY_AVAILABLE = False
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.embedding_service import EmbeddingResult, embedding_service
from app.cache.similarity import SimilaritySearchEngine, SimilaritySearchResult
from app.config import settings
from app.logger import get_logger
from app.schemas.query import QueryResultSet, SQLGenerationDetails, TokenUsage

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Redis client (optional — gracefully degrades to PG-only mode)
# ---------------------------------------------------------------------------

_redis_client = None
_redis_available = False


async def _get_redis():
    """Lazily initialise and return the async Redis client."""
    global _redis_client, _redis_available
    if _redis_client is not None:
        return _redis_client if _redis_available else None
    try:
        import redis.asyncio as aioredis  # type: ignore

        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        # Ping to verify connection
        await _redis_client.ping()
        _redis_available = True
        logger.info("Redis connected: %s", settings.REDIS_URL)
    except Exception as exc:
        _redis_available = False
        _redis_client = None
        logger.warning("Redis unavailable — using PostgreSQL-only cache. Reason: %s", exc)
    return _redis_client if _redis_available else None


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    """A single cached query result."""
    cache_key: str                          # SHA-256 of (connection_id + question)
    connection_id: str                      # Which DB connection generated this
    question: str                           # Original NL question
    sql_query: str                          # Validated SQL
    sql_reasoning: str = ""                 # LLM reasoning
    sql_confidence: float = 0.0
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    result_preview: list[list[Any]] = field(default_factory=list)  # First 10 rows
    truncated: bool = False
    execution_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_model: str = ""
    pipeline_ms: float = 0.0
    embedding: list[float] = field(default_factory=list)
    embedding_model: str = ""
    hit_count: int = 0                      # Number of times served from cache
    created_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict:
        return {
            "cache_key": self.cache_key,
            "connection_id": self.connection_id,
            "question": self.question,
            "sql_query": self.sql_query,
            "sql_reasoning": self.sql_reasoning,
            "sql_confidence": self.sql_confidence,
            "columns": self.columns,
            "row_count": self.row_count,
            "result_preview": self.result_preview,
            "truncated": self.truncated,
            "execution_ms": self.execution_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "llm_model": self.llm_model,
            "pipeline_ms": self.pipeline_ms,
            "embedding": self.embedding,
            "embedding_model": self.embedding_model,
            "hit_count": self.hit_count,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CacheEntry":
        return cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})


@dataclass
class CacheLookupResult:
    """Result of a cache lookup."""
    is_hit: bool
    entry: Optional[CacheEntry] = None
    similarity: float = 0.0
    cache_key: Optional[str] = None
    search_ms: float = 0.0
    candidates_searched: int = 0
    backend: str = ""                       # "redis" | "postgresql" | "miss"


@dataclass
class CacheMetrics:
    """Per-connection cache performance metrics."""
    connection_id: str
    total_queries: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    hit_rate: float = 0.0
    avg_similarity: float = 0.0
    tokens_saved: int = 0
    avg_lookup_ms: float = 0.0
    entries_stored: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cache_key(connection_id: str, question: str) -> str:
    """Stable, unique key for a (connection, question) pair."""
    raw = f"{connection_id}:{question.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ttl_expires_at() -> datetime:
    return _now_utc() + timedelta(seconds=settings.CACHE_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Redis backend helpers
# ---------------------------------------------------------------------------

_REDIS_INDEX_PREFIX = "qcache:index:"
_REDIS_ENTRY_PREFIX = "qcache:entry:"
_REDIS_METRICS_PREFIX = "qcache:metrics:"


async def _redis_get_index(r, connection_id: str) -> list[dict]:
    """Load the vector index for a connection from Redis."""
    raw = await r.get(f"{_REDIS_INDEX_PREFIX}{connection_id}")
    if raw:
        return json.loads(raw)
    return []


async def _redis_save_index(r, connection_id: str, index: list[dict], ttl: int) -> None:
    await r.setex(
        f"{_REDIS_INDEX_PREFIX}{connection_id}",
        ttl,
        json.dumps(index),
    )


async def _redis_get_entry(r, cache_key: str) -> Optional[CacheEntry]:
    raw = await r.get(f"{_REDIS_ENTRY_PREFIX}{cache_key}")
    if raw:
        return CacheEntry.from_dict(json.loads(raw))
    return None


async def _redis_save_entry(r, entry: CacheEntry, ttl: int) -> None:
    await r.setex(
        f"{_REDIS_ENTRY_PREFIX}{entry.cache_key}",
        ttl,
        json.dumps(entry.to_dict()),
    )


async def _redis_increment_hit(r, cache_key: str, connection_id: str) -> None:
    """Atomically increment hit count and metrics."""
    await r.hincrby(f"{_REDIS_METRICS_PREFIX}{connection_id}", "hits", 1)
    await r.hincrby(f"{_REDIS_METRICS_PREFIX}{connection_id}", "total", 1)
    # Update hit_count on the entry
    raw = await r.get(f"{_REDIS_ENTRY_PREFIX}{cache_key}")
    if raw:
        entry_dict = json.loads(raw)
        entry_dict["hit_count"] = entry_dict.get("hit_count", 0) + 1
        ttl = await r.ttl(f"{_REDIS_ENTRY_PREFIX}{cache_key}")
        if ttl > 0:
            await r.setex(f"{_REDIS_ENTRY_PREFIX}{cache_key}", ttl, json.dumps(entry_dict))


async def _redis_increment_miss(r, connection_id: str) -> None:
    await r.hincrby(f"{_REDIS_METRICS_PREFIX}{connection_id}", "misses", 1)
    await r.hincrby(f"{_REDIS_METRICS_PREFIX}{connection_id}", "total", 1)


# ---------------------------------------------------------------------------
# PostgreSQL backend helpers
# ---------------------------------------------------------------------------

async def _pg_get_candidates(
    db: AsyncSession, connection_id: str
) -> list[tuple[str, list[float]]]:
    """Return all (cache_key, embedding) pairs for a connection from PG."""
    result = await db.execute(
        text(
            """
            SELECT cache_key, embedding
            FROM query_cache
            WHERE connection_id = :cid
              AND expires_at > NOW()
              AND is_active = TRUE
            ORDER BY hit_count DESC, created_at DESC
            LIMIT 1000
            """
        ),
        {"cid": connection_id},
    )
    rows = result.fetchall()
    candidates = []
    for row in rows:
        try:
            vec = json.loads(row[1]) if isinstance(row[1], str) else row[1]
            candidates.append((row[0], vec))
        except Exception:
            pass
    return candidates


async def _pg_get_entry(db: AsyncSession, cache_key: str) -> Optional[CacheEntry]:
    result = await db.execute(
        text(
            """
            SELECT cache_key, connection_id, question, sql_query, sql_reasoning,
                   sql_confidence, columns, row_count, result_preview, truncated,
                   execution_ms, input_tokens, output_tokens, llm_model, pipeline_ms,
                   embedding, embedding_model, hit_count, created_at, expires_at
            FROM query_cache
            WHERE cache_key = :key AND is_active = TRUE AND expires_at > NOW()
            """
        ),
        {"key": cache_key},
    )
    row = result.fetchone()
    if row is None:
        return None
    keys = [
        "cache_key", "connection_id", "question", "sql_query", "sql_reasoning",
        "sql_confidence", "columns", "row_count", "result_preview", "truncated",
        "execution_ms", "input_tokens", "output_tokens", "llm_model", "pipeline_ms",
        "embedding", "embedding_model", "hit_count", "created_at", "expires_at",
    ]
    data = dict(zip(keys, row))
    # Deserialise JSON columns
    for col in ("columns", "result_preview", "embedding"):
        if isinstance(data[col], str):
            data[col] = json.loads(data[col])
    for col in ("created_at", "expires_at"):
        if isinstance(data[col], datetime):
            data[col] = data[col].isoformat()
    return CacheEntry.from_dict(data)


async def _pg_insert_entry(db: AsyncSession, entry: CacheEntry) -> None:
    await db.execute(
        text(
            """
            INSERT INTO query_cache (
                cache_key, connection_id, question, sql_query, sql_reasoning,
                sql_confidence, columns, row_count, result_preview, truncated,
                execution_ms, input_tokens, output_tokens, llm_model, pipeline_ms,
                embedding, embedding_model, hit_count, created_at, expires_at, is_active
            ) VALUES (
                :cache_key, :connection_id, :question, :sql_query, :sql_reasoning,
                :sql_confidence, :columns::jsonb, :row_count, :result_preview::jsonb,
                :truncated, :execution_ms, :input_tokens, :output_tokens, :llm_model,
                :pipeline_ms, :embedding::jsonb, :embedding_model, :hit_count,
                :created_at, :expires_at, TRUE
            )
            ON CONFLICT (cache_key) DO UPDATE SET
                hit_count = query_cache.hit_count,
                expires_at = EXCLUDED.expires_at,
                is_active = TRUE
            """
        ),
        {
            "cache_key": entry.cache_key,
            "connection_id": entry.connection_id,
            "question": entry.question,
            "sql_query": entry.sql_query,
            "sql_reasoning": entry.sql_reasoning,
            "sql_confidence": entry.sql_confidence,
            "columns": json.dumps(entry.columns),
            "row_count": entry.row_count,
            "result_preview": json.dumps(entry.result_preview),
            "truncated": entry.truncated,
            "execution_ms": entry.execution_ms,
            "input_tokens": entry.input_tokens,
            "output_tokens": entry.output_tokens,
            "llm_model": entry.llm_model,
            "pipeline_ms": entry.pipeline_ms,
            "embedding": json.dumps(entry.embedding),
            "embedding_model": entry.embedding_model,
            "hit_count": entry.hit_count,
            "created_at": entry.created_at,
            "expires_at": entry.expires_at,
        },
    )
    await db.commit()


async def _pg_increment_hit(db: AsyncSession, cache_key: str) -> None:
    await db.execute(
        text(
            "UPDATE query_cache SET hit_count = hit_count + 1 WHERE cache_key = :key"
        ),
        {"key": cache_key},
    )
    await db.commit()


async def _pg_get_metrics(db: AsyncSession, connection_id: str) -> dict:
    result = await db.execute(
        text(
            """
            SELECT
                COUNT(*) AS entries_stored,
                COALESCE(SUM(hit_count), 0) AS total_hits,
                COALESCE(AVG(sql_confidence), 0) AS avg_confidence
            FROM query_cache
            WHERE connection_id = :cid AND is_active = TRUE AND expires_at > NOW()
            """
        ),
        {"cid": connection_id},
    )
    row = result.fetchone()
    if row:
        return {
            "entries_stored": int(row[0]),
            "total_hits": int(row[1]),
            "avg_confidence": float(row[2]),
        }
    return {"entries_stored": 0, "total_hits": 0, "avg_confidence": 0.0}


# ---------------------------------------------------------------------------
# Main CacheService
# ---------------------------------------------------------------------------

class CacheService:
    """
    Dual-tier intelligent query cache with semantic similarity search.

    Primary tier:  Redis (fast, in-memory, with TTL)
    Fallback tier: PostgreSQL (persistent, survives restarts)

    Usage::

        svc = CacheService()
        result = await svc.lookup("Show top customers", "conn-uuid", db)

        if result.is_hit:
            # Return cached entry directly — skip LLM entirely
            entry = result.entry

        else:
            # Run full pipeline...
            await svc.store(question, sql_details, query_result, token_usage, "conn-uuid", db)
    """

    def __init__(self, similarity_threshold: float | None = None) -> None:
        self._engine = SimilaritySearchEngine(
            threshold=similarity_threshold or settings.CACHE_SIMILARITY_THRESHOLD
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def lookup(
        self,
        question: str,
        connection_id: str,
        db: AsyncSession,
    ) -> CacheLookupResult:
        """
        Search the cache for a semantically equivalent previous question.

        Returns a ``CacheLookupResult`` with ``is_hit=True`` and a populated
        ``entry`` if a match above threshold is found, otherwise ``is_hit=False``.
        """
        t_start = time.perf_counter()

        # 1. Embed the question
        embedding: EmbeddingResult = await embedding_service.embed(question)

        # 2. Load candidates (try Redis first, then PG)
        candidates: list[tuple[str, np.ndarray]] = []
        backend = "miss"
        r = await _get_redis()

        if r is not None:
            try:
                index = await _redis_get_index(r, connection_id)
                candidates = [
                    (item["key"], np.array(item["vec"], dtype=np.float32))
                    for item in index
                ]
                backend = "redis"
            except Exception as exc:
                logger.warning("Redis index load failed: %s", exc)

        if not candidates:
            try:
                pg_candidates = await _pg_get_candidates(db, connection_id)
                candidates = [
                    (key, np.array(vec, dtype=np.float32))
                    for key, vec in pg_candidates
                ]
                backend = "postgresql" if candidates else "miss"
            except Exception as exc:
                logger.warning("PG candidate load failed: %s", exc)

        # 3. Similarity search
        search: SimilaritySearchResult = self._engine.search(
            query_vec=embedding.vector,
            candidates=candidates,
        )

        search_ms = (time.perf_counter() - t_start) * 1000

        if search.best_match is None or not search.best_match.is_hit:
            # Record miss in metrics (non-blocking)
            asyncio.create_task(self._record_miss(connection_id, r))
            return CacheLookupResult(
                is_hit=False,
                search_ms=round(search_ms, 2),
                candidates_searched=search.candidates_searched,
                backend="miss",
            )

        # 4. Fetch full entry
        best_key = search.best_match.cache_key
        entry: Optional[CacheEntry] = None

        if r is not None:
            try:
                entry = await _redis_get_entry(r, best_key)
                if entry:
                    backend = "redis"
            except Exception:
                pass

        if entry is None:
            try:
                entry = await _pg_get_entry(db, best_key)
                if entry:
                    backend = "postgresql"
            except Exception as exc:
                logger.error("Failed to fetch cache entry %s: %s", best_key, exc)

        if entry is None:
            return CacheLookupResult(
                is_hit=False,
                search_ms=round(search_ms, 2),
                candidates_searched=search.candidates_searched,
                backend="miss",
            )

        # 5. Record hit (non-blocking)
        asyncio.create_task(self._record_hit(best_key, connection_id, r, db))

        logger.info(
            "Cache HIT",
            extra={
                "cache_key": best_key,
                "similarity": round(search.best_match.similarity, 4),
                "backend": backend,
                "search_ms": round(search_ms, 2),
            },
        )

        total_ms = (time.perf_counter() - t_start) * 1000
        return CacheLookupResult(
            is_hit=True,
            entry=entry,
            similarity=search.best_match.similarity,
            cache_key=best_key,
            search_ms=round(total_ms, 2),
            candidates_searched=search.candidates_searched,
            backend=backend,
        )

    async def store(
        self,
        question: str,
        connection_id: str,
        sql_details: SQLGenerationDetails,
        query_result: QueryResultSet,
        token_usage: TokenUsage,
        pipeline_ms: float,
        db: AsyncSession,
    ) -> str:
        """
        Persist a successful query result to the cache.

        Returns the ``cache_key`` of the stored entry.
        """
        cache_key = _make_cache_key(connection_id, question)
        now = _now_utc()

        # Embed the question
        embedding = await embedding_service.embed(question)

        # Build entry (store only first 10 rows in preview to keep size sane)
        preview_rows = query_result.rows[:10] if query_result.rows else []

        entry = CacheEntry(
            cache_key=cache_key,
            connection_id=connection_id,
            question=question,
            sql_query=sql_details.sql_query,
            sql_reasoning=sql_details.reasoning,
            sql_confidence=sql_details.confidence,
            columns=query_result.columns,
            row_count=query_result.row_count,
            result_preview=preview_rows,
            truncated=query_result.truncated,
            execution_ms=query_result.execution_ms,
            input_tokens=token_usage.input_tokens,
            output_tokens=token_usage.output_tokens,
            llm_model=token_usage.model,
            pipeline_ms=pipeline_ms,
            embedding=embedding.to_list(),
            embedding_model=embedding.model,
            hit_count=0,
            created_at=now.isoformat(),
            expires_at=_ttl_expires_at().isoformat(),
        )

        r = await _get_redis()

        # Write to Redis
        if r is not None:
            try:
                await _redis_save_entry(r, entry, settings.CACHE_TTL_SECONDS)

                # Update the vector index
                index = await _redis_get_index(r, connection_id)
                # Remove stale entry with same key if present
                index = [i for i in index if i["key"] != cache_key]
                index.append({"key": cache_key, "vec": embedding.to_list()})
                # Prune index if too large (keep most recent 5000)
                if len(index) > 5000:
                    index = index[-5000:]
                await _redis_save_index(r, connection_id, index, settings.CACHE_TTL_SECONDS)

                logger.debug("Cache stored in Redis: %s", cache_key)
            except Exception as exc:
                logger.warning("Redis write failed: %s", exc)

        # Write to PostgreSQL (always — for persistence)
        try:
            await _pg_insert_entry(db, entry)
            logger.debug("Cache stored in PostgreSQL: %s", cache_key)
        except Exception as exc:
            logger.warning("PG cache write failed: %s", exc)

        return cache_key

    async def get_metrics(
        self, connection_id: str, db: AsyncSession
    ) -> CacheMetrics:
        """Return hit/miss metrics for a connection."""
        metrics = CacheMetrics(connection_id=connection_id)
        r = await _get_redis()

        if r is not None:
            try:
                raw = await r.hgetall(f"{_REDIS_METRICS_PREFIX}{connection_id}")
                if raw:
                    hits = int(raw.get("hits", 0))
                    misses = int(raw.get("misses", 0))
                    total = int(raw.get("total", 0))
                    metrics.cache_hits = hits
                    metrics.cache_misses = misses
                    metrics.total_queries = total
                    metrics.hit_rate = round(hits / total, 4) if total > 0 else 0.0
            except Exception:
                pass

        # Always enrich with PG data (tokens saved, entries stored)
        try:
            pg_data = await _pg_get_metrics(db, connection_id)
            metrics.entries_stored = pg_data["entries_stored"]
            # Rough token savings estimate: each cache hit saves avg input+output tokens
            # We compute from stored entry data when we record hits, but for now:
            metrics.tokens_saved = metrics.cache_hits * 150  # Conservative estimate
        except Exception:
            pass

        return metrics

    async def invalidate(
        self, connection_id: str, db: AsyncSession, cache_key: Optional[str] = None
    ) -> int:
        """
        Invalidate cache entries.

        - If ``cache_key`` is provided, invalidate that specific entry.
        - If only ``connection_id`` is provided, invalidate ALL entries for that connection.

        Returns the number of entries invalidated.
        """
        count = 0
        r = await _get_redis()

        if cache_key:
            # Single entry invalidation
            if r is not None:
                try:
                    await r.delete(f"{_REDIS_ENTRY_PREFIX}{cache_key}")
                    # Remove from index
                    index = await _redis_get_index(r, connection_id)
                    new_index = [i for i in index if i["key"] != cache_key]
                    await _redis_save_index(r, connection_id, new_index, settings.CACHE_TTL_SECONDS)
                    count += 1
                except Exception:
                    pass

            try:
                await db.execute(
                    text("UPDATE query_cache SET is_active = FALSE WHERE cache_key = :key"),
                    {"key": cache_key},
                )
                await db.commit()
                count += 1
            except Exception:
                pass
        else:
            # Full connection invalidation
            if r is not None:
                try:
                    await r.delete(f"{_REDIS_INDEX_PREFIX}{connection_id}")
                    await r.delete(f"{_REDIS_METRICS_PREFIX}{connection_id}")
                    count += 1
                except Exception:
                    pass

            try:
                result = await db.execute(
                    text(
                        "UPDATE query_cache SET is_active = FALSE "
                        "WHERE connection_id = :cid AND is_active = TRUE"
                    ),
                    {"cid": connection_id},
                )
                await db.commit()
                count += result.rowcount
            except Exception:
                pass

        return count

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _record_hit(
        self, cache_key: str, connection_id: str, r, db: AsyncSession
    ) -> None:
        if r is not None:
            try:
                await _redis_increment_hit(r, cache_key, connection_id)
            except Exception:
                pass
        try:
            await _pg_increment_hit(db, cache_key)
        except Exception:
            pass

    async def _record_miss(self, connection_id: str, r) -> None:
        if r is not None:
            try:
                await _redis_increment_miss(r, connection_id)
            except Exception:
                pass


# Singleton
cache_service = CacheService()
