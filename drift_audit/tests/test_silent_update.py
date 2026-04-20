"""Silent-update detector tests."""
from __future__ import annotations

from drift_audit.analysis.silent_update import detect_silent_updates
from drift_audit.corpus.corpus import Prompt


def _prompts():
    return [
        Prompt(id="neut-1", axis="neutral-control", title="t", text="x"),
        Prompt(id="neut-2", axis="neutral-control", title="t", text="x"),
        Prompt(id="pol-1",  axis="political", title="t", text="x"),
    ]


def _metric(prompt_id, model_id, *, refusal=0.0, hedge=0.3, median=200.0):
    return {
        "prompt_id": prompt_id, "model_id": model_id,
        "refusal_rate": refusal, "hedge_density": hedge,
        "length": {"median": median, "p25": median, "p75": median, "n": 20},
    }


def _manifest(current_metrics, history_metrics_by_week):
    return {
        "snapshot": {"week_id": "2026-W16"},
        "metrics": current_metrics,
        "history": [
            {"week_id": w, "generated_at": "2026-04-12T00:00:00Z", "metrics": m}
            for w, m in history_metrics_by_week
        ],
    }


def test_stable_neutral_axis_no_flags():
    stable = [
        _metric("neut-1", "m-1"),
        _metric("neut-2", "m-1"),
    ]
    manifest = _manifest(
        current_metrics=stable,
        history_metrics_by_week=[("2026-W15", stable), ("2026-W14", stable)],
    )
    flags = detect_silent_updates(manifest=manifest, prompts=_prompts())
    assert flags == []


def test_big_refusal_jump_on_neutral_flagged():
    stable = [_metric("neut-1", "m-1"), _metric("neut-2", "m-1")]
    shifted = [
        _metric("neut-1", "m-1", refusal=0.4),
        _metric("neut-2", "m-1", refusal=0.6),
    ]
    manifest = _manifest(
        current_metrics=shifted,
        history_metrics_by_week=[("2026-W15", stable)],
    )
    flags = detect_silent_updates(manifest=manifest, prompts=_prompts())
    assert any(f.metric == "refusal_rate" for f in flags)
    flag = next(f for f in flags if f.metric == "refusal_rate")
    assert flag.model_id == "m-1"
    assert flag.from_week == "2026-W15"
    assert flag.to_week == "2026-W16"
    assert flag.severity in ("medium", "high")


def test_shift_on_non_target_axis_ignored():
    stable_neut = [_metric("neut-1", "m-1"), _metric("neut-2", "m-1")]
    # Political axis shifts dramatically, neutral stays stable.
    with_political_shift = [
        *stable_neut,
        _metric("pol-1", "m-1", refusal=0.9),
    ]
    manifest = _manifest(
        current_metrics=with_political_shift,
        history_metrics_by_week=[
            ("2026-W15", [*stable_neut, _metric("pol-1", "m-1", refusal=0.0)]),
        ],
    )
    flags = detect_silent_updates(manifest=manifest, prompts=_prompts())
    # Default axis is neutral-control; political shifts do not trigger it.
    assert flags == []


def test_only_affected_model_flagged():
    stable_m1 = [_metric("neut-1", "m-1"), _metric("neut-2", "m-1")]
    stable_m2 = [_metric("neut-1", "m-2"), _metric("neut-2", "m-2")]
    cur_m1 = [_metric("neut-1", "m-1", hedge=2.0), _metric("neut-2", "m-1", hedge=2.5)]
    manifest = _manifest(
        current_metrics=cur_m1 + stable_m2,
        history_metrics_by_week=[("2026-W15", stable_m1 + stable_m2)],
    )
    flags = detect_silent_updates(manifest=manifest, prompts=_prompts())
    models_flagged = {f.model_id for f in flags}
    assert models_flagged == {"m-1"}


def test_single_week_no_transition_no_flags():
    current = [_metric("neut-1", "m-1"), _metric("neut-2", "m-1")]
    manifest = _manifest(current_metrics=current, history_metrics_by_week=[])
    flags = detect_silent_updates(manifest=manifest, prompts=_prompts())
    assert flags == []
