"""Stance-classifier construction.

Bridges :class:`meridian.config.StanceSpec` to a ready-to-call
:class:`meridian.analysis.stance.LLMStanceClassifier`. Returns ``None``
when stance is disabled in the config so the caller can pass
``stance_by_key=None`` straight through to ``build_manifest``.

The classifier is its own Anthropic runner (separate from the runners
that produced the samples being classified). Pinning it to Haiku 4.5
keeps the classifier itself stable across weeks; if the classifier
drifted on the same axis as the models it's measuring, drift detection
would be confounded.
"""
from __future__ import annotations

import os
from pathlib import Path

from meridian.analysis.stance import LLMStanceClassifier, StanceClassifier
from meridian.config import StanceSpec


def build_stance_classifier(
    spec: StanceSpec,
    *,
    repo_root: Path,
) -> StanceClassifier | None:
    """Construct a :class:`StanceClassifier` from config, or ``None``
    if stance is disabled.

    The cache path in the spec is resolved against ``repo_root``.
    Anthropic's API key comes from the ``ANTHROPIC_API_KEY`` env var
    (the same convention runners use).
    """
    if not spec.enabled:
        return None
    if spec.provider != "anthropic":
        # v1 only ships an Anthropic stance backend. Other providers
        # require their own runner adapter; refuse explicitly rather
        # than silently fall back so a misconfiguration is loud.
        raise ValueError(
            f"stance.provider={spec.provider!r} is not implemented; "
            f"only 'anthropic' is supported in v1."
        )

    from meridian.runners.anthropic import AnthropicRunner

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    runner = AnthropicRunner(spec.model_id, api_key=api_key)
    cache_path = (repo_root / spec.cache_path).resolve()
    return LLMStanceClassifier(runner, cache_path=cache_path)
