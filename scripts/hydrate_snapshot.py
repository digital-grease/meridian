#!/usr/bin/env python3
"""Restore a week's responses snapshot back into local raw storage.

CI runs the pipeline on ephemeral runners and only commits the
gzipped `data/snapshots/{week}/responses.jsonl.gz` artifact (plus the
manifests). The raw per-(prompt × model × week) JSONL files under
`data/raw/` are gitignored. So when a maintainer wants to run
additional samplers locally — e.g. Ollama for the control-group
baseline that hosted CI runners can't host — their `data/raw/` is
empty for that week, and a naive local run would overwrite the
bot's manifest with sampler-only data.

This script reverses the snapshot emit step: it reads
`data/snapshots/{week}/responses.jsonl.gz` and re-appends every
sample into `data/raw/{week}/{model}/{prompt}/`, mirroring what the
CI pipeline would have on disk if it had persisted it. After running
this, you can safely add new sampler runs locally and rebuild the
manifest from the merged storage.

Usage:
    uv run python scripts/hydrate_snapshot.py --week 2026-W18

Refuses to overwrite existing per-pair files; pass --force to
override.
"""
from __future__ import annotations

import argparse
import gzip
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from meridian.runners.base import Sample  # noqa: E402
from meridian.storage import LocalSampleStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--week", required=True, help="ISO week id, e.g. 2026-W18")
    p.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Path to responses.jsonl.gz (defaults to data/snapshots/{week}/responses.jsonl.gz)",
    )
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=REPO_ROOT / "data" / "raw",
        help="Local sample-store base dir (defaults to data/raw)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing per-pair files instead of refusing.",
    )
    args = p.parse_args(argv)

    snapshot = args.snapshot or (
        REPO_ROOT / "data" / "snapshots" / args.week / "responses.jsonl.gz"
    )
    if not snapshot.exists():
        print(f"snapshot not found: {snapshot}", file=sys.stderr)
        return 1

    store = LocalSampleStore(args.raw_dir)

    # Group samples by (model_id, prompt_id) so we can detect existing
    # files up-front and emit per-pair counts at the end.
    grouped: dict[tuple[str, str], list[Sample]] = defaultdict(list)
    with gzip.open(snapshot, "rt", encoding="utf-8") as gz:
        for line in gz:
            line = line.strip()
            if not line:
                continue
            s = Sample.model_validate_json(line)
            grouped[(s.model_id, s.prompt_id)].append(s)

    skipped: list[tuple[str, str]] = []
    written = 0
    for (model_id, prompt_id), samples in grouped.items():
        target = store.path(args.week, model_id, prompt_id)
        if target.exists() and target.stat().st_size > 0 and not args.force:
            skipped.append((model_id, prompt_id))
            continue
        if target.exists() and args.force:
            target.unlink()
        for s in samples:
            store.append(args.week, model_id, prompt_id, s)
            written += 1

    print(
        f"hydrated week {args.week}: {written} sample(s) across "
        f"{len(grouped) - len(skipped)}/{len(grouped)} pair(s)"
    )
    if skipped:
        print(
            f"skipped {len(skipped)} pair(s) with existing data "
            f"(re-run with --force to overwrite):",
            file=sys.stderr,
        )
        for m, pp in skipped[:10]:
            print(f"  {m} / {pp}", file=sys.stderr)
        if len(skipped) > 10:
            print(f"  ... and {len(skipped) - 10} more", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
