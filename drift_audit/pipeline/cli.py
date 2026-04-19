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
from pathlib import Path

from drift_audit.config import PipelineConfig, build_runners, load_config
from drift_audit.corpus import load_corpus
from drift_audit.pipeline.manifest_writer import (
    RunnerDisplayInfo,
    build_manifest,
    write_manifest,
)
from drift_audit.sampling.orchestrator import Orchestrator, SamplingPlan
from drift_audit.sampling.pricing import estimate_cost
from drift_audit.sampling.weeks import iso_week_for
from drift_audit.storage import LocalSampleStore

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


def _build_context(args: argparse.Namespace) -> tuple[PipelineConfig, LocalSampleStore, list]:
    config = load_config(args.config)
    if not config.runners:
        print("no runners configured", file=sys.stderr)
        raise SystemExit(2)
    corpus = load_corpus()
    raw_dir = REPO_ROOT / config.storage.raw_dir
    store = LocalSampleStore(raw_dir)
    runners = build_runners(config)
    if not runners:
        print("no enabled runners", file=sys.stderr)
        raise SystemExit(2)
    return config, store, runners


async def _cmd_run(args: argparse.Namespace) -> int:
    config, store, runners = _build_context(args)
    corpus = load_corpus()
    week_id = _resolve_week(args.week)
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

    orch = Orchestrator(runners, store, corpus, plan)
    outcome = await orch.run(force=args.force)
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

    manifest = build_manifest(
        store=store,
        corpus=corpus,
        week_id=week_id,
        display_info=_display_info_for(config),
    )
    paths = _output_paths(week_id)
    write_manifest(manifest, paths)
    print(f"wrote manifest to {paths[0]}")

    return 1 if outcome.pairs_failed > 0 else 0


def _cmd_estimate(args: argparse.Namespace) -> int:
    _, _, runners = _build_context(args)
    config = load_config(args.config)
    corpus = load_corpus()
    plan_samples = config.sampling.n_default_temp + config.sampling.n_zero_temp
    est = estimate_cost(
        runners, n_prompts=len(corpus.public()), samples_per_pair=plan_samples
    )
    print(est.pretty())
    return 0


def _cmd_build_manifest(args: argparse.Namespace) -> int:
    config, store, _runners = _build_context(args)
    corpus = load_corpus()
    week_id = _resolve_week(args.week)
    manifest = build_manifest(
        store=store,
        corpus=corpus,
        week_id=week_id,
        history_weeks=args.history_weeks,
        display_info=_display_info_for(config),
    )
    paths = _output_paths(week_id)
    write_manifest(manifest, paths)
    print(f"wrote manifest for {week_id} to:")
    for p in paths:
        print(f"  {p}")
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

    p_mf = sub.add_parser("build-manifest",
                          help="Rebuild the site manifest from existing storage")
    p_mf.add_argument("--week", required=True, help="ISO week id to (re)build")
    p_mf.add_argument("--history-weeks", type=int, default=8)

    args = parser.parse_args(argv)

    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    if args.cmd == "estimate":
        return _cmd_estimate(args)
    if args.cmd == "build-manifest":
        return _cmd_build_manifest(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
