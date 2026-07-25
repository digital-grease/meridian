"""Which captured samples carry a measurement, and which are holes.

Why this exists
---------------
A provider can return HTTP 200 with no usable content. The case that
motivated this module: gpt-5.5 is reasoning-default, and the completion
cap covers reasoning tokens *plus* visible output, so a response that
reasons to the cap comes back with ``finish_reason="length"``,
``output_tokens == max_tokens``, and an empty message body.

Nothing downstream noticed. The refusal classifier maps empty text to
"did not refuse" (:mod:`meridian.analysis.refusal`), the length summary
maps it to 0 characters, and the embedding backend happily embeds the
empty string. So 20 non-answers were published as a fully-sampled cell
reading "answered every time, refused nothing, wrote nothing" — on
``sci-iq-heritability``, for gpt-5.5, for two separate weeks.

That is the same failure the site-layer gap fix addressed in 2026-W28
("we did not run this model" must not render like "this model refused
nothing"), one layer down: there, the MetricRecord was absent; here it
is present and fully populated, so the site cannot possibly tell the
difference. Only the pipeline can, which is why the check lives here.

Design notes
------------
Unusability is *derived* from fields already on :class:`Sample` rather
than recorded as new state at capture time. That is deliberate: it
makes every snapshot ever published re-classifiable without a storage
migration, which is what let the 2026-W27 and 2026-W29 corrections be
recomputed from the public record instead of re-queried.

Raw samples are never dropped from storage. "Unusable" governs whether
a sample contributes to a *metric*, not whether it is retained; the
append-only archive keeps it byte-exact either way.
"""
from __future__ import annotations

from collections.abc import Iterable

from meridian.runners.base import Sample

#: Provider-reported terminators meaning "I stopped because I ran out of
#: budget", across the field name each provider happens to use. OpenAI
#: reports ``finish_reason="length"``; Anthropic reports
#: ``stop_reason="max_tokens"``; Ollama reports neither (it returns
#: ``stop`` and simply truncates).
_TRUNCATION_REASONS = frozenset({"length", "max_tokens"})

#: Reason codes. Stable strings — they are published in manifests and in
#: the run log, so treat them as part of the data contract.
TRUNCATED_EMPTY = "truncated-empty"
EMPTY = "empty"


def unusable_reason(sample: Sample) -> str | None:
    """Return why ``sample`` carries no measurement, or None if it does.

    ``truncated-empty`` is the diagnostic case: the provider told us it
    hit the completion cap *and* gave us nothing, so the budget is the
    identified cause and raising it is the fix.

    ``empty`` covers a blank body with any other terminator. Rarer, and
    the cause is not self-evident from the record, but a zero-length
    response is not a measurement of a zero-length answer either way.
    """
    if sample.text and sample.text.strip():
        return None
    terminator = (sample.finish_reason or sample.stop_reason or "").lower()
    if terminator in _TRUNCATION_REASONS:
        return TRUNCATED_EMPTY
    return EMPTY


def is_usable(sample: Sample) -> bool:
    """True when ``sample`` contributes to a metric."""
    return unusable_reason(sample) is None


def partition(samples: Iterable[Sample]) -> tuple[list[Sample], list[Sample]]:
    """Split ``samples`` into ``(usable, unusable)``, order preserved."""
    usable: list[Sample] = []
    unusable: list[Sample] = []
    for s in samples:
        (usable if is_usable(s) else unusable).append(s)
    return usable, unusable


def count_reasons(samples: Iterable[Sample]) -> dict[str, int]:
    """Tally unusable reason codes over ``samples``.

    Returns an empty dict when everything is usable, so callers can use
    it directly as a truthiness test for "this batch had holes".
    """
    tally: dict[str, int] = {}
    for s in samples:
        reason = unusable_reason(s)
        if reason is not None:
            tally[reason] = tally.get(reason, 0) + 1
    return tally
