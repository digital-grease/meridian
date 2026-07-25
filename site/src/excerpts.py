"""Pick sample responses to show on a prompt's drill-down page.

Why this exists
---------------
CLAUDE.md's dashboard spec calls for "individual prompt drill-downs
(with sample responses shown)". Until now the drill-down showed only
aggregate numbers, so a reader had to take our refusal rate and hedge
density on faith or go download a gzipped JSONL and grep it. For a
project whose entire pitch is "audit us", the receipts should be on the
page.

Selection rule
--------------
Cherry-picking is the first accusation this project expects (CLAUDE.md,
"Accusations to preempt"), so the choice of which responses to display
cannot be a judgement call. The rule is fixed, mechanical, and stated on
the page itself:

    show the shortest, the median-length, and the longest usable
    response, ties broken by request_index

That triple is chosen because it exposes the *spread* the metrics
compress. Median alone would imply the model says one thing; the
extremes show how much N=20 sampling actually varies, which is the whole
reason the corpus is sampled N=20 rather than N=1. When a cell has
fewer than three usable samples, every one of them is shown.

Refusal-mixed cells get one extra guarantee: if the cell contains both
refusals and non-refusals, at least one of each appears. A cell at
refusal rate 0.5 whose three length-selected excerpts happened to all be
refusals would misrepresent the number sitting directly above it.

Unusable samples (:mod:`meridian.analysis.usability`) are never shown as
excerpts — they have no content to show — but their count is reported so
the page never implies a cell was fully answered when it was not.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

#: Hard cap on displayed characters per excerpt. Long responses are
#: truncated *visibly*, with the full text always available in the
#: week's published snapshot. Silent truncation would be its own small
#: version of the failure this project exists to catch.
MAX_EXCERPT_CHARS = 1200

#: Excerpts per (prompt x model) cell.
EXCERPTS_PER_CELL = 3


@dataclass(frozen=True)
class Excerpt:
    request_index: int
    temperature: float
    text: str
    length: int
    truncated: bool
    is_refusal: bool
    role: str  # "shortest" | "median" | "longest" | "refusal" | "answer"


@dataclass(frozen=True)
class CellExcerpts:
    prompt_id: str
    model_id: str
    excerpts: list[Excerpt]
    usable: int
    unusable: int


def _classify_refusal(text: str) -> bool:
    """Reuse the pipeline's classifier so the page and the published
    refusal_rate can never disagree about what a refusal is."""
    from meridian.analysis.refusal import classify_refusal
    return classify_refusal(text).is_refusal


def _is_usable(rec: dict) -> bool:
    from meridian.analysis.usability import unusable_reason
    from meridian.runners.base import Sample
    try:
        return unusable_reason(Sample.model_validate(rec)) is None
    except Exception:
        # A record we cannot even parse is not something to excerpt.
        return False


def _truncate(text: str) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= MAX_EXCERPT_CHARS:
        return text, False
    # Cut on a word boundary so the excerpt does not end mid-token.
    cut = text[:MAX_EXCERPT_CHARS].rsplit(" ", 1)[0]
    return cut, True


def select(records: list[dict]) -> list[Excerpt]:
    """Apply the documented selection rule to one cell's raw records."""
    usable = [r for r in records if _is_usable(r)]
    if not usable:
        return []

    scored = sorted(usable, key=lambda r: (len((r.get("text") or "").strip()),
                                           r.get("request_index", 0)))
    picks: dict[int, str] = {}  # request_index -> role

    def _mark(rec: dict, role: str) -> None:
        picks.setdefault(rec.get("request_index", 0), role)

    if len(scored) <= EXCERPTS_PER_CELL:
        for r in scored:
            _mark(r, "sample")
    else:
        _mark(scored[0], "shortest")
        _mark(scored[len(scored) // 2], "median")
        _mark(scored[-1], "longest")

    by_index = {r.get("request_index", 0): r for r in usable}

    # Refusal-mix guarantee: never let the excerpts imply a uniformity
    # the refusal rate contradicts.
    refusals = [r for r in usable if _classify_refusal(r.get("text") or "")]
    answers = [r for r in usable if not _classify_refusal(r.get("text") or "")]
    if refusals and answers:
        shown = [by_index[i] for i in picks]
        if not any(_classify_refusal(r.get("text") or "") for r in shown):
            _mark(refusals[0], "refusal")
        if all(_classify_refusal(r.get("text") or "") for r in shown):
            _mark(answers[0], "answer")

    out: list[Excerpt] = []
    for idx, role in sorted(picks.items()):
        rec = by_index[idx]
        raw = (rec.get("text") or "").strip()
        text, truncated = _truncate(raw)
        out.append(Excerpt(
            request_index=idx,
            temperature=float(rec.get("temperature", 0.0)),
            text=text,
            length=len(raw),
            truncated=truncated,
            is_refusal=_classify_refusal(raw),
            role=role,
        ))
    return out


def load_for_week(
    week_id: str,
    snapshot_path: Path,
    *,
    prompt_ids: set[str] | None = None,
) -> dict[tuple[str, str], CellExcerpts]:
    """Read one week's snapshot and select excerpts for every cell.

    Returns ``{}`` when the snapshot is absent. That is not an error:
    weeks predating snapshot emission, and test builds that seed no raw
    samples, still have to render.

    ``prompt_ids`` restricts the result to the public corpus. Held-out
    prompts must never reach a rendered page, and the snapshot is the
    one input here that could carry them.
    """
    if not snapshot_path.exists():
        return {}

    buckets: dict[tuple[str, str], list[dict]] = {}
    with gzip.open(snapshot_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            pid, mid = rec.get("prompt_id"), rec.get("model_id")
            if not pid or not mid:
                continue
            if prompt_ids is not None and pid not in prompt_ids:
                continue
            buckets.setdefault((pid, mid), []).append(rec)

    out: dict[tuple[str, str], CellExcerpts] = {}
    for (pid, mid), records in buckets.items():
        usable = [r for r in records if _is_usable(r)]
        excerpts = select(records)
        if not excerpts:
            continue
        out[(pid, mid)] = CellExcerpts(
            prompt_id=pid, model_id=mid, excerpts=excerpts,
            usable=len(usable), unusable=len(records) - len(usable),
        )
    return out
