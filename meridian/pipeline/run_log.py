"""Pipeline run log — structured audit trail of every orchestrator invocation.

Every call to ``cli.py run`` writes one JSONL record at the end capturing
what ran, when, how much it cost, and what failed. The log is append-only
and is the operational answer to questions like "did we run last week?",
"what did W15 actually cost?", and "which pairs have historically failed?"

Location: ``data/run_log.jsonl`` by default. The file is excluded from
git only insofar as it can grow over time; small deployments may choose
to commit it for extra durability.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from meridian.config import PipelineConfig
from meridian.sampling.orchestrator import RunOutcome


@dataclass(frozen=True)
class RunLogEntry:
    started_at: str
    finished_at: str
    week_id: str
    host: str
    pid: int
    config_hash: str
    runners: list[str]
    # Sample + failure counts come from RunOutcome; cost from CostReport.
    total_samples_written: int
    pairs_complete: int
    pairs_skipped: int
    pairs_failed: int
    per_runner_samples: dict[str, int]
    estimated_cost_usd: float
    actual_cost_usd: float
    errors: list[dict] = field(default_factory=list)
    note: str | None = None
    # Stored-but-unmeasurable samples, "provider/model" -> reason -> count.
    # Distinct from `errors`: those requests failed, these succeeded and
    # returned nothing usable. Defaulted so entries written before
    # 2026-07-24 stay parseable (retention is forever; the reader must
    # never break on an old line).
    unusable_samples: dict[str, dict[str, int]] = field(default_factory=dict)
    # Provider-declared refusals, "provider/model" -> prompt_id -> count.
    # Distinct from both fields above: these requests succeeded AND
    # returned a measurement, so they are neither an error nor a hole,
    # and they must never move a fail threshold. They are logged because
    # a provider switching refusal mechanism corpus-wide is a
    # model-version-instability event that otherwise leaves no trace
    # anywhere in the pipeline (2026-W32, claude-opus-4-8 on
    # ref-pipe-bomb-construct, 20/20). Keyed by prompt so a reader can
    # tell one saturated cell from a thin spread across the corpus.
    # Defaulted so entries written before 2026-08-15 stay parseable:
    # retention is forever and the reader must never break on an old line.
    api_refusal_samples: dict[str, dict[str, int]] = field(default_factory=dict)
    # Requests the provider declined to run, "provider/model" -> prompt_id
    # -> count. Distinct from all three fields above because there is no
    # response to classify: the platform rejected the request before the
    # model saw it, so it is not an error in our pipeline, not a hole in
    # a response, and not a model behaviour.
    #
    # Logged because it moves a cell's denominator without appearing in
    # any count that explains why. 2026-W33 published gpt-5.5 on
    # ref-wifi-unauthorized at n_samples=2 flagged "insufficient data",
    # and nothing anywhere distinguished requests the platform refused
    # from requests that were never issued at all. Defaulted so entries
    # written before 2026-08-25 stay parseable: retention is forever and
    # the reader must never break on an old line.
    content_policy_rejections: dict[str, dict[str, int]] = field(
        default_factory=dict
    )


def _config_hash(config: PipelineConfig) -> str:
    # Deterministic hash of the enabled-runner set + sampling params.
    key = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]


def append_run_log(
    log_path: Path,
    *,
    started_at: datetime,
    finished_at: datetime,
    week_id: str,
    config: PipelineConfig,
    outcome: RunOutcome,
    estimated_cost_usd: float,
    actual_cost_usd: float,
    note: str | None = None,
) -> RunLogEntry:
    entry = RunLogEntry(
        started_at=started_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        finished_at=finished_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        week_id=week_id,
        host=socket.gethostname(),
        pid=os.getpid(),
        config_hash=_config_hash(config),
        runners=sorted(
            f"{s.provider}/{s.model_id}" for s in config.runners if s.enabled
        ),
        total_samples_written=outcome.total_samples_written,
        pairs_complete=outcome.pairs_complete,
        pairs_skipped=outcome.pairs_skipped,
        pairs_failed=outcome.pairs_failed,
        per_runner_samples=dict(outcome.per_runner_samples),
        unusable_samples={
            k: dict(v) for k, v in outcome.unusable_samples.items() if v
        },
        api_refusal_samples={
            k: dict(v) for k, v in outcome.api_refusal_samples.items() if v
        },
        content_policy_rejections={
            k: dict(v) for k, v in outcome.content_policy_rejections.items() if v
        },
        estimated_cost_usd=round(estimated_cost_usd, 4),
        actual_cost_usd=round(actual_cost_usd, 4),
        errors=[
            {
                "provider": e.provider,
                "model_id": e.model_id,
                "prompt_id": e.prompt_id,
                "error_type": e.error_type,
                "message": e.message,
            }
            for e in outcome.errors[:50]  # cap to keep lines bounded
        ],
        note=note,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry), sort_keys=True) + "\n")
    return entry


def read_run_log(log_path: Path) -> list[RunLogEntry]:
    if not log_path.exists():
        return []
    out: list[RunLogEntry] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        out.append(RunLogEntry(**obj))
    return out
