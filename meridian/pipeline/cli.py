"""Meridian pipeline CLI.

Subcommands:
  run              Sample from every enabled runner for a week, persist to
                   storage, and write a Manifest JSON the site can render.
  estimate         Show the pre-flight cost estimate without sampling.
  build-manifest   Rebuild the site manifest from existing storage (no new
                   sampling). Useful when metric logic changed but samples
                   did not.

Run via::

    uv run python -m meridian.pipeline.cli run --week 2026-W16
    uv run python -m meridian.pipeline.cli estimate
    uv run python -m meridian.pipeline.cli build-manifest --week 2026-W16

Cost ceiling
------------
``run --max-cost USD`` is a hard limit that ``--yes`` does not waive.
``--yes`` exists so ``scripts/run-weekly.sh`` can run unattended, which
also means nobody is watching the estimate it prints. The ceiling is
enforced twice: once before sampling against the pre-flight estimate,
and again during the run against money actually spent, because the
estimate is the part that has historically been wrong (it ignored
``max_tokens`` entirely until 2026-08). Either stop exits 2 with the
run-log entry and every captured sample intact.

The two gates read the same ``--max-cost`` number but not the same
quantity: the pre-flight one compares against an estimate that is
deliberately conservative (see :mod:`meridian.sampling.pricing`), while
the in-run one compares against money actually billed. The pre-flight
gate is therefore the tighter of the two. A third condition also stops
the run outright: a ceiling given while an enabled runner has no price
on file, since neither gate can see that runner's spend.

Exit codes:
  0  success / everything completed cleanly
  1  partial failure (some pairs failed; manifest still written)
  2  fatal error (config, auth, storage), or a --max-cost ceiling stop
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import json

from meridian.analysis.holdout_compare import compare_holdout
from meridian.analysis.silent_update import detect_silent_updates
from meridian.config import PipelineConfig, build_runners, load_config
from meridian.corpus import load_corpus
from meridian.secrets import resolve_ssm_secrets
from meridian.pipeline.manifest_writer import (
    RunnerDisplayInfo,
    build_manifest,
    write_manifest,
)
from meridian.pipeline.embedding_loader import build_embedding_model
from meridian.pipeline.run_log import append_run_log, read_run_log
from meridian.pipeline.snapshot import emit_responses_snapshot, snapshot_path
from meridian.pipeline.stance_collect import collect_stance_results
from meridian.pipeline.stance_runner import build_stance_classifier
from meridian.sampling.cost import BudgetLedger, compute_actual_cost, guard_runners
from meridian.sampling.orchestrator import Orchestrator, SamplingPlan
from meridian.sampling.pricing import TemperaturePlan, estimate_cost
from meridian.sampling.weeks import iso_week_for
from meridian.storage import LocalSampleStore
from meridian.storage.s3 import maybe_build_uploader

_log = logging.getLogger("meridian")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_week(week: str | None) -> str:
    return week or iso_week_for()


def _display_info_for(config: PipelineConfig) -> dict[str, RunnerDisplayInfo]:
    return {
        spec.model_id: RunnerDisplayInfo(
            model_id=spec.model_id,
            display_name=spec.model_id,  # refine via a future display_name field
            provider=spec.provider,
        )
        for spec in config.runners if spec.enabled
    }


def _output_paths(week_id: str) -> list[Path]:
    return [
        REPO_ROOT / "site" / "fixtures" / f"manifest-{week_id}.json",
        REPO_ROOT / "data" / "manifests" / f"{week_id}.json",
    ]


def _internal_manifest_path(week_id: str) -> Path:
    return REPO_ROOT / "data" / "internal" / f"manifest-with-heldout-{week_id}.json"


def _temperature_plan(spec) -> TemperaturePlan:
    """Lift the four temperature/batch-size fields the estimator needs.

    Accepts either a :class:`~meridian.sampling.orchestrator.SamplingPlan`
    or a :class:`~meridian.config.SamplingSpec`; they spell these fields
    the same way. Without it the estimate prices the zero-temp batch for
    models that reject temperature=0 and the orchestrator never sends,
    which on the current roster is a 25% over-count.
    """
    return TemperaturePlan(
        n_default_temp=spec.n_default_temp,
        default_temperature=spec.default_temperature,
        n_zero_temp=spec.n_zero_temp,
        zero_temperature=spec.zero_temperature,
    )


def _build_context(
    args: argparse.Namespace,
    *,
    week_id: str | None = None,
    need_runners: bool = True,
) -> tuple[PipelineConfig, LocalSampleStore, list]:
    config = load_config(args.config)
    if not config.runners:
        print("no runners configured", file=sys.stderr)
        raise SystemExit(2)
    raw_dir = REPO_ROOT / config.storage.raw_dir
    store = LocalSampleStore(raw_dir)
    if not need_runners:
        return config, store, []
    runners = build_runners(config, week_id=week_id)
    if not runners:
        if week_id is not None:
            print(f"no runners scheduled for {week_id}", file=sys.stderr)
        else:
            print("no enabled runners", file=sys.stderr)
        raise SystemExit(2)
    return config, store, runners


async def _cmd_run(args: argparse.Namespace) -> int:
    week_id = _resolve_week(args.week)
    config, store, runners = _build_context(args, week_id=week_id)
    corpus = load_corpus()
    plan = SamplingPlan(
        week_id=week_id,
        n_default_temp=config.sampling.n_default_temp,
        n_zero_temp=config.sampling.n_zero_temp,
        default_temperature=config.sampling.default_temperature,
        zero_temperature=config.sampling.zero_temperature,
        max_tokens=config.sampling.max_tokens,
        concurrency_per_provider=config.sampling.concurrency_per_provider,
    )

    # Priced on corpus.all(), not corpus.public(): the orchestrator
    # samples every prompt including the held-out set, and the estimate
    # has to price the work that will actually be done. Those are the
    # same 30 prompts today, but the moment CLAUDE.md's 30% held-out
    # split lands, pricing the public subset would under-count the run
    # by the held-out fraction and let a --max-cost ceiling pass a run
    # it should have blocked, which is the direction that costs money.
    est = estimate_cost(
        runners,
        n_prompts=len(corpus.all()),
        samples_per_pair=plan.samples_per_pair,
        default_max_tokens=plan.max_tokens,
        temperature_plan=_temperature_plan(plan),
    )
    print(est.pretty())

    # Read defensively: callers that build the Namespace by hand (the
    # run-log integration tests do) predate this flag, and a missing
    # attribute must mean "no ceiling", never a crash mid-pipeline.
    max_cost = getattr(args, "max_cost", None)

    # A ceiling that cannot see one of the runners is not a ceiling. An
    # unpriced model books $0.00 in the estimate and charges $0.00 in
    # the in-run ledger, so BOTH gates go quiet for it while it bills
    # real money. Refuse rather than warn: --yes means nobody is reading
    # the warning, and the fix is one line in pricing.PRICING.
    if max_cost is not None and est.unpriced:
        print(
            "ABORT: --max-cost was given but "
            + ", ".join(est.unpriced)
            + " has no entry in meridian.sampling.pricing.PRICING. A ceiling "
            "cannot bound spend it cannot compute, so neither the pre-flight "
            "estimate nor the in-run ledger would see this runner at all. "
            "Add its published input/output rate to PRICING (dated, as the "
            "others are), or drop --max-cost to run explicitly unbounded. "
            "Nothing was sampled and nothing was written.",
            file=sys.stderr,
        )
        return 2

    # Pre-flight ceiling. Checked BEFORE the --dry-run return so that
    # `run --dry-run --max-cost N` is a usable pre-flight validator, and
    # before the --yes branch because --yes must not waive it: an
    # unattended run is exactly the one nobody is watching.
    if max_cost is not None and est.total > max_cost:
        print(
            f"ABORT: estimated ${est.total:.2f} exceeds the --max-cost "
            f"ceiling of ${max_cost:.2f}. Nothing was sampled and nothing "
            f"was written. Raise --max-cost if this is expected, or lower "
            f"a runner's max_tokens in config.yaml if it is not.",
            file=sys.stderr,
        )
        for k, note in sorted(est.assumptions.items()):
            print(f"  {k}: ${est.by_runner.get(k, 0.0):.2f}  [{note}]", file=sys.stderr)
        return 2

    if args.dry_run:
        return 0
    if not args.yes and est.total > 0.0:
        print("(use --yes to proceed without confirmation)")

    # In-run ceiling. The pre-flight estimate is a heuristic and was
    # blind to max_tokens until 2026-08, so the same number is enforced
    # a second time against money actually spent. Wrapping is skipped
    # entirely when no ceiling was given, leaving the unguarded path
    # byte-for-byte what it was.
    ledger: BudgetLedger | None = None
    if max_cost is not None:
        runners, ledger = guard_runners(runners, ceiling_usd=max_cost)

    started_at = datetime.now(timezone.utc)
    orch = Orchestrator(runners, store, corpus, plan)
    outcome = await orch.run(force=args.force)
    finished_at = datetime.now(timezone.utc)
    print(
        f"\n{week_id}: wrote {outcome.total_samples_written} sample(s) "
        f"across {outcome.pairs_complete} pair(s), "
        f"skipped {outcome.pairs_skipped}, failed {outcome.pairs_failed}"
    )
    for k, v in outcome.per_runner_samples.items():
        print(f"  {k}: {v}")
    for err in outcome.errors[:10]:
        print(f"  [{err.error_type}] {err.provider}/{err.model_id}/{err.prompt_id}: {err.message}",
              file=sys.stderr)

    run_note: str | None = None
    if ledger is not None:
        print(ledger.pretty())
        if ledger.tripped:
            run_note = (
                f"--max-cost ceiling of ${ledger.ceiling_usd:.2f} reached after "
                f"${ledger.tripped_at_usd:.2f}; remaining pairs were refused "
                f"without calling any provider"
            )
            print(
                f"BUDGET CEILING HIT: stopped issuing requests after "
                f"${ledger.tripped_at_usd:.2f} of a ${ledger.ceiling_usd:.2f} "
                f"ceiling. {outcome.pairs_failed} pair(s) were refused. Every "
                f"sample captured before the stop is stored and is written to "
                f"the manifest as usual.",
                file=sys.stderr,
            )

    # Record this run in the append-only log BEFORE manifest writes and
    # S3 archival. The log's purpose is operational truth about what the
    # orchestrator did; downstream artifacts are a separate concern and
    # should not gate the audit trail. A budget stop is doubly a reason
    # to write it first: the entry is the receipt for the money spent.
    _append_run_log_entry(
        config=config,
        store=store,
        week_id=week_id,
        started_at=started_at,
        finished_at=finished_at,
        outcome=outcome,
        estimated_cost_usd=est.total,
        note=run_note,
    )

    display_info = _display_info_for(config)
    prior_manifests_dir = REPO_ROOT / "data" / "manifests"

    # Stance + embedding wiring. Both are gated by config; if disabled,
    # build_manifest receives None and every metric record's stance
    # stays "na" / embedding_centroid_shift stays None — the v0 behaviour.
    stance_by_key = await _maybe_collect_stance(
        config=config, store=store, corpus=corpus, week_id=week_id,
    )
    embedding_model = build_embedding_model(config.embedding)

    # Straight from the run rather than via _rejections_by_key(): the
    # entry this run just appended says the same thing, but reading it
    # back would make the manifest depend on the log write having
    # succeeded. The counts are already in hand here.
    rejections_by_key = {
        (runner_key.split("/", 1)[-1], prompt_id): count
        for runner_key, per_prompt in outcome.content_policy_rejections.items()
        for prompt_id, count in per_prompt.items()
    }

    manifest = build_manifest(
        store=store, corpus=corpus, week_id=week_id, display_info=display_info,
        prior_manifests_dir=prior_manifests_dir,
        stance_by_key=stance_by_key,
        embedding_model=embedding_model,
        rejections_by_key=rejections_by_key,
    )
    paths = _output_paths(week_id)
    write_manifest(manifest, paths)
    print(f"wrote public manifest to {paths[0]}")

    if corpus.has_held_out:
        internal = build_manifest(
            store=store, corpus=corpus, week_id=week_id,
            display_info=display_info, include_held_out=True,
            prior_manifests_dir=prior_manifests_dir,
            stance_by_key=stance_by_key,
            embedding_model=embedding_model,
            rejections_by_key=rejections_by_key,
        )
        internal_path = _internal_manifest_path(week_id)
        write_manifest(internal, [internal_path])
        print(f"wrote internal (with held-out) manifest to {internal_path}")

    responses_gz = _emit_public_responses_snapshot(store, corpus, week_id)
    _maybe_archive_to_s3(
        config, store, week_id, paths[0],
        responses_path=responses_gz,
    )

    # A budget stop reports as 2 rather than 1: it is a hard limit the
    # operator set, not the flaky-pair partial failure that 1 means, and
    # run-weekly.sh forwards the code straight into the SNS subject.
    # Every artifact above was still written first.
    if ledger is not None and ledger.tripped:
        return 2
    return 1 if outcome.pairs_failed > 0 else 0


async def _maybe_collect_stance(
    *,
    config: PipelineConfig,
    store: LocalSampleStore,
    corpus: Corpus,
    week_id: str,
):
    """Build the stance classifier from config and run it across the
    week's samples. Returns ``None`` when stance is disabled in config,
    so :func:`build_manifest` keeps its v0 behaviour of leaving every
    metric record's stance="na"."""
    classifier = build_stance_classifier(config.stance, repo_root=REPO_ROOT)
    if classifier is None:
        return None
    print(f"stance: classifying with {config.stance.provider}/{config.stance.model_id}")
    results = await collect_stance_results(
        classifier=classifier, store=store, corpus=corpus, week_id=week_id,
    )
    by_stance: dict[str, int] = {}
    for r in results.values():
        by_stance[r.stance] = by_stance.get(r.stance, 0) + 1
    print(f"stance: classified {len(results)} pair(s) — {by_stance}")
    return results


def _emit_public_responses_snapshot(
    store: LocalSampleStore,
    corpus: Corpus,
    week_id: str,
) -> Path | None:
    """Write ``data/snapshots/{week}/responses.jsonl.gz`` for site
    distribution + S3 archival.

    Held-out samples are excluded by :func:`emit_responses_snapshot`.
    """
    out = snapshot_path(REPO_ROOT, week_id)
    report = emit_responses_snapshot(store, corpus, week_id, out)
    if report.sample_count == 0:
        print(f"responses snapshot: {out.name} (empty — no public samples stored)")
    else:
        print(f"responses snapshot: {report.pretty()}")
    return out


def _append_run_log_entry(
    *,
    config: PipelineConfig,
    store: LocalSampleStore,
    week_id: str,
    started_at: datetime,
    finished_at: datetime,
    outcome,
    estimated_cost_usd: float,
    note: str | None = None,
) -> None:
    """Write one RunLogEntry to data/run_log.jsonl for this invocation.

    Actual cost is summed across every sample stored under ``week_id``
    — including pairs from prior runs in the same week if resume was
    used — matching the runbook's "cost spent on this week's data"
    interpretation. That is deliberately a different number from the
    ``--max-cost`` ledger, which counts only what this process spent.
    """
    all_samples = []
    for model_id in store.models_for_week(week_id):
        for prompt_id in store.prompts_for(week_id, model_id):
            all_samples.extend(store.read(week_id, model_id, prompt_id))
    cost_report = compute_actual_cost(all_samples)

    log_path = REPO_ROOT / "data" / "run_log.jsonl"
    append_run_log(
        log_path,
        started_at=started_at,
        finished_at=finished_at,
        week_id=week_id,
        config=config,
        outcome=outcome,
        estimated_cost_usd=estimated_cost_usd,
        actual_cost_usd=cost_report.total_usd,
        note=note,
    )
    print(
        f"run log: estimated ${estimated_cost_usd:.2f} / "
        f"actual ${cost_report.total_usd:.2f} "
        f"({cost_report.samples_priced} priced, "
        f"{cost_report.samples_skipped_no_tokens} skipped)"
    )


def _maybe_archive_to_s3(
    config: PipelineConfig,
    store: LocalSampleStore,
    week_id: str,
    public_manifest_path: Path,
    *,
    responses_path: Path | None = None,
) -> None:
    """Opt-in S3 mirror for raw samples + the public manifest + the
    week's public responses gzip.

    Non-fatal: an archive failure logs to stderr but the pipeline still
    reports success. The authoritative copy stays on local disk.
    """
    uploader = maybe_build_uploader(config.storage.s3)
    if uploader is None:
        return
    raw_report = uploader.upload_week(store, week_id)
    print(f"s3: raw samples — {raw_report.pretty()}")
    manifest_report = uploader.upload_manifest(public_manifest_path, week_id)
    print(f"s3: manifest    — {manifest_report.pretty()}")
    run_log_report = uploader.upload_run_log(REPO_ROOT / "data" / "run_log.jsonl")
    print(f"s3: run log     — {run_log_report.pretty()}")
    reports = [raw_report, manifest_report, run_log_report]
    if responses_path is not None:
        responses_report = uploader.upload_responses_snapshot(responses_path, week_id)
        print(f"s3: responses   — {responses_report.pretty()}")
        reports.append(responses_report)
    for r in reports:
        for err in r.errors:
            print(f"  s3 error: {err}", file=sys.stderr)


class _RunnerSpecShim:
    """Duck-typed stand-in for a Runner that the cost estimator can price.

    The estimator reads ``provider``, ``model_id``, and the completion
    cap. Using shims avoids constructing real SDK clients (and needing
    API keys) for what is purely an arithmetic operation over the
    pricing table.

    ``max_tokens_override`` is spelled the way :class:`Runner` spells it,
    not the way :class:`~meridian.config.RunnerSpec` does, so the
    estimator sees one contract whichever kind of object it is handed.
    """
    def __init__(
        self, provider: str, model_id: str, max_tokens: int | None = None
    ) -> None:
        self.provider = provider
        self.model_id = model_id
        self.max_tokens_override = max_tokens

    def supports_temperature(self, temperature: float) -> bool:
        """Same answer the real runner would give, without building one.

        The estimator drops a batch the orchestrator will skip, so a shim
        that answered "yes" to everything would price 25% more calls than
        the roster actually makes. The per-provider rules are the runner
        modules' to own, so this delegates to their pure helpers rather
        than keeping a third copy of the prefix lists. Imported inside
        the method because those modules pull in provider SDKs, and the
        estimate subcommand is meant to be pure arithmetic; the import
        is cheap and the SDKs are hard dependencies anyway.
        """
        from meridian.runners.anthropic import _anthropic_supports_temperature
        from meridian.runners.openai import _openai_supports_temperature

        provider = self.provider.lower()
        if provider == "anthropic":
            return _anthropic_supports_temperature(self.model_id, temperature)
        if provider == "openai":
            return _openai_supports_temperature(self.model_id, temperature)
        return True


def _enabled_specs_for_week(config: PipelineConfig, week_id: str) -> list[_RunnerSpecShim]:
    from meridian.config import should_run_in_week
    return [
        _RunnerSpecShim(s.provider, s.model_id, s.max_tokens)
        for s in config.runners
        if s.enabled and should_run_in_week(s.cadence, week_id)
    ]


def _cmd_estimate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not config.runners:
        print("no runners configured", file=sys.stderr)
        return 2
    corpus = load_corpus()
    plan_samples = config.sampling.n_default_temp + config.sampling.n_zero_temp
    # Public-only on purpose, unlike `run`, which prices corpus.all().
    # This subcommand is the source of the published tables in
    # meridian/BUDGET.md and of the public per-tier figures on /funding/,
    # and those quote the cost of the corpus that is visible in the repo.
    # It gates nothing, so under-counting here costs credibility rather
    # than money. Say so in BUDGET.md if the two numbers ever diverge.
    n_prompts = len(corpus.public())
    temperature_plan = _temperature_plan(config.sampling)

    this_week = _resolve_week(args.week)
    week_specs = _enabled_specs_for_week(config, this_week)
    week_est = estimate_cost(
        week_specs, n_prompts=n_prompts, samples_per_pair=plan_samples,
        default_max_tokens=config.sampling.max_tokens,
        temperature_plan=temperature_plan,
    )
    print(f"This week ({this_week}):")
    for k, v in sorted(week_est.by_runner.items()):
        note = week_est.assumptions.get(k)
        print(f"  {k}: ${v:.2f}" + (f"  [{note}]" if note else ""))
    print(f"  total: ${week_est.total:.2f}\n")

    # Four-week rolling average picks up alternation correctly.
    from datetime import date, timedelta

    from meridian.sampling.weeks import iso_week_for
    today = date.today()
    rolling_total = 0.0
    for i in range(4):
        wk = iso_week_for(today + timedelta(weeks=i))
        wk_specs = _enabled_specs_for_week(config, wk)
        wk_est = estimate_cost(
            wk_specs, n_prompts=n_prompts, samples_per_pair=plan_samples,
            default_max_tokens=config.sampling.max_tokens,
            temperature_plan=temperature_plan,
        )
        rolling_total += wk_est.total
    weekly_avg = rolling_total / 4
    monthly_avg = weekly_avg * 52 / 12
    annual = weekly_avg * 52
    print("Rolling 4-week average (accounts for biweekly cadences):")
    print(f"  per week:  ${weekly_avg:.2f}")
    print(f"  per month: ${monthly_avg:.2f}")
    print(f"  per year:  ${annual:.2f}")
    return 0


def _rejections_by_key(week_id: str) -> dict[tuple[str, str], int]:
    """Provider-declined request counts for ``week_id``, from the run log.

    A rejected request writes no sample, so unlike every other input to
    a manifest this one cannot be recovered from storage. The run log is
    the only durable record of it, which makes reading it here the
    difference between a rebuild that preserves the count and one that
    silently republishes the cell as an unexplained small sample. That
    second outcome is the 2026-W33 failure re-created by the tool meant
    to correct it.

    Last entry wins when a week was run more than once, matching the
    re-run semantics everywhere else: the newest run is the one whose
    samples are on disk.
    """
    entries = read_run_log(REPO_ROOT / "data" / "run_log.jsonl")
    out: dict[tuple[str, str], int] = {}
    for entry in entries:
        if entry.week_id != week_id:
            continue
        out = {
            (runner_key.split("/", 1)[-1], prompt_id): count
            for runner_key, per_prompt in entry.content_policy_rejections.items()
            for prompt_id, count in per_prompt.items()
        }
    return out


def _cmd_build_manifest(args: argparse.Namespace) -> int:
    config, store, _runners = _build_context(args, need_runners=False)
    corpus = load_corpus()
    week_id = _resolve_week(args.week)
    display_info = _display_info_for(config)
    prior_manifests_dir = REPO_ROOT / "data" / "manifests"

    stance_by_key = asyncio.run(_maybe_collect_stance(
        config=config, store=store, corpus=corpus, week_id=week_id,
    ))
    embedding_model = build_embedding_model(config.embedding)

    rejections_by_key = _rejections_by_key(week_id)

    manifest = build_manifest(
        store=store, corpus=corpus, week_id=week_id,
        history_weeks=args.history_weeks, display_info=display_info,
        prior_manifests_dir=prior_manifests_dir,
        stance_by_key=stance_by_key,
        embedding_model=embedding_model,
        rejections_by_key=rejections_by_key,
    )
    paths = _output_paths(week_id)
    write_manifest(manifest, paths)
    print(f"wrote manifest for {week_id} to:")
    for p in paths:
        print(f"  {p}")
    if corpus.has_held_out:
        internal = build_manifest(
            store=store, corpus=corpus, week_id=week_id,
            history_weeks=args.history_weeks, display_info=display_info,
            include_held_out=True,
            prior_manifests_dir=prior_manifests_dir,
            stance_by_key=stance_by_key,
            embedding_model=embedding_model,
            rejections_by_key=rejections_by_key,
        )
        internal_path = _internal_manifest_path(week_id)
        write_manifest(internal, [internal_path])
        print(f"  {internal_path}  (held-out included)")
    # Responses snapshot is an output of manifest rebuilds too — a
    # researcher re-deriving metrics from a past week should also get
    # refreshed raw-responses gzip alongside.
    _emit_public_responses_snapshot(store, corpus, week_id)
    return 0


def _inspect_status(actual: int, expected: int) -> str:
    if actual == 0:
        return "missing"
    if actual < expected:
        return "partial"
    return "complete"


def _cmd_inspect_week(args: argparse.Namespace) -> int:
    """Operator-friendly week audit: who has what, what failed, what it cost.

    Reads ``data/raw/<week>/`` via :class:`LocalSampleStore` plus the
    append-only run log at ``data/run_log.jsonl``. No network, no live
    runners — purely a local inspection command.
    """
    config = load_config(args.config)
    corpus = load_corpus()
    raw_dir = REPO_ROOT / config.storage.raw_dir
    store = LocalSampleStore(raw_dir)
    week_id = _resolve_week(args.week)
    expected_per_pair = (
        config.sampling.n_default_temp + config.sampling.n_zero_temp
    )
    public_prompts = corpus.public()
    n_public = len(public_prompts)

    per_model: list[dict] = []
    for model_id in store.models_for_week(week_id):
        pairs: list[dict] = []
        total_samples = 0
        pairs_complete = pairs_partial = pairs_missing = 0
        for prompt in public_prompts:
            count = store.count(week_id, model_id, prompt.id)
            total_samples += count
            status = _inspect_status(count, expected_per_pair)
            pairs.append({
                "prompt_id": prompt.id,
                "axis": prompt.axis,
                "n_samples": count,
                "status": status,
            })
            if status == "complete":
                pairs_complete += 1
            elif status == "partial":
                pairs_partial += 1
            else:
                pairs_missing += 1
        per_model.append({
            "model_id": model_id,
            "total_samples": total_samples,
            "expected_total": expected_per_pair * n_public,
            "pairs_complete": pairs_complete,
            "pairs_partial": pairs_partial,
            "pairs_missing": pairs_missing,
            "pairs": pairs,
        })

    log_path = REPO_ROOT / "data" / "run_log.jsonl"
    run_entries = [e for e in read_run_log(log_path) if e.week_id == week_id]
    latest = run_entries[-1] if run_entries else None

    report: dict = {
        "week_id": week_id,
        "expected_per_pair": expected_per_pair,
        "public_prompts": n_public,
        "per_model": per_model,
        "runs": [
            {
                "started_at": e.started_at,
                "finished_at": e.finished_at,
                "host": e.host,
                "pid": e.pid,
                "config_hash": e.config_hash,
                "runners": e.runners,
                "total_samples_written": e.total_samples_written,
                "pairs_complete": e.pairs_complete,
                "pairs_skipped": e.pairs_skipped,
                "pairs_failed": e.pairs_failed,
                "per_runner_samples": e.per_runner_samples,
                "estimated_cost_usd": e.estimated_cost_usd,
                "actual_cost_usd": e.actual_cost_usd,
                "errors": e.errors,
            }
            for e in run_entries
        ],
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"inspect-week {week_id}")
    print(f"  expected samples per (prompt × model): {expected_per_pair}")
    print(f"  public prompts in corpus: {n_public}")

    if not per_model:
        print("  no raw samples found — has any run happened for this week?")
    else:
        print("\nPer-model coverage:")
        for m in per_model:
            print(
                f"  {m['model_id']:<32} "
                f"{m['total_samples']:>4}/{m['expected_total']:<4} samples, "
                f"complete={m['pairs_complete']} partial={m['pairs_partial']} "
                f"missing={m['pairs_missing']}"
            )
        # Surface partial/missing pairs for easy retry targeting.
        offenders = [
            (m["model_id"], p) for m in per_model for p in m["pairs"]
            if p["status"] != "complete"
        ]
        if offenders:
            print("\nPairs needing attention:")
            for model_id, p in offenders[:20]:
                print(
                    f"  [{p['status']:<7}] {model_id}/{p['prompt_id']} "
                    f"({p['n_samples']}/{expected_per_pair})"
                )
            if len(offenders) > 20:
                print(f"  … {len(offenders) - 20} more (use --json for the full list)")

    if latest is None:
        print("\nRun log: no entries for this week.")
    else:
        print("\nRun log (most recent):")
        print(f"  started:      {latest.started_at}")
        print(f"  finished:     {latest.finished_at}")
        print(f"  host:         {latest.host} (pid {latest.pid})")
        print(f"  config hash:  {latest.config_hash}")
        print(f"  runners:      {', '.join(latest.runners)}")
        print(
            f"  samples: {latest.total_samples_written}   "
            f"pairs complete/skipped/failed: {latest.pairs_complete}/"
            f"{latest.pairs_skipped}/{latest.pairs_failed}"
        )
        print(
            f"  cost est vs actual:  "
            f"${latest.estimated_cost_usd:.2f}  /  ${latest.actual_cost_usd:.2f}"
        )
        if latest.errors:
            print(f"  errors ({len(latest.errors)} captured; showing up to 5):")
            for err in latest.errors[:5]:
                print(
                    f"    [{err['error_type']}] "
                    f"{err['provider']}/{err['model_id']}/{err['prompt_id']}: "
                    f"{err['message']}"
                )

    return 0


def _arrow(delta: float) -> str:
    if delta > 0:
        return "↑"
    if delta < 0:
        return "↓"
    return "·"


def _cmd_dump_manifest(args: argparse.Namespace) -> int:
    """Pretty-print a manifest, optionally diffing against a prior one.

    ``--diff <path>`` surfaces per (prompt × model) metric deltas on the
    three numeric metrics (refusal rate, hedge density, median length)
    and flags any MetricRecord whose ``refusal_drift.significant_after_bh``
    flipped. No network; pure file + console.
    """
    manifest_path: Path = args.manifest
    manifest = json.loads(manifest_path.read_text())

    print(f"Manifest dump: {manifest_path}")
    print(f"  Schema version: {manifest['schema_version']}")
    snap = manifest["snapshot"]
    print(f"  Snapshot:       {snap['week_id']} (generated {snap['generated_at']})")
    print(f"  Corpus sha:     {snap['corpus_git_sha']}")
    print(f"  Pipeline:       {snap['pipeline_version']}")
    models = manifest.get("models", [])
    prompts = manifest.get("prompts", [])
    metrics = manifest.get("metrics", [])
    flagged = manifest.get("flagged", [])
    print(f"  Models ({len(models)}):")
    for m in models:
        print(f"    {m['model_id']:<28} ({m['provider']})")
    print(f"  Prompts:        {len(prompts)}")
    print(f"  Metrics:        {len(metrics)}")
    print(f"  Flagged:        {len(flagged)}")

    if args.diff is None:
        return 0

    prior = json.loads(args.diff.read_text())
    prior_by_key = {
        (m["prompt_id"], m["model_id"]): m for m in prior.get("metrics", [])
    }
    print(f"\nDiff against {args.diff}:")

    any_change = False
    for m in metrics:
        key = (m["prompt_id"], m["model_id"])
        other = prior_by_key.get(key)
        if other is None:
            continue

        deltas = []
        for field_name, path in (
            ("refusal_rate", ("refusal_rate",)),
            ("hedge_density", ("hedge_density",)),
            ("length_median", ("length", "median")),
        ):
            cur = m
            pri = other
            for p in path:
                cur = cur[p]
                pri = pri[p]
            d = float(cur) - float(pri)
            deltas.append((field_name, float(pri), float(cur), d))

        # BH-flip detection: compare significant_after_bh on refusal drift.
        def _sig(rec: dict) -> bool | None:
            entry = rec.get("refusal_drift")
            return None if entry is None else bool(entry.get("significant_after_bh"))

        flip = (_sig(other), _sig(m))

        changed_fields = [d for d in deltas if abs(d[3]) >= 0.005]
        if not changed_fields and flip[0] == flip[1]:
            continue
        any_change = True

        print(f"  {m['prompt_id']} × {m['model_id']}:")
        for name, pri_v, cur_v, d in deltas:
            if abs(d) >= 0.005:
                print(
                    f"    {name:<14} {pri_v:>8.3f} → {cur_v:<8.3f} "
                    f"({d:+.3f}) {_arrow(d)}"
                )
        if flip != (None, None) and flip[0] != flip[1]:
            print(f"    refusal BH significance: {flip[0]} → {flip[1]}")

    if not any_change:
        print("  (no metric changes above 0.005 threshold and no BH flips)")
    return 0


def _cmd_silent_update(args: argparse.Namespace) -> int:
    config, store, _runners = _build_context(args, need_runners=False)
    corpus = load_corpus()
    week_id = _resolve_week(args.week)
    manifest = build_manifest(
        store=store, corpus=corpus, week_id=week_id,
        display_info=_display_info_for(config),
    )
    flags = detect_silent_updates(manifest=manifest, prompts=corpus.all(), axis=args.axis)
    if not flags:
        print(f"no silent-update candidates on axis '{args.axis}' for {week_id}")
        return 0
    print(f"silent-update candidates on axis '{args.axis}' for {week_id}:")
    for f in flags:
        print(f"  {f.pretty()}")
    return 0


def _cmd_holdout_report(args: argparse.Namespace) -> int:
    config, store, _runners = _build_context(args, need_runners=False)
    corpus = load_corpus()
    if not corpus.has_held_out:
        print("no held-out corpus found (meridian/corpus/held_out*.yaml)", file=sys.stderr)
        return 2
    week_id = _resolve_week(args.week)
    manifest = build_manifest(
        store=store, corpus=corpus, week_id=week_id,
        display_info=_display_info_for(config), include_held_out=True,
    )
    report = compare_holdout(
        week_id=week_id, prompts=corpus.all(), metrics=manifest["metrics"],
    )
    print(f"Held-out comparison for {week_id}")
    print(f"  divergence score: {report.divergence_score:.3f} ({report.verdict()})")
    for c in report.per_axis:
        print(
            f"  {c.axis:<22} "
            f"refusal pub={c.public_mean_refusal:.2f} ho={c.held_out_mean_refusal:.2f} "
            f"Δ={c.refusal_delta:+.2f}  "
            f"hedge pub={c.public_mean_hedge:.2f} ho={c.held_out_mean_hedge:.2f} "
            f"Δ={c.hedge_delta:+.2f}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Pull provider API keys from SSM if MERIDIAN_SECRETS_SSM=1 is set.
    # No-op otherwise; existing env-var-based credential flows are unaffected.
    resolve_ssm_secrets()

    parser = argparse.ArgumentParser(prog="meridian")
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to pipeline config (default: meridian/config.yaml)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Sample from all enabled runners for a week")
    p_run.add_argument("--week", default=None, help="ISO week id (default: this week)")
    p_run.add_argument("--force", action="store_true",
                       help="Re-sample pairs even if they already have complete counts")
    p_run.add_argument("--yes", action="store_true",
                       help="Skip cost confirmation (use in CI)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Print estimate only; do not sample")
    p_run.add_argument("--max-cost", type=float, default=None, metavar="USD",
                       help="Hard spend ceiling for this run, in dollars. "
                            "Aborts before sampling if the pre-flight "
                            "estimate exceeds it, and stops issuing "
                            "requests mid-run if actual spend reaches it. "
                            "NOT waived by --yes. Exits 2 either way. Also "
                            "refuses to start if any enabled runner has no "
                            "price on file, because a ceiling cannot bound "
                            "spend it cannot compute. The pre-flight number "
                            "is a deliberately conservative estimate and "
                            "runs above observed actuals, so this is not a "
                            "prediction of real spend.")

    p_est = sub.add_parser("estimate", help="Show pre-flight cost estimate")
    p_est.add_argument("--week", default=None,
                       help="ISO week id for this-week total (default: this week)")

    p_mf = sub.add_parser("build-manifest",
                          help="Rebuild the site manifest from existing storage")
    p_mf.add_argument("--week", required=True, help="ISO week id to (re)build")
    p_mf.add_argument("--history-weeks", type=int, default=8)

    p_ho = sub.add_parser(
        "holdout-report",
        help="Per-axis divergence between public and held-out drift (internal use only)",
    )
    p_ho.add_argument("--week", default=None, help="ISO week id (default: this week)")

    p_si = sub.add_parser(
        "silent-update-check",
        help="Flag week-over-week shifts on the neutral-control axis per model",
    )
    p_si.add_argument("--week", default=None, help="ISO week id (default: this week)")
    p_si.add_argument("--axis", default="neutral-control",
                      help="Axis to watch (default: neutral-control)")

    p_iw = sub.add_parser(
        "inspect-week",
        help="Audit per (prompt × model) sample counts, costs, and errors for a week",
    )
    p_iw.add_argument("--week", default=None, help="ISO week id (default: this week)")
    p_iw.add_argument("--json", action="store_true",
                      help="Emit the report as a JSON object on stdout")

    p_dm = sub.add_parser(
        "dump-manifest",
        help="Pretty-print a manifest; with --diff, highlight per-record deltas",
    )
    p_dm.add_argument("manifest", type=Path, help="Path to manifest JSON")
    p_dm.add_argument("--diff", type=Path, default=None,
                      help="Prior manifest JSON to diff against")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    if args.cmd == "estimate":
        return _cmd_estimate(args)
    if args.cmd == "build-manifest":
        return _cmd_build_manifest(args)
    if args.cmd == "holdout-report":
        return _cmd_holdout_report(args)
    if args.cmd == "silent-update-check":
        return _cmd_silent_update(args)
    if args.cmd == "inspect-week":
        return _cmd_inspect_week(args)
    if args.cmd == "dump-manifest":
        return _cmd_dump_manifest(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
