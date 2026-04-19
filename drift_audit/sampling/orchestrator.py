"""Weekly sampling orchestrator.

Iterates over (prompt × runner) pairs and invokes each runner's
:meth:`Runner.batch` method to capture N samples per pair, persisting each
to the :class:`LocalSampleStore` as it arrives.

Idempotency: before sampling a (prompt, runner) pair, checks existing
sample count in storage. Skips pairs that already have the required count
unless ``force=True``.

Concurrency: each runner gets its own concurrency slot (the runner's
internal :meth:`batch` already bounds concurrency to the provider).
Different runners proceed in parallel via :func:`asyncio.gather`.

Error handling: per-pair errors are collected in the RunOutcome. One
provider going dark does not stop the rest of the run.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from drift_audit.corpus import Corpus, Prompt
from drift_audit.runners import Runner, RunnerError
from drift_audit.storage import LocalSampleStore

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SamplingPlan:
    week_id: str
    n_default_temp: int = 20
    n_zero_temp: int = 5
    default_temperature: float = 1.0
    zero_temperature: float = 0.0
    max_tokens: int = 1024
    concurrency_per_provider: int = 4

    @property
    def samples_per_pair(self) -> int:
        return self.n_default_temp + self.n_zero_temp


@dataclass
class PairError:
    provider: str
    model_id: str
    prompt_id: str
    error_type: str
    message: str


@dataclass
class RunOutcome:
    week_id: str
    total_samples_written: int = 0
    pairs_complete: int = 0
    pairs_skipped: int = 0
    pairs_failed: int = 0
    per_runner_samples: dict[str, int] = field(default_factory=dict)
    errors: list[PairError] = field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        runners: list[Runner],
        store: LocalSampleStore,
        corpus: Corpus,
        plan: SamplingPlan,
    ) -> None:
        self.runners = runners
        self.store = store
        self.corpus = corpus
        self.plan = plan

    async def run(
        self,
        *,
        prompts: list[Prompt] | None = None,
        force: bool = False,
    ) -> RunOutcome:
        prompts = prompts if prompts is not None else self.corpus.public()
        outcome = RunOutcome(week_id=self.plan.week_id)

        async def run_one_runner(runner: Runner) -> None:
            runner_key = f"{runner.provider}/{runner.model_id}"
            outcome.per_runner_samples.setdefault(runner_key, 0)
            for prompt in prompts:
                existing = self.store.count(
                    self.plan.week_id, runner.model_id, prompt.id
                )
                if not force and existing >= self.plan.samples_per_pair:
                    outcome.pairs_skipped += 1
                    continue

                if force:
                    # Append another full plan's worth; storage is append-only,
                    # so old samples stay intact and indices continue from here.
                    n_default_batch = self.plan.n_default_temp
                    start_default = existing
                    n_zero_batch = self.plan.n_zero_temp
                    start_zero = existing + n_default_batch
                else:
                    n_default_batch = max(0, self.plan.n_default_temp - existing)
                    start_default = existing
                    already_have = max(existing, self.plan.n_default_temp)
                    n_zero_batch = max(0, self.plan.samples_per_pair - already_have)
                    start_zero = already_have

                try:
                    if n_default_batch > 0:
                        async for s in runner.batch(
                            prompt.text,
                            prompt_id=prompt.id,
                            n=n_default_batch,
                            temperature=self.plan.default_temperature,
                            max_tokens=self.plan.max_tokens,
                            concurrency=self.plan.concurrency_per_provider,
                            start_index=start_default,
                        ):
                            self.store.append(
                                self.plan.week_id,
                                runner.model_id,
                                prompt.id,
                                s,
                            )
                            outcome.total_samples_written += 1
                            outcome.per_runner_samples[runner_key] += 1

                    if n_zero_batch > 0:
                        async for s in runner.batch(
                            prompt.text,
                            prompt_id=prompt.id,
                            n=n_zero_batch,
                            temperature=self.plan.zero_temperature,
                            max_tokens=self.plan.max_tokens,
                            concurrency=self.plan.concurrency_per_provider,
                            start_index=start_zero,
                        ):
                            self.store.append(
                                self.plan.week_id,
                                runner.model_id,
                                prompt.id,
                                s,
                            )
                            outcome.total_samples_written += 1
                            outcome.per_runner_samples[runner_key] += 1

                    outcome.pairs_complete += 1
                except RunnerError as e:
                    outcome.pairs_failed += 1
                    outcome.errors.append(
                        PairError(
                            provider=runner.provider,
                            model_id=runner.model_id,
                            prompt_id=prompt.id,
                            error_type=type(e).__name__,
                            message=str(e)[:200],
                        )
                    )
                    _log.warning(
                        "pair failed: %s / %s / %s: %s",
                        runner.provider, runner.model_id, prompt.id, e,
                    )

        await asyncio.gather(*[run_one_runner(r) for r in self.runners])
        return outcome
