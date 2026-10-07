"""Provider probe: page on Sunday when Monday's run could not pay.

Why this exists
---------------
Anthropic credit is prepaid and auto-reload is deliberately off. When
the balance runs out nothing announces it: the account simply starts
answering every request with a 400 whose only distinguishing feature is
the sentence "credit balance is too low". 2026-W36 and 2026-W38 were
both lost that way. The weekly run sampled nothing from the affected
runners, the loss was only visible after the fact, and by then the week
could not be recovered, because a sample taken Thursday is not a Monday
sample and the project does not backfill.

The pipeline's own preflight (``prepare()`` in meridian/runners) catches
the same condition, but only at 09:00 UTC on the Monday itself, which
is the moment it stops being fixable. This function asks the identical
question roughly 21 hours earlier, when topping up is still a five
minute job.

What it does
------------
One minimal real request per target, every commercial model in the
weekly roster plus the stance classifier, using the same SSM-held keys
the instance uses. A target whose ``last_week`` is before the coming
run's label is retired and skipped entirely, so a model that has left
the roster on schedule never pages; one before its ``first_week`` is
still probed, so a new model's access is proven before its first run.
Each answer is sorted into exactly one status:

  OK            2xx. The key authenticates, the account can pay, the
                model is served.
  BILLING       The account cannot pay. Top up before Monday.
  AUTH          The key was rejected, or its SSM parameter is gone.
  MODEL-GONE    The provider no longer serves this model id.
  INCONCLUSIVE  Anything else: a timeout, a 5xx that survived the one
                retry, an unfamiliar 400. Worth a look, not a page.

The billing rules mirror ``_is_billing_error`` in
meridian/runners/anthropic.py and meridian/runners/openai.py. They are
copied rather than imported because this file ships alone in a zip and
cannot import the meridian package. Keep the two in step: the probe is
only useful if it calls the same failure by the same name the runner
will on Monday.

What it does not do
-------------------
It does not touch EC2. The instance stays stopped; this runs entirely
inside Lambda and costs a fraction of a cent per week in tokens.

It does not prove Monday will succeed. Credit can still run out between
Sunday noon and Monday morning if the balance is close to the edge, and
the probe spends almost none of it. A probe that passes with a balance
of a few dollars is a false comfort, which is why the runbook says to
keep a week's worth of headroom rather than to trust a green Sunday.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

import boto3

_log = logging.getLogger()
_log.setLevel(logging.INFO)

ssm = boto3.client("ssm")
sns = boto3.client("sns")

SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
ANTHROPIC_KEY_PARAM = os.environ["ANTHROPIC_KEY_PARAM"]
OPENAI_KEY_PARAM = os.environ["OPENAI_KEY_PARAM"]

#: Per-request socket timeout. Thirty seconds is generous for a 16 token
#: completion; anything slower than that on a Sunday is reported as
#: INCONCLUSIVE rather than waited out.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30"))

#: Cap on any single backoff, including one a Retry-After header asks
#: for. The function has one retry per target and a fixed timeout, so a
#: provider asking for a minute's patience is answered INCONCLUSIVE
#: rather than obeyed.
MAX_BACKOFF_SECONDS = 10.0
DEFAULT_BACKOFF_SECONDS = 2.0

#: Large enough that no model rejects it as too small, small enough that
#: the probe costs nothing. claude-opus-5 runs adaptive thinking when
#: ``thinking`` is omitted, and on claude-opus-5-5 thinking cannot be
#: disabled at all; either may spend all 16 on reasoning, which still
#: comes back 200, and 200 is the only thing being asked. Should a model
#: ever reject a cap this small, that is a 400 and so INCONCLUSIVE (a
#: WARN), never a page.
PROBE_MAX_TOKENS = 16
PROBE_PROMPT = "ping"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

OK = "OK"
BILLING = "BILLING"
AUTH = "AUTH"
MODEL_GONE = "MODEL-GONE"
INCONCLUSIVE = "INCONCLUSIVE"

#: Statuses that page. Order is severity, and is also the order the
#: subject line lists them in.
PAGE_STATUSES = (BILLING, AUTH, MODEL_GONE)
STATUS_ORDER = (*PAGE_STATUSES, INCONCLUSIVE)

#: Mirrors _CREDIT_BALANCE_MARKER in meridian/runners/anthropic.py.
_ANTHROPIC_CREDIT_MARKER = "credit balance is too low"

#: Mirrors _BILLING_CODES in meridian/runners/openai.py.
_OPENAI_BILLING_CODES = frozenset(
    {"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"}
)

#: Mirrors _COMPLETION_TOKENS_PREFIXES in meridian/runners/openai.py:
#: GPT-5, GPT-6 and the o-series reject ``max_tokens`` and need
#: ``max_completion_tokens``.
_OPENAI_COMPLETION_TOKENS_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")

#: Mirrors _ISO_WEEK_RE in meridian/config.py. Zero-padded labels compare
#: correctly as strings, which the first/last week bounds rely on.
_ISO_WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")

_SUPPORTED_PROVIDERS = ("anthropic", "openai")


def _load_targets(raw: str) -> list[dict]:
    """Parse and check PROBE_TARGETS at import time.

    A malformed list fails the cold start, which raises Errors and pages
    through meridian-provider-probe-errors. That is the right outcome:
    a probe quietly checking nothing is worse than one that is visibly
    broken.
    """
    targets = json.loads(raw)
    if not isinstance(targets, list) or not targets:
        raise ValueError("PROBE_TARGETS must be a non-empty JSON list")
    for t in targets:
        if t.get("provider") not in _SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported provider in PROBE_TARGETS: {t!r}")
        if not t.get("model_id"):
            raise ValueError(f"PROBE_TARGETS entry has no model_id: {t!r}")
        # Terraform's optional() attributes arrive as JSON null when unset.
        for bound in ("first_week", "last_week"):
            value = t.get(bound)
            if value is not None and not (
                isinstance(value, str) and _ISO_WEEK_RE.match(value)
            ):
                raise ValueError(f"PROBE_TARGETS {bound} is not an ISO week label: {t!r}")
        if t.get("first_week") and t.get("last_week") and t["first_week"] > t["last_week"]:
            raise ValueError(f"PROBE_TARGETS first_week is after last_week: {t!r}")
    return targets


TARGETS = _load_targets(os.environ["PROBE_TARGETS"])

# Indirection so tests can run the retry path without sleeping.
_sleep = time.sleep


# ---------- Secrets -----------------------------------------------------


def _key_param(provider: str) -> str:
    return ANTHROPIC_KEY_PARAM if provider == "anthropic" else OPENAI_KEY_PARAM


def _read_key(provider: str) -> tuple[str | None, str | None, str | None]:
    """Return ``(key, failure_status, failure_message)`` for a provider.

    A deleted parameter is AUTH: Monday's run would have no key either,
    so it is the same emergency as a revoked one. Any other SSM failure
    (most plausibly AccessDenied on this function's own role) is
    INCONCLUSIVE, because it says something about the probe rather than
    about the credential. It is flagged ``key_read_failed`` so the email
    points at the IAM grant instead of at the provider.
    """
    name = _key_param(provider)
    try:
        resp = ssm.get_parameter(Name=name, WithDecryption=True)
    except Exception as e:  # noqa: BLE001 - reported per target, never raised
        code = getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__
        status = AUTH if code == "ParameterNotFound" else INCONCLUSIVE
        return None, status, f"could not read SSM parameter {name} ({code})"
    value = (resp.get("Parameter") or {}).get("Value") or ""
    if not value.strip():
        return None, AUTH, f"SSM parameter {name} is empty"
    return value.strip(), None, None


# ---------- Message hygiene ---------------------------------------------

#: Anything shaped like a provider key. OpenAI echoes a masked prefix of
#: a rejected key in its 401 body ("Incorrect API key provided:
#: sk-proj-****abcd"), and a masked key is still not something to email.
_KEY_SHAPE = re.compile(r"sk-[A-Za-z0-9_\-*]{4,}")

_MESSAGE_LIMIT = 200


def _sanitize(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Make a provider message safe and short enough to email.

    Secrets are removed by exact value first and then by shape, so a key
    is redacted even when the provider mangles or partially masks it.
    """
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    out = _KEY_SHAPE.sub("[REDACTED]", out)
    out = " ".join(out.split())
    out = out.encode("ascii", "replace").decode("ascii")
    if len(out) > _MESSAGE_LIMIT:
        out = out[: _MESSAGE_LIMIT - 3] + "..."
    return out


# ---------- HTTP --------------------------------------------------------


def _build_request(provider: str, model_id: str, key: str) -> urllib.request.Request:
    """One minimal completion request.

    ``temperature`` is omitted for both providers: the API default is the
    one value every model accepts, including the Opus models that reject
    anything else and the default-only GPT-5.5 / GPT-6 models. Nothing
    else is sent either: no ``thinking`` (claude-opus-5-5 400s on any
    attempt to disable it) and never the server-side ``fallbacks``
    parameter, which would let a different model answer for this one.
    """
    messages = [{"role": "user", "content": PROBE_PROMPT}]
    if provider == "anthropic":
        url = ANTHROPIC_URL
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        payload = {"model": model_id, "max_tokens": PROBE_MAX_TOKENS, "messages": messages}
    else:
        url = OPENAI_URL
        headers = {"Authorization": f"Bearer {key}"}
        token_kwarg = (
            "max_completion_tokens"
            if model_id.lower().startswith(_OPENAI_COMPLETION_TOKENS_PREFIXES)
            else "max_tokens"
        )
        payload = {"model": model_id, "messages": messages, token_kwarg: PROBE_MAX_TOKENS}
    headers["content-type"] = "application/json"
    return urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )


def _send(req: urllib.request.Request) -> tuple[int | None, dict | None, str, str | None]:
    """Send once. Returns ``(http_status, json_body, text, retry_after)``.

    ``http_status`` is None for a failure below HTTP (DNS, TLS, refused,
    timed out). Nothing raised here escapes: every outcome is data.
    """
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return resp.status, None, "", None
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a body we cannot read is just empty
            text = ""
        try:
            body = json.loads(text) if text else None
        except ValueError:
            body = None
        retry_after = e.headers.get("retry-after") if e.headers else None
        return e.code, body if isinstance(body, dict) else None, text, retry_after
    except Exception as e:  # noqa: BLE001 - URLError, timeout, reset: all network
        return None, None, f"{type(e).__name__}: {e}", None


def _error_fields(body: dict | None) -> tuple[str | None, str | None, str | None]:
    """``(type, code, message)`` from either provider's error envelope."""
    inner = (body or {}).get("error")
    if not isinstance(inner, dict):
        return None, None, None

    def _str(key: str) -> str | None:
        value = inner.get(key)
        return value if isinstance(value, str) else None

    return _str("type"), _str("code"), _str("message")


# ---------- Classification ---------------------------------------------


def classify(provider: str, http_status: int | None, body: dict | None, text: str) -> str:
    """Sort one response into a status.

    Billing is checked before the status code means anything, for the
    same reason the runners do it: Anthropic reports an empty balance as
    a plain 400 and OpenAI reports exhausted quota as a 429, statuses
    that otherwise mean "malformed" and "slow down".
    """
    if http_status is not None and 200 <= http_status < 300:
        return OK
    err_type, err_code, err_message = _error_fields(body)
    message = (err_message or text or "").lower()

    if provider == "anthropic":
        # Positive evidence only, as in _is_billing_error: a 402, the
        # documented billing_error type, or the credit sentence on a 400.
        if http_status == 402 or err_type == "billing_error":
            return BILLING
        if http_status == 400 and _ANTHROPIC_CREDIT_MARKER in message:
            return BILLING
    else:
        # Machine-readable code or type only; prose is not trusted.
        for value in (err_code, err_type):
            if value and value.lower() in _OPENAI_BILLING_CODES:
                return BILLING

    # Model before auth: OpenAI answers a project that has lost access to
    # a model with a 403 carrying model_not_found. The key is fine there,
    # and AUTH's action (replace the key) would be the wrong fix.
    if http_status == 404 or err_type == "not_found_error" or err_code == "model_not_found":
        return MODEL_GONE
    if http_status in (401, 403):
        return AUTH
    return INCONCLUSIVE


def _retryable(http_status: int | None) -> bool:
    return http_status is None or http_status == 429 or http_status >= 500


def _backoff(retry_after: str | None) -> float:
    try:
        wanted = float(retry_after) if retry_after is not None else DEFAULT_BACKOFF_SECONDS
    except ValueError:
        wanted = DEFAULT_BACKOFF_SECONDS
    return max(0.0, min(wanted, MAX_BACKOFF_SECONDS))


def probe_target(target: dict, key: str) -> dict:
    """Probe one target. At most two requests: one, plus one retry when
    the first answer was a 429, a 5xx or a network failure that did not
    classify as billing."""
    provider, model_id = target["provider"], target["model_id"]
    attempts = 0
    while True:
        attempts += 1
        http_status, body, text, retry_after = _send(_build_request(provider, model_id, key))
        status = classify(provider, http_status, body, text)
        if status == INCONCLUSIVE and _retryable(http_status) and attempts == 1:
            _sleep(_backoff(retry_after))
            continue
        break
    _, _, err_message = _error_fields(body)
    return {
        "status": status,
        "http_status": http_status,
        "message": "" if status == OK else _sanitize(err_message or text, (key,)),
        "attempts": attempts,
    }


# ---------- Schedule awareness -----------------------------------------


def _next_run_week(now: dt.datetime) -> tuple[str, int]:
    """The ISO week label Monday's run will use, and its week number.

    scripts/run-weekly.sh labels a run with the ISO week of the day
    before it starts, so the Monday after ``now`` samples the week
    containing the Sunday before it. Fired on schedule that is the
    probe's own week; fired by hand midweek it is still the right one.
    """
    days_to_monday = 7 - now.weekday()  # Monday is 0, so always 1..7
    sunday = (now + dt.timedelta(days=days_to_monday - 1)).date()
    year, week, _ = sunday.isocalendar()
    return f"{year}-W{week:02d}", week


def _retired(target: dict, week: str) -> bool:
    """True once the target's last_week is before the coming run's label.

    Mirrors the last_week half of RunnerSpec.runs_in_week in
    meridian/config.py. A retired model is off the roster on purpose, so
    probing it would only page about a model nobody intends to run.
    """
    last = target.get("last_week")
    return bool(last) and week > last


def _in_run(target: dict, week: str, week_number: int) -> bool:
    """Whether the target is due in the run labelled ``week``: cadence
    plus the first/last week bounds, as RunnerSpec.runs_in_week."""
    cadence = target.get("cadence", "every_week")
    if cadence == "even_weeks" and week_number % 2 != 0:
        return False
    if cadence == "odd_weeks" and week_number % 2 != 1:
        return False
    first = target.get("first_week")
    if first and week < first:
        return False
    return not _retired(target, week)


# ---------- Reporting --------------------------------------------------


_ACTIONS = {
    ("anthropic", BILLING): "Top up Anthropic credit before Monday 09:00 UTC. Auto-reload is off by design, so nothing else will.",
    ("openai", BILLING): "Add OpenAI credit or raise the project's usage limit before Monday 09:00 UTC.",
    AUTH: "The key was rejected or is missing. Put a working key in {param} with aws ssm put-parameter (see infra/terraform/ec2-cohabit/README.md) before Monday 09:00 UTC.",
    MODEL_GONE: "The provider no longer serves this model id. Update meridian/config.yaml and var.provider_probe_targets together, and note the series break on the methodology page.",
    INCONCLUSIVE: "Not a known failure shape. Re-run the probe by hand (scripts/ec2-runbook.md) and read /aws/lambda/meridian-provider-probe if it repeats.",
}

#: An SSM read that failed for a reason other than ParameterNotFound.
#: Nothing was sent to the provider, so the generic INCONCLUSIVE advice
#: (re-run, read the logs) would point away from the actual fault.
_KEY_READ_ACTION = (
    "The probe could not read {param}, so these models were not checked. "
    "Check the meridian-provider-probe role's ssm:GetParameter (and "
    "kms:Decrypt, if the parameter moved to a customer managed key) grant."
)


def _action(provider: str, status: str, hits: list[dict] = ()) -> str:
    if status == INCONCLUSIVE and hits and all(r.get("key_read_failed") for r in hits):
        text = _KEY_READ_ACTION
    else:
        text = _ACTIONS.get((provider, status)) or _ACTIONS.get(status, "")
    return text.format(param=_key_param(provider))


def _short_model(model_id: str) -> str:
    """claude-haiku-4-5-20251001 -> haiku-4-5. Subject lines are short."""
    short = model_id[len("claude-"):] if model_id.startswith("claude-") else model_id
    return re.sub(r"-\d{8}$", "", short)


def _groups(results: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """Non-OK results grouped by (provider, status), most severe first,
    providers in roster order within a status."""
    providers = list(dict.fromkeys(r["provider"] for r in results))
    out = []
    for status in STATUS_ORDER:
        for provider in providers:
            hits = [r for r in results if r["provider"] == provider and r["status"] == status]
            if hits:
                out.append((provider, status, hits))
    return out


def _subject(level: str, groups: list[tuple[str, str, list[dict]]]) -> str:
    parts = [
        f"{provider} {status} ({', '.join(_short_model(r['model_id']) for r in hits)})"
        for provider, status, hits in groups
    ]
    subject = f"{level} provider-probe " + "; ".join(parts)
    subject = subject.encode("ascii", "replace").decode("ascii")
    # SNS rejects a Subject over 100 characters, which would turn a
    # detected failure into an unreported one.
    if len(subject) > 100:
        subject = subject[:97] + "..."
    return subject


def _body(results: list[dict], week: str, groups) -> str:
    failed = sum(1 for r in results if r["status"] != OK)
    lines = [
        f"Provider probe ahead of Monday's run ({week}, starts 09:00 UTC).",
        f"{failed} of {len(results)} targets are not OK.",
        "",
    ]
    for r in results:
        code = r["http_status"] if r["http_status"] is not None else "no response"
        when = "in Monday's run" if r["in_next_run"] else "not in Monday's run"
        lines.append(
            f"  {r['status']:<12} {r['provider']}/{r['model_id']} "
            f"({r['role']}, {when}) HTTP {code}"
        )
        if r["message"]:
            lines.append(f"               {r['message']}")
    lines += ["", "What to do:"]
    for provider, status, hits in groups:
        models = ", ".join(r["model_id"] for r in hits)
        lines.append(f"  {provider} {status} ({models}):")
        lines.append(f"    {_action(provider, status, hits)}")
    lines += [
        "",
        "Credit is account-wide, so a billing failure on a model that is",
        "not in Monday's run still stops the ones that are. The stance",
        "classifier runs every week whichever frontier models do.",
        "",
        "Re-check after fixing: scripts/ec2-runbook.md, 'Provider probe'.",
    ]
    return "\n".join(lines) + "\n"


def _alert(subject: str, message: str) -> None:
    """Publish, and raise if it did not land.

    Same contract as the canary: a probe that found a problem and could
    not say so must surface as a function error, so that
    meridian-provider-probe-errors pages through CloudWatch's own path to
    the topic instead of through the grant that just failed.
    """
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
    except Exception:
        _log.exception("provider probe could not publish to SNS; escalating via Errors")
        raise


def lambda_handler(event, context):  # noqa: ANN001, ARG001 - AWS signature
    now = dt.datetime.now(dt.timezone.utc)
    week, week_number = _next_run_week(now)

    keys: dict[str, tuple[str | None, str | None, str | None]] = {}
    results = []
    retired = []
    for target in TARGETS:
        provider = target["provider"]
        if _retired(target, week):
            _log.info(
                "provider-probe: %s/%s retired after %s; not probed for %s",
                provider, target["model_id"], target["last_week"], week,
            )
            retired.append(f"{provider}/{target['model_id']}")
            continue
        if provider not in keys:
            keys[provider] = _read_key(provider)
        key, key_status, key_message = keys[provider]
        if key is None:
            outcome = {
                "status": key_status,
                "http_status": None,
                "message": key_message,
                "attempts": 0,
                "key_read_failed": key_status == INCONCLUSIVE,
            }
        else:
            outcome = probe_target(target, key)
        result = {
            "provider": provider,
            "model_id": target["model_id"],
            "role": target.get("role", "runner"),
            "cadence": target.get("cadence", "every_week"),
            "in_next_run": _in_run(target, week, week_number),
            **outcome,
        }
        _log.info(
            "provider-probe: %s/%s %s http=%s attempts=%d %s",
            provider, result["model_id"], result["status"],
            result["http_status"], result["attempts"], result["message"],
        )
        results.append(result)

    groups = _groups(results)
    if not groups:
        _log.info("provider-probe: all %d targets OK for %s", len(results), week)
        return {
            "ok": True,
            "level": OK,
            "week": week,
            "published": False,
            "results": results,
            "retired": retired,
        }

    # A Sunday on which no target reached its provider (every key read
    # failed) has checked nothing, which is the gap this function exists
    # to close. That is a page even though no status alone would be.
    nothing_probed = all(r["attempts"] == 0 for r in results)
    page = nothing_probed or any(status in PAGE_STATUSES for _, status, _ in groups)
    level = "PAGE" if page else "WARN"
    subject = _subject(level, groups)
    _alert(subject, _body(results, week, groups))
    return {
        "ok": False,
        "level": level,
        "week": week,
        "published": True,
        "subject": subject,
        "results": results,
        "retired": retired,
    }
