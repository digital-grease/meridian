"""Append-only local JSONL storage for raw LLM samples.

Layout:
    {base}/{week_id}/{model_id}/{prompt_id}/samples.jsonl

Each line is one serialized Sample. Writes are atomic at the line level
(single ``write()`` + fsync). Concurrent writers to the same file are not
supported; the orchestrator is responsible for serializing writes per
(prompt × model × week), which is naturally how sampling loops are shaped.

Raw files are never modified after creation. New samples append; old
samples stay byte-identical. This is the durability guarantee that makes
downstream analysis reproducible.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from meridian.runners.base import Sample


class LocalSampleStore:
    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    def path(self, week_id: str, model_id: str, prompt_id: str) -> Path:
        return self.base_dir / week_id / model_id / prompt_id / "samples.jsonl"

    def append(
        self, week_id: str, model_id: str, prompt_id: str, sample: Sample
    ) -> None:
        p = self.path(week_id, model_id, prompt_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(sample.model_dump(mode="json"), separators=(",", ":"))
        # Line-buffered append; fsync to survive power loss mid-write.
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read(self, week_id: str, model_id: str, prompt_id: str) -> list[Sample]:
        p = self.path(week_id, model_id, prompt_id)
        if not p.exists():
            return []
        out: list[Sample] = []
        for raw in p.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            out.append(Sample.model_validate_json(raw))
        return out

    def count(self, week_id: str, model_id: str, prompt_id: str) -> int:
        p = self.path(week_id, model_id, prompt_id)
        if not p.exists():
            return 0
        return sum(1 for line in p.read_text().splitlines() if line.strip())

    def weeks(self) -> list[str]:
        if not self.base_dir.exists():
            return []
        return sorted(d.name for d in self.base_dir.iterdir() if d.is_dir())

    def models_for_week(self, week_id: str) -> list[str]:
        d = self.base_dir / week_id
        if not d.exists():
            return []
        return sorted(m.name for m in d.iterdir() if m.is_dir())

    def prompts_for(self, week_id: str, model_id: str) -> list[str]:
        d = self.base_dir / week_id / model_id
        if not d.exists():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_dir())
