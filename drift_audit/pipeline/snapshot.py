"""Public raw-responses snapshot emission.

For every weekly run we publish ``responses.jsonl.gz`` alongside the
computed metrics. The file is the concatenation of the stored
:class:`~drift_audit.runners.base.Sample` records for every public
(non-held-out) prompt in the corpus, gzipped.

This closes the CLAUDE.md "raw data export" transparency commitment:
a researcher can recompute any dashboard metric from scratch given
the prompts file + the responses file.

**Held-out exclusion is hard**: the held-out corpus's measurement
value comes from not being public, so samples keyed to a held-out
prompt id must never land in the emitted gzip. The ``Corpus.public()``
filter is the single source of truth — anything outside that set is
skipped.
"""
from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path

from drift_audit.corpus import Corpus
from drift_audit.storage import LocalSampleStore


@dataclass(frozen=True)
class SnapshotReport:
    path: Path
    sample_count: int
    bytes_written: int
    prompts_included: int
    prompts_skipped_held_out: int

    def pretty(self) -> str:
        return (
            f"{self.sample_count} sample(s) from {self.prompts_included} "
            f"public prompt(s) → {self.path.name} ({self.bytes_written:,} bytes)"
        )


def emit_responses_snapshot(
    store: LocalSampleStore,
    corpus: Corpus,
    week_id: str,
    out_path: Path,
) -> SnapshotReport:
    """Gzip every public prompt's samples for ``week_id`` into ``out_path``.

    Held-out prompts are filtered by id before any bytes are written.
    If a held-out prompt id somehow appears in storage (e.g. a test
    fixture mistake), it is silently skipped and counted in
    ``prompts_skipped_held_out`` on the returned report. Silent skip is
    the right behavior here: *never* publishing held-out is the hard
    rule; surfacing why is a separate concern.
    """
    public_ids: set[str] = {p.id for p in corpus.public()}
    held_out_ids: set[str] = {p.id for p in corpus.all() if p.held_out}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sample_count = 0
    prompts_included = 0
    prompts_skipped = 0

    with gzip.open(out_path, "wt", encoding="utf-8") as gz:
        for model_id in store.models_for_week(week_id):
            for prompt_id in store.prompts_for(week_id, model_id):
                if prompt_id in held_out_ids:
                    prompts_skipped += 1
                    continue
                if prompt_id not in public_ids:
                    # Prompt id in storage but not in the current corpus —
                    # could be an old prompt that was rotated out. Skip
                    # defensively rather than publishing something the
                    # corpus manifest doesn't advertise.
                    continue
                samples = store.read(week_id, model_id, prompt_id)
                if not samples:
                    continue
                for s in samples:
                    gz.write(s.model_dump_json())
                    gz.write("\n")
                    sample_count += 1
                prompts_included += 1

    return SnapshotReport(
        path=out_path,
        sample_count=sample_count,
        bytes_written=out_path.stat().st_size if out_path.exists() else 0,
        prompts_included=prompts_included,
        prompts_skipped_held_out=prompts_skipped,
    )


def snapshot_path(repo_root: Path, week_id: str) -> Path:
    """Canonical on-disk location for a week's responses snapshot."""
    return repo_root / "data" / "snapshots" / week_id / "responses.jsonl.gz"
