"""
app/cache/embedding_service.py
================================
Embedding Service — converts natural language questions into dense vectors.

Architecture Decision
---------------------
We use ``sentence-transformers`` with the ``all-MiniLM-L6-v2`` model:

  * 384-dimensional float32 vectors (compact, fast to compare)
  * ~80 MB on disk, runs purely on CPU (no GPU required)
  * No external API calls, no cost, no latency from network
  * State-of-the-art semantic quality for English sentence similarity

The model is loaded **once** at module import time into a module-level
singleton. Subsequent calls to ``embed()`` are pure CPU/RAM operations
taking ~5–15 ms per sentence.

Fallback
--------
If ``sentence-transformers`` is unavailable (e.g., CI environment with
limited dependencies), we fall back to a deterministic TF-IDF-like hash
embedding using only Python builtins + numpy. The fallback is intentionally
marked in the returned ``EmbeddingResult.model`` so the cache layer knows
the quality is reduced.

Thread / Async Safety
---------------------
``SentenceTransformer.encode()`` is a synchronous CPU call. We run it in
the default ``asyncio`` thread pool via ``asyncio.to_thread()`` to keep the
event loop non-blocked.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    _NUMPY_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model loading (module-level singleton, lazy)
# ---------------------------------------------------------------------------

_MODEL_NAME = "all-MiniLM-L6-v2"
_EMBEDDING_DIM = 384
_model = None
_model_loaded = False
_model_error: Optional[str] = None


def _load_model():
    """Load the sentence-transformer model once. Silently falls back on error."""
    global _model, _model_loaded, _model_error
    if _model_loaded:
        return
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore

        logger.info("Loading sentence-transformer model: %s", _MODEL_NAME)
        _model = SentenceTransformer(_MODEL_NAME)
        _model_loaded = True
        logger.info("Embedding model loaded successfully (dim=%d).", _EMBEDDING_DIM)
    except Exception as exc:
        _model_error = str(exc)
        _model_loaded = True  # Don't retry
        logger.warning(
            "sentence-transformers unavailable; using hash fallback. Reason: %s", exc
        )


# ---------------------------------------------------------------------------
# Fallback: deterministic hash-based embedding (poor quality but always works)
# ---------------------------------------------------------------------------

def _hash_embed(text: str) -> np.ndarray:
    """
    Produce a stable 384-dim vector from the text by hashing sliding 4-grams.

    Quality: Adequate for identical strings; poor for paraphrases.
    Use only as a fallback when sentence-transformers is unavailable.
    """
    text = text.lower().strip()
    vec = np.zeros(_EMBEDDING_DIM, dtype=np.float32)
    for i in range(0, max(1, len(text) - 3)):
        chunk = text[i : i + 4]
        digest = hashlib.md5(chunk.encode()).digest()
        # Map 16 bytes → 16 float32 values → scatter into vec
        for j, byte in enumerate(digest):
            idx = (hash(chunk) + j * 7) % _EMBEDDING_DIM
            vec[idx] += (byte / 255.0) - 0.5
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass
class EmbeddingResult:
    vector: np.ndarray            # Shape: (384,) float32
    model: str                    # "all-MiniLM-L6-v2" or "hash-fallback"
    dimension: int = field(default=_EMBEDDING_DIM)
    text_length: int = 0

    def to_list(self) -> list[float]:
        """Convert to plain Python list for JSON / DB storage."""
        return self.vector.tolist()

    @classmethod
    def from_list(cls, data: list[float], model: str = "all-MiniLM-L6-v2") -> "EmbeddingResult":
        """Reconstruct from stored list."""
        return cls(
            vector=np.array(data, dtype=np.float32),
            model=model,
            dimension=len(data),
            text_length=0,
        )


# ---------------------------------------------------------------------------
# Embedding Service
# ---------------------------------------------------------------------------

class EmbeddingService:
    """
    Stateless async service that converts text → embedding vector.

    Usage::

        svc = EmbeddingService()
        result = await svc.embed("Show me the top 10 customers by revenue")
        # result.vector: np.ndarray shape (384,)
        # result.model: "all-MiniLM-L6-v2"
    """

    def __init__(self) -> None:
        _load_model()  # Ensure model is loaded at construction

    async def embed(self, text: str) -> EmbeddingResult:
        """
        Asynchronously embed ``text`` into a 384-dim vector.

        Runs the CPU-bound model in the thread pool so the asyncio event loop
        stays responsive.
        """
        text = text.strip()
        if not text:
            # Return a zero vector for empty input
            return EmbeddingResult(
                vector=np.zeros(_EMBEDDING_DIM, dtype=np.float32),
                model="zero-vector",
                text_length=0,
            )

        if _model is not None:
            # sentence-transformers path
            vector: np.ndarray = await asyncio.to_thread(
                _model.encode,  # type: ignore[arg-type]
                text,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
            return EmbeddingResult(
                vector=vector.astype(np.float32),
                model=_MODEL_NAME,
                text_length=len(text),
            )
        else:
            # Fallback: hash embedding
            vector = await asyncio.to_thread(_hash_embed, text)
            return EmbeddingResult(
                vector=vector,
                model="hash-fallback",
                text_length=len(text),
            )

    async def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        """Embed a list of texts. Uses batched encoding when model is available."""
        if not texts:
            return []

        if _model is not None:
            clean = [t.strip() for t in texts]
            vectors = await asyncio.to_thread(
                _model.encode,  # type: ignore[arg-type]
                clean,
                normalize_embeddings=True,
                convert_to_numpy=True,
                batch_size=32,
            )
            return [
                EmbeddingResult(
                    vector=vectors[i].astype(np.float32),
                    model=_MODEL_NAME,
                    text_length=len(clean[i]),
                )
                for i in range(len(clean))
            ]
        else:
            results = []
            for text in texts:
                vec = await asyncio.to_thread(_hash_embed, text.strip())
                results.append(EmbeddingResult(vector=vec, model="hash-fallback", text_length=len(text)))
            return results


# Singleton — import and use directly
embedding_service = EmbeddingService()
