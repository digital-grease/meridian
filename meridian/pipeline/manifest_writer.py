"""Pipeline → site manifest writer.

Reads stored samples from :class:`LocalSampleStore`, runs the analysis
suite on each (prompt × model × week) bucket, and emits a Manifest JSON
matching the schema the site already knows how to render
(``site/schemas/manifest.schema.json``).

This module is what turns raw sample JSONL into the artifact the public
site consumes. It is the bridge between pipeline and presentation.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import random

from meridian.analysis.change_point import weeks_between
from meridian.analysis.confidence import bootstrap_ci
from meridian.analysis.drift_tests import (
    hedge_p_value,
    length_p_value,
    refusal_p_value,
)
from meridian.analysis.hedge import hedge_density
from meridian.analysis.length import summarize_lengths
from meridian.analysis.multiple_testing import bh_correct
from meridian.analysis.refusal import classify_sample
from meridian.analysis.silent_update import detect_silent_updates
from meridian.analysis import usability
from meridian.corpus import Corpus
from meridian.corpus import corpus_git_sha as detect_corpus_git_sha
from meridian.runners.base import Sample
from meridian.storage import LocalSampleStore

# Imported lazily to avoid circular import and keep optional-dep types at
# type-check time only.
from typing import TYPE_CHECKING
if TYPE_CHECKING:  # pragma: no cover
    from meridian.analysis.embedding import EmbeddingModel
    from meridian.analysis.stance import StanceResult

# Import the site's Pydantic schema so we can validate what we emit against
# the exact shape the site consumes. Path hack mirrors what site/src/build.py
# uses internally — it keeps the site stack a single Python module tree.
_SITE_SRC = Path(__file__).resolve().parent.parent.parent / "site" / "src"
if str(_SITE_SRC) not in sys.path:
    sys.path.insert(0, str(_SITE_SRC))

import schema as site_schema  # noqa: E402


@dataclass(frozen=True)
class RunnerDisplayInfo:
    model_id: str
    display_name: str
    provider: str


# Child of the "meridian" logger the CLI configures, so warnings here
# surface on stderr in the EC2 weekly run (captured in run-weekly.sh's log).
_log = logging.getLogger("meridian.manifest")

MIN_SAMPLES_FOR_PUBLICATION = 10

# Default FDR for within-week BH correction across the
# (prompt × model × metric) family. See meridian/analysis/STATISTICS.md.
BH_FDR = 0.05

_DRIFT_METRICS: tuple[str, ...] = ("refusal", "hedge", "length")

# Keys under MetricRecord that hold the time-series values the site's
# change-point detector runs over. Site schema names these metrics
# differently than the drift-test names above — keep them paired.
_CHANGE_POINT_METRICS: tuple[tuple[str, str], ...] = (
    ("refusal_rate", "refusal_rate"),
    ("hedge_density", "hedge_density"),
    ("length_median", "length.median"),
)


def _metric_record_dict(
    *,
    prompt_id: str,
    model_id: str,
    samples: list[Sample],
    bootstrap_seed: int | None,
    stance_stance: str = "na",
    stance_confidence: float | None = None,
    centroid_shift: float | None = None,
    prior_samples: list[Sample] | None = None,
    insufficient_data_n: int = MIN_SAMPLES_FOR_PUBLICATION,
    unusable_count: int = 0,
    week_id: str | None = None,
    prior_week: str | None = None,
) -> dict:
    """Compute a single MetricRecord's dict shape from raw samples.

    ``samples`` must already be filtered to usable ones (see
    :mod:`meridian.analysis.usability`); ``unusable_count`` is how many
    were dropped, carried onto the record so the published data states
    its own sample loss rather than quietly reporting a smaller ``n``.

    ``week_id`` and ``prior_week`` identify the two ends of the drift
    comparison and are recorded on every emitted ``*_drift`` entry (see
    :func:`_drift_window`). Drift tests only run when both are known,
    because a p-value published without the window it was measured over
    is not reproducible.
    """
    # Refusals are scored from the whole sample, not from its text.
    # Between 2026-07-13 and 2026-08-10 Anthropic changed how
    # claude-opus-4-8 declines: 2026-W28 returned prose refusals with
    # stop_reason='end_turn', 2026-W32 returned stop_reason='refusal'
    # with an empty body. Reading only ``s.text`` scores the second form
    # as a non-refusal, so a cell that refused 20 times out of 20 would
    # publish refusal_rate=0.00. classify_sample reads the provider's own
    # declaration first, which keeps a change of refusal MECHANISM from
    # looking like a change of refusal RATE.
    refusals = [1.0 if classify_sample(s).is_refusal else 0.0 for s in samples]
    refusal_rate = sum(refusals) / len(refusals) if refusals else 0.0
    ci = bootstrap_ci(refusals, seed=bootstrap_seed)
    # Text analyzers see only the samples that carry text. The two
    # denominators differ on purpose and both are published: the refusal
    # rate counts every usable sample, while length and hedge density
    # count the ones there was something to measure. Feeding empty
    # refusal bodies to the length summary would claim a run of
    # zero-word answers that nobody wrote.
    measurable = usability.text_bearing(samples)
    lengths = summarize_lengths([s.text for s in measurable])
    combined_text = "\n\n".join(s.text for s in measurable)
    # None, not 0.0, when there was no text. hedge_density("") returns
    # 0.0 as a divide-by-zero sentinel, and publishing that asserts "this
    # model hedged in none of its answers" about a cell that produced no
    # answers. Same fabricated-zero failure the null length quantiles
    # below exist to prevent; hedge was missed when they were fixed.
    hedge = hedge_density(combined_text) if measurable else None

    reasons: list[str] = []
    if len(samples) < insufficient_data_n:
        reasons.append(
            f"insufficient data (n={len(samples)} < {insufficient_data_n})"
        )
    if unusable_count:
        total = len(samples) + unusable_count
        reasons.append(
            f"{unusable_count}/{total} sample(s) returned no usable content "
            f"and were excluded"
        )
    flagged = bool(reasons)
    flag_reason = "; ".join(reasons) if reasons else None

    window = _drift_window(week_id, prior_week) if prior_samples else None
    drift_p_values: dict[str, float | None] = {m: None for m in _DRIFT_METRICS}
    if prior_samples and window is not None:
        # Deterministic when bootstrap_seed is set; fresh RNG per metric so
        # results do not depend on call order.
        def _rng(salt: int) -> random.Random:
            if bootstrap_seed is None:
                return random.Random()
            return random.Random(bootstrap_seed + salt)
        drift_p_values["refusal"] = refusal_p_value(samples, prior_samples, rng=_rng(1))
        drift_p_values["hedge"] = hedge_p_value(samples, prior_samples, rng=_rng(2))
        drift_p_values["length"] = length_p_value(samples, prior_samples, rng=_rng(3))

    return {
        "prompt_id": prompt_id,
        "model_id": model_id,
        "n_samples": len(samples),
        "unusable_samples": unusable_count,
        "refusal_rate": round(refusal_rate, 3),
        "refusal_ci": {
            "lower": round(max(0.0, ci.lower), 3),
            "upper": round(min(1.0, ci.upper), 3),
        },
        "hedge_density": round(hedge, 2) if hedge is not None else None,
        # Null quantiles when nothing was measurable. summarize_lengths([])
        # returns 0.0 for median/p25/p75, and a published 0.0 reads as
        # "the model answered with zero words". For an all-api-refusal
        # cell that is a fabricated observation: the samples exist, the
        # refusal rate is 1.00, and there is simply no length. Only
        # ``n`` used to carry the distinction, and no consumer read it,
        # so the zeros reached the CSV export and the averaged axis
        # figures on the site.
        "length": {
            "median": round(lengths.median, 1) if lengths.n else None,
            "p25": round(lengths.p25, 1) if lengths.n else None,
            "p75": round(lengths.p75, 1) if lengths.n else None,
            "n": lengths.n,
        },
        "stance": stance_stance,
        "stance_confidence": stance_confidence,
        "embedding_centroid_shift": centroid_shift,
        "refusal_drift": _raw_drift_entry(drift_p_values["refusal"], window),
        "hedge_drift": _raw_drift_entry(drift_p_values["hedge"], window),
        "length_drift": _raw_drift_entry(drift_p_values["length"], window),
        "change_points": {
            "refusal_rate": [],
            "hedge_density": [],
            "length_median": [],
        },
        "flagged_for_review": flagged,
        "flag_reason": flag_reason,
    }


def _drift_window(
    week_id: str | None, prior_week: str | None
) -> tuple[str, int] | None:
    """Resolve the ``(compared_to_week, weeks_elapsed)`` pair for a drift test.

    Drift here is "since this model last ran", not a fixed 7-day delta:
    :func:`_prior_week_for_model` walks back to the nearest earlier week
    the model was actually sampled in, and the roster alternates by
    ISO-week parity. For 2026-W32 that made claude-opus-4-8's comparison
    a four-week window back to 2026-W28, because 2026-W30 and 2026-W31
    were lost to an EC2 capacity outage. Until 2026-08 the emitted
    record carried only the p-values, so nothing in the published
    manifest said which weeks had been compared and a reader could not
    reproduce the number. These two fields are that statement.

    Returns None when either end is unknown or is not a parseable ISO
    week id, in which case the caller emits no drift entry at all. An
    unlabelled drift claim is worse than an absent one, and malformed
    week ids mean something upstream is broken, so the failure is logged
    rather than papered over with a guessed window.
    """
    if week_id is None or prior_week is None:
        return None
    try:
        elapsed = weeks_between(prior_week, week_id)
    except ValueError:
        _log.error(
            "cannot compute drift window between %r and %r; emitting no drift "
            "entry for this record rather than an unlabelled comparison",
            prior_week, week_id, exc_info=True,
        )
        return None
    if elapsed < 1:  # pragma: no cover - _prior_week_for_model guarantees w < week_id
        _log.error(
            "drift baseline %r is not earlier than %r (elapsed=%d); emitting no "
            "drift entry for this record",
            prior_week, week_id, elapsed,
        )
        return None
    return prior_week, elapsed


def _raw_drift_entry(
    p_value: float | None, window: tuple[str, int] | None
) -> dict | None:
    """Emit a DriftTest-shaped dict with placeholder BH fields, or None.

    The BH pass in :func:`_apply_bh_correction` fills in
    ``adjusted_p_value`` and ``significant_after_bh`` once the full
    within-week family is assembled. ``window`` is the
    ``(compared_to_week, weeks_elapsed)`` pair from
    :func:`_drift_window`; when an entry exists, both fields are always
    present and ``weeks_elapsed`` is always at least 1.
    """
    if p_value is None or window is None:
        return None
    compared_to_week, weeks_elapsed = window
    return {
        "p_value": round(p_value, 6),
        "adjusted_p_value": 1.0,
        "significant_after_bh": False,
        "compared_to_week": compared_to_week,
        "weeks_elapsed": weeks_elapsed,
    }


def _metric_value(record: dict, dotted_key: str) -> float | None:
    """Resolve a ``MetricRecord``-ish dict value by dotted key, e.g. ``length.median``.

    None when the record carries no value for that metric. Since
    2026-W32 ``length.median`` is null on a cell whose usable samples all
    came back as body-less provider refusals, and coercing that to 0.0
    would hand the change-point detector a length collapse that never
    happened.
    """
    node: object = record
    for part in dotted_key.split("."):
        assert isinstance(node, dict)
        node = node[part]
    return None if node is None else float(node)  # type: ignore[arg-type]


def _populate_change_points(
    current_metrics: list[dict], history: list[dict], week_id: str
) -> None:
    """Fill ``change_points`` on each current metric record in place.

    Reconstructs the oldest-first time series for each
    (prompt × model × metric) across ``history`` and ``current_metrics``,
    then runs PELT from :mod:`meridian.analysis.change_point`. Silent
    fallback to empty indices when the optional ``changepoint`` dep
    group is not installed, so the pipeline never fails the build over
    an optional analysis.

    The current week is labelled with its real ``week_id``, not a
    sentinel. The detector needs every point's week identity to tell a
    genuine week-over-week transition from a shift that accumulated
    across weeks the model never ran: 2026-W29 and 2026-W32 are three
    weeks apart, not adjacent, because the 2026-W30 and 2026-W31 runs
    were lost to an EC2 capacity outage. A sentinel label on the newest
    point would defeat exactly the continuity check that matters most,
    since the newest interval is the one that spans the outage.
    """
    try:
        from meridian.analysis.change_point import detect_change_points
    except Exception:
        _log.warning(
            "change-point detection unavailable (ruptures import failed); "
            "change_points will be empty for every record this run. "
            "Ensure the 'changepoint' dependency group is installed "
            "(uv sync --group changepoint).",
            exc_info=True,
        )
        return

    # Build a lookup: (prompt_id, model_id) -> oldest-first list of (week_id, record).
    series_by_key: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for snap in history:  # oldest-first by caller contract
        for rec in snap["metrics"]:
            series_by_key.setdefault(
                (rec["prompt_id"], rec["model_id"]), []
            ).append((snap["week_id"], rec))
    # Current week is the most recent — append last so indices line up
    # with the rendered sparkline.
    for rec in current_metrics:
        series_by_key.setdefault(
            (rec["prompt_id"], rec["model_id"]), []
        ).append((week_id, rec))

    for rec in current_metrics:
        key = (rec["prompt_id"], rec["model_id"])
        records = series_by_key.get(key, [])
        for site_name, record_key in _CHANGE_POINT_METRICS:
            # Weeks with no value for this metric drop out of the series
            # entirely rather than being imputed. The site's
            # Manifest.timeseries drops the same points, so the indices
            # emitted here keep pointing at the weeks the sparkline
            # actually plots.
            series = [
                (week, value)
                for week, r in records
                if (value := _metric_value(r, record_key)) is not None
            ]
            try:
                cps = detect_change_points(series)
            except Exception:
                _log.warning(
                    "change-point detection failed for %s/%s on %s; leaving "
                    "change_points empty for this metric",
                    rec["prompt_id"], rec["model_id"], site_name, exc_info=True,
                )
                cps = []
            rec["change_points"][site_name] = [cp.index for cp in cps]


#: How many (prompt × model) cells get flagged for human eyes each week
#: on the strength of movement alone. CLAUDE.md's spot-check requirement
#: is "every week, flag prompts with highest metric deltas for human
#: review"; this is that rule.
REVIEW_TOP_N = 3

#: Reference scale per metric for making deltas comparable. Mirrors
#: ``_DRIFT_SCALES`` in ``site/src/build.py``: refusal_rate is a
#: probability so its full range is 1.0, hedge_density is markers per
#: 100 tokens against a nominal 5.0 ceiling. Length is handled
#: separately because it has no fixed scale (relative shift instead).
_REVIEW_SCALES: dict[str, float] = {
    "refusal_rate": 1.0,
    "hedge_density": 5.0,
}


def _flag_largest_deltas(
    current_metrics: list[dict],
    history: list[dict],
    *,
    top_n: int = REVIEW_TOP_N,
) -> None:
    """Flag the ``top_n`` biggest week-over-week movers for review.

    Until 2026-07-24 ``flagged_for_review`` could only ever be set by
    the insufficient-sample rule, and since every cell carries 20-25
    samples against a threshold of 10, it had never once been true
    across 13 weeks and 690 records. The weekly human spot-check that
    CLAUDE.md specifies therefore had an empty worklist by
    construction, which is how two weeks of empty gpt-5.5 completions
    went unreviewed.

    Each model is compared against the last week *it* ran, matching the
    drift tests: the frontier roster alternates by ISO-week parity, so
    the calendar-previous week usually holds a different model
    entirely. Flagging here is advisory and additive: it never clears a
    flag another rule set, and it does not touch the statistics.
    """
    prior_by_key: dict[tuple[str, str], dict] = {}
    for snap in reversed(history):  # newest first
        for rec in snap.get("metrics", []):
            prior_by_key.setdefault((rec["prompt_id"], rec["model_id"]), rec)

    scored: list[tuple[float, str, dict]] = []
    for rec in current_metrics:
        prior = prior_by_key.get((rec["prompt_id"], rec["model_id"]))
        if prior is None:
            continue
        for metric, scale in _REVIEW_SCALES.items():
            cur_v = rec.get(metric)
            prior_v = prior.get(metric)
            # A null on either end means that week had no text to
            # measure, not a measured zero. Same reasoning as the length
            # branch below, and skipping is what keeps a non-measurement
            # out of the human review worklist.
            if cur_v is None or prior_v is None:
                continue
            scored.append((abs(cur_v - prior_v) / scale, metric, rec))
        # A null median means the week had no text to measure, not a
        # length of zero. Comparing it as 0.0 would rank an
        # all-api-refusal cell as the largest length collapse of the
        # week; the refusal-rate delta on the same cell already carries
        # the real signal, and it is the honest one.
        cur_len = (rec.get("length") or {}).get("median")
        prior_len = (prior.get("length") or {}).get("median")
        if prior_len and cur_len is not None:
            denom = max(prior_len, cur_len) / 2 or 1.0
            scored.append((abs(cur_len - prior_len) / denom, "length_median", rec))

    scored.sort(key=lambda s: s[0], reverse=True)
    seen: set[tuple[str, str]] = set()
    for magnitude, metric, rec in scored:
        if len(seen) >= top_n:
            break
        if magnitude <= 0.0:
            break
        key = (rec["prompt_id"], rec["model_id"])
        if key in seen:
            continue
        seen.add(key)
        note = f"largest weekly delta ({metric}, normalized {magnitude:.3f})"
        rec["flagged_for_review"] = True
        rec["flag_reason"] = (
            f"{rec['flag_reason']}; {note}" if rec.get("flag_reason") else note
        )


def _apply_bh_correction(metrics: list[dict], *, fdr: float = BH_FDR) -> None:
    """Fill in adjusted p-values and rejection decisions in-place.

    Runs Benjamini–Hochberg once across the within-week family of
    (prompt × model × metric) tests that produced a non-None p-value.
    Metrics with no prior week are excluded from the family entirely
    (they keep ``*_drift=None``).
    """
    family: list[tuple[str, float]] = []
    for idx, m in enumerate(metrics):
        for metric_name in _DRIFT_METRICS:
            entry = m.get(f"{metric_name}_drift")
            if entry is None:
                continue
            family.append((f"{idx}:{metric_name}", entry["p_value"]))
    if not family:
        return
    decisions = bh_correct(family, fdr=fdr)
    for decision in decisions:
        idx_str, metric_name = decision.test_id.split(":", 1)
        entry = metrics[int(idx_str)][f"{metric_name}_drift"]
        entry["adjusted_p_value"] = decision.adjusted_p_value
        entry["significant_after_bh"] = decision.rejected


def _prior_week_for_model(
    store: LocalSampleStore, week_id: str, model_id: str
) -> str | None:
    """Nearest week before ``week_id`` in which ``model_id`` was actually sampled.

    The roster runs on an alternating cadence (e.g. one commercial model on
    even ISO weeks, another on odd — see ``meridian/sampling/weeks.py``), so
    the calendar-previous week usually does NOT contain a given commercial
    model. Week-over-week drift must therefore compare a model against the
    last week *it* ran, not the immediately-preceding week.

    Resolving against the immediately-preceding week instead (the original
    behaviour) meant alternating-cadence models never had a prior bucket to
    diff against, so their refusal/hedge/length/embedding drift was always
    ``None`` — the headline drift signal for exactly the commercial models
    the project exists to track. ``llama3.2:3b`` (every week) was unaffected
    and masked the bug.

    The comparison window is therefore variable: usually one cadence gap
    (e.g. an even-week model vs. two ISO weeks prior), but wider if the model
    skipped a scheduled week. Drift is "since this model last ran", not a
    fixed 7-day delta.
    """
    for w in reversed(store.weeks()):  # store.weeks() is sorted ascending
        if w < week_id and model_id in store.models_for_week(w):
            return w
    return None


def _probe_embedding_model(embedding_model: "EmbeddingModel") -> bool:
    """Encode a throwaway string to surface a dead embedding backend loudly.

    ``SentenceTransformerModel`` loads its transformer lazily on first
    ``encode``, and the per-record centroid call swallows exceptions to keep
    an optional analysis from failing the whole build. Together that means a
    missing ``analysis-heavy`` dependency group (sentence-transformers /
    numpy not installed) produces ``embedding_centroid_shift=None`` on *every*
    record with no signal anywhere. Probe once up front and log an error so
    "embedding drift silently disabled" is visible in the run log instead of
    looking like "no drift detected".
    """
    try:
        embedding_model.encode(["probe"])
        return True
    except Exception:  # intentionally broad: any failure means "no embeddings"
        _log.error(
            "embedding model unavailable (encode probe failed); "
            "embedding_centroid_shift will be None for every record this run. "
            "Ensure the 'analysis-heavy' dependency group is installed "
            "(uv sync --group analysis-heavy).",
            exc_info=True,
        )
        return False


def _metrics_for_week(
    store: LocalSampleStore,
    week_id: str,
    prompts: list,
    bootstrap_seed: int | None,
    *,
    stance_by_key: dict[tuple[str, str], "StanceResult"] | None = None,
    embedding_model: "EmbeddingModel | None" = None,
    include_drift_tests: bool = False,
    insufficient_data_n: int = MIN_SAMPLES_FOR_PUBLICATION,
    unmeasured_out: list[dict] | None = None,
) -> list[dict]:
    """Compute per-(prompt × model) metrics for one week over ``prompts``.

    When ``include_drift_tests`` is True, also compute per-metric
    two-sample p-values against the last week *this model* ran (see
    :func:`_prior_week_for_model` — not the calendar-previous week, which
    the alternating cadence usually leaves empty for a given commercial
    model). Because that window is variable, every emitted drift entry
    records which week it was measured against
    (``compared_to_week``) and how many ISO weeks wide the comparison
    was (``weeks_elapsed``). BH correction is *not* applied here — see
    :func:`_apply_bh_correction`, which must run over the whole
    returned list.

    Samples carrying no measurement (:mod:`meridian.analysis.usability`)
    are excluded from every metric. A cell where *nothing* was usable
    yields no MetricRecord at all: publishing one would mean publishing
    a refusal rate of 0.00 and a median length of 0 for a model that
    answered nothing, which is precisely the fabricated-zero failure
    this pipeline exists not to commit. Those cells are appended to
    ``unmeasured_out`` instead, so the manifest can say "we sampled and
    got nothing back" rather than staying silent.
    """
    metrics: list[dict] = []
    want_prior = embedding_model is not None or include_drift_tests
    # The embedding backend loads lazily on first encode, so we probe it
    # lazily too — on the first record that actually has a prior week to
    # compare against. A dead backend (e.g. the analysis-heavy dep group
    # missing on the host) is then reported loudly and disabled for the rest
    # of the run, instead of silently nulling every record. On weeks where no
    # model has a prior bucket yet, nothing is embeddable and the probe never
    # runs — so we neither pay the model load nor log a spurious error.
    embedding_ok: bool | None = None
    for model_id in store.models_for_week(week_id):
        prior_week = (
            _prior_week_for_model(store, week_id, model_id) if want_prior else None
        )
        for prompt in prompts:
            stored = store.read(week_id, model_id, prompt.id)
            if not stored:
                continue
            samples, unusable = usability.partition(stored)
            if not samples:
                # Sampled, nothing measurable came back. Emit no record.
                if unmeasured_out is not None:
                    unmeasured_out.append({
                        "prompt_id": prompt.id,
                        "model_id": model_id,
                        "unusable_samples": len(unusable),
                        "reasons": usability.count_reasons(unusable),
                    })
                _log.error(
                    "%s/%s in %s: all %d sample(s) unusable (%s); emitting no "
                    "metric record for this cell",
                    model_id, prompt.id, week_id, len(unusable),
                    usability.count_reasons(unusable),
                )
                continue
            stance = (stance_by_key or {}).get((prompt.id, model_id))
            prior_samples: list[Sample] = []
            if prior_week is not None:
                # Prior-week comparisons must use the same usable-only
                # basis, or a drift test compares real text against a
                # bucket half full of empty strings.
                prior_samples, _ = usability.partition(
                    store.read(prior_week, model_id, prompt.id)
                )
            cshift: float | None = None
            # Only text-bearing samples are embeddable. A provider-declared
            # refusal carries an empty body, and embedding a run of empty
            # strings would move the centroid on the strength of nothing.
            embeddable = usability.text_bearing(samples)
            prior_embeddable = usability.text_bearing(prior_samples)
            if embedding_model is not None and embeddable and prior_embeddable:
                if embedding_ok is None:
                    embedding_ok = _probe_embedding_model(embedding_model)
                if embedding_ok:
                    try:
                        from meridian.analysis.embedding import centroid_shift
                        cshift = centroid_shift(
                            [s.text for s in embeddable],
                            [s.text for s in prior_embeddable],
                            embedding_model,
                        )
                    except Exception:
                        _log.warning(
                            "centroid_shift failed for %s/%s; leaving "
                            "embedding_centroid_shift=None",
                            model_id, prompt.id, exc_info=True,
                        )
                        cshift = None
            metrics.append(
                _metric_record_dict(
                    prompt_id=prompt.id,
                    model_id=model_id,
                    samples=samples,
                    bootstrap_seed=bootstrap_seed,
                    stance_stance=stance.stance if stance else "na",
                    stance_confidence=stance.confidence if stance else None,
                    centroid_shift=cshift,
                    prior_samples=prior_samples if include_drift_tests else None,
                    insufficient_data_n=insufficient_data_n,
                    unusable_count=len(unusable),
                    week_id=week_id,
                    prior_week=prior_week if include_drift_tests else None,
                )
            )
    return metrics


def _models_from_storage(
    store: LocalSampleStore,
    week_id: str,
    display_info: dict[str, RunnerDisplayInfo],
    prompts: list,
) -> list[dict]:
    """Return ModelRecord dicts for every model_id observed this week.

    Display name / provider / exact version come from ``display_info`` when
    available, falling back to metadata on one of the stored samples.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for model_id in store.models_for_week(week_id):
        if model_id in seen:
            continue
        seen.add(model_id)
        info = display_info.get(model_id)
        any_sample: Sample | None = None
        for prompt in prompts:
            samples = store.read(week_id, model_id, prompt.id)
            if samples:
                any_sample = samples[0]
                break
        provider = info.provider if info else (any_sample.provider if any_sample else "unknown")
        version = any_sample.model_version_string if any_sample else model_id
        out.append({
            "model_id": model_id,
            "display_name": info.display_name if info else model_id,
            "provider": provider,
            "version_string": version,
            "available": True,
        })
    return out


def _enrich_models_with_history(
    current_models: list[dict],
    manifests_dir: Path,
) -> list[dict]:
    """Carry forward models from prior committed manifests.

    Without this, model index pages cycle in/out as cadence-alternated
    runners come and go (Opus on even weeks, GPT-5.1 on odd, etc.) —
    which fails the link-rot guard every other week and breaks the
    "never 404 a published URL" guarantee.

    Source of truth is ``data/manifests/*.json`` because it's
    committed and therefore present in CI's fresh checkout. Raw
    samples under ``data/raw/`` are gitignored, so the in-process
    LocalSampleStore can't see prior weeks in CI.

    Models seen in current-week storage stay marked ``available=True``;
    models known only from history are added with ``available=False``
    so templates can show "this model didn't run this week" if they
    want to.
    """
    out = list(current_models)
    seen = {m["model_id"] for m in current_models}
    if not manifests_dir.exists():
        return out
    for f in sorted(manifests_dir.glob("*.json"), reverse=True):
        try:
            prior = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for mm in prior.get("models", []):
            mid = mm.get("model_id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            entry = dict(mm)
            entry["available"] = False
            out.append(entry)
    return out


def build_manifest(
    *,
    store: LocalSampleStore,
    corpus: Corpus,
    week_id: str,
    history_weeks: int = 8,
    # None means "detect it". The previous default was the literal string
    # "unknown", and because no caller ever passed anything, every
    # manifest ever published carried "unknown" — the field looked wired
    # up and was not. Defaulting to detection makes forgetting produce
    # the right answer instead of a plausible-looking wrong one.
    corpus_git_sha: str | None = None,
    pipeline_version: str = "0.1.0",
    display_info: dict[str, RunnerDisplayInfo] | None = None,
    bootstrap_seed: int | None = None,
    include_held_out: bool = False,
    stance_by_key: dict[tuple[str, str], "StanceResult"] | None = None,
    embedding_model: "EmbeddingModel | None" = None,
    insufficient_data_n: int = MIN_SAMPLES_FOR_PUBLICATION,
    prior_manifests_dir: Path | None = None,
) -> dict:
    """Construct a Manifest dict matching the site schema.

    By default the returned manifest contains ONLY public prompts and their
    metrics — held-out prompts are excluded unconditionally. This is the
    manifest that goes on the public site.

    Pass ``include_held_out=True`` to build the internal manifest used by
    the held-out comparison analysis. That variant must NEVER be written
    to ``site/fixtures/``; write it to ``data/internal/`` or similar.
    """
    display_info = display_info or {}

    scoped_prompts = corpus.all() if include_held_out else corpus.public()

    unmeasured: list[dict] = []
    current_metrics = _metrics_for_week(
        store, week_id, scoped_prompts, bootstrap_seed,
        stance_by_key=stance_by_key, embedding_model=embedding_model,
        include_drift_tests=True,
        insufficient_data_n=insufficient_data_n,
        unmeasured_out=unmeasured,
    )
    _apply_bh_correction(current_metrics)
    models = _models_from_storage(store, week_id, display_info, scoped_prompts)
    if prior_manifests_dir is not None:
        models = _enrich_models_with_history(models, prior_manifests_dir)

    all_weeks = store.weeks()
    prior_weeks = [w for w in all_weeks if w < week_id][-history_weeks:]
    history: list[dict] = []
    weeks_seen: set[str] = set()
    for w in prior_weeks:
        # History metrics do not re-run stance/embedding (expensive and
        # already captured in the contemporaneous record).
        metrics = _metrics_for_week(
            store, w, scoped_prompts, bootstrap_seed,
            insufficient_data_n=insufficient_data_n,
        )
        if not metrics:
            continue
        history.append({
            "week_id": w,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metrics": metrics,
        })
        weeks_seen.add(w)

    # Backfill history from prior committed manifests for weeks the
    # local sample store has no data for. Critical in CI, where
    # `data/raw/` is gitignored and only contains the current week —
    # without this, every week's manifest would have an empty history
    # and the home-page heatmap would lose half its frontier columns
    # (the cadence-skipped Opus or GPT-5.1) for lack of data to draw.
    if prior_manifests_dir is not None and prior_manifests_dir.exists():
        scoped_pids = {p.id for p in scoped_prompts}
        for f in sorted(prior_manifests_dir.glob("*.json")):
            try:
                prior = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            wk = prior.get("snapshot", {}).get("week_id")
            if not wk or wk in weeks_seen or wk >= week_id:
                continue
            kept = [
                mx for mx in prior.get("metrics", [])
                if mx.get("prompt_id") in scoped_pids
            ]
            if not kept:
                continue
            history.append({
                "week_id": wk,
                "generated_at": prior.get("snapshot", {}).get(
                    "generated_at",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
                "metrics": kept,
            })
            weeks_seen.add(wk)
    history.sort(key=lambda h: h["week_id"])  # oldest first

    # Change-point detection runs AFTER history is assembled so each
    # current MetricRecord carries precomputed indices into its
    # oldest-first time series. The site reads these directly rather
    # than invoking the analysis library at render time.
    _populate_change_points(current_metrics, history, week_id)
    # Runs last: it reads the assembled history and only sets advisory
    # flags, so it must not influence any statistic computed above.
    _flag_largest_deltas(current_metrics, history)

    prompts_out = [
        {
            "prompt_id": p.id,
            "axis": p.axis,
            "title": p.title,
            "text_hash": p.text_hash,
            "description": p.text,
            "held_out": p.held_out,
        }
        for p in scoped_prompts
    ]

    manifest = {
        "schema_version": site_schema.SCHEMA_VERSION,
        "snapshot": {
            "week_id": week_id,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "corpus_git_sha": (
                corpus_git_sha if corpus_git_sha is not None
                else detect_corpus_git_sha()
            ),
            "corpus_version": corpus.corpus_version,
            "pipeline_version": pipeline_version,
        },
        "models": models,
        "prompts": prompts_out,
        "metrics": current_metrics,
        "history": history,
        "unmeasured": unmeasured,
        "flagged": [m["prompt_id"] for m in current_metrics if m["flagged_for_review"]],
        "silent_update_warnings": [],
    }

    # Silent-update detection runs on the assembled manifest so
    # maintainers see candidate model-update events without having to
    # remember to invoke the standalone `silent-update-check` CLI.
    # Detector is advisory: it flags, it never fails the build.
    try:
        flags = detect_silent_updates(manifest=manifest, prompts=scoped_prompts)
        manifest["silent_update_warnings"] = [
            {
                "model_id": f.model_id,
                "from_week": f.from_week,
                "to_week": f.to_week,
                "axis": f.axis,
                "metric": f.metric,
                "from_value": f.from_value,
                "to_value": f.to_value,
                "delta": f.delta,
                "severity": f.severity,
            }
            for f in flags
        ]
    except Exception:  # pragma: no cover - advisory, never fatal
        manifest["silent_update_warnings"] = []

    # Belt-and-suspenders: the public manifest must not contain any
    # held_out prompt. If this ever trips, it is a credibility bug.
    if not include_held_out:
        leaks = [p["prompt_id"] for p in prompts_out if p.get("held_out")]
        if leaks:
            raise RuntimeError(
                f"held-out prompts leaked into public manifest: {leaks}"
            )

    site_schema.Manifest.model_validate(manifest)
    return manifest


def write_manifest(manifest: dict, paths: list[Path]) -> None:
    """Write the Manifest JSON to every path, creating parents as needed."""
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
