"""Re-classify stance that was published as unmeasured, and graft it in.

Why this exists
---------------
The stance classifier runs on the same Anthropic account as the measured
Claude runners. From 2026-W36 to 2026-W38 that account's prepaid balance
was empty, every classifier call failed, and every stance-bearing cell
for every model published as ``stance: "na"`` at
``stance_confidence: 0.0``, which reads like "took no position" and
means "never measured":

    2026-W36   claude-opus-4-8 2, claude-opus-5 1, llama3.2:3b 10
    2026-W37   gpt-5.5 10, llama3.2:3b 9 (the tenth came from the cache)
    2026-W38   llama3.2:3b 10

Unlike the 2026-07-24 truncation bug this is an *analysis* failure, not a
capture failure. The responses were captured in full and are published in
``data/snapshots/<week>/responses.jsonl.gz``; only the classifier call
failed, and failed calls are never cached
(``meridian/analysis/stance.py``). So the cells can be measured now,
exactly as they would have been then: same responses, same
representative-response rule, same pinned classifier model at
temperature 0.

Two steps, because only the first needs the API key
----------------------------------------------------
``classify`` (on the instance, where the key lives) rehydrates each
week's committed snapshot into a temporary store, re-runs the classifier
on exactly the cells the committed manifest publishes as unmeasured, and
writes one JSON line per cell to ``<out>/stance-results.jsonl``. It
writes nothing under ``data/`` except new lines in the classifier cache,
which is what a live run does too.

``graft`` (anywhere, no network) copies ``stance``, ``stance_confidence``
and ``stance_reason`` from that file onto those cells in both copies of
each manifest (``data/manifests/<week>.json`` and
``site/fixtures/manifest-<week>.json``), appends one entry to the
manifest's ``corrections`` list, validates the schema, and then proves
the edit: with those three keys removed from every current-week metric
record and ``corrections`` removed, the corrected manifest must equal the
published one exactly, every other cell's stance must be untouched, and
``history`` must be identical.

Side effects, checked rather than assumed
-----------------------------------------
* Drift, BH correction, change points, review flags and silent-update
  warnings do not read stance, so none of them moves; the diff check
  above would refuse the write if anything else did.
* No history entry anywhere carries a W36 to W38 stance: those weeks
  appear in later manifests' ``history`` recomputed from raw samples,
  which never runs the classifier (stance ``na``, confidence null). So
  no other week's file is touched.
* Later builds that backfill history from committed manifests (CI, or a
  host without the raw samples) will copy the corrected values. That is
  the intended direction.
* The S3 copies of these manifests are left as they were published, as
  in every earlier correction. Re-dispatching the publish workflow for
  one of these weeks would copy the uncorrected S3 manifest back over the
  correction: do not.

Usage:
    # on the instance, MERIDIAN_SECRETS_SSM=1 in the environment
    uv run python scripts/backfill_stance.py classify \\
        --weeks 2026-W36,2026-W37,2026-W38 --out /data/meridian/corrections/stance-<date>
    # anywhere
    uv run python scripts/backfill_stance.py graft \\
        --results <dir>/stance-results.jsonl --date <date> --dry-run
    uv run python scripts/backfill_stance.py graft \\
        --results <dir>/stance-results.jsonl --date <date> --write
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import gzip
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "site" / "src"))

from meridian.analysis.stance import STANCE_AXES, StanceResult  # noqa: E402
from meridian.pipeline.manifest_writer import (  # noqa: E402
    _stance_reason_code,
    write_manifest,
)
from meridian.pipeline.stance_collect import _representative_response  # noqa: E402
from meridian.storage import LocalSampleStore  # noqa: E402

MANIFESTS = REPO_ROOT / "data" / "manifests"
FIXTURES = REPO_ROOT / "site" / "fixtures"
SNAPSHOTS = REPO_ROOT / "data" / "snapshots"

DEFAULT_WEEKS = ("2026-W36", "2026-W37", "2026-W38")
RESULTS_NAME = "stance-results.jsonl"

#: The only metric-record fields this correction may change.
STANCE_FIELDS = ("stance", "stance_confidence", "stance_reason")

#: Reason codes meaning the classifier was asked and the call failed.
_CALL_FAILED = ("runner-error", "classifier-error")


# --------------------------------------------------------------------------
# Shared
# --------------------------------------------------------------------------

def _axes(manifest: dict) -> dict[str, str]:
    return {p["prompt_id"]: p["axis"] for p in manifest.get("prompts", [])}


def unmeasured_cells(manifest: dict) -> list[tuple[str, str]]:
    """``(prompt_id, model_id)`` of every stance-bearing current-week cell
    published as ``na`` at confidence exactly 0.0: a classifier call that
    failed. 0.85 is a scored cell, 1.0 a cell with nothing to classify,
    null a week with stance disabled."""
    axes = _axes(manifest)
    out = []
    for rec in manifest.get("metrics", []):
        if axes.get(rec["prompt_id"]) not in STANCE_AXES:
            continue
        conf = rec.get("stance_confidence")
        if (
            rec.get("stance") == "na"
            and isinstance(conf, (int, float))
            and not isinstance(conf, bool)
            and conf == 0.0
        ):
            out.append((rec["prompt_id"], rec["model_id"]))
    return out


def _manifest_paths(week: str, manifests: Path, fixtures: Path) -> list[Path]:
    return [manifests / f"{week}.json", fixtures / f"manifest-{week}.json"]


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------

def rehydrate_week(snapshot: Path, dest: Path, week: str) -> LocalSampleStore:
    """One week's published snapshot back into a LocalSampleStore."""
    store = LocalSampleStore(dest)
    buckets: dict[tuple[str, str], list[str]] = {}
    with gzip.open(snapshot, "rt", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            buckets.setdefault((rec["model_id"], rec["prompt_id"]), []).append(
                line if line.endswith("\n") else line + "\n"
            )
    for (model_id, prompt_id), lines in buckets.items():
        p = store.path(week, model_id, prompt_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(lines), encoding="utf-8")
    return store


async def classify_cells(
    classifier,
    store: LocalSampleStore,
    prompts: dict,
    week: str,
    cells: list[tuple[str, str]],
    *,
    classifier_name: str,
) -> list[dict]:
    """Classify ``cells`` the way ``stance_collect`` would have.

    Same representative response (longest non-refusal), same classifier,
    and the same fallbacks: a cell with no substantive response is ``na``
    at 1.0 without a call, and an exception becomes a failed result
    rather than aborting the run.
    """
    out = []
    for prompt_id, model_id in cells:
        prompt = prompts[prompt_id]
        samples = store.read(week, model_id, prompt_id)
        response = _representative_response(samples)
        if response is None:
            result = StanceResult("na", 1.0, "no-substantive-response")
            digest = None
        else:
            digest = hashlib.sha256(response.encode("utf-8")).hexdigest()
            try:
                result = await classifier.classify(
                    prompt_id=prompt_id,
                    axis=prompt.axis,
                    prompt_text=prompt.text,
                    response_text=response,
                )
            except Exception as e:  # pragma: no cover - real-API path
                result = StanceResult(
                    "na", 0.0, f"classifier-error: {type(e).__name__}"
                )
        out.append({
            "week_id": week,
            "prompt_id": prompt_id,
            "model_id": model_id,
            "stance": result.stance,
            "stance_confidence": result.confidence,
            "stance_reason": _stance_reason_code(result.reason),
            "samples": len(samples),
            "response_sha256": digest,
            "classifier": classifier_name,
            "classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
    return out


def _failed(rec: dict) -> bool:
    return bool(rec.get("stance_reason")) and str(rec["stance_reason"]).startswith(
        _CALL_FAILED
    )


def cmd_classify(args: argparse.Namespace) -> int:
    from meridian.config import load_config
    from meridian.corpus import load_corpus
    from meridian.pipeline.stance_runner import build_stance_classifier
    from meridian.secrets import resolve_ssm_secrets

    out_dir: Path = args.out
    out_file = out_dir / RESULTS_NAME
    if out_file.exists():
        print(f"REFUSED: {out_file} exists; results are never overwritten.",
              file=sys.stderr)
        return 2

    resolve_ssm_secrets()
    config = load_config(args.config)
    if not config.stance.enabled:
        print("REFUSED: stance is disabled in the config.", file=sys.stderr)
        return 2
    classifier = build_stance_classifier(config.stance, repo_root=REPO_ROOT)
    name = f"{config.stance.provider}/{config.stance.model_id}"
    prompts = {p.id: p for p in load_corpus().all()}

    results: list[dict] = []
    tmp = Path(tempfile.mkdtemp(prefix="meridian-stance-backfill-"))
    try:
        for week in args.weeks:
            path = args.manifests_dir / f"{week}.json"
            snap = args.snapshots_dir / week / "responses.jsonl.gz"
            if not path.exists() or not snap.exists():
                print(f"REFUSED: {week} needs both {path} and {snap}.",
                      file=sys.stderr)
                return 2
            manifest = json.loads(path.read_text(encoding="utf-8"))
            cells = unmeasured_cells(manifest)
            unknown = [c for c in cells if c[0] not in prompts]
            if unknown:
                print(f"REFUSED: {week} cells not in the corpus: {unknown}",
                      file=sys.stderr)
                return 2
            store = rehydrate_week(snap, tmp / week, week)
            week_results = asyncio.run(classify_cells(
                classifier, store, prompts, week, cells, classifier_name=name,
            ))
            results.extend(week_results)
            tally: dict[str, int] = {}
            for r in week_results:
                key = r["stance_reason"] or r["stance"]
                tally[key] = tally.get(key, 0) + 1
            print(f"{week}: {len(cells)} unmeasured cell(s) re-classified: {tally}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    failed = [r for r in results if _failed(r)]
    print(f"wrote {len(results)} result(s) to {out_file}")
    if failed:
        print(
            f"CLASSIFIER STILL FAILING on {len(failed)} of {len(results)} "
            f"cell(s) (first: {failed[0]['stance_reason']}). graft will refuse "
            f"this file. Fix the key or balance and re-run classify into a new "
            f"--out directory; successful calls are cached and cost nothing.",
            file=sys.stderr,
        )
        return 1
    return 0


# --------------------------------------------------------------------------
# graft
# --------------------------------------------------------------------------

def strip_stance(manifest: dict) -> dict:
    """The manifest minus everything this correction may change."""
    out = copy.deepcopy(manifest)
    out.pop("corrections", None)
    for rec in out.get("metrics", []):
        for f in STANCE_FIELDS:
            rec.pop(f, None)
    return out


def graft_week(
    manifest: dict, results: list[dict], correction: dict | None,
) -> tuple[dict, list[tuple[str, str, str, str]]]:
    """Return ``(corrected copy, changes)``. Pure; ``manifest`` untouched.

    ``changes`` is ``(prompt_id, model_id, old, new)`` per grafted cell.
    Raises ``ValueError`` when the results do not cover exactly the cells
    the manifest publishes as unmeasured, or when any of them is itself a
    failed call: a correction that leaves a cell half-done is not one.
    """
    targets = set(unmeasured_cells(manifest))
    by_key = {(r["prompt_id"], r["model_id"]): r for r in results}
    stray = sorted(set(by_key) - targets)
    if stray:
        raise ValueError(f"results for cells that are not unmeasured: {stray}")
    missing = sorted(targets - set(by_key))
    if missing:
        raise ValueError(f"no result for unmeasured cell(s): {missing}")
    failed = sorted(k for k, r in by_key.items() if _failed(r))
    if failed:
        raise ValueError(f"result is itself a failed call for: {failed}")

    out = copy.deepcopy(manifest)
    changes = []
    for rec in out.get("metrics", []):
        key = (rec["prompt_id"], rec["model_id"])
        r = by_key.get(key)
        if r is None:
            continue
        old = f"{rec.get('stance')}@{rec.get('stance_confidence')}"
        rec["stance"] = r["stance"]
        rec["stance_confidence"] = r["stance_confidence"]
        rec["stance_reason"] = r["stance_reason"]
        changes.append((key[0], key[1], old, f"{r['stance']}@{r['stance_confidence']}"))
    if changes and correction is not None:
        out.setdefault("corrections", []).append(
            dict(correction, cells=len(changes))
        )
    return out, changes


def verify_graft(before: dict, after: dict, results: list[dict]) -> None:
    """Prove only the three stance fields of the listed cells moved."""
    if strip_stance(before) != strip_stance(after):
        raise AssertionError(
            "something other than stance fields and corrections changed"
        )
    if before.get("history") != after.get("history"):
        raise AssertionError("history changed")
    allowed = {(r["prompt_id"], r["model_id"]) for r in results}
    for b, a in zip(before.get("metrics", []), after.get("metrics", [])):
        if (b["prompt_id"], b["model_id"]) in allowed:
            continue
        for f in STANCE_FIELDS:
            if b.get(f, "<absent>") != a.get(f, "<absent>"):
                raise AssertionError(
                    f"stance of an untargeted cell changed: "
                    f"{b['prompt_id']} / {b['model_id']} {f}"
                )
    prior = before.get("corrections", [])
    if after.get("corrections", [])[: len(prior)] != prior:
        raise AssertionError("an earlier correction entry was altered")


def _load_results(path: Path) -> dict[str, list[dict]]:
    by_week: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            by_week.setdefault(rec["week_id"], []).append(rec)
    return by_week


def cmd_graft(args: argparse.Namespace) -> int:
    from schema import Manifest

    by_week = _load_results(args.results)
    if not by_week:
        print(f"REFUSED: {args.results} holds no results.", file=sys.stderr)
        return 2
    classifiers = sorted({r["classifier"] for rs in by_week.values() for r in rs})
    classified_on = min(
        r["classified_at"][:10] for rs in by_week.values() for r in rs
    )
    report = args.report or f"/reports/{args.date}-stance-classifier-correction/"

    planned: list[tuple[str, dict, list[Path]]] = []
    table: list[tuple[str, str, str, str, str, str]] = []
    for week in sorted(by_week):
        paths = _manifest_paths(week, args.manifests_dir, args.fixtures_dir)
        texts = [p.read_text(encoding="utf-8") for p in paths if p.exists()]
        if len(texts) != len(paths) or len(set(texts)) != 1:
            print(f"REFUSED: {week}: both copies must exist and be identical: "
                  f"{', '.join(str(p) for p in paths)}", file=sys.stderr)
            return 2
        before = json.loads(texts[0])
        if json.dumps(before, indent=2, sort_keys=True) + "\n" != texts[0]:
            print(f"REFUSED: {week}: the file is not in write_manifest's format, "
                  f"so rewriting it would churn bytes this correction does not "
                  f"own.", file=sys.stderr)
            return 2
        results = by_week[week]
        cells = len(results)
        correction = {
            "date": args.date,
            "fields": list(STANCE_FIELDS),
            "summary": (
                f"Stance re-classified for {cells} stance-bearing cell(s) "
                f"published as na at confidence 0.0 because every "
                f"classifier call failed on an exhausted Anthropic balance. "
                f"Classified {classified_on} with {', '.join(classifiers)} "
                f"from this week's published responses, using the same "
                f"representative-response rule. No other field changed."
            ),
            "report": report,
        }
        try:
            after, changes = graft_week(before, results, correction)
        except ValueError as e:
            print(f"REFUSED: {week}: {e}", file=sys.stderr)
            return 2
        if not changes:
            print(f"{week}: nothing to correct (already corrected?)")
            continue
        verify_graft(before, after, results)
        Manifest.model_validate(after)
        planned.append((week, after, paths))
        digests = {(r["prompt_id"], r["model_id"]): r.get("response_sha256") for r in results}
        for pid, mid, old, new in changes:
            # The digest goes into the public report so a reader can find
            # the exact response that was classified in the week's
            # responses.jsonl.gz; the results file itself stays in S3.
            table.append((week, mid, pid, old, new, digests.get((pid, mid)) or "none"))
        print(f"{week}: {len(changes)} cell(s) corrected; diff check passed "
              f"(only {', '.join(STANCE_FIELDS)} and corrections differ)")

    if table:
        print("\n| Week | Model | Prompt | Published | Corrected | Response SHA-256 |")
        print("| --- | --- | --- | --- | --- | --- |")
        for week, mid, pid, old, new, digest in table:
            print(f"| {week} | {mid} | `{pid}` | {old} | {new} | `{digest}` |")
        print()

    if not args.write:
        print("dry run: nothing written.")
        return 0
    for week, after, paths in planned:
        write_manifest(after, paths)
        print(f"wrote {week}: " + ", ".join(str(p) for p in paths))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify", help="re-classify unmeasured cells (calls the API)")
    c.add_argument("--weeks", default=",".join(DEFAULT_WEEKS),
                   type=lambda s: [w.strip() for w in s.split(",") if w.strip()])
    c.add_argument("--out", type=Path, required=True,
                   help=f"directory for {RESULTS_NAME}; must not already hold one")
    c.add_argument("--config", type=Path, default=None)
    c.add_argument("--manifests-dir", type=Path, default=MANIFESTS)
    c.add_argument("--snapshots-dir", type=Path, default=SNAPSHOTS)

    g = sub.add_parser("graft", help="graft classify results into the manifests (no network)")
    g.add_argument("--results", type=Path, required=True)
    g.add_argument("--date", default=date.today().isoformat(),
                   help="correction date for the manifest entry and report slug")
    g.add_argument("--report", default=None,
                   help="report path (default /reports/<date>-stance-classifier-correction/)")
    g.add_argument("--manifests-dir", type=Path, default=MANIFESTS)
    g.add_argument("--fixtures-dir", type=Path, default=FIXTURES)
    mode = g.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--write", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "classify":
        return cmd_classify(args)
    try:
        date.fromisoformat(args.date)
    except ValueError:
        print(f"REFUSED: --date {args.date!r} is not an ISO date.", file=sys.stderr)
        return 2
    return cmd_graft(args)


if __name__ == "__main__":
    raise SystemExit(main())
