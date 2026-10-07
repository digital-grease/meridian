"""OpenAI / GPT runner.

Uses the official ``openai`` SDK (chat completions endpoint). The SDK
reports the exact deployed model string in ``response.model`` — critical
for detecting silent upstream upgrades.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import openai
from openai import APIStatusError, AsyncOpenAI

from meridian.runners._retry import with_retry
from meridian.runners.base import (
    AuthError,
    BillingError,
    ContentPolicyError,
    RateLimitError,
    Runner,
    RunnerError,
    Sample,
    UpstreamError,
)

_log = logging.getLogger(__name__)


#: o-series reasoning model prefixes reject the `temperature` parameter
#: at ANY value (400 `unsupported_value`). Extend this list when a 400
#: on temperature at every value appears for a new family.
_TEMPERATURE_UNSUPPORTED_PREFIXES: tuple[str, ...] = (
    "o1",
    "o3",
    "o4",
)

#: Reasoning-default GPT-5 model prefixes that accept ONLY the API
#: default temperature (1.0) and 400 on anything else with
#: "'temperature' does not support 0 ... Only the default (1) value is
#: supported." gpt-5.5 joined this class on the 2026-06-30 cadence swap;
#: gpt-5.1 (the prior frontier) still accepted any value. Extend this
#: list when that specific 400 appears for a new GPT-5.x model.
#:
#: "gpt-6" is a family prefix, added 2026-10-06 with gpt-6-astra. Its
#: parameter support is not documented, so the family is treated like
#: gpt-5.5, the reasoning model it succeeds. Erring this way costs at
#: most the zero-temperature batch on a model that would have accepted
#: it; erring the other way loses it to 400s, as 2026-W27 did.
_TEMPERATURE_DEFAULT_ONLY_PREFIXES: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-6",
)

#: OpenAI chat-completions treats 1.0 as the default temperature; only
#: this value is accepted by the default-only prefixes above.
_OPENAI_DEFAULT_TEMPERATURE = 1.0


def _openai_supports_temperature(model_id: str, temperature: float) -> bool:
    """Pure-function helper mirroring the runner method, so tests can
    exercise it without constructing an SDK-backed runner."""
    mid = model_id.lower()
    if any(mid.startswith(p) for p in _TEMPERATURE_UNSUPPORTED_PREFIXES):
        return False  # o-series rejects temperature at any value
    if any(mid.startswith(p) for p in _TEMPERATURE_DEFAULT_ONLY_PREFIXES):
        return temperature == _OPENAI_DEFAULT_TEMPERATURE
    return True


def _openai_sends_temperature(model_id: str) -> bool:
    """Whether ``temperature`` goes on the request at all.

    The o-series and the default-only families above never get it: the
    only value they could take is the API default, and leaving the
    parameter out yields that default without betting on how a family
    with undocumented parameter support (gpt-6) treats an explicit
    value. ``Sample.temperature`` still records the intended 1.0.
    """
    mid = model_id.lower()
    return not any(
        mid.startswith(p)
        for p in _TEMPERATURE_UNSUPPORTED_PREFIXES + _TEMPERATURE_DEFAULT_ONLY_PREFIXES
    )


class OpenAIRunner(Runner):
    provider = "openai"

    def __init__(
        self,
        model_id: str,
        *,
        api_key: str | None = None,
        client: AsyncOpenAI | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.model_id = model_id
        self.client = client or AsyncOpenAI(api_key=api_key)
        self.max_tokens_override = max_tokens

    def supports_temperature(self, temperature: float) -> bool:
        return _openai_supports_temperature(self.model_id, temperature)

    async def prepare(self) -> None:
        """Send one 1-token request so a dead account fails the runner up front.

        Raises :class:`BillingError` or :class:`AuthError`, which the
        orchestrator records as one failure for the whole runner before
        any prompt is attempted. Every other failure is logged and
        swallowed, because the probe exists to answer "can this account
        pay" and an inconclusive answer must not cost a week of samples.

        ``temperature`` is omitted rather than sent, so the API default
        applies. That is the one value every model accepts, including
        the default-only gpt-5.5 and the o-series that rejects the
        parameter outright. The token cap goes under whichever name
        :func:`_token_kwarg_for` says this model accepts.
        """
        try:
            await self.client.chat.completions.create(
                model=self.model_id,
                messages=[{"role": "user", "content": _PROBE_PROMPT}],
                **{_token_kwarg_for(self.model_id): _PROBE_MAX_TOKENS},
            )
        except openai.APIError as e:
            err = _map_error(e)
            if isinstance(err, (BillingError, AuthError)):
                raise err from e
            _log.warning(
                "[%s/%s] preflight probe inconclusive (%s: %s); sampling anyway",
                self.provider, self.model_id, type(err).__name__, e,
            )
            return
        _log.info("[%s/%s] preflight probe ok", self.provider, self.model_id)

    async def sample(
        self,
        prompt: str,
        *,
        prompt_id: str,
        request_index: int,
        temperature: float,
        max_tokens: int = 1024,
    ) -> Sample:
        request_kwargs: dict = {_token_kwarg_for(self.model_id): max_tokens}
        if _openai_sends_temperature(self.model_id):
            request_kwargs["temperature"] = temperature

        async def one_call() -> Sample:
            started = time.monotonic()
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model_id,
                    messages=[{"role": "user", "content": prompt}],
                    **request_kwargs,
                )
            except openai.APIError as e:
                raise _map_error(e) from e

            latency_ms = int((time.monotonic() - started) * 1000)
            choice = resp.choices[0] if resp.choices else None
            text = (choice.message.content or "") if choice else ""
            usage = getattr(resp, "usage", None)
            return Sample(
                prompt_id=prompt_id,
                model_id=self.model_id,
                provider=self.provider,
                request_index=request_index,
                temperature=temperature,
                max_tokens=max_tokens,
                text=text,
                model_version_string=resp.model,
                stop_reason=None,
                finish_reason=(choice.finish_reason if choice else None),
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
                request_id=getattr(resp, "id", None),
                api_version=f"openai-sdk-{openai.__version__}",
                latency_ms=latency_ms,
                captured_at=datetime.now(timezone.utc),
                safety_flags=[],
            )

        return await with_retry(one_call)


#: The preflight probe: the smallest completion the API accepts.
_PROBE_MAX_TOKENS = 1
_PROBE_PROMPT = "ping"

#: Error codes OpenAI uses when the account cannot pay for the request.
#: ``insufficient_quota`` arrives as a 429, the same status as a real
#: rate limit, so without this check an empty balance is retried with
#: backoff on every request and then logged as rate limiting.
_BILLING_CODES: frozenset[str] = frozenset(
    {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "billing_not_active",
    }
)


def _is_billing_error(e: APIStatusError) -> bool:
    """True when OpenAI refused the request because the account cannot pay.

    Matches on the machine-readable ``code`` (or ``type``, which OpenAI
    sets to the same string for quota errors) and nothing else. A false
    positive halts the runner for the rest of the week, so prose is not
    trusted here.
    """
    for attr in ("code", "type"):
        value = getattr(e, attr, None)
        if isinstance(value, str) and value.lower() in _BILLING_CODES:
            return True
    return False


def _map_error(e: openai.APIError) -> RunnerError:
    """Translate an SDK exception into the runner error taxonomy.

    Billing is checked first because OpenAI reports it under 429 and 400,
    statuses that otherwise mean "retry" and "malformed request".
    """
    if isinstance(e, APIStatusError) and _is_billing_error(e):
        return BillingError(str(e))
    if isinstance(e, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return AuthError(str(e))
    if isinstance(e, openai.RateLimitError):
        return RateLimitError(str(e), retry_after_s=_parse_retry_after(e))
    if isinstance(e, APIStatusError) and _is_content_policy_rejection(e):
        return ContentPolicyError(str(e))
    return UpstreamError(str(e))


#: Error codes OpenAI uses when it declines the request itself rather
#: than the request being malformed.
_CONTENT_POLICY_CODES: frozenset[str] = frozenset(
    {
        "content_policy_violation",
        "content_filter",
    }
)

#: Message fragments for the same thing when no machine-readable code
#: comes with it, which was the case for the first one observed.
#:
#: Matching on prose is unpleasant and it is here under protest. The
#: 2026-W33 rejection of ``ref-wifi-unauthorized`` carried a `type` of
#: `invalid_request_error` and no `code` at all, so the only signal
#: distinguishing "we will not run this prompt" from "your request is
#: malformed" was the sentence itself. Prefer the code path above; add
#: to this list only from a rejection actually seen in the archive, and
#: keep the fragments long enough that they cannot match a genuine
#: parameter error.
_CONTENT_POLICY_MESSAGE_MARKERS: tuple[str, ...] = (
    "flagged for possible cybersecurity risk",
    "violates our content policy",
    "against our usage policies",
    "rejected by the safety system",
)


def _is_content_policy_rejection(e: APIStatusError) -> bool:
    """True when a 4xx is the provider declining the prompt on content.

    Conservative on purpose, and the asymmetry is deliberate. A missed
    detection costs a retry storm and a failed pair, both of which are
    visible in the run log and cost a few seconds. A false positive
    quietly reclassifies a genuine API fault as a content decision, and
    since the class exists precisely to stop retrying, it would also
    convert a transient failure into a permanent one. So this returns
    True only on positive evidence.

    Scoped to 400 rather than any 4xx: 401/403/429 already have their
    own branches upstream of this, and a 404 for a retired model must
    stay a loud error rather than becoming a content finding.
    """
    if getattr(e, "status_code", None) != 400:
        return False
    code = (getattr(e, "code", None) or "").lower()
    if code in _CONTENT_POLICY_CODES:
        return True
    message = (getattr(e, "message", None) or str(e)).lower()
    return any(marker in message for marker in _CONTENT_POLICY_MESSAGE_MARKERS)


#: Model families that take ``max_completion_tokens``. "gpt-6" was added
#: 2026-10-06 with gpt-6-astra, a reasoning model; every OpenAI reasoning
#: family since GPT-5 rejects ``max_tokens``, and sending it would 400
#: every request of the week.
_COMPLETION_TOKENS_PREFIXES: tuple[str, ...] = ("gpt-5", "gpt-6", "o1", "o3", "o4")


def _token_kwarg_for(model_id: str) -> str:
    """Return the token-cap parameter name the model's API accepts.

    GPT-5 family and o-series reasoning models (o1, o3, o4, ...)
    reject `max_tokens` with `unsupported_parameter`; they require
    `max_completion_tokens`. Legacy `gpt-4*` / `gpt-3.5*` still accept
    the older name. OpenAI flipped the default as part of the GPT-5
    rollout; the error message on a failed call is the signal to
    extend this list when new model families ship.
    """
    mid = model_id.lower()
    if mid.startswith(_COMPLETION_TOKENS_PREFIXES):
        return "max_completion_tokens"
    return "max_tokens"


def _parse_retry_after(e: openai.RateLimitError) -> float | None:
    resp = getattr(e, "response", None)
    if resp is None:
        return None
    hdr = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
    if hdr is None:
        return None
    try:
        return float(hdr)
    except (TypeError, ValueError):
        return None
