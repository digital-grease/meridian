"""Reconstruct the record of a week whose run never recorded itself.

Why this exists
---------------
2026-W34 sampled on 2026-08-24 and was killed at the one-hour SSM
execution timeout while ``claude-opus-5`` was still running. A killed run
writes nothing: no run_log entry (that is appended after sampling), no
manifest, no snapshot. Its raw samples survived on the instance and were
archived to S3 on 2026-10-05, and the week is published as a disclosed
partial week rather than embargoed.

``cli recover-week`` turns those archived samples into the artifacts a
finished run would have left, built by the same code a live run uses, and
says plainly in each of them that it is a reconstruction:

* the manifest carries ``partial``, per-runner ``coverage`` and ``notes``;
* the run_log gets ONE entry with ``recovery: true`` and a ``note`` giving
  the sampling date, the cause, the archive date and the build date;
* pair counts are what the archive holds, nothing more: a pair is
  complete only when every sample it owed is present, and every other
  pair the roster owed is counted as failed.

No sample is taken. Nothing here calls a provider except the stance
classifier, which a live run would also have called on these responses.

This module holds the pure parts (counting, the run_log outcome, the
manifest annotation) so they can be tested without the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from meridian.analysis import usability
from meridian.sampling.orchestrator import RunOutcome
from meridian.storage import LocalSampleStore

#: Fixed bootstrap/permutation seed for a reconstructed manifest. The live
#: pipeline draws unseeded; a manifest built months after its week should
#: be byte-reproducible by anyone holding the same raw samples, so it is
#: seeded, and its notes say so.
RECOVERY_SEED = 20261005


@dataclass
class RunnerTally:
    """One runner's holdings for the week, prompt by prompt."""
    provider: str
    model_id: str
    #: Samples one pair owes this runner (temperature batches it accepts).
    per_pair: int
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model_id}"

    @property
    def expected(self) -> int:
        return self.per_pair * len(self.counts)

    @property
    def captured(self) -> int:
        return sum(self.counts.values())

    @property
    def complete_prompts(self) -> list[str]:
        return [p for p, n in self.counts.items() if n >= self.per_pair]

    @property
    def partial_prompts(self) -> list[str]:
        return [p for p, n in self.counts.items() if 0 < n < self.per_pair]

    @property
    def missing_prompts(self) -> list[str]:
        return [p for p, n in self.counts.items() if n == 0]

    @property
    def status(self) -> str:
        if self.captured == 0:
            return "lost"
        if self.partial_prompts or self.missing_prompts:
            return "partial"
        return "complete"


def tally_week(
    store: LocalSampleStore,
    week_id: str,
    prompt_ids: list[str],
    roster: list[tuple[str, str, int]],
) -> list[RunnerTally]:
    """Count stored samples per (runner, prompt) for the roster.

    ``roster`` is ``(provider, model_id, samples_per_pair)``. Prompts are
    kept in the order given, which is corpus order, so "the first
    incomplete prompt" means the same thing everywhere.
    """
    out = []
    for provider, model_id, per_pair in roster:
        tally = RunnerTally(provider, model_id, per_pair)
        for pid in prompt_ids:
            tally.counts[pid] = store.count(week_id, model_id, pid)
        out.append(tally)
    return out


def unexpected_models(
    store: LocalSampleStore, week_id: str, roster: list[tuple[str, str, int]],
) -> list[str]:
    """Models with samples on disk that the week's roster did not owe.

    A reconstruction states what the roster ran; samples from anything
    else mean the archive is not what it is believed to be, and the
    command refuses rather than publish them unexplained.
    """
    due = {m for _p, m, _n in roster}
    return sorted(m for m in store.models_for_week(week_id) if m not in due)


def capture_window(
    store: LocalSampleStore, week_id: str, tallies: list[RunnerTally],
) -> tuple[datetime, datetime] | None:
    """Earliest and latest ``captured_at`` among the roster's samples."""
    first: datetime | None = None
    last: datetime | None = None
    for t in tallies:
        for pid, n in t.counts.items():
            if not n:
                continue
            for s in store.read(week_id, t.model_id, pid):
                ts = s.captured_at
                first = ts if first is None or ts < first else first
                last = ts if last is None or ts > last else last
    if first is None or last is None:
        return None
    return first, last


def reconstructed_outcome(
    store: LocalSampleStore,
    week_id: str,
    tallies: list[RunnerTally],
    *,
    halt_type: str,
    cause: str,
) -> RunOutcome:
    """The :class:`RunOutcome` the killed run would have reported.

    Counted from disk. ``pairs_failed`` is every owed pair that is not
    complete, whether cut short or never started; a runner with any such
    pair gets one ``runner_halts`` entry naming ``halt_type`` and the
    first prompt it did not finish, so the health check and the coverage
    page can attribute the loss. ``errors`` stays empty: no request
    failed, the process was killed, and inventing per-pair error records
    would put words in the run's mouth.
    """
    outcome = RunOutcome(week_id=week_id)
    for t in tallies:
        outcome.per_runner_samples[t.key] = t.captured
        outcome.total_samples_written += t.captured
        outcome.pairs_complete += len(t.complete_prompts)
        unfinished = [
            p for p in t.counts if t.counts[p] < t.per_pair
        ]
        outcome.pairs_failed += len(unfinished)
        if unfinished:
            outcome.runner_halts[t.key] = {
                "error_type": halt_type,
                "stage": "sample",
                "prompt_id": unfinished[0],
                "message": cause,
                "pairs_not_attempted": len(t.missing_prompts),
            }
        for pid, n in t.counts.items():
            if not n:
                continue
            for code, count in usability.count_outcomes(
                store.read(week_id, t.model_id, pid)
            ).items():
                if code == usability.API_REFUSAL:
                    per = outcome.api_refusal_samples.setdefault(t.key, {})
                    per[pid] = per.get(pid, 0) + count
                else:
                    per = outcome.unusable_samples.setdefault(t.key, {})
                    per[code] = per.get(code, 0) + count
    return outcome


def recovery_note(
    *,
    week_id: str,
    sampled: tuple[datetime, datetime],
    cause: str,
    archived_on: date,
    built_on: date,
    tallies: list[RunnerTally],
    config_hash: str | None = None,
) -> str:
    """The run_log ``note`` for a reconstructed entry. One paragraph.

    Says which of the row's fields describe the week and which describe
    the rebuild, because a reader of an append-only log cannot ask.
    """
    start, end = sampled
    held = "; ".join(
        f"{t.key} {t.captured}/{t.expected} samples, "
        f"{len(t.complete_prompts)}/{len(t.counts)} prompts complete"
        for t in tallies
    )
    return (
        f"Reconstruction (recovery: true), written by recover-week, not by "
        f"the run. {week_id} was sampled {start.date().isoformat()} "
        f"({start.strftime('%H:%M')} to {end.strftime('%H:%M')} UTC, first "
        f"and last captured sample) and {cause}, so it left no run_log entry. "
        f"Raw samples archived to S3 {archived_on.isoformat()}; manifest and "
        f"this entry built {built_on.isoformat()} from them. No sample was "
        f"taken by this invocation; counts are what the archive holds, a "
        f"pair counting as complete only with every sample it owed: {held}. "
        f"started_at and finished_at are the first and last captured sample. "
        f"runners is the roster due in {week_id}, not the config the rebuild "
        f"ran under. "
        + (
            f"config_hash {config_hash} is the hash logged by the scheduled "
            f"runs either side of {week_id}, under the same config; it was "
            f"not computed by the run itself. "
            if config_hash else
            "config_hash is null: the config the run used is not known. "
        )
        + "host and pid are those of the recover-week invocation."
    )


def coverage_records(tallies: list[RunnerTally]) -> list[dict]:
    """``Manifest.coverage`` entries, one per runner."""
    return [
        {
            "model_id": t.model_id,
            "provider": t.provider,
            "expected_samples": t.expected,
            "captured_samples": t.captured,
            "prompts_expected": len(t.counts),
            "prompts_complete": len(t.complete_prompts),
            "partial_prompts": t.partial_prompts,
            "missing_prompts": t.missing_prompts,
            "status": t.status,
        }
        for t in tallies
    ]


def manifest_notes(
    *,
    week_id: str,
    sampled: tuple[datetime, datetime],
    cause: str,
    archived_on: date,
    built_on: date,
    tallies: list[RunnerTally],
) -> list[str]:
    """``Manifest.notes`` for a reconstructed partial week."""
    start, _end = sampled
    short = [t for t in tallies if t.status != "complete"]
    whole = [t for t in tallies if t.status == "complete"]
    notes = [
        f"Partial week. {week_id} was sampled on "
        f"{start.date().isoformat()} and {cause} before every runner "
        f"finished. This manifest publishes what was captured.",
    ]
    for t in short:
        bits = [
            f"{t.model_id} has {t.captured} of {t.expected} samples, "
            f"{len(t.complete_prompts)} of {len(t.counts)} prompts complete"
        ]
        if t.partial_prompts:
            bits.append(
                "cut short: " + ", ".join(
                    f"{p} ({t.counts[p]}/{t.per_pair})" for p in t.partial_prompts
                )
            )
        if t.missing_prompts:
            bits.append(f"{len(t.missing_prompts)} prompt(s) never sampled")
        notes.append("; ".join(bits) + ".")
    if whole:
        notes.append(
            "Complete: " + ", ".join(
                f"{t.model_id} ({t.captured} samples)" for t in whole
            ) + "."
        )
    notes.append(
        f"Built {built_on.isoformat()} from the raw samples archived to S3 on "
        f"{archived_on.isoformat()}, with the same code a live run uses and "
        f"a fixed bootstrap seed ({RECOVERY_SEED}), so confidence intervals "
        f"and p-values can differ trivially from the copies of {week_id} "
        f"already embedded in later weeks' history, which were drawn "
        f"unseeded."
    )
    return notes


def annotate_partial(manifest: dict, *, notes: list[str], coverage: list[dict]) -> dict:
    """Mark a built manifest as a partial week, in place, and return it."""
    manifest["partial"] = True
    manifest["notes"] = list(notes)
    manifest["coverage"] = list(coverage)
    return manifest
