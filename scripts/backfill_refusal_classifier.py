"""Backfill published refusal metrics after the U+2019 classifier fix.

Why this exists
---------------
Until 2026-07-16 the refusal classifier matched ASCII-only markers
("i can't help") against response text. gpt-5.1 emits U+2019 apostrophes
exclusively and Claude emits ASCII exclusively, so the marker list
matched Anthropic and silently ignored OpenAI. Published refusal rate
for gpt-5.1 on the refusal-boundary axis was 0.00 against a true ~0.98.
See :mod:`meridian.analysis.text_norm`.

The raw response bodies were never wrong, so every corrected number here
is recomputed from the published snapshots in ``data/snapshots/``. This
script rewrites only the metrics the bug actually touched:

    refusal_rate, refusal_ci, refusal_drift.p_value

plus the two values that are *derived* from the whole family and
therefore cannot stay stale once refusal p-values move:

    {refusal,hedge,length}_drift.adjusted_p_value / .significant_after_bh
    change_points.refusal_rate

Everything else (hedge_density, length, stance, embedding_centroid_shift,
and the raw hedge/length p-values) is carried through byte-identical.
That is deliberate: hedge and length were verified unaffected by the bug
(delta 0.000 across all weeks), stance and embeddings would cost LLM
calls and a model load to regenerate, and recomputing the permutation
p-values would churn them by RNG noise that has nothing to do with the
correction. A reviewer should be able to diff the manifests and see only
refusal-derived movement.

Usage:
    uv run python scripts/backfill_refusal_classifier.py --dry-run
    uv run python scripts/backfill_refusal_classifier.py --write
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "site" / "src"))

from meridian.corpus.corpus import load_corpus  # noqa: E402
from meridian.pipeline.manifest_writer import (  # noqa: E402
    _apply_bh_correction,
    _metrics_for_week,
    _populate_change_points,
    write_manifest,
)
from meridian.storage.local import LocalSampleStore  # noqa: E402

MANIFESTS = REPO_ROOT / "data" / "manifests"
SNAPSHOTS = REPO_ROOT / "data" / "snapshots"
FIXTURES = REPO_ROOT / "site" / "fixtures"

# The weekly pipeline does not pass a bootstrap seed, so its confidence
# intervals are redrawn on every run and are reproducible only to within
# sampling noise. That is tolerable for a live pipeline, but not for a
# correction: a reader who re-runs this script to audit our numbers
# should get byte-identical manifests, not CIs that wobble. Seeding here
# makes the correction exactly reproducible. It is deliberately a fixed,
# published constant rather than a hidden default.
BACKFILL_SEED = 20260716


def _output_paths(week_id: str) -> list[Path]:
    """Mirror :func:`meridian.pipeline.cli._output_paths`.

    The pipeline writes every manifest to two places, and they are
    byte-identical. ``site/fixtures/manifest-<week>.json`` is the copy
    the site actually renders (weekly-build.yml picks the newest one by
    filename), so a correction that only rewrote data/manifests/ would
    fix the archive and leave the live dashboard wrong.
    """
    return [
        FIXTURES / f"manifest-{week_id}.json",
        MANIFESTS / f"{week_id}.json",
    ]


# Fields the correction is allowed to move on a current-week record.
_REFUSAL_FIELDS = ("refusal_rate", "refusal_ci")


def rehydrate(dest: Path) -> LocalSampleStore:
    """Rebuild a LocalSampleStore layout from the published snapshots.

    The snapshots are the public record of what each provider actually
    said; they are the input of record for this correction. Writing them
    into a scratch store lets the real pipeline functions recompute the
    metrics, rather than this script reimplementing their semantics
    (notably `_prior_week_for_model`, which compares against the last
    week a model *ran*, not the calendar-previous week).
    """
    store = LocalSampleStore(dest)
    n = 0
    for snap in sorted(SNAPSHOTS.glob("*/responses.jsonl.gz")):
        week = snap.parent.name
        buckets: dict[tuple[str, str], list[str]] = {}
        with gzip.open(snap, "rt", encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                buckets.setdefault((rec["model_id"], rec["prompt_id"]), []).append(line)
                n += 1
        for (model_id, prompt_id), lines in buckets.items():
            p = store.path(week, model_id, prompt_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("".join(lines), encoding="utf-8")
    print(f"rehydrated {n} samples across {len(store.weeks())} weeks -> {dest}")
    return store


def _key(rec: dict) -> tuple[str, str]:
    return (rec["prompt_id"], rec["model_id"])


def correct_week(manifest: dict, corrected: list[dict]) -> list[tuple]:
    """Graft corrected refusal values onto one manifest, in place.

    Returns a list of (prompt_id, model_id, old_rate, new_rate) for every
    record whose refusal_rate moved.
    """
    by_key = {_key(r): r for r in corrected}
    changes = []

    for rec in manifest["metrics"]:
        fresh = by_key.get(_key(rec))
        if fresh is None:
            continue
        old = rec["refusal_rate"]
        for f in _REFUSAL_FIELDS:
            rec[f] = fresh[f]
        # Raw refusal p-value moves because the refusal series moved.
        # Keep the published hedge/length raw p-values: those series are
        # unchanged, and re-running the permutation would only add RNG
        # noise to a correction that should be attributable.
        if rec.get("refusal_drift") is not None and fresh.get("refusal_drift") is not None:
            rec["refusal_drift"]["p_value"] = fresh["refusal_drift"]["p_value"]
        if old != rec["refusal_rate"]:
            changes.append((rec["prompt_id"], rec["model_id"], old, rec["refusal_rate"]))

    # History entries are recomputed-from-raw records (stance "na", no
    # embeddings, no drift tests), so only the refusal values need the
    # same correction.
    for snap in manifest.get("history", []):
        fresh_hist = {_key(r): r for r in HISTORY_CACHE.get(snap["week_id"], [])}
        for rec in snap["metrics"]:
            f = fresh_hist.get(_key(rec))
            if f is None:
                continue
            for fld in _REFUSAL_FIELDS:
                rec[fld] = f[fld]

    # BH spans the whole within-week family, so moving refusal p-values
    # legitimately re-ranks hedge and length too.
    _apply_bh_correction(manifest["metrics"])
    # Change points must be re-detected against the corrected series, or
    # the site would keep claiming break-points in a series that no
    # longer exists.
    for rec in manifest["metrics"]:
        rec["change_points"]["refusal_rate"] = []
    _populate_change_points(manifest["metrics"], manifest["history"])
    return changes


HISTORY_CACHE: dict[str, list[dict]] = {}


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    g.add_argument("--write", action="store_true", help="rewrite data/manifests/*.json")
    args = ap.parse_args()

    corpus = load_corpus()
    prompts = corpus.public()

    tmp = Path(tempfile.mkdtemp(prefix="meridian-backfill-"))
    try:
        store = rehydrate(tmp)

        # Recompute every week once, oldest-first, with the real pipeline
        # code path and the fixed classifier.
        print("\nrecomputing metrics from snapshots with the fixed classifier...")
        for week in store.weeks():
            HISTORY_CACHE[week] = _metrics_for_week(
                store, week, prompts, BACKFILL_SEED, include_drift_tests=True,
            )
            print(f"  {week}: {len(HISTORY_CACHE[week])} records")

        total = 0
        print("\ncorrections per week (refusal_rate):\n")
        for path in sorted(MANIFESTS.glob("*.json")):
            week = path.stem
            manifest = json.loads(path.read_text())
            corrected = HISTORY_CACHE.get(week)
            if corrected is None:
                print(f"  {week}: NO SNAPSHOT — skipped")
                continue
            changes = correct_week(manifest, corrected)
            total += len(changes)
            if changes:
                worst = sorted(changes, key=lambda c: abs(c[3] - c[2]), reverse=True)[:3]
                print(f"  {week}: {len(changes):3} record(s) corrected. largest:")
                for pid, mid, old, new in worst:
                    print(f"        {mid:16} {pid:28} {old:.2f} -> {new:.2f}")
            else:
                print(f"  {week}: no change")

            if args.write:
                # Validate against the published schema before writing: a
                # correction that breaks the site contract is worse than
                # the bug it fixes.
                from schema import Manifest
                Manifest.model_validate(manifest)
                write_manifest(manifest, _output_paths(week))

        print(f"\n{total} metric record(s) corrected across all weeks.")
        if args.dry_run:
            print("dry run — nothing written.")
        else:
            print(f"rewrote {len(list(MANIFESTS.glob('*.json')))} manifest(s).")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
