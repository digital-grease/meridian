"""The weekly human spot-check needs a non-empty worklist.

CLAUDE.md requires: "every week, flag prompts with highest metric deltas
for human review". Until 2026-07-24 the only rule that could set
``flagged_for_review`` was ``n_samples < 10``, and since every cell
carries 20-25 samples the flag had never been true once across 13 weeks
and 690 records. The review page was empty every week by construction,
which is how two weeks of dead gpt-5.5 samples went unreviewed.
"""
from __future__ import annotations

from meridian.pipeline.manifest_writer import _flag_largest_deltas


def _rec(prompt_id: str, model_id: str, *, refusal=0.0, hedge=0.0,
         length=100.0, flagged=False, reason=None) -> dict:
    return {
        "prompt_id": prompt_id, "model_id": model_id,
        "refusal_rate": refusal, "hedge_density": hedge,
        "length": {"median": length, "p25": length, "p75": length, "n": 20},
        "flagged_for_review": flagged, "flag_reason": reason,
    }


def _history(week: str, metrics: list[dict]) -> list[dict]:
    return [{"week_id": week, "metrics": metrics}]


def test_largest_mover_gets_flagged():
    current = [
        _rec("p-quiet", "m1", refusal=0.10),
        _rec("p-loud", "m1", refusal=0.90),
    ]
    history = _history("2026-W29", [
        _rec("p-quiet", "m1", refusal=0.08),
        _rec("p-loud", "m1", refusal=0.10),
    ])
    _flag_largest_deltas(current, history, top_n=1)
    loud = next(r for r in current if r["prompt_id"] == "p-loud")
    quiet = next(r for r in current if r["prompt_id"] == "p-quiet")
    assert loud["flagged_for_review"] is True
    assert "largest weekly delta" in loud["flag_reason"]
    assert quiet["flagged_for_review"] is False


def test_flagging_is_additive_not_destructive():
    """A cell already flagged for a data-quality reason must keep it:
    the reason a human is looking is part of the record."""
    current = [_rec("p", "m1", refusal=0.9, flagged=True,
                    reason="8/20 sample(s) returned no usable content")]
    history = _history("2026-W29", [_rec("p", "m1", refusal=0.0)])
    _flag_largest_deltas(current, history, top_n=1)
    assert current[0]["flagged_for_review"] is True
    assert "no usable content" in current[0]["flag_reason"]
    assert "largest weekly delta" in current[0]["flag_reason"]


def test_no_prior_week_means_no_delta_flag():
    """A model's first appearance has nothing to move against."""
    current = [_rec("p", "newmodel", refusal=1.0)]
    _flag_largest_deltas(current, _history("2026-W29", []), top_n=3)
    assert current[0]["flagged_for_review"] is False


def test_completely_static_week_flags_nothing():
    """No movement must not manufacture three flags just to fill the
    quota — that would train the reviewer to ignore the page."""
    same = [_rec("p1", "m1", refusal=0.5), _rec("p2", "m1", refusal=0.5)]
    current = [dict(r, length=dict(r["length"])) for r in same]
    _flag_largest_deltas(current, _history("2026-W29", same), top_n=3)
    assert all(not r["flagged_for_review"] for r in current)


def test_each_cell_flagged_at_most_once():
    """One cell moving on all three metrics should not consume the
    whole top-N budget."""
    current = [
        _rec("p1", "m1", refusal=0.9, hedge=4.0, length=900.0),
        _rec("p2", "m1", refusal=0.4),
    ]
    history = _history("2026-W29", [
        _rec("p1", "m1", refusal=0.0, hedge=0.0, length=100.0),
        _rec("p2", "m1", refusal=0.0),
    ])
    _flag_largest_deltas(current, history, top_n=2)
    assert [r["prompt_id"] for r in current if r["flagged_for_review"]] == ["p1", "p2"]


def test_model_compared_against_its_own_prior_week():
    """The frontier roster alternates by ISO-week parity, so the
    calendar-previous week usually holds a different model."""
    current = [_rec("p", "gpt", refusal=1.0)]
    history = [
        {"week_id": "2026-W27", "metrics": [_rec("p", "gpt", refusal=0.9)]},
        {"week_id": "2026-W28", "metrics": [_rec("p", "opus", refusal=0.1)]},
    ]
    _flag_largest_deltas(current, history, top_n=1)
    # Delta is vs gpt's own W27 (0.1), not vs opus's W28.
    assert "0.100" in current[0]["flag_reason"]
