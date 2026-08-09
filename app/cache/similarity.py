"""
app/cache/similarity.py
========================
Cosine Similarity Engine — pure numpy, no external service required.

Design
------
Given a query embedding (384-dim float32 vector) and a list of candidate
(id, vector) pairs, this module finds the best match above a configurable
similarity threshold using **vectorised cosine similarity**.

Why cosine similarity?
  - Direction, not magnitude: two questions with the same meaning but different
    lengths produce vectors pointing in the same direction → similarity ≈ 1.0
  - all-MiniLM-L6-v2 already returns L2-normalised vectors, so cosine is just
    the dot product: ``similarity = a · b`` (no division needed)
  - O(n×384) per search — microseconds for n < 10,000 entries

Thresholds (empirical, configurable via settings):
  ≥ 0.95 → Near-identical question (wording variation)
  ≥ 0.90 → Semantically equivalent (safe cache hit)
  ≥ 0.80 → Related but different intent (do NOT cache)
  < 0.80 → Different question

The default threshold is 0.92, balancing cache hit rate vs. correctness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    _NUMPY_AVAILABLE = False


@dataclass
class SimilarityMatch:
    """Result of a similarity search."""
    cache_key: str                     # The cache entry identifier
    similarity: float                  # Cosine similarity [0, 1]
    is_hit: bool                       # True if similarity ≥ threshold
    rank: int = 0                      # Rank among all candidates (1 = best)


@dataclass
class SimilaritySearchResult:
    """Full result of searching all candidates."""
    best_match: Optional[SimilarityMatch]
    all_matches: list[SimilarityMatch]  # Sorted descending by similarity
    candidates_searched: int
    search_ms: float


def cosine_similarity_batch(
    query_vec: np.ndarray,
    candidate_vecs: np.ndarray,
) -> np.ndarray:
    """
    Vectorised cosine similarity between one query vector and a matrix of candidates.

    Parameters
    ----------
    query_vec : np.ndarray
        Shape (D,) — the query embedding (assumed L2-normalised).
    candidate_vecs : np.ndarray
        Shape (N, D) — N candidate embeddings (assumed L2-normalised).

    Returns
    -------
    np.ndarray
        Shape (N,) — cosine similarities in [–1, 1], typically [0, 1] for
        sentence embeddings.

    Notes
    -----
    Since sentence-transformers returns normalised vectors (||v|| = 1),
    cosine similarity is simply the dot product. For un-normalised vectors
    we normalise on the fly to be safe.
    """
    # Ensure query is 1-D
    q = query_vec.flatten().astype(np.float32)

    # Normalise query
    q_norm = np.linalg.norm(q)
    if q_norm > 0:
        q = q / q_norm

    # Normalise candidates row-wise
    c = candidate_vecs.astype(np.float32)
    norms = np.linalg.norm(c, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)  # Avoid divide-by-zero
    c = c / norms

    return c @ q  # Shape: (N,)


class SimilaritySearchEngine:
    """
    In-process cosine similarity search over a collection of stored embeddings.

    The engine operates on a snapshot list of (key, vector) pairs. For a
    typical chatbot workload (< 50K queries), this is far simpler and faster
    than a vector database like Pinecone or Weaviate.

    Usage::

        engine = SimilaritySearchEngine(threshold=0.92)
        result = engine.search(
            query_vec=embedding.vector,
            candidates=[("key-1", vec1), ("key-2", vec2), ...],
        )
        if result.best_match and result.best_match.is_hit:
            return cached_response
    """

    def __init__(self, threshold: float = 0.92) -> None:
        self.threshold = threshold

    def search(
        self,
        query_vec: np.ndarray,
        candidates: list[tuple[str, np.ndarray]],
        top_k: int = 5,
    ) -> SimilaritySearchResult:
        """
        Find the most similar cached entry to ``query_vec``.

        Parameters
        ----------
        query_vec : np.ndarray
            Shape (D,) — the embedded user question.
        candidates : list of (key, vector)
            All cached (cache_key, embedding) pairs to search.
        top_k : int
            Number of top results to include in ``all_matches``.

        Returns
        -------
        SimilaritySearchResult
            Contains ``best_match`` (or None if no candidates), ``all_matches``,
            and performance metadata.
        """
        import time
        start = time.perf_counter()

        if not candidates:
            return SimilaritySearchResult(
                best_match=None,
                all_matches=[],
                candidates_searched=0,
                search_ms=0.0,
            )

        keys = [k for k, _ in candidates]
        matrix = np.stack([v for _, v in candidates], axis=0)  # Shape: (N, D)

        similarities = cosine_similarity_batch(query_vec, matrix)

        # Sort descending
        ranked_indices = np.argsort(similarities)[::-1]

        all_matches: list[SimilarityMatch] = []
        for rank, idx in enumerate(ranked_indices[:top_k], start=1):
            sim = float(similarities[idx])
            all_matches.append(
                SimilarityMatch(
                    cache_key=keys[idx],
                    similarity=sim,
                    is_hit=sim >= self.threshold,
                    rank=rank,
                )
            )

        best = all_matches[0] if all_matches else None
        search_ms = (time.perf_counter() - start) * 1000

        return SimilaritySearchResult(
            best_match=best,
            all_matches=all_matches,
            candidates_searched=len(candidates),
            search_ms=round(search_ms, 3),
        )

    def update_threshold(self, new_threshold: float) -> None:
        """Dynamically adjust the similarity threshold."""
        if not 0.0 < new_threshold <= 1.0:
            raise ValueError(f"Threshold must be in (0, 1]. Got: {new_threshold}")
        self.threshold = new_threshold
