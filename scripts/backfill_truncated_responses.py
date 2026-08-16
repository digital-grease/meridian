"""Backfill published metrics that counted empty completions as answers.

Why this exists
---------------
gpt-5.5 is reasoning-default: its completion cap covers reasoning tokens
plus visible output. Against the shared 1024-token cap it repeatedly
reasoned to the limit and returned HTTP 200 with an empty body and
``finish_reason="length"``. Nothing downstream treated that as a hole,
so an empty string was scored as "did not refuse", summarized as length
0, and embedded as-is.

Affected published cells, all gpt-5.5, both weeks it has run:

    2026-W27   sci-iq-heritability     20/20 empty
               hist-churchill-bengal   15/20 empty
               pol-israel-palestine    12/20 empty
    2026-W29   sci-iq-heritability     20/20 empty
               pol-israel-palestine    15/20 empty
               hist-churchill-bengal    8/20 empty

No other model is affected: gpt-5.1, claude-opus-4-7, claude-opus-4-8
and llama3.2:3b have zero empty responses across every published
snapshot.

What this can and cannot fix
----------------------------
The 2026-07-16 refusal-classifier correction was an *analysis* bug: the
raw responses were complete and correct, so every number could be
recomputed exactly. This one is a *capture* bug. The responses gpt-5.5
would have given were never received and cannot be recovered. Re-asking
the model today would return the model as of today, labelled as a week
that has passed, which would defeat the point of a longitudinal record.

So this script does not invent the missing data. It recomputes each
affected cell from the samples that *are* usable, and where nothing is
usable it removes the record entirely and lists the cell under the
manifest's ``unmeasured`` key. Cells whose usable count falls below
MIN_SAMPLES_FOR_PUBLICATION are flagged rather than deleted: an
under-powered measurement, labelled as such, is still evidence.

Deliberately NOT applied here: the new top-N largest-delta review
flagging. It is a live-pipeline feature, and running it over historical
weeks would churn ``flagged``/``flag_reason`` on records this correction
has no business touching. A reviewer should be able to diff these
manifests and see only truncation-derived movement.

Usage:
    uv run python scripts/backfill_truncated_responses.py --dry-run
    uv run python scripts/backfill_truncated_responses.py --write
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

#: Same fixed constant as the refusal correction, for the same reason:
#: an auditor re-running this must get byte-identical output, not
#: confidence intervals that wobble with a fresh RNG draw.
BACKFILL_SEED = 20260724

#: Fields recomputed from the usable-sample subset. Stance is excluded
#: on purpose: `stance_collect._representative_response` already skipped
#: empty text before choosing which response to classify, so the
#: published stance labels were computed on real responses and are not
#: affected by this bug. Re-running them would cost LLM calls and churn
#: the manifests for no correction.
_RECOMPUTED_FIELDS = (
    "n_samples",
    "unusable_samples",
    "refusal_rate",
    "refusal_ci",
    "hedge_density",
    "length",
    "flagged_for_review",
    "flag_reason",
)

#: Grafting is restricted to cells that actually contained unusable
#: samples. This matters more than it looks: `refusal_ci` is a bootstrap
#: draw and the drift entries are permutation p-values, and the live
#: pipeline runs them unseeded. Recomputing them here under a fixed seed
#: would rewrite every CI and p-value in all thirteen weeks with numbers
#: that differ only by RNG — burying six real corrections in ~700 lines
#: of noise and making the correction unreviewable. The 2026-07-16
#: refusal correction drew the same line for the same reason.
#:
#: BH-adjusted values and change points are still recomputed globally:
#: those are deterministic functions of the (now corrected) family, so
#: leaving them stale would be an actual inconsistency rather than noise.


def _is_affected(fresh: dict) -> bool:
    return bool(fresh.get("unusable_samples"))


def _output_paths(week_id: str) -> list[Path]:
    """Both copies the pipeline writes, kept byte-identical.

    ``site/fixtures/manifest-<week>.json`` is what the site actually
    renders, so a correction that only rewrote data/manifests/ would fix
    the archive and leave the live dashboard wrong.
    """
    return [
        FIXTURES / f"manifest-{week_id}.json",
        MANIFESTS / f"{week_id}.json",
    ]


def rehydrate(dest: Path) -> LocalSampleStore:
    """Rebuild a LocalSampleStore from the published snapshots.

    The snapshots are the public record of what each provider actually
    sent, and they are unchanged by this correction: the empty responses
    were captured faithfully, we simply scored them as answers. Running
    the real pipeline functions over a rehydrated store keeps this
    script from reimplementing their semantics.
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


def correct_week(
    manifest: dict,
    corrected: list[dict],
    unmeasured: list[dict],
) -> tuple[list[tuple], list[tuple]]:
    """Graft recomputed values onto one manifest, in place.

    Returns ``(changed, dropped)`` where ``changed`` is
    (prompt_id, model_id, old_n, new_n) for records whose sample basis
    moved, and ``dropped`` is (prompt_id, model_id, unusable) for cells
    that lost their record entirely.
    """
    by_key = {_key(r): r for r in corrected}
    unmeasured_keys = {(u["prompt_id"], u["model_id"]) for u in unmeasured}
    changed: list[tuple] = []
    dropped: list[tuple] = []

    kept: list[dict] = []
    for rec in manifest["metrics"]:
        k = _key(rec)
        if k in unmeasured_keys:
            u = next(x for x in unmeasured if (x["prompt_id"], x["model_id"]) == k)
            dropped.append((rec["prompt_id"], rec["model_id"], u["unusable_samples"]))
            continue
        fresh = by_key.get(k)
        if fresh is not None and _is_affected(fresh):
            old_n = rec.get("n_samples")
            for f in _RECOMPUTED_FIELDS:
                rec[f] = fresh[f]
            # Raw drift p-values move because this cell's series did.
            for d in ("refusal_drift", "hedge_drift", "length_drift"):
                if rec.get(d) is not None and fresh.get(d) is not None:
                    rec[d]["p_value"] = fresh[d]["p_value"]
            if old_n != rec["n_samples"]:
                changed.append(
                    (rec["prompt_id"], rec["model_id"], old_n, rec["n_samples"])
                )
        else:
            # Unaffected cell: only gains the new field, at its true value
            # of zero. Every published number stays byte-identical.
            rec.setdefault("unusable_samples", 0)
        kept.append(rec)
    manifest["metrics"] = kept
    manifest["unmeasured"] = unmeasured

    # History carries recomputed-from-raw records (stance "na", no
    # embeddings, no drift tests), so it needs the same treatment or the
    # site's sparklines keep plotting the fabricated zeros.
    for snap in manifest.get("history", []):
        fresh_hist = {_key(r): r for r in HISTORY_CACHE.get(snap["week_id"], [])}
        drop_hist = {
            (u["prompt_id"], u["model_id"])
            for u in UNMEASURED_CACHE.get(snap["week_id"], [])
        }
        rows = []
        for rec in snap["metrics"]:
            k = _key(rec)
            if k in drop_hist:
                continue
            f = fresh_hist.get(k)
            if f is not None and _is_affected(f):
                for fld in _RECOMPUTED_FIELDS:
                    rec[fld] = f[fld]
            else:
                rec.setdefault("unusable_samples", 0)
            rows.append(rec)
        snap["metrics"] = rows

    # BH spans the whole within-week family, so moving any raw p-value
    # legitimately re-ranks the rest.
    _apply_bh_correction(manifest["metrics"])
    # Change points must be re-detected: the old series contained
    # fabricated zeros, and a break-point detected against those is an
    # artifact of the bug.
    for rec in manifest["metrics"]:
        rec["change_points"] = {
            "refusal_rate": [], "hedge_density": [], "length_median": [],
        }
    # week_id is required: the detector needs the newest point's week
    # identity to tell a real week-over-week transition from a shift
    # that accumulated across weeks the model never ran.
    _populate_change_points(
        manifest["metrics"], manifest["history"], manifest["snapshot"]["week_id"]
    )
    # The review page reads this list, not the per-record flag. Leaving
    # it stale would flag the corrected cells in the data and still show
    # a reviewer an empty worklist, which is the failure that let this
    # bug survive two weeks in the first place.
    manifest["flagged"] = [
        m["prompt_id"] for m in manifest["metrics"] if m["flagged_for_review"]
    ]
    return changed, dropped


HISTORY_CACHE: dict[str, list[dict]] = {}
UNMEASURED_CACHE: dict[str, list[dict]] = {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    g.add_argument("--write", action="store_true", help="rewrite data/manifests/*.json")
    args = ap.parse_args(argv)

    corpus = load_corpus()
    prompts = corpus.public()

    tmp = Path(tempfile.mkdtemp(prefix="meridian-truncation-backfill-"))
    try:
        store = rehydrate(tmp)

        print("\nrecomputing metrics from snapshots, excluding unusable samples...")
        for week in store.weeks():
            unmeasured: list[dict] = []
            HISTORY_CACHE[week] = _metrics_for_week(
                store, week, prompts, BACKFILL_SEED, include_drift_tests=True,
                unmeasured_out=unmeasured,
            )
            UNMEASURED_CACHE[week] = unmeasured
            note = f", {len(unmeasured)} unmeasurable cell(s)" if unmeasured else ""
            print(f"  {week}: {len(HISTORY_CACHE[week])} records{note}")

        total_changed = total_dropped = 0
        print("\ncorrections per week:\n")
        for path in sorted(MANIFESTS.glob("*.json")):
            week = path.stem
            manifest = json.loads(path.read_text())
            corrected = HISTORY_CACHE.get(week)
            if corrected is None:
                print(f"  {week}: NO SNAPSHOT — skipped")
                continue
            changed, dropped = correct_week(
                manifest, corrected, UNMEASURED_CACHE.get(week, [])
            )
            total_changed += len(changed)
            total_dropped += len(dropped)
            if changed or dropped:
                print(f"  {week}:")
                for pid, mid, old_n, new_n in changed:
                    print(f"        n {old_n:3} -> {new_n:3}   {mid:12} {pid}")
                for pid, mid, unusable in dropped:
                    print(f"        RECORD REMOVED ({unusable} unusable)  "
                          f"{mid:12} {pid}")
            else:
                print(f"  {week}: no change")

            if args.write:
                # Validate against the published schema before writing: a
                # correction that breaks the site contract is worse than
                # the bug it fixes.
                from schema import Manifest
                Manifest.model_validate(manifest)
                write_manifest(manifest, _output_paths(week))

        print(f"\n{total_changed} record(s) recomputed, "
              f"{total_dropped} record(s) removed as unmeasurable.")
        if args.dry_run:
            print("dry run — nothing written.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
