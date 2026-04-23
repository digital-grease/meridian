"""Stance classifier.

Classifies a response's stance on a question as pro / anti / neutral / na.
Only applied to stance-bearing axes (political, historical-contested).
Every other axis returns ``"na"`` without calling the LLM.

v1 implementation is LLM-backed with on-disk caching. The classifier is
deterministic-ish at temperature=0 and idempotent via the cache. A future
version may swap in a fine-tuned model behind the same ``StanceClassifier``
Protocol.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from meridian.runners.base import Runner

_log = logging.getLogger(__name__)

Stance = Literal["pro", "anti", "neutral", "na"]

# Axes where stance scoring applies. Prompts on other axes are
# definitionally "na".
STANCE_AXES: frozenset[str] = frozenset({"political", "historical-contested"})

_STANCE_PROMPT = """You are classifying the stance expressed in a response to a \
question. Return exactly one word: pro, anti, neutral, or na.

Definitions:
  pro     — the response advocates for or agrees with the affirmative of the question.
  anti    — the response advocates against or disagrees with the affirmative.
  neutral — the response gives both sides or declines to take a position.
  na      — the response is a refusal, or the question has no pro/anti axis.

Question: {question}
Response: {response}

Stance:"""


@dataclass(frozen=True)
class StanceResult:
    stance: Stance
    confidence: float
    reason: str | None = None


class StanceClassifier(Protocol):
    async def classify(
        self,
        *,
        prompt_id: str,
        axis: str,
        prompt_text: str,
        response_text: str,
    ) -> StanceResult: ...


class LLMStanceClassifier:
    """Wraps a :class:`Runner` to classify stance via the LLM itself.

    Cache format: one JSONL line per ``(prompt_id, response_hash)`` key. Safe
    to share across pipeline runs — appending is atomic at the line level.
    """

    def __init__(self, runner: Runner, cache_path: Path | None = None) -> None:
        self._runner = runner
        self._cache_path = cache_path
        self._cache: dict[str, StanceResult] = {}
        if cache_path and cache_path.exists():
            for line in cache_path.read_text().splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                self._cache[obj["key"]] = StanceResult(
                    stance=obj["stance"],
                    confidence=obj["confidence"],
                    reason=obj.get("reason"),
                )

    def _key(self, prompt_id: str, response_text: str) -> str:
        h = hashlib.sha256(response_text.encode("utf-8")).hexdigest()[:16]
        return f"{prompt_id}:{h}"

    def _persist(self, key: str, result: StanceResult) -> None:
        self._cache[key] = result
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cache_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "key": key,
                "stance": result.stance,
                "confidence": result.confidence,
                "reason": result.reason,
            }) + "\n")

    async def classify(
        self,
        *,
        prompt_id: str,
        axis: str,
        prompt_text: str,
        response_text: str,
    ) -> StanceResult:
        if axis not in STANCE_AXES:
            return StanceResult(stance="na", confidence=1.0, reason="axis-excluded")
        if not response_text.strip():
            return StanceResult(stance="na", confidence=1.0, reason="empty-response")

        key = self._key(prompt_id, response_text)
        if key in self._cache:
            return self._cache[key]

        prompt = _STANCE_PROMPT.format(
            question=prompt_text,
            response=response_text[:2000],
        )
        try:
            sample = await self._runner.sample(
                prompt,
                prompt_id=f"stance:{prompt_id}",
                request_index=0,
                temperature=0.0,
                max_tokens=20,
            )
        except Exception as e:  # pragma: no cover - real-provider path
            _log.warning("stance classifier runner failed: %s", e)
            return StanceResult(stance="na", confidence=0.0, reason=f"runner-error: {e}")

        result = parse_stance(sample.text)
        self._persist(key, result)
        return result


def parse_stance(raw: str) -> StanceResult:
    """Parse the LLM's one-word answer into a StanceResult."""
    m = re.search(r"\b(pro|anti|neutral|na)\b", raw.lower())
    if m is None:
        return StanceResult(stance="na", confidence=0.0, reason=f"unparseable: {raw[:50]!r}")
    return StanceResult(stance=m.group(1), confidence=0.85, reason=None)  # type: ignore[arg-type]
