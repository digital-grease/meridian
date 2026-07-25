#!/usr/bin/env python3
"""Fail loudly when a published week's run recorded failures.

Context: the weekly-pipeline publish job pulls the raw artifacts (manifest,
snapshot, run_log) from S3 and commits them to ``main`` (append-only
retention is a hard rule). Historically the job only went red when the S3
*sync* itself failed, so a run that sampled but recorded failed pairs --
e.g. 2026-W27, where ``gpt-5.5`` 400'd on every zero-temperature request --
published as a green "success" with nothing surfacing the failure.

This script closes that gap. It is invoked *after* the commit step (so the
raw record is preserved regardless) and exits non-zero when the run_log
entry for the target week reports failed pairs or errors, turning the
workflow red and firing the issue-on-failure step.

Usage:
    check_run_health.py <ISO-WEEK> [--run-log PATH]

When ``$GITHUB_STEP_SUMMARY`` / ``$GITHUB_ENV`` are present (i.e. running
under Actions) it writes a run summary and a ``HEALTH_DETAIL=`` line and
emits a ``::error::`` annotation; run locally it just prints and exits.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def latest_entry_for_week(entries: list[dict], week: str) -> dict | None:
    """Return the most recent run_log entry for ``week`` (last wins), or
    None if the week never ran."""
    match = None
    for rec in entries:
        if rec.get("week_id") == week:
            match = rec
    return match


def evaluate(entry: dict) -> tuple[bool, str]:
    """Return ``(healthy, detail)`` for a run_log entry.

    Unhealthy when any pair failed, any error was recorded, or any
    sample came back with no usable content. A run that merely *skips*
    on-cadence batches (e.g. a thinking-default model that only exposes
    the API-default temperature, so the zero-temp batch is skipped) is
    healthy: skips don't increment ``pairs_failed``.

    The unusable-sample check was added 2026-07-24. Until then this
    function only read ``pairs_failed``/``errors``, which are request
    *failures* — so gpt-5.5 returning HTTP 200 with an empty body on 43
    of 600 samples passed as a clean run for two consecutive weeks. A
    request that succeeds and returns nothing is not an error by any
    transport measure and still has to stop the pipeline.
    """
    failed = int(entry.get("pairs_failed", 0) or 0)
    errors = entry.get("errors") or []
    unusable = entry.get("unusable_samples") or {}
    n_unusable = sum(sum(v.values()) for v in unusable.values())
    week = entry.get("week_id", "?")
    summary = (
        f"week={week} pairs_complete={entry.get('pairs_complete')} "
        f"pairs_failed={failed} pairs_skipped={entry.get('pairs_skipped')} "
        f"errors={len(errors)} unusable_samples={n_unusable}"
    )
    if failed > 0 or errors:
        first = errors[0] if errors else {}
        msg = str(first.get("message", ""))[:200]
        detail = (
            f"{failed} failed pair(s) in {week}. "
            f"first error: {first.get('provider')}/{first.get('model_id')} "
            f"{first.get('error_type')}: {msg}"
        )
        return False, detail
    if n_unusable:
        breakdown = "; ".join(
            f"{runner} " + ", ".join(f"{r}={c}" for r, c in sorted(reasons.items()))
            for runner, reasons in sorted(unusable.items())
        )
        detail = (
            f"{n_unusable} sample(s) in {week} returned no usable content "
            f"({breakdown}). They are stored but excluded from every metric, "
            f"so affected cells are under-sampled or absent. A "
            f"'truncated-empty' reason means the model exhausted its "
            f"completion budget without emitting output: raise that runner's "
            f"max_tokens in meridian/config.yaml."
        )
        return False, detail
    return True, summary


def _read_entries(path: str) -> list[dict]:
    entries: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _emit(annotation_title: str, message: str) -> None:
    """Emit a GitHub Actions error annotation (harmless plain text locally)."""
    print(f"::error title={annotation_title}::{message}")


def _write_env(key: str, value: str) -> None:
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def _write_summary(text: str) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(f"### Pipeline run health\n\n{text}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("week", help="ISO week id, e.g. 2026-W27")
    ap.add_argument("--run-log", default="data/run_log.jsonl")
    args = ap.parse_args(argv)

    try:
        entries = _read_entries(args.run_log)
    except FileNotFoundError:
        _emit("Pipeline health", f"run_log not found at {args.run_log}")
        return 1

    entry = latest_entry_for_week(entries, args.week)
    if entry is None:
        _emit("Pipeline health", f"no run_log entry for {args.week}")
        return 1

    healthy, detail = evaluate(entry)
    _write_summary(detail)
    if not healthy:
        _emit("Pipeline run had failures", detail)
        _write_env("HEALTH_DETAIL", detail)
        return 1

    print(f"run healthy: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
