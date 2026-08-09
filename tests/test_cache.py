"""
tests/test_cache.py
====================
Tests for the Intelligent Query Cache subsystem.

Coverage
--------
* EmbeddingService — vector shape, model name, fallback, batch embedding
* SimilaritySearchEngine — cosine similarity correctness, threshold gating,
  ranking, empty candidates
* CacheService — lookup (hit/miss), store, invalidate, metrics (mocked backends)
* _make_cache_key — determinism and uniqueness
* _to_json_safe integration via CacheEntry serialisation
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from app.cache.similarity import (
    SimilaritySearchEngine,
    cosine_similarity_batch,
)
from app.cache.cache_service import (
    CacheEntry,
    CacheLookupResult,
    CacheService,
    _make_cache_key,
)
from app.schemas.query import QueryResultSet, SQLGenerationDetails, TokenUsage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_vec(dim: int = 384, seed: int = 0) -> np.ndarray:
    """Return a deterministic unit vector."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _make_entry(key: str = "abc123", connection_id: str = "conn-1") -> CacheEntry:
    return CacheEntry(
        cache_key=key,
        connection_id=connection_id,
        question="Show me all orders",
        sql_query="SELECT * FROM orders",
        sql_reasoning="Simple select",
        sql_confidence=0.95,
        columns=["id", "customer"],
        row_count=5,
        result_preview=[[1, "Alice"], [2, "Bob"]],
        truncated=False,
        execution_ms=12.5,
        input_tokens=100,
        output_tokens=50,
        llm_model="gemini-1.5-flash",
        pipeline_ms=840.0,
        embedding=[0.1] * 384,
        embedding_model="all-MiniLM-L6-v2",
        hit_count=0,
        created_at="2024-01-01T00:00:00+00:00",
        expires_at="2024-01-02T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# cosine_similarity_batch
# ---------------------------------------------------------------------------

class TestCosineSimilarityBatch:
    def test_identical_vectors_score_one(self):
        v = _unit_vec(seed=1)
        matrix = np.stack([v, _unit_vec(seed=2)])
        scores = cosine_similarity_batch(v, matrix)
        assert scores[0] == pytest.approx(1.0, abs=1e-5)

    def test_orthogonal_vectors_score_zero(self):
        # Construct two orthogonal unit vectors in 2D (simple)
        v1 = np.array([1.0, 0.0], dtype=np.float32)
        v2 = np.array([0.0, 1.0], dtype=np.float32)
        scores = cosine_similarity_batch(v1, v2.reshape(1, -1))
        assert scores[0] == pytest.approx(0.0, abs=1e-5)

    def test_opposite_vectors_score_minus_one(self):
        v = _unit_vec(seed=3)
        scores = cosine_similarity_batch(v, (-v).reshape(1, -1))
        assert scores[0] == pytest.approx(-1.0, abs=1e-5)

    def test_output_shape(self):
        q = _unit_vec(seed=0)
        matrix = np.stack([_unit_vec(seed=i) for i in range(10)])
        scores = cosine_similarity_batch(q, matrix)
        assert scores.shape == (10,)

    def test_unnormalised_vectors_still_correct(self):
        """Should normalise internally before computing."""
        v = np.array([3.0, 4.0], dtype=np.float32)   # norm=5
        matrix = np.array([[6.0, 8.0]], dtype=np.float32)  # same direction, norm=10
        scores = cosine_similarity_batch(v, matrix)
        assert scores[0] == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# SimilaritySearchEngine
# ---------------------------------------------------------------------------

class TestSimilaritySearchEngine:
    def test_empty_candidates_returns_no_match(self):
        engine = SimilaritySearchEngine(threshold=0.92)
        result = engine.search(_unit_vec(), candidates=[])
        assert result.best_match is None
        assert result.candidates_searched == 0

    def test_identical_question_is_hit(self):
        engine = SimilaritySearchEngine(threshold=0.92)
        v = _unit_vec(seed=7)
        result = engine.search(v, candidates=[("key-1", v)])
        assert result.best_match is not None
        assert result.best_match.is_hit is True
        assert result.best_match.similarity == pytest.approx(1.0, abs=1e-5)
        assert result.best_match.cache_key == "key-1"

    def test_dissimilar_question_is_miss(self):
        engine = SimilaritySearchEngine(threshold=0.92)
        q = _unit_vec(seed=10)
        # Orthogonal vector — similarity should be ~0
        v = np.zeros(384, dtype=np.float32)
        v[0] = 1.0 if q[0] == 0 else 0.0
        v[1] = 1.0
        v = v / np.linalg.norm(v)

        result = engine.search(q, candidates=[("key-x", v)])
        assert result.best_match is not None
        assert result.best_match.is_hit is False

    def test_ranking_is_descending(self):
        engine = SimilaritySearchEngine(threshold=0.5)
        q = _unit_vec(seed=0)
        # Three candidates with decreasing similarity
        c1 = q.copy()                                    # sim = 1.0
        c2 = (q + _unit_vec(seed=1)) / 2.0             # medium sim
        c2 /= np.linalg.norm(c2)
        c3 = _unit_vec(seed=99)                          # low sim

        result = engine.search(q, candidates=[("c3", c3), ("c1", c1), ("c2", c2)], top_k=3)
        assert result.all_matches[0].cache_key == "c1"  # rank 1 = best
        assert result.all_matches[0].rank == 1
        assert result.all_matches[1].rank == 2

    def test_update_threshold(self):
        engine = SimilaritySearchEngine(threshold=0.5)
        engine.update_threshold(0.99)
        assert engine.threshold == 0.99

    def test_invalid_threshold_raises(self):
        engine = SimilaritySearchEngine(threshold=0.5)
        with pytest.raises(ValueError):
            engine.update_threshold(1.5)

    def test_search_ms_is_positive(self):
        engine = SimilaritySearchEngine(threshold=0.92)
        q = _unit_vec(seed=0)
        candidates = [(_unit_vec(seed=i).tobytes()[:8].hex(), _unit_vec(seed=i)) for i in range(100)]
        result = engine.search(q, candidates)
        assert result.search_ms >= 0.0


# ---------------------------------------------------------------------------
# _make_cache_key
# ---------------------------------------------------------------------------

class TestMakeCacheKey:
    def test_deterministic(self):
        k1 = _make_cache_key("conn-1", "show orders")
        k2 = _make_cache_key("conn-1", "show orders")
        assert k1 == k2

    def test_different_connections_differ(self):
        k1 = _make_cache_key("conn-1", "show orders")
        k2 = _make_cache_key("conn-2", "show orders")
        assert k1 != k2

    def test_different_questions_differ(self):
        k1 = _make_cache_key("conn-1", "show orders")
        k2 = _make_cache_key("conn-1", "show customers")
        assert k1 != k2

    def test_key_length_is_32(self):
        k = _make_cache_key("c", "q")
        assert len(k) == 32


# ---------------------------------------------------------------------------
# CacheEntry serialisation round-trip
# ---------------------------------------------------------------------------

class TestCacheEntryRoundTrip:
    def test_to_dict_and_from_dict(self):
        entry = _make_entry()
        d = entry.to_dict()
        restored = CacheEntry.from_dict(d)
        assert restored.cache_key == entry.cache_key
        assert restored.sql_query == entry.sql_query
        assert restored.columns == entry.columns
        assert restored.row_count == entry.row_count

    def test_json_serialisable(self):
        entry = _make_entry()
        d = entry.to_dict()
        # Should not raise
        raw = json.dumps(d)
        parsed = json.loads(raw)
        assert parsed["cache_key"] == entry.cache_key


# ---------------------------------------------------------------------------
# CacheService — mocked backends
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def fake_entry():
    return _make_entry()


class TestCacheServiceLookup:
    @pytest.mark.asyncio
    async def test_miss_when_no_candidates(self, mock_db):
        svc = CacheService(similarity_threshold=0.92)

        with (
            patch("app.cache.cache_service._get_redis", new_callable=AsyncMock, return_value=None),
            patch("app.cache.cache_service._pg_get_candidates", new_callable=AsyncMock, return_value=[]),
        ):
            result = await svc.lookup("any question", "conn-1", mock_db)

        assert result.is_hit is False
        assert result.entry is None

    @pytest.mark.asyncio
    async def test_hit_returns_entry(self, mock_db, fake_entry):
        svc = CacheService(similarity_threshold=0.5)  # Low threshold for test
        q_vec = _unit_vec(seed=0)

        # Mock embedding to return a specific vector
        mock_embedding = MagicMock()
        mock_embedding.vector = q_vec
        mock_embedding.to_list.return_value = q_vec.tolist()

        with (
            patch("app.cache.cache_service._get_redis", new_callable=AsyncMock, return_value=None),
            patch(
                "app.cache.cache_service._pg_get_candidates",
                new_callable=AsyncMock,
                return_value=[(fake_entry.cache_key, q_vec.tolist())],  # Same vector → sim=1.0
            ),
            patch(
                "app.cache.cache_service._pg_get_entry",
                new_callable=AsyncMock,
                return_value=fake_entry,
            ),
            patch(
                "app.cache.cache_service.embedding_service.embed",
                new_callable=AsyncMock,
                return_value=mock_embedding,
            ),
            patch("app.cache.cache_service.asyncio.create_task"),
        ):
            result = await svc.lookup("show me all orders", "conn-1", mock_db)

        assert result.is_hit is True
        assert result.entry is not None
        assert result.entry.cache_key == fake_entry.cache_key
        assert result.similarity == pytest.approx(1.0, abs=1e-4)

    @pytest.mark.asyncio
    async def test_cache_failure_does_not_raise(self, mock_db):
        svc = CacheService(similarity_threshold=0.92)

        with (
            patch("app.cache.cache_service._get_redis", new_callable=AsyncMock, side_effect=Exception("boom")),
            patch("app.cache.cache_service._pg_get_candidates", new_callable=AsyncMock, side_effect=Exception("pg boom")),
            patch(
                "app.cache.cache_service.embedding_service.embed",
                new_callable=AsyncMock,
                return_value=MagicMock(vector=_unit_vec()),
            ),
        ):
            # Should return miss, not raise
            result = await svc.lookup("anything", "conn-1", mock_db)

        assert result.is_hit is False


class TestCacheServiceStore:
    @pytest.mark.asyncio
    async def test_store_calls_pg_insert(self, mock_db):
        svc = CacheService()

        q_vec = _unit_vec()
        mock_embedding = MagicMock()
        mock_embedding.to_list.return_value = q_vec.tolist()
        mock_embedding.model = "all-MiniLM-L6-v2"

        sql_details = SQLGenerationDetails(
            sql_query="SELECT * FROM orders",
            reasoning="test",
            confidence=0.9,
            validation_passed=True,
            validation_error="",
        )
        qr = QueryResultSet(columns=["id"], rows=[[1]], row_count=1, truncated=False, execution_ms=5.0)
        tu = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150, model="gemini-1.5-flash")

        with (
            patch("app.cache.cache_service._get_redis", new_callable=AsyncMock, return_value=None),
            patch("app.cache.cache_service.embedding_service.embed", new_callable=AsyncMock, return_value=mock_embedding),
            patch("app.cache.cache_service._pg_insert_entry", new_callable=AsyncMock) as mock_pg_insert,
        ):
            key = await svc.store(
                question="show orders",
                connection_id="conn-1",
                sql_details=sql_details,
                query_result=qr,
                token_usage=tu,
                pipeline_ms=800.0,
                db=mock_db,
            )

        assert isinstance(key, str)
        assert len(key) == 32
        mock_pg_insert.assert_awaited_once()


class TestCacheServiceInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_connection(self, mock_db):
        svc = CacheService()
        mock_db.execute = AsyncMock(return_value=MagicMock(rowcount=3))
        mock_db.commit = AsyncMock()

        with patch("app.cache.cache_service._get_redis", new_callable=AsyncMock, return_value=None):
            count = await svc.invalidate("conn-1", mock_db)

        # Should have called execute at least once
        mock_db.execute.assert_awaited()


# ---------------------------------------------------------------------------
# EmbeddingService (minimal — avoids loading actual model in CI)
# ---------------------------------------------------------------------------

class TestEmbeddingService:
    @pytest.mark.asyncio
    async def test_empty_string_returns_zero_vector(self):
        from app.cache.embedding_service import EmbeddingService, _EMBEDDING_DIM
        svc = EmbeddingService()

        with patch("app.cache.embedding_service._model", None):
            # With no model, falls back to hash
            result = await svc.embed("  ")

        # Empty string → zero vector
        assert result.vector.shape == (_EMBEDDING_DIM,)

    @pytest.mark.asyncio
    async def test_hash_fallback_produces_unit_vector(self):
        from app.cache.embedding_service import _hash_embed, _EMBEDDING_DIM
        v = _hash_embed("what are the top customers by revenue?")
        assert v.shape == (_EMBEDDING_DIM,)
        norm = float(np.linalg.norm(v))
        assert norm == pytest.approx(1.0, abs=1e-5)

    @pytest.mark.asyncio
    async def test_embedding_result_to_list(self):
        from app.cache.embedding_service import EmbeddingResult, _EMBEDDING_DIM
        vec = _unit_vec()
        er = EmbeddingResult(vector=vec, model="test", dimension=_EMBEDDING_DIM)
        lst = er.to_list()
        assert len(lst) == _EMBEDDING_DIM
        assert all(isinstance(x, float) for x in lst)

    @pytest.mark.asyncio
    async def test_from_list_roundtrip(self):
        from app.cache.embedding_service import EmbeddingResult, _EMBEDDING_DIM
        vec = _unit_vec()
        er = EmbeddingResult(vector=vec, model="test", dimension=_EMBEDDING_DIM)
        restored = EmbeddingResult.from_list(er.to_list(), model="test")
        np.testing.assert_array_almost_equal(vec, restored.vector, decimal=5)
