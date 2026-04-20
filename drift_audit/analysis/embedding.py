"""Embedding centroid drift.

For each (prompt × model), compute the mean embedding of this week's
responses and measure its distance from last week's centroid. Large
weekly shifts flag candidate drift events for human review.

The :class:`SentenceTransformerModel` wrapper around
``sentence-transformers/all-mpnet-base-v2`` is the recommended default;
the library is a large optional dependency, so callers can substitute
any implementation that satisfies the :class:`EmbeddingModel` Protocol.
"""
from __future__ import annotations

from typing import Protocol

# Type alias without importing numpy eagerly so the module stays importable
# without the optional heavy-analysis deps installed.
FloatArray = "object"  # runtime is numpy.ndarray when deps are present


class EmbeddingModel(Protocol):
    def encode(self, texts: list[str]) -> FloatArray: ...


def centroid_shift(
    current_samples: list[str],
    prior_samples: list[str],
    model: EmbeddingModel,
) -> float | None:
    """Cosine distance between this week's and prior week's centroids.

    Returns ``None`` if either side lacks data (no comparison possible).
    Returns a value in [0, 2] where 0 is identical direction and 2 is
    opposite (antipodal). In practice, sub-0.05 shifts are sampling
    noise; shifts above 0.15 warrant human review.
    """
    if not current_samples or not prior_samples:
        return None
    try:
        import numpy as np
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "embedding.centroid_shift requires the `analysis-heavy` dep group. "
            "Install with: uv sync --group analysis-heavy"
        ) from e

    current = np.asarray(model.encode(current_samples), dtype=float)
    prior = np.asarray(model.encode(prior_samples), dtype=float)
    if current.size == 0 or prior.size == 0:
        return None
    cur_c = current.mean(axis=0)
    pri_c = prior.mean(axis=0)
    cur_n = float(np.linalg.norm(cur_c))
    pri_n = float(np.linalg.norm(pri_c))
    if cur_n == 0.0 or pri_n == 0.0:
        return None
    cos_sim = float(np.dot(cur_c, pri_c) / (cur_n * pri_n))
    # Clamp numerical drift outside [-1, 1].
    cos_sim = max(-1.0, min(1.0, cos_sim))
    return round(1.0 - cos_sim, 4)


class SentenceTransformerModel:
    """Default embedding model: sentence-transformers all-mpnet-base-v2.

    Lazily loads the underlying transformer on first encode — construction
    is cheap and the import overhead is paid only when embedding is needed.
    """

    def __init__(self, model_name: str = "sentence-transformers/all-mpnet-base-v2") -> None:
        self.model_name = model_name
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def encode(self, texts: list[str]) -> FloatArray:
        return self._load().encode(texts, normalize_embeddings=False)
