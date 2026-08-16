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
import time
from dataclasses import dataclass, field

from meridian.analysis import usability
from meridian.corpus import Corpus, Prompt
from meridian.runners import Runner, RunnerError
from meridian.runners.base import IntegrityError, Sample
from meridian.storage import LocalSampleStore

_log = logging.getLogger(__name__)


def _fmt_secs(seconds: float) -> str:
    """Compact wall-clock formatter for progress logs: "8s", "3m12s",
    "1h05m". Drops sub-second precision; cosmetic only."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


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
    #: Samples that were captured and stored but carry no measurement
    #: (see :mod:`meridian.analysis.usability`), keyed
    #: ``"provider/model_id"`` -> reason code -> count. Non-empty means
    #: the run produced holes: the artifacts still publish, but
    #: ``scripts/check_run_health.py`` turns the job red so a human
    #: looks. Kept separate from ``errors``, which mean "the request
    #: failed"; these requests succeeded and returned nothing.
    unusable_samples: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Provider-declared refusals (``stop_reason="refusal"`` with an
    #: empty body), keyed ``"provider/model_id"`` -> prompt_id -> count.
    #:
    #: Deliberately NOT folded into ``unusable_samples``. These samples
    #: are measured outcomes: they count toward the cell's N and toward
    #: the published refusal rate, and an empty ``unusable_samples`` must
    #: keep meaning "the run produced no holes" so the existing fail
    #: thresholds in ``scripts/check_run_health.py`` are untouched.
    #:
    #: It is still notable. Between 2026-W28 and 2026-W32 Anthropic moved
    #: ``claude-opus-4-8``'s refusal on ``ref-pipe-bomb-construct`` out of
    #: the completion body and into the terminator. Once an API refusal
    #: became a usable sample, that switch produced no warning, no error
    #: and no run-log entry anywhere in the pipeline, which inverts
    #: CLAUDE.md's "model version instability ... flag loudly when
    #: detected". Keyed by prompt because a mechanism switch is a
    #: per-cell event: "20 on ref-pipe-bomb-construct" and "1 each across
    #: 20 prompts" mean completely different things, and the runner-level
    #: total cannot tell them apart.
    api_refusal_samples: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_unusable(self) -> int:
        return sum(sum(v.values()) for v in self.unusable_samples.values())

    @property
    def total_api_refusals(self) -> int:
        return sum(sum(v.values()) for v in self.api_refusal_samples.values())


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
        # Default samples BOTH public and held-out — the whole point of the
        # held-out set is to measure against it. The manifest writer is
        # what decides which ones cross the boundary into the public site.
        prompts = prompts if prompts is not None else self.corpus.all()
        outcome = RunOutcome(week_id=self.plan.week_id)

        async def run_one_runner(runner: Runner) -> None:
            runner_key = f"{runner.provider}/{runner.model_id}"
            outcome.per_runner_samples.setdefault(runner_key, 0)
            total = len(prompts)
            run_started = time.monotonic()
            samples_per_pair = self.plan.samples_per_pair
            try:
                await runner.prepare()
            except IntegrityError as e:
                _log.error("[%s] prepare failed: %s", runner_key, e)
                outcome.errors.append(PairError(
                    provider=runner.provider,
                    model_id=runner.model_id,
                    prompt_id="*",
                    error_type="integrity",
                    message=str(e),
                ))
                outcome.pairs_failed += total
                return
            except RunnerError as e:
                _log.error("[%s] prepare failed: %s", runner_key, e)
                outcome.errors.append(PairError(
                    provider=runner.provider,
                    model_id=runner.model_id,
                    prompt_id="*",
                    error_type="prepare",
                    message=str(e),
                ))
                outcome.pairs_failed += total
                return
            # A runner may pin its own completion cap (reasoning-default
            # models need a bigger budget because reasoning tokens are
            # billed against it); otherwise the plan's shared cap applies.
            run_max_tokens = runner.max_tokens_override or self.plan.max_tokens
            _log.info(
                "[%s] starting: %d prompts × up to %d samples/pair "
                "(max_tokens=%d%s)",
                runner_key, total, samples_per_pair, run_max_tokens,
                " [runner override]" if runner.max_tokens_override else "",
            )
            for idx, prompt in enumerate(prompts, 1):
                pair_started = time.monotonic()
                samples_before = outcome.per_runner_samples[runner_key]
                existing = self.store.count(
                    self.plan.week_id, runner.model_id, prompt.id
                )
                if not force and existing >= self.plan.samples_per_pair:
                    outcome.pairs_skipped += 1
                    self._log_progress(
                        runner_key, idx, total, prompt.id, "SKIP",
                        pair_started, run_started,
                        samples_added=0,
                    )
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
                        if not runner.supports_temperature(
                            self.plan.default_temperature
                        ):
                            _log.warning(
                                "%s/%s does not accept temperature=%s; "
                                "skipping %d default-temp sample(s) for %s",
                                runner.provider, runner.model_id,
                                self.plan.default_temperature,
                                n_default_batch, prompt.id,
                            )
                        else:
                            async for s in runner.batch(
                                prompt.text,
                                prompt_id=prompt.id,
                                n=n_default_batch,
                                temperature=self.plan.default_temperature,
                                max_tokens=run_max_tokens,
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
                                self._note_outcome(
                                    outcome, runner_key, prompt.id, s
                                )

                    if n_zero_batch > 0:
                        if not runner.supports_temperature(
                            self.plan.zero_temperature
                        ):
                            _log.info(
                                "%s/%s does not accept temperature=%s; "
                                "skipping %d zero-temp sample(s) for %s "
                                "(model does not expose non-default temperature)",
                                runner.provider, runner.model_id,
                                self.plan.zero_temperature,
                                n_zero_batch, prompt.id,
                            )
                        else:
                            async for s in runner.batch(
                                prompt.text,
                                prompt_id=prompt.id,
                                n=n_zero_batch,
                                temperature=self.plan.zero_temperature,
                                max_tokens=run_max_tokens,
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
                                self._note_outcome(
                                    outcome, runner_key, prompt.id, s
                                )

                    outcome.pairs_complete += 1
                    status = "OK"
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
                    status = "FAIL"

                samples_added = (
                    outcome.per_runner_samples[runner_key] - samples_before
                )
                self._log_progress(
                    runner_key, idx, total, prompt.id, status,
                    pair_started, run_started, samples_added=samples_added,
                )

            holes = outcome.unusable_samples.get(runner_key, {})
            _log.info(
                "[%s] complete in %s — %d ok, %d skipped, %d failed",
                runner_key, _fmt_secs(time.monotonic() - run_started),
                outcome.pairs_complete, outcome.pairs_skipped,
                outcome.pairs_failed,
            )
            if holes:
                _log.error(
                    "[%s] %d sample(s) returned no usable content (%s). These "
                    "are stored but excluded from every metric. If the reason "
                    "is 'truncated-empty', the model spent its whole "
                    "completion budget without emitting output — raise this "
                    "runner's max_tokens in config.yaml.",
                    runner_key, sum(holes.values()),
                    ", ".join(f"{k}={v}" for k, v in sorted(holes.items())),
                )
            declined = outcome.api_refusal_samples.get(runner_key, {})
            if declined:
                _log.warning(
                    "[%s] %d sample(s) were provider-declared refusals, by "
                    "prompt: %s. These are measurements, not holes: they "
                    "count toward N and toward the published refusal rate, "
                    "and they carry no text, so they are excluded from "
                    "length, hedge, embedding and stance. Compare the prior "
                    "week for the same cell before reading this as drift. A "
                    "cell that returned prose refusals last time and "
                    "terminator refusals now has changed refusal MECHANISM, "
                    "not refusal RATE, which is what claude-opus-4-8 did on "
                    "ref-pipe-bomb-construct between 2026-W28 and 2026-W32.",
                    runner_key, sum(declined.values()),
                    ", ".join(f"{k}={v}" for k, v in sorted(declined.items())),
                )

        await asyncio.gather(*[run_one_runner(r) for r in self.runners])
        return outcome

    @staticmethod
    def _note_outcome(
        outcome: RunOutcome,
        runner_key: str,
        prompt_id: str,
        sample: Sample,
    ) -> None:
        """Tally a stored sample whose outcome is not an ordinary answer.

        The sample is already persisted by the time this runs, and stays
        persisted: retention is append-only and a non-answer is itself
        evidence. This only records how the metric layer will read it.

        Two destinations, kept apart on purpose.
        ``outcome.unusable_samples`` holds holes, samples that carry no
        measurement at all, and drives the fail thresholds in
        ``scripts/check_run_health.py``.
        ``outcome.api_refusal_samples`` holds provider-declared refusals,
        which are measurements and must not move those thresholds.

        Before 2026-W32 an API refusal landed in the first bucket and the
        run failed loudly. Once it became a usable sample it landed in
        neither, so a provider changing refusal mechanism across the
        whole corpus would have run green and silent. Routing it to its
        own bucket is what keeps it visible without calling it a defect.
        """
        for code, count in usability.count_outcomes([sample]).items():
            if code == usability.API_REFUSAL:
                by_prompt = outcome.api_refusal_samples.setdefault(
                    runner_key, {}
                )
                by_prompt[prompt_id] = by_prompt.get(prompt_id, 0) + count
                _log.info(
                    "api refusal: %s / %s (index %d): provider declared the "
                    "refusal in its terminator (finish_reason=%r, "
                    "stop_reason=%r, output_tokens=%s); counted as a refusal, "
                    "excluded from every text-derived metric",
                    runner_key, prompt_id, sample.request_index,
                    sample.finish_reason, sample.stop_reason,
                    sample.output_tokens,
                )
                continue
            by_reason = outcome.unusable_samples.setdefault(runner_key, {})
            by_reason[code] = by_reason.get(code, 0) + count
            _log.warning(
                "unusable sample: %s / %s (index %d): %s "
                "(finish_reason=%r, output_tokens=%s, max_tokens=%d)",
                runner_key, prompt_id, sample.request_index, code,
                sample.finish_reason or sample.stop_reason,
                sample.output_tokens, sample.max_tokens,
            )

    @staticmethod
    def _log_progress(
        runner_key: str,
        idx: int,
        total: int,
        prompt_id: str,
        status: str,
        pair_started: float,
        run_started: float,
        *,
        samples_added: int,
    ) -> None:
        """One log line per (runner, prompt) iteration with status, timing,
        and a rough ETA derived from average pair time so far. ETA is
        cosmetic — it gets less wrong as the run progresses."""
        now = time.monotonic()
        pair_elapsed = now - pair_started
        run_elapsed = now - run_started
        rate = run_elapsed / idx if idx else 0
        eta_secs = rate * (total - idx)
        _log.info(
            "[%s] %d/%d %s %s "
            "(pair %s, +%d samples, elapsed %s, eta %s)",
            runner_key, idx, total, status, prompt_id,
            _fmt_secs(pair_elapsed), samples_added,
            _fmt_secs(run_elapsed), _fmt_secs(eta_secs),
        )
