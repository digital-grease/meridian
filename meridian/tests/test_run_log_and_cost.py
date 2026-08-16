"""Run log persistence + actual-cost computation + the --max-cost ceiling.

The ceiling cases cover the gap 2026-W33 opened: it is the first
production run of gpt-5.5 at ``max_tokens: 8192``, the cap is what
bounds spend on a reasoning-default model, and ``scripts/run-weekly.sh``
runs unattended with ``--yes``. Worst case is roughly 600 calls x 8192
tokens x $30/MM, about $147, against a $5.39 historical actual. Nothing
in the pipeline used to stop that, and ``--yes`` waived the only
cost-related prompt there was.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from meridian.config import PipelineConfig, RunnerSpec, SamplingSpec
from meridian.pipeline import cli as cli_module
from meridian.pipeline.run_log import append_run_log, read_run_log
from meridian.runners.base import Runner, RunnerError, Sample
from meridian.sampling.cost import (
    BudgetExceeded,
    BudgetLedger,
    compute_actual_cost,
    guard_runners,
    sample_cost_usd,
)
from meridian.sampling.orchestrator import PairError, RunOutcome
from meridian.storage import LocalSampleStore


def _cfg():
    return PipelineConfig(
        sampling=SamplingSpec(),
        runners=[
            RunnerSpec(provider="ollama", model_id="llama3.2:3b", enabled=True),
            RunnerSpec(provider="anthropic", model_id="claude-haiku-4-5-20251001", enabled=False),
        ],
    )


def _outcome():
    return RunOutcome(
        week_id="2026-W16",
        total_samples_written=42,
        pairs_complete=3,
        pairs_skipped=1,
        pairs_failed=1,
        per_runner_samples={"ollama/llama3.2:3b": 42},
        errors=[
            PairError(provider="anthropic", model_id="claude",
                      prompt_id="pol-x", error_type="RateLimitError", message="quota"),
        ],
    )


def test_run_log_roundtrip(tmp_path: Path):
    log = tmp_path / "run_log.jsonl"
    started = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
    finished = datetime(2026, 4, 19, 0, 15, tzinfo=timezone.utc)
    entry = append_run_log(
        log,
        started_at=started, finished_at=finished,
        week_id="2026-W16",
        config=_cfg(),
        outcome=_outcome(),
        estimated_cost_usd=3.00,
        actual_cost_usd=2.87,
        note="first real run",
    )
    assert entry.week_id == "2026-W16"
    assert entry.pairs_failed == 1

    entries = read_run_log(log)
    assert len(entries) == 1
    got = entries[0]
    assert got.note == "first real run"
    assert got.actual_cost_usd == 2.87
    assert got.per_runner_samples == {"ollama/llama3.2:3b": 42}
    assert len(got.errors) == 1


def test_run_log_append_preserves_prior(tmp_path: Path):
    log = tmp_path / "run_log.jsonl"
    for wk in ("2026-W15", "2026-W16"):
        append_run_log(
            log,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            week_id=wk,
            config=_cfg(),
            outcome=_outcome(),
            estimated_cost_usd=1.0,
            actual_cost_usd=1.0,
        )
    entries = read_run_log(log)
    assert [e.week_id for e in entries] == ["2026-W15", "2026-W16"]


# --- cost ---


def _s(provider: str, model_id: str, in_tokens: int | None, out_tokens: int | None) -> Sample:
    return Sample(
        prompt_id="p", model_id=model_id, provider=provider,
        request_index=0, temperature=1.0, max_tokens=1024,
        text="ok", model_version_string="v",
        input_tokens=in_tokens, output_tokens=out_tokens,
        latency_ms=1, captured_at=datetime.now(timezone.utc),
    )


def test_cost_sum_matches_pricing():
    samples = [
        _s("anthropic", "claude-haiku-4-5-20251001", 1_000_000, 1_000_000),
        _s("openai",    "gpt-4.1-mini",            1_000_000, 1_000_000),
        _s("ollama",    "llama3.2:3b",             1_000_000, 1_000_000),
    ]
    report = compute_actual_cost(samples)
    # Claude Haiku: 0.80 + 4.00 = 4.80
    # GPT-4.1-mini: 0.15 + 0.60 = 0.75
    # Ollama: 0
    assert report.total_usd == 5.55
    assert report.samples_priced == 3
    assert report.samples_skipped_no_tokens == 0
    assert report.by_runner["anthropic/claude-haiku-4-5-20251001"] == 4.80
    assert report.by_runner["openai/gpt-4.1-mini"] == 0.75


def test_cost_skips_samples_missing_tokens():
    samples = [
        _s("anthropic", "claude-haiku-4-5-20251001", None, 100),
        _s("anthropic", "claude-haiku-4-5-20251001", 100, None),
        _s("anthropic", "claude-haiku-4-5-20251001", 100, 100),
    ]
    report = compute_actual_cost(samples)
    assert report.samples_skipped_no_tokens == 2
    assert report.samples_priced == 1
    assert report.total_usd > 0.0


def test_cost_unknown_model_treated_as_free():
    samples = [_s("openai", "unknown-model-xyz", 100_000, 100_000)]
    report = compute_actual_cost(samples)
    assert report.total_usd == 0.0
    assert report.samples_priced == 1


# --- budget ledger ---


def test_sample_cost_distinguishes_free_from_unpriceable():
    """``None`` means the sample cannot be priced, which is not the same
    as a genuine $0.00. A ceiling that cannot tell those apart silently
    stops watching."""
    assert sample_cost_usd(_s("ollama", "llama3.2:3b", 1000, 1000)) == 0.0
    assert sample_cost_usd(_s("anthropic", "claude-opus-4-8", None, 1000)) is None


def test_sample_cost_is_none_for_a_model_with_no_price_on_file():
    """This used to return 0.00 ("unknown model; treat as free"), which
    disarmed --max-cost entirely for that runner: the ledger charged
    nothing, never tripped, and the run billed real money against a
    ceiling watching an empty tally. PRICING is exact-match apart from
    the self-hosted wildcard, so this is what a roster addition or a
    point release looks like on its first week."""
    assert sample_cost_usd(_s("google", "gemini-3-pro", 1000, 1000)) is None
    assert sample_cost_usd(_s("openai", "gpt-5.6", 1000, 1000)) is None
    # A self-hosted model that is not in the table is still genuinely
    # free, and must not be mistaken for a blind spot: the local control
    # group runs every week and a ceiling has to let it through.
    assert sample_cost_usd(_s("ollama", "qwen3:8b-never-listed", 1000, 1000)) == 0.0


def test_ledger_counts_an_unpriced_model_separately_from_missing_tokens():
    """Different causes, different fixes: one is a provider going quiet
    on usage reporting, the other is a roster addition nobody priced."""
    ledger = BudgetLedger(ceiling_usd=1.00)
    ledger.charge(_s("google", "gemini-3-pro", 1000, 1_000_000))
    assert ledger.spent_usd == 0.0
    assert ledger.calls_charged == 0
    assert ledger.calls_no_price == 1
    assert ledger.calls_missing_tokens == 0
    assert ledger.calls_unpriced == 1
    assert "no price on file" in ledger.pretty()
    # And the ceiling stays open, because there is nothing to charge:
    # this is precisely why the CLI refuses to start such a run.
    ledger.check()


def test_ledger_accumulates_and_trips_at_the_ceiling():
    ledger = BudgetLedger(ceiling_usd=1.00)
    ledger.check()  # under budget: no raise
    # 1M output tokens of Opus at $25/MM = $25.00, well past the ceiling.
    charged = ledger.charge(_s("anthropic", "claude-opus-4-8", 0, 1_000_000))
    assert charged == pytest.approx(25.0)
    assert ledger.spent_usd == pytest.approx(25.0)
    assert ledger.calls_charged == 1
    assert not ledger.tripped

    with pytest.raises(BudgetExceeded):
        ledger.check()
    assert ledger.tripped
    assert ledger.tripped_at_usd == pytest.approx(25.0)


def test_ledger_counts_samples_without_token_counts_separately():
    """A run whose provider stops reporting usage must not read as a
    cheap run; the operator needs to see the ceiling went blind."""
    ledger = BudgetLedger(ceiling_usd=1.00)
    ledger.charge(_s("anthropic", "claude-opus-4-8", None, None))
    assert ledger.spent_usd == 0.0
    assert ledger.calls_charged == 0
    assert ledger.calls_unpriced == 1
    assert "no token counts" in ledger.pretty()


def test_zero_ceiling_still_lets_free_models_run():
    """`--max-cost 0` reads as "free calls only". It must not stop the
    ollama control group, which runs every week precisely because it
    costs nothing and the silent-update detector needs it continuous."""
    ledger = BudgetLedger(ceiling_usd=0.0)
    for _ in range(3):
        ledger.check()
        ledger.charge(_s("ollama", "llama3.2:3b", 1000, 1000))
    assert not ledger.tripped
    assert ledger.spent_usd == 0.0

    # The first charged dollar under that ceiling stops the run.
    ledger.charge(_s("anthropic", "claude-opus-4-8", 0, 1_000_000))
    with pytest.raises(BudgetExceeded):
        ledger.check()


def test_budget_exceeded_is_a_runner_error():
    """The orchestrator only catches RunnerError per pair. If this stops
    being one, a budget stop kills the process mid-write instead of
    failing the remaining pairs for free."""
    assert issubclass(BudgetExceeded, RunnerError)


# --- guarded runner ---


class _PricedFakeRunner(Runner):
    """Fake runner wearing a real, priced model identity.

    ``output_tokens`` is the knob that makes actual spend diverge from
    the pre-flight estimate, which is the exact failure the in-run
    ceiling exists for: the estimate is a heuristic and was blind to
    max_tokens until 2026-08.
    """
    provider = "anthropic"

    def __init__(self, model_id: str = "claude-opus-4-8", *, output_tokens: int = 200):
        self.model_id = model_id
        self.output_tokens = output_tokens
        self.calls = 0

    async def sample(self, prompt, *, prompt_id, request_index, temperature, max_tokens=1024):
        self.calls += 1
        return Sample(
            prompt_id=prompt_id,
            model_id=self.model_id,
            provider=self.provider,
            request_index=request_index,
            temperature=temperature,
            max_tokens=max_tokens,
            text="This is a substantive answer.",
            model_version_string=f"{self.model_id}-2026-08-15",
            stop_reason="end_turn",
            latency_ms=1,
            captured_at=datetime.now(timezone.utc),
            input_tokens=100,
            output_tokens=self.output_tokens,
        )


def test_guard_preserves_runner_identity():
    """Storage, the manifest, and the estimator all key off these."""
    inner = _PricedFakeRunner()
    inner.max_tokens_override = 8192
    (guarded,), _ledger = guard_runners([inner], ceiling_usd=10.0)
    assert guarded.provider == "anthropic"
    assert guarded.model_id == "claude-opus-4-8"
    assert guarded.max_tokens_override == 8192


def test_guard_shares_one_ledger_across_runners():
    """The ceiling is a limit on the run, not per provider: one
    unexpectedly expensive model must stop the whole run rather than
    leave the others the rest of the budget."""
    a, b = _PricedFakeRunner(), _PricedFakeRunner("claude-opus-4-7")
    (ga, gb), ledger = guard_runners([a, b], ceiling_usd=10.0)
    assert ga.ledger is ledger and gb.ledger is ledger


def test_guard_refuses_calls_once_the_ceiling_is_spent():
    inner = _PricedFakeRunner(output_tokens=20_000)  # $0.50 per sample
    (guarded,), ledger = guard_runners([inner], ceiling_usd=1.00)

    async def _drive():
        out = []
        for i in range(6):
            try:
                out.append(await guarded.sample(
                    "q", prompt_id="p", request_index=i, temperature=1.0,
                    max_tokens=8192,
                ))
            except BudgetExceeded:
                break
        return out

    captured = asyncio.run(_drive())
    # Two samples at $0.5005 cross $1.00, so the third call is refused.
    assert len(captured) == 2
    assert inner.calls == 2, "a refused call must never reach the provider"
    assert ledger.tripped
    assert ledger.spent_usd == pytest.approx(1.001)


# --- CLI ceiling integration ---


@pytest.fixture
def _isolated_repo(tmp_path: Path, monkeypatch) -> Path:
    """REPO_ROOT-pinned paths point into a tmpdir so the real
    ``data/run_log.jsonl`` and ``site/fixtures/`` are untouched."""
    (tmp_path / "data").mkdir()
    (tmp_path / "site" / "fixtures").mkdir(parents=True)
    monkeypatch.setattr(cli_module, "REPO_ROOT", tmp_path)
    return tmp_path


def _ceiling_config(path: Path) -> Path:
    """Two prompts x 3 samples of a priced, reasoning-default model at
    the 8192 cap: the shape of the 2026-W33 gpt-5.5 run, shrunk. The
    pre-flight estimate for this comes to about $0.19.
    """
    cfg = {
        "sampling": {
            "n_default_temp": 2,
            "n_zero_temp": 1,
            "max_tokens": 8192,
            "concurrency_per_provider": 2,
        },
        "storage": {"raw_dir": "data/raw"},
        "runners": [
            {"provider": "anthropic", "model_id": "claude-opus-4-8",
             "enabled": True, "cadence": "every_week"},
        ],
    }
    config_path = path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    return config_path


def _install_fakes(monkeypatch, runner: Runner) -> None:
    """Swap in the fake runner and a two-prompt corpus. No network, and
    no dependence on the real corpus staying 30 prompts long."""
    from meridian import config as config_module
    from meridian.corpus import load_corpus

    def _fake_build_runners(cfg, *, week_id=None):
        return [runner]

    monkeypatch.setattr(config_module, "build_runners", _fake_build_runners)
    monkeypatch.setattr(cli_module, "build_runners", _fake_build_runners)

    full = load_corpus()

    class _TrimmedCorpus:
        def __init__(self, inner):
            self._inner = inner
            self._subset = inner.public()[:2]

        def public(self):
            return self._subset

        def all(self):
            return self._subset

        def by_id(self, pid):
            return self._inner.by_id(pid)

        def by_axis(self, axis):
            return [p for p in self._subset if p.axis == axis]

        @property
        def has_held_out(self):
            return False

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(cli_module, "load_corpus", lambda: _TrimmedCorpus(full))


def _run_args(config_path: Path, **overrides) -> argparse.Namespace:
    ns = {
        "config": config_path, "week": "2026-W33", "force": False,
        "yes": True, "dry_run": False, "max_cost": None,
    }
    ns.update(overrides)
    return argparse.Namespace(**ns)


def _stored_sample_count(repo: Path, week_id: str) -> int:
    store = LocalSampleStore(repo / "data" / "raw")
    return sum(
        store.count(week_id, model_id, prompt_id)
        for model_id in store.models_for_week(week_id)
        for prompt_id in store.prompts_for(week_id, model_id)
    )


def test_yes_does_not_bypass_max_cost(_isolated_repo: Path, monkeypatch, capsys):
    """The whole point of the flag: run-weekly.sh passes --yes for
    unattended operation, and --yes must not waive the ceiling."""
    config_path = _ceiling_config(_isolated_repo)
    runner = _PricedFakeRunner()
    _install_fakes(monkeypatch, runner)

    rc = asyncio.run(cli_module._cmd_run(
        _run_args(config_path, yes=True, max_cost=0.05)
    ))
    assert rc == 2
    err = capsys.readouterr().err
    assert "--max-cost" in err
    assert runner.calls == 0, "aborted run must not have sampled anything"
    assert not (_isolated_repo / "data" / "run_log.jsonl").exists()


def test_ceiling_refuses_a_runner_with_no_price_on_file(
    _isolated_repo: Path, monkeypatch, capsys,
):
    """A ceiling that cannot see a runner is not a ceiling.

    An unpriced model books $0.00 in the pre-flight estimate and charges
    $0.00 in the in-run ledger, so both gates go quiet for it while it
    bills real money. That is a live risk, not a hypothetical: PRICING is
    exact-match apart from the self-hosted wildcard, so every roster
    addition on the CLAUDE.md roadmap and every point release lands here
    first.
    """
    config_path = _ceiling_config(_isolated_repo)
    runner = _PricedFakeRunner()
    runner.provider = "google"
    runner.model_id = "gemini-3-pro"
    _install_fakes(monkeypatch, runner)

    rc = asyncio.run(cli_module._cmd_run(
        _run_args(config_path, yes=True, max_cost=40.00)
    ))
    assert rc == 2
    err = capsys.readouterr().err
    assert "google/gemini-3-pro" in err
    assert "PRICING" in err
    assert runner.calls == 0, "the run must not start under a blind ceiling"


def test_unpriced_runner_without_a_ceiling_still_runs(
    _isolated_repo: Path, monkeypatch,
):
    """The refusal is about the ceiling, not about the model. Omitting
    --max-cost is an explicit choice to run unbounded, and an unpriced
    provider must not become a reason the weekly run stops firing."""
    config_path = _ceiling_config(_isolated_repo)
    runner = _PricedFakeRunner()
    runner.provider = "google"
    runner.model_id = "gemini-3-pro"
    _install_fakes(monkeypatch, runner)

    assert asyncio.run(cli_module._cmd_run(
        _run_args(config_path, max_cost=None)
    )) == 0
    assert runner.calls == 6


def test_preflight_prices_the_corpus_the_run_will_sample(
    _isolated_repo: Path, monkeypatch,
):
    """`run` prices corpus.all(), which is what the orchestrator samples,
    not corpus.public(). They are the same 30 prompts today; once the
    CLAUDE.md 30% held-out split lands, pricing the public subset would
    under-count the run by the held-out fraction and pass a --max-cost
    ceiling that should have blocked it."""
    config_path = _ceiling_config(_isolated_repo)
    runner = _PricedFakeRunner()
    _install_fakes(monkeypatch, runner)

    # Two public prompts plus two held-out, which is four pairs' worth of
    # work regardless of what the public site ends up showing.
    corpus = cli_module.load_corpus()
    public = corpus.public()

    class _HeldOutCorpus:
        def public(self):
            return public

        def all(self):
            return public + public

        @property
        def has_held_out(self):
            return False

        def __getattr__(self, name):
            return getattr(corpus, name)

    monkeypatch.setattr(cli_module, "load_corpus", lambda: _HeldOutCorpus())
    # The public-only estimate for this config is about $0.19; doubling
    # the prompt count doubles it, and only the corpus.all() figure trips
    # a ceiling set between the two.
    rc = asyncio.run(cli_module._cmd_run(
        _run_args(config_path, yes=True, dry_run=True, max_cost=0.30)
    ))
    assert rc == 2, "held-out prompts must count toward the pre-flight estimate"
    assert asyncio.run(cli_module._cmd_run(
        _run_args(config_path, yes=True, dry_run=True, max_cost=0.60)
    )) == 0


def test_estimate_under_ceiling_proceeds(_isolated_repo: Path, monkeypatch):
    """A ceiling above the estimate must not block the run, otherwise
    Monday's production run never fires."""
    config_path = _ceiling_config(_isolated_repo)
    runner = _PricedFakeRunner()
    _install_fakes(monkeypatch, runner)

    rc = asyncio.run(cli_module._cmd_run(
        _run_args(config_path, yes=True, max_cost=40.00)
    ))
    assert rc == 0
    assert runner.calls == 6
    entries = read_run_log(_isolated_repo / "data" / "run_log.jsonl")
    assert len(entries) == 1
    assert entries[0].pairs_failed == 0
    assert entries[0].note is None


def test_no_ceiling_leaves_the_run_unguarded(_isolated_repo: Path, monkeypatch):
    """Omitting --max-cost keeps the historical behaviour exactly."""
    config_path = _ceiling_config(_isolated_repo)
    runner = _PricedFakeRunner(output_tokens=20_000)
    _install_fakes(monkeypatch, runner)

    rc = asyncio.run(cli_module._cmd_run(_run_args(config_path, max_cost=None)))
    assert rc == 0
    assert runner.calls == 6


def test_dry_run_with_ceiling_is_a_preflight_validator(
    _isolated_repo: Path, monkeypatch,
):
    """--dry-run --max-cost N answers "would Monday's run be blocked?"
    without spending anything either way."""
    config_path = _ceiling_config(_isolated_repo)
    runner = _PricedFakeRunner()
    _install_fakes(monkeypatch, runner)

    assert asyncio.run(cli_module._cmd_run(
        _run_args(config_path, dry_run=True, max_cost=0.05)
    )) == 2
    assert asyncio.run(cli_module._cmd_run(
        _run_args(config_path, dry_run=True, max_cost=40.00)
    )) == 0
    assert runner.calls == 0


def test_mid_run_ceiling_stops_spending_but_keeps_the_data(
    _isolated_repo: Path, monkeypatch, capsys,
):
    """The estimate is exactly the thing that has proven unreliable, so
    the ceiling is enforced a second time against real spend.

    A run that passes pre-flight at ~$0.19 but bills $0.50 per sample
    must stop, and must still leave the run-log entry and every captured
    sample behind: retention is append-only and forever, and a budget
    stop may never cost data already paid for.
    """
    config_path = _ceiling_config(_isolated_repo)
    runner = _PricedFakeRunner(output_tokens=20_000)  # $0.50 per sample
    _install_fakes(monkeypatch, runner)

    rc = asyncio.run(cli_module._cmd_run(
        _run_args(config_path, yes=True, max_cost=1.00)
    ))
    assert rc == 2, "a budget stop must not report as a clean or partial run"
    assert runner.calls == 2, "no provider call may be made past the ceiling"

    err = capsys.readouterr().err
    assert "BUDGET CEILING HIT" in err

    log_path = _isolated_repo / "data" / "run_log.jsonl"
    assert log_path.exists(), "a partial run must still write its receipt"
    entry = read_run_log(log_path)[0]
    assert entry.total_samples_written == 2
    assert entry.pairs_failed == 2
    assert entry.note is not None and "--max-cost" in entry.note
    assert entry.actual_cost_usd == pytest.approx(1.001, abs=0.001)

    assert _stored_sample_count(_isolated_repo, "2026-W33") == 2, (
        "captured samples must survive the abort"
    )
    manifest = _isolated_repo / "data" / "manifests" / "2026-W33.json"
    assert manifest.exists(), "artifacts are still written after a budget stop"
