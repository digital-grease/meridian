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
