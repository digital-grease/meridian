"""The 2026-10 roster succession happens on schedule, with no edit on the day.

claude-opus-5-5 succeeds claude-opus-4-8 and claude-opus-5, and
gpt-6-astra succeeds gpt-5.5, each with exactly one overlap week:

  2026-W41  gpt-5.5 + gpt-6-astra                  (+ llama control)
  2026-W42  opus-4-8 + opus-5 + opus-5-5            (+ llama control)
  2026-W43+ gpt-6-astra on odd weeks, opus-5-5 on even weeks

Weeks are run LABELS (scripts/run-weekly.sh labels Monday's run with
the ISO week of the day before), which is what cadence and the
first/last week bounds are evaluated against. The pins here cover every
place that decides the roster: build_runners (orchestrator, run-log
expected_runners / expected_samples, and through those the health
check), the estimate subcommand, and the per-week cost figures quoted
in meridian/BUDGET.md and scripts/run-weekly.sh.
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from meridian.config import PipelineConfig, RunnerSpec, build_runners, load_config
from meridian.pipeline import cli as cli_module
from meridian.sampling.pricing import is_priceable, is_reasoning_default

SHIPPED_ROSTER = {
    "2026-W40": {"llama3.2:3b", "claude-opus-4-8", "claude-opus-5"},
    "2026-W41": {"llama3.2:3b", "gpt-5.5", "gpt-6-astra"},
    "2026-W42": {"llama3.2:3b", "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5"},
    "2026-W43": {"llama3.2:3b", "gpt-6-astra"},
    "2026-W44": {"llama3.2:3b", "claude-opus-5-5"},
    "2026-W45": {"llama3.2:3b", "gpt-6-astra"},
    "2027-W02": {"llama3.2:3b", "claude-opus-5-5"},
}


@pytest.fixture
def _keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("MERIDIAN_SKIP_PROVIDERS", raising=False)


# ---------- RunnerSpec bounds ------------------------------------------


def test_bounds_default_to_unbounded():
    spec = RunnerSpec(provider="ollama", model_id="x")
    assert spec.first_week is None and spec.last_week is None
    assert spec.runs_in_week("1999-W01")
    assert spec.runs_in_week("2099-W53")


def test_bounds_are_inclusive_and_cadence_still_applies():
    spec = RunnerSpec(provider="ollama", model_id="x", cadence="even_weeks",
                      first_week="2026-W42", last_week="2026-W46")
    assert not spec.runs_in_week("2026-W40")
    assert not spec.runs_in_week("2026-W41")
    assert spec.runs_in_week("2026-W42")
    assert not spec.runs_in_week("2026-W43")  # odd: cadence excludes it
    assert spec.runs_in_week("2026-W46")
    assert not spec.runs_in_week("2026-W48")


def test_bounds_cross_the_iso_year():
    spec = RunnerSpec(provider="ollama", model_id="x", first_week="2026-W52",
                      last_week="2027-W01")
    assert not spec.runs_in_week("2026-W51")
    assert spec.runs_in_week("2026-W52")
    assert spec.runs_in_week("2026-W53")
    assert spec.runs_in_week("2027-W01")
    assert not spec.runs_in_week("2027-W02")


@pytest.mark.parametrize(
    "bad", ["2026-W4", "2026-41", "2026-W00", "2026-W54", "26-W41", "2026-w41", ""],
)
def test_malformed_bound_is_rejected(bad):
    with pytest.raises(ValueError):
        RunnerSpec(provider="ollama", model_id="x", first_week=bad)
    with pytest.raises(ValueError):
        RunnerSpec(provider="ollama", model_id="x", last_week=bad)


def test_first_after_last_is_rejected():
    with pytest.raises(ValueError, match="never run"):
        RunnerSpec(provider="ollama", model_id="x",
                   first_week="2026-W43", last_week="2026-W42")


def test_single_week_runner_is_allowed():
    spec = RunnerSpec(provider="ollama", model_id="x",
                      first_week="2026-W42", last_week="2026-W42")
    assert spec.runs_in_week("2026-W42")
    assert not spec.runs_in_week("2026-W43")


def test_unparseable_week_raises_rather_than_comparing_garbage():
    spec = RunnerSpec(provider="ollama", model_id="x", last_week="2026-W42")
    with pytest.raises(ValueError, match="unparseable"):
        spec.runs_in_week("2026-W4")


def test_build_runners_honours_bounds():
    config = PipelineConfig(runners=[
        RunnerSpec(provider="ollama", model_id="old", last_week="2026-W42"),
        RunnerSpec(provider="ollama", model_id="new", first_week="2026-W42"),
    ])
    assert {r.model_id for r in build_runners(config, week_id="2026-W41")} == {"old"}
    assert {r.model_id for r in build_runners(config, week_id="2026-W42")} == {"old", "new"}
    assert {r.model_id for r in build_runners(config, week_id="2026-W43")} == {"new"}
    # No week: the bounds, like cadence, are not applied.
    assert len(build_runners(config, week_id=None)) == 2


# ---------- the shipped config -----------------------------------------


@pytest.mark.parametrize("week", sorted(SHIPPED_ROSTER))
def test_shipped_roster_per_week(_keys, week):
    """The owner's succession table, checked against the runners the
    orchestrator would actually build. expected_runners and
    expected_samples in the run log are keyed over exactly these, so
    2026-W43 cannot "expect" gpt-5.5."""
    got = {r.model_id for r in build_runners(load_config(), week_id=week)}
    assert got == SHIPPED_ROSTER[week]


@pytest.mark.parametrize("week", sorted(SHIPPED_ROSTER))
def test_estimate_prices_the_same_roster_the_run_builds(_keys, week):
    config = load_config()
    built = {(r.provider, r.model_id) for r in build_runners(config, week_id=week)}
    priced = {
        (s.provider, s.model_id)
        for s in cli_module._enabled_specs_for_week(config, week)
    }
    assert priced == built


def test_retired_runners_keep_their_config_entries():
    """The config is part of the record of what ran when."""
    by_id = {s.model_id: s for s in load_config().runners}
    assert by_id["claude-opus-4-8"].last_week == "2026-W42"
    assert by_id["claude-opus-5"].last_week == "2026-W42"
    assert by_id["gpt-5.5"].last_week == "2026-W41"
    assert by_id["claude-opus-5-5"].first_week == "2026-W42"
    assert by_id["gpt-6-astra"].first_week == "2026-W41"
    for model in ("claude-opus-4-8", "claude-opus-5", "gpt-5.5"):
        assert by_id[model].enabled is True


def test_new_models_cadence_and_caps():
    config = load_config()
    by_id = {s.model_id: s for s in config.runners}
    assert by_id["claude-opus-5-5"].provider == "anthropic"
    assert by_id["claude-opus-5-5"].cadence == "even_weeks"
    assert by_id["gpt-6-astra"].provider == "openai"
    assert by_id["gpt-6-astra"].cadence == "odd_weeks"
    # Thinking / reasoning tokens bill against the cap on both, so both
    # need at least the headroom of the model they succeed.
    assert by_id["claude-opus-5-5"].max_tokens >= by_id["claude-opus-5"].max_tokens
    assert by_id["gpt-6-astra"].max_tokens >= by_id["gpt-5.5"].max_tokens


def test_stance_classifier_is_unchanged():
    stance = load_config().stance
    assert stance.provider == "anthropic"
    assert stance.model_id == "claude-haiku-4-5-20251001"


def test_every_shipped_runner_is_priced():
    """An unpriced model disarms --max-cost for itself (the run refuses
    to start), so a new roster entry must come with its price."""
    for spec in load_config().runners:
        if spec.enabled:
            assert is_priceable(spec.provider, spec.model_id), spec.model_id


def test_new_models_are_priced_as_reasoning_default():
    assert is_reasoning_default("anthropic", "claude-opus-5-5")
    assert is_reasoning_default("openai", "gpt-6-astra")


# ---------- per-week cost figures ----------------------------------------

#: The figures quoted in meridian/BUDGET.md, the 2026-10-06 roster notice
#: and the run-weekly.sh ceiling comment. If this moves, update them.
EXPECTED_RUN_TOTALS = {
    "2026-W41": "61.11",
    "2026-W42": "42.73",
    "2026-W43": "38.20",
    "2026-W44": "15.28",
}


@pytest.mark.parametrize("week", sorted(EXPECTED_RUN_TOTALS))
def test_run_total_estimate_per_week(capsys, week):
    rc = cli_module._cmd_estimate(
        argparse.Namespace(config=None, week=week, run_total=True)
    )
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    # Machine-readable: run-weekly.sh reads the last stdout line.
    assert out[-1] == EXPECTED_RUN_TOTALS[week]


def test_run_total_matches_what_run_prices(_keys, monkeypatch, tmp_path, capsys):
    """run-weekly.sh derives --max-cost from --run-total, and `run`
    checks that ceiling against its own estimate. The two must agree."""
    monkeypatch.setattr(cli_module, "REPO_ROOT", tmp_path)
    week = "2026-W42"
    cli_module._cmd_estimate(argparse.Namespace(config=None, week=week, run_total=True))
    run_total = capsys.readouterr().out.strip().splitlines()[-1]

    import asyncio

    ns = argparse.Namespace(config=None, week=week, force=False, yes=True,
                            dry_run=True, max_cost=None)
    assert asyncio.run(cli_module._cmd_run(ns)) == 0
    printed = capsys.readouterr().out
    assert f"${run_total}" in printed


# ---------- request shape of the new models ------------------------------


def test_opus_5_5_request_carries_no_thinking_effort_sampling_or_fallbacks():
    from meridian.runners.anthropic import (
        _anthropic_supports_temperature,
        _build_message_kwargs,
    )

    kwargs = _build_message_kwargs(
        model_id="claude-opus-5-5", prompt="p", temperature=1.0, max_tokens=8192,
    )
    assert set(kwargs) == {"model", "max_tokens", "messages"}
    assert not _anthropic_supports_temperature("claude-opus-5-5", 0.0)
    assert _anthropic_supports_temperature("claude-opus-5-5", 1.0)


def test_gpt_6_family_treated_like_gpt_5_5():
    from meridian.runners.openai import _openai_supports_temperature, _token_kwarg_for

    for mid in ("gpt-6-astra", "gpt-6", "gpt-6-astra-2026-10-01"):
        assert _token_kwarg_for(mid) == "max_completion_tokens"
        assert not _openai_supports_temperature(mid, 0.0)
        assert _openai_supports_temperature(mid, 1.0)


@pytest.mark.parametrize(
    "model_id, sends_temperature",
    [("gpt-6-astra", False), ("gpt-5.5", False), ("o3-mini", False), ("gpt-4o", True)],
)
def test_openai_request_omits_temperature_for_default_only_families(
    model_id, sends_temperature,
):
    """The default batch must not send ``temperature`` to gpt-6-astra.

    Its parameter support is undocumented; an explicit value that it
    rejects would 400 every request of the debut week. The sample still
    records the intended 1.0.
    """
    import asyncio

    from meridian.runners.openai import OpenAIRunner

    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            model=model_id,
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok"), finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
            id="chatcmpl-1",
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    runner = OpenAIRunner(model_id, client=client)
    sample = asyncio.run(runner.sample(
        "p", prompt_id="x", request_index=0, temperature=1.0, max_tokens=8192,
    ))

    (kwargs,) = calls
    assert ("temperature" in kwargs) is sends_temperature
    if model_id.startswith(("gpt-6", "gpt-5.5", "o3")):
        assert kwargs["max_completion_tokens"] == 8192
        assert "max_tokens" not in kwargs
    assert sample.temperature == 1.0


def test_refusal_category_is_recorded_as_a_safety_flag():
    from meridian.runners.anthropic import _safety_flags

    refused = SimpleNamespace(
        stop_reason="refusal",
        stop_details=SimpleNamespace(type="refusal", category="cyber", explanation="x"),
    )
    assert _safety_flags(refused) == ["refusal_category:cyber"]
    as_dict = SimpleNamespace(stop_reason="refusal", stop_details={"category": "bio"})
    assert _safety_flags(as_dict) == ["refusal_category:bio"]
    no_category = SimpleNamespace(stop_reason="refusal", stop_details=None)
    assert _safety_flags(no_category) == ["refusal_category:unspecified"]
    ordinary = SimpleNamespace(stop_reason="end_turn", stop_details=None)
    assert _safety_flags(ordinary) == []
