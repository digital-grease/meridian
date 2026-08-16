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

Usable is not the same as quotable
----------------------------------
As of 2026-W32 a provider-declared refusal (Anthropic's
``stop_reason="refusal"`` with an empty body) is a usable sample: it
counts toward N and toward the published refusal rate. It still has no
text. Selecting on usability alone therefore admitted body-less records
into the candidate list, where the length sort put them *first* and the
text-only classifier scored them as answers, so a cell that refused 20
times out of 20 would have rendered three blank cards labelled
"classified answer" directly beneath a published refusal rate of 1.00.

Selection filters on :func:`meridian.analysis.usability.carries_text`
for that reason, and refusal labelling goes through
:func:`meridian.analysis.refusal.classify_sample`, which reads the
provider's own declaration rather than the wording of a body that is not
there. A cell whose refusals all arrived that way still gets a card on
the page; it states the count and says there is no message body to
quote, because silently dropping the cell would hide the strongest
refusal signal the corpus can produce.
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
    #: Usable samples whose refusal arrived in the provider's terminator
    #: with no message body at all. They are counted rather than quoted:
    #: there is nothing to quote, and rendering them as empty response
    #: cards is the regression this field exists to make impossible.
    bodyless_refusals: int = 0


def _as_sample(rec: dict):
    """Parse one raw snapshot record, or None if it will not parse.

    A record we cannot even parse is not something to excerpt, and it is
    not something to reason about either, so every predicate below
    treats None as "no".
    """
    from meridian.runners.base import Sample
    try:
        return Sample.model_validate(rec)
    except Exception:
        return None


def _is_refusal(rec: dict) -> bool:
    """Reuse the pipeline's classifier so the page and the published
    refusal_rate can never disagree about what a refusal is.

    ``classify_sample``, not ``classify_refusal``: since 2026-W32 a
    provider can declare the refusal in its terminator and send no prose,
    and the text-only classifier scores that as "did not refuse". The
    page would then label a refusal an answer while the refusal rate
    above it read 1.00.
    """
    from meridian.analysis.refusal import classify_refusal, classify_sample
    sample = _as_sample(rec)
    if sample is None:
        return classify_refusal(rec.get("text") or "").is_refusal
    return classify_sample(sample).is_refusal


def _is_usable(rec: dict) -> bool:
    """True when the record counts toward the cell's N."""
    from meridian.analysis.usability import is_usable
    sample = _as_sample(rec)
    return sample is not None and is_usable(sample)


def _carries_text(rec: dict) -> bool:
    """True when the record has a response body to show.

    Deliberately narrower than :func:`_is_usable`: a provider-declared
    refusal is usable but body-less. See the module docstring.
    """
    from meridian.analysis.usability import carries_text
    sample = _as_sample(rec)
    return sample is not None and carries_text(sample)


def _is_bodyless_refusal(rec: dict) -> bool:
    """True for a provider-declared refusal that carried no message body."""
    from meridian.analysis.usability import carries_text, is_api_refusal
    sample = _as_sample(rec)
    return (
        sample is not None
        and is_api_refusal(sample)
        and not carries_text(sample)
    )


def _truncate(text: str) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= MAX_EXCERPT_CHARS:
        return text, False
    # Cut on a word boundary so the excerpt does not end mid-token.
    cut = text[:MAX_EXCERPT_CHARS].rsplit(" ", 1)[0]
    return cut, True


def select(records: list[dict]) -> list[Excerpt]:
    """Apply the documented selection rule to one cell's raw records.

    Candidates are the usable records that actually carry text. Filtering
    on usability alone let body-less provider refusals in, where the
    length sort ranked them shortest and put a blank card in the first
    slot on the page.
    """
    candidates = [r for r in records if _is_usable(r) and _carries_text(r)]
    if not candidates:
        return []

    scored = sorted(candidates, key=lambda r: (len((r.get("text") or "").strip()),
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

    by_index = {r.get("request_index", 0): r for r in candidates}

    # Refusal-mix guarantee: never let the excerpts imply a uniformity
    # the refusal rate contradicts.
    refusals = [r for r in candidates if _is_refusal(r)]
    answers = [r for r in candidates if not _is_refusal(r)]
    if refusals and answers:
        shown = [by_index[i] for i in picks]
        if not any(_is_refusal(r) for r in shown):
            _mark(refusals[0], "refusal")
        if all(_is_refusal(r) for r in shown):
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
            is_refusal=_is_refusal(rec),
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
        bodyless = sum(1 for r in usable if _is_bodyless_refusal(r))
        cell = select(records)
        # A cell with no text to quote still gets an entry when the
        # silence is itself a measurement: 20 body-less refusals out of
        # 20 is the strongest signal the refusal-boundary axis produces,
        # and dropping the cell here would leave the page showing a
        # refusal rate of 1.00 with nothing under it to explain the
        # absence of any response. A cell whose samples were all
        # unusable is still skipped: that one is a hole, and the
        # "Not measurable this week" notice already covers it.
        if not cell and not bodyless:
            continue
        out[(pid, mid)] = CellExcerpts(
            prompt_id=pid, model_id=mid, excerpts=cell,
            usable=len(usable), unusable=len(records) - len(usable),
            bodyless_refusals=bodyless,
        )
    return out
