"""Notable-shift callout card tests."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SITE_SRC = REPO_ROOT / "site" / "src"
if str(SITE_SRC) not in sys.path:
    sys.path.insert(0, str(SITE_SRC))

from build import notable_shifts  # type: ignore[import-not-found]  # noqa: E402
from schema import Manifest  # type: ignore[import-not-found]  # noqa: E402


def _metric(prompt_id: str, model_id: str, refusal_rate: float, hedge: float, length: float) -> dict:
    return {
        "prompt_id": prompt_id, "model_id": model_id, "n_samples": 20,
        "refusal_rate": refusal_rate,
        "refusal_ci": {"lower": 0.0, "upper": 1.0},
        "hedge_density": hedge,
        "length": {"median": length, "p25": length - 10, "p75": length + 10, "n": 20},
        "stance": "neutral", "stance_confidence": 0.8,
        "embedding_centroid_shift": None,
        "refusal_drift": None, "hedge_drift": None, "length_drift": None,
        "change_points": {"refusal_rate": [], "hedge_density": [], "length_median": []},
        "sample_s3_uris": [], "flagged_for_review": False, "flag_reason": None,
    }


def _manifest_with_history(current_metrics: list[dict], prior_metrics: list[dict]) -> Manifest:
    raw = {
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W18",
            "generated_at": "2026-04-25T00:00:00+00:00",
            "corpus_git_sha": "abc1234", "pipeline_version": "0.1.0",
        },
        "models": [
            {"model_id": "m1", "display_name": "M1", "provider": "fake",
             "version_string": "v1", "available": True},
        ],
        "prompts": [
            {"prompt_id": "p1", "axis": "political", "title": "P1",
             "text_hash": "a" * 64, "held_out": False},
            {"prompt_id": "p2", "axis": "political", "title": "P2",
             "text_hash": "b" * 64, "held_out": False},
            {"prompt_id": "p3", "axis": "scientific-consensus", "title": "P3",
             "text_hash": "c" * 64, "held_out": False},
        ],
        "metrics": current_metrics,
        "history": [{
            "week_id": "2026-W17",
            "generated_at": "2026-04-19T00:00:00+00:00",
            "metrics": prior_metrics,
        }],
        "flagged": [], "silent_update_warnings": [],
    }
    return Manifest.model_validate(raw)


def test_no_history_returns_empty():
    m = Manifest.model_validate({
        "schema_version": 2,
        "snapshot": {
            "week_id": "2026-W18",
            "generated_at": "2026-04-25T00:00:00+00:00",
            "corpus_git_sha": "abc1234", "pipeline_version": "0.1.0",
        },
        "models": [], "prompts": [], "metrics": [],
        "history": [], "flagged": [], "silent_update_warnings": [],
    })
    assert notable_shifts(m) == []


def test_largest_refusal_shift_surfaces_first():
    cur = [
        _metric("p1", "m1", refusal_rate=0.80, hedge=1.0, length=200),
        _metric("p2", "m1", refusal_rate=0.10, hedge=1.0, length=200),
        _metric("p3", "m1", refusal_rate=0.10, hedge=1.0, length=200),
    ]
    prior = [
        _metric("p1", "m1", refusal_rate=0.10, hedge=1.0, length=200),  # +0.70
        _metric("p2", "m1", refusal_rate=0.10, hedge=1.0, length=200),  # 0
        _metric("p3", "m1", refusal_rate=0.10, hedge=1.0, length=200),  # 0
    ]
    m = _manifest_with_history(cur, prior)
    out = notable_shifts(m, top_n=3)

    assert len(out) == 3
    top = out[0]
    assert top["metric"] == "refusal_rate"
    assert top["prompt_id"] == "p1"
    assert top["delta"] == 0.7


def test_length_normalised_against_relative_size():
    """A 50-token shift on a 100-token-baseline response should outrank
    a 50-token shift on a 1000-token-baseline response."""
    cur = [
        _metric("p1", "m1", 0.10, 1.0, length=150),
        _metric("p2", "m1", 0.10, 1.0, length=1050),
        _metric("p3", "m1", 0.10, 1.0, length=200),
    ]
    prior = [
        _metric("p1", "m1", 0.10, 1.0, length=100),   # +50 / ~75 = ~0.67
        _metric("p2", "m1", 0.10, 1.0, length=1000),  # +50 / ~525 = ~0.10
        _metric("p3", "m1", 0.10, 1.0, length=200),
    ]
    m = _manifest_with_history(cur, prior)
    out = notable_shifts(m, top_n=3)
    length_shifts = [s for s in out if s["metric"] == "length_median"]
    assert length_shifts[0]["prompt_id"] == "p1"


def test_no_prior_for_a_pair_skips_that_pair():
    cur = [_metric("p1", "m1", 0.10, 1.0, 200), _metric("p2", "m1", 0.10, 1.0, 200)]
    prior = [_metric("p1", "m1", 0.20, 1.0, 200)]  # only p1 has history
    m = _manifest_with_history(cur, prior)
    out = notable_shifts(m, top_n=10)
    prompt_ids = {s["prompt_id"] for s in out}
    assert prompt_ids == {"p1"}
