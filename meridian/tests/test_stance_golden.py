"""Stance-classifier golden-set validation.

The golden set at ``meridian/corpus/stance_golden.yaml`` is a small
hand-labelled dataset of (response, expected_stance) pairs. This test
locks two contracts:

1. The file has the expected schema and is well-formed.
2. ``parse_stance`` — the deterministic post-processing layer — handles
   one-word + sentence-padded LLM outputs correctly. (We don't run the
   actual LLM here; that test belongs in a separate, opt-in integration
   that talks to Anthropic. The pipeline plan says to run that one
   manually before Phase 1 closes.)

If the LLM classifier on real Anthropic credentials starts producing
< 80% accuracy on this set, the methodology page's "periodically
re-validated" claim has gone stale and we need to look at why.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from meridian.analysis.stance import parse_stance

GOLDEN = Path(__file__).resolve().parent.parent / "corpus" / "stance_golden.yaml"


def _load_golden() -> list[dict]:
    raw = yaml.safe_load(GOLDEN.read_text())
    return raw["cases"]


def test_golden_file_well_formed():
    cases = _load_golden()
    assert len(cases) >= 10, "v1 floor for the golden set is 10 cases"
    valid_stances = {"pro", "anti", "neutral", "na"}
    for c in cases:
        assert "prompt_id" in c and "expected_stance" in c and "response" in c
        assert c["expected_stance"] in valid_stances
        assert isinstance(c["response"], str) and c["response"].strip()


def test_golden_covers_both_directions_on_at_least_one_prompt():
    """A classifier that always says 'pro' would pass a single-direction
    test trivially. Force the dataset to cover at least one
    pro/anti pair on the same prompt."""
    cases = _load_golden()
    by_prompt: dict[str, set[str]] = {}
    for c in cases:
        by_prompt.setdefault(c["prompt_id"], set()).add(c["expected_stance"])
    has_both = any({"pro", "anti"} <= v for v in by_prompt.values())
    assert has_both, (
        "expected at least one prompt with both pro and anti labels; "
        f"found per-prompt stance sets: {by_prompt}"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The classifier prompt asks for one word, but in practice models
        # sometimes return punctuation or short padding. parse_stance
        # has to recover from both.
        ("pro", "pro"),
        ("anti", "anti"),
        ("neutral", "neutral"),
        ("na", "na"),
        ("Pro", "pro"),
        ("PRO.", "pro"),
        ("anti.", "anti"),
        ("Stance: anti", "anti"),
        ("The stance is neutral.", "neutral"),
        ("na (refusal)", "na"),
    ],
)
def test_parse_stance_handles_realistic_classifier_outputs(raw: str, expected: str):
    assert parse_stance(raw).stance == expected


def test_parse_stance_returns_na_on_unparseable():
    """Anything that doesn't contain a recognised stance word falls back
    to 'na' with confidence 0 — never silently misclassifies."""
    out = parse_stance("hmm, hard to say")
    assert out.stance == "na"
    assert out.confidence == 0.0
