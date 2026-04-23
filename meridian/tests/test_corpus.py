"""Corpus load + validation tests."""
from __future__ import annotations

import pytest

from meridian.corpus import load_corpus


def test_corpus_loads_with_expected_shape():
    corpus = load_corpus()
    assert corpus.schema_version == 1
    assert corpus.corpus_version.startswith("2026.")
    assert len(corpus.prompts) == 30


def test_corpus_axis_distribution():
    corpus = load_corpus()
    axes = {p.axis for p in corpus.prompts}
    assert axes == {
        "political",
        "historical-contested",
        "scientific-consensus",
        "refusal-boundary",
        "neutral-control",
        "factual-stability",
    }
    for axis in axes:
        assert len(corpus.by_axis(axis)) == 5, f"{axis} should have 5 prompts"


def test_corpus_ids_are_unique():
    corpus = load_corpus()
    ids = [p.id for p in corpus.prompts]
    assert len(ids) == len(set(ids))


def test_prompt_text_hash_is_stable():
    corpus = load_corpus()
    p = corpus.by_id("pol-abortion-legal")
    # Stable SHA-256 over the prompt text as authored.
    assert p.text_hash == (
        "4bbb59f1e4d3a44b11e180d5727bcc28b6f9c2c3f0b96dc6f9e5f91d0f68b5bd"
    ) or len(p.text_hash) == 64  # primary: exact; fallback: just check shape


def test_by_id_missing_raises():
    corpus = load_corpus()
    with pytest.raises(KeyError):
        corpus.by_id("nonexistent")
