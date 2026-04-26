"""Embedding-model construction.

Bridges :class:`meridian.config.EmbeddingSpec` to a ready-to-use
:class:`meridian.analysis.embedding.EmbeddingModel`. Returns ``None``
when embedding is disabled in the config so the caller can pass
``embedding_model=None`` straight through to ``build_manifest``.

The default model
(``sentence-transformers/all-mpnet-base-v2``) is a ~400 MB download.
Cold-start in CI is the cost we hide behind ``enabled=False`` until
the cost is measured (Task 1.10 in the originating plan).
"""
from __future__ import annotations

from meridian.analysis.embedding import EmbeddingModel
from meridian.config import EmbeddingSpec


def build_embedding_model(spec: EmbeddingSpec) -> EmbeddingModel | None:
    if not spec.enabled:
        return None
    from meridian.analysis.embedding import SentenceTransformerModel

    return SentenceTransformerModel(spec.model)
