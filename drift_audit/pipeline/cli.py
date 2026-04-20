"""Drift Audit pipeline CLI.

Subcommands:
  run              Sample from every enabled runner for a week, persist to
                   storage, and write a Manifest JSON the site can render.
  estimate         Show the pre-flight cost estimate without sampling.
  build-manifest   Rebuild the site manifest from existing storage (no new
                   sampling). Useful when metric logic changed but samples
                   did not.

Run via::

    uv run python -m drift_audit.pipeline.cli run --week 2026-W16
    uv run python -m drift_audit.pipeline.cli estimate
    uv run python -m drift_audit.pipeline.cli build-manifest --week 2026-W16

Exit codes:
  0  success / everything completed cleanly
  1  partial failure (some pairs failed; manifest still written)
  2  fatal error (config, auth, storage)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import json

from drift_audit.analysis.holdout_compare import compare_holdout
from drift_audit.analysis.silent_update import detect_silent_updates
from drift_audit.config import PipelineConfig, build_runners, load_config
from drift_audit.corpus import load_corpus
from drift_audit.pipeline.manifest_writer import (
    RunnerDisplayInfo,
    build_manifest,
    write_manifest,
)
from drift_audit.pipeline.run_log import append_run_log, read_run_log
from drift_audit.sampling.cost import compute_actual_cost
from drift_audit.sampling.orchestrator import Orchestrator, SamplingPlan
from drift_audit.sampling.pricing import estimate_cost
from drift_audit.sampling.weeks import iso_week_for
from drift_audit.storage import LocalSampleStore
from drift_audit.storage.s3 import maybe_build_uploader

_log = logging.getLogger("drift_audit")

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


def _build_context(
    args: argparse.Namespace,
    *,
    week_id: str | None = None,
) -> tuple[PipelineConfig, LocalSampleStore, list]:
    config = load_config(args.config)
    if not config.runners:
        print("no runners configured", file=sys.stderr)
        raise SystemExit(2)
    corpus = load_corpus()
    raw_dir = REPO_ROOT / config.storage.raw_dir
    store = LocalSampleStore(raw_dir)
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

    est = estimate_cost(
        runners, n_prompts=len(corpus.public()), samples_per_pair=plan.samples_per_pair
    )
    print(est.pretty())
    if args.dry_run:
        return 0
    if not args.yes and est.total > 0.0:
        print("(use --yes to proceed without confirmation)")

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

    # Record this run in the append-only log BEFORE manifest writes and
    # S3 archival. The log's purpose is operational truth about what the
    # orchestrator did; downstream artifacts are a separate concern and
    # should not gate the audit trail.
    _append_run_log_entry(
        config=config,
        store=store,
        week_id=week_id,
        started_at=started_at,
        finished_at=finished_at,
        outcome=outcome,
        estimated_cost_usd=est.total,
    )

    display_info = _display_info_for(config)
    manifest = build_manifest(
        store=store, corpus=corpus, week_id=week_id, display_info=display_info,
    )
    paths = _output_paths(week_id)
    write_manifest(manifest, paths)
    print(f"wrote public manifest to {paths[0]}")

    if corpus.has_held_out:
        internal = build_manifest(
            store=store, corpus=corpus, week_id=week_id,
            display_info=display_info, include_held_out=True,
        )
        internal_path = _internal_manifest_path(week_id)
        write_manifest(internal, [internal_path])
        print(f"wrote internal (with held-out) manifest to {internal_path}")

    _maybe_archive_to_s3(config, store, week_id, paths[0])

    return 1 if outcome.pairs_failed > 0 else 0


def _append_run_log_entry(
    *,
    config: PipelineConfig,
    store: LocalSampleStore,
    week_id: str,
    started_at: datetime,
    finished_at: datetime,
    outcome,
    estimated_cost_usd: float,
) -> None:
    """Write one RunLogEntry to data/run_log.jsonl for this invocation.

    Actual cost is summed across every sample stored under ``week_id``
    — including pairs from prior runs in the same week if resume was
    used — matching the runbook's "cost spent on this week's data"
    interpretation.
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
) -> None:
    """Opt-in S3 mirror for raw samples + the public manifest.

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
    for err in raw_report.errors + manifest_report.errors:
        print(f"  s3 error: {err}", file=sys.stderr)


class _RunnerSpecShim:
    """Duck-typed stand-in for a Runner that the cost estimator can price.

    The estimator only reads ``provider`` and ``model_id``. Using shims
    avoids constructing real SDK clients (and needing API keys) for what
    is purely an arithmetic operation over the pricing table.
    """
    def __init__(self, provider: str, model_id: str) -> None:
        self.provider = provider
        self.model_id = model_id


def _enabled_specs_for_week(config: PipelineConfig, week_id: str) -> list[_RunnerSpecShim]:
    from drift_audit.config import should_run_in_week
    return [
        _RunnerSpecShim(s.provider, s.model_id)
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
    n_prompts = len(corpus.public())

    this_week = _resolve_week(args.week)
    week_specs = _enabled_specs_for_week(config, this_week)
    week_est = estimate_cost(week_specs, n_prompts=n_prompts, samples_per_pair=plan_samples)
    print(f"This week ({this_week}):")
    for k, v in sorted(week_est.by_runner.items()):
        print(f"  {k}: ${v:.2f}")
    print(f"  total: ${week_est.total:.2f}\n")

    # Four-week rolling average picks up alternation correctly.
    from datetime import date, timedelta

    from drift_audit.sampling.weeks import iso_week_for
    today = date.today()
    rolling_total = 0.0
    for i in range(4):
        wk = iso_week_for(today + timedelta(weeks=i))
        wk_specs = _enabled_specs_for_week(config, wk)
        wk_est = estimate_cost(wk_specs, n_prompts=n_prompts, samples_per_pair=plan_samples)
        rolling_total += wk_est.total
    weekly_avg = rolling_total / 4
    monthly_avg = weekly_avg * 52 / 12
    annual = weekly_avg * 52
    print("Rolling 4-week average (accounts for biweekly cadences):")
    print(f"  per week:  ${weekly_avg:.2f}")
    print(f"  per month: ${monthly_avg:.2f}")
    print(f"  per year:  ${annual:.2f}")
    return 0


def _cmd_build_manifest(args: argparse.Namespace) -> int:
    config, store, _runners = _build_context(args)
    corpus = load_corpus()
    week_id = _resolve_week(args.week)
    display_info = _display_info_for(config)
    manifest = build_manifest(
        store=store, corpus=corpus, week_id=week_id,
        history_weeks=args.history_weeks, display_info=display_info,
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
        )
        internal_path = _internal_manifest_path(week_id)
        write_manifest(internal, [internal_path])
        print(f"  {internal_path}  (held-out included)")
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
    config, store, _runners = _build_context(args)
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
    config, store, _runners = _build_context(args)
    corpus = load_corpus()
    if not corpus.has_held_out:
        print("no held-out corpus found (drift_audit/corpus/held_out*.yaml)", file=sys.stderr)
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

    parser = argparse.ArgumentParser(prog="drift-audit")
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to pipeline config (default: drift_audit/config.yaml)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Sample from all enabled runners for a week")
    p_run.add_argument("--week", default=None, help="ISO week id (default: this week)")
    p_run.add_argument("--force", action="store_true",
                       help="Re-sample pairs even if they already have complete counts")
    p_run.add_argument("--yes", action="store_true",
                       help="Skip cost confirmation (use in CI)")
    p_run.add_argument("--dry-run", action="store_true",
                       help="Print estimate only; do not sample")

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
