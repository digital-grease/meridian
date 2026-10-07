"""Sunday provider probe: does it name a dead account before Monday does?

2026-W36 and 2026-W38 were lost to an empty Anthropic balance that
nothing reported until the run itself was refused. The probe exists to
say so a day early, so the behaviours pinned here are the ones that
decide whether that message is sent, what it calls the problem, and
whether it is safe to email:

  * each provider's billing shape is called BILLING, including the ones
    that arrive under statuses meaning something else (Anthropic's 400,
    OpenAI's 429);
  * a healthy Sunday sends nothing;
  * no key material reaches the email, the logs or the return value;
  * the subject fits SNS's 100 character limit;
  * the target list in Terraform cannot drift from meridian/config.yaml.

Like the other Lambdas, this one ships as a zip and is loaded by path.
"""
from __future__ import annotations

import datetime as dt
import email.message
import importlib.util
import io
import json
import logging
import re
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from meridian.config import load_config
from meridian.runners import anthropic as anthropic_runner
from meridian.runners import openai as openai_runner

REPO = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO / "infra/terraform/ec2-cohabit"
LAMBDA_SRC = MODULE_DIR / "lambda/provider_probe.py"

TOPIC = "arn:aws:sns:us-east-2:000000000000:meridian-pipeline-alerts"
ANTHROPIC_PARAM = "/meridian/anthropic-api-key"
OPENAI_PARAM = "/meridian/openai-api-key"

# Shaped like real keys so the shape-based redaction is exercised as
# well as the exact-value one.
ANTHROPIC_KEY = "sk-ant-api03-TESTSECRETabcdefghijklmnopqrstuvwxyz0123456789"
OPENAI_KEY = "sk-proj-TESTSECRETzyxwvutsrqponmlkjihgfedcba9876543210"

TARGETS = [
    {"provider": "anthropic", "model_id": "claude-opus-4-8", "role": "runner", "cadence": "even_weeks"},
    {"provider": "anthropic", "model_id": "claude-opus-5", "role": "runner", "cadence": "even_weeks"},
    {"provider": "openai", "model_id": "gpt-5.5", "role": "runner", "cadence": "odd_weeks"},
    {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001", "role": "stance", "cadence": "every_week"},
]


# ---------- harness ----------------------------------------------------


class _Ok:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok():
    return _Ok()


def http(code: int, body: dict | str | None = None, headers: dict | None = None):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    raw = body if isinstance(body, str) else json.dumps(body or {})
    return urllib.error.HTTPError(
        "https://example.invalid", code, "error", msg, io.BytesIO(raw.encode())
    )


def anthropic_error(code: int, err_type: str, message: str):
    return http(code, {"type": "error", "error": {"type": err_type, "message": message}})


def openai_error(code: int, message: str, err_type: str | None = None, err_code: str | None = None):
    return http(code, {"error": {"message": message, "type": err_type, "code": err_code}})


class FakeHttp:
    """Scripted urlopen. Responses are queued per model id, so a test
    says what each target answers without caring about probe order."""

    def __init__(self):
        self.script: dict[str, list] = {}
        self.calls: list[dict] = []

    def answer(self, model_id: str, *responses):
        self.script.setdefault(model_id, []).extend(responses)

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode())
        self.calls.append(
            {
                "url": req.full_url,
                "headers": {k.lower(): v for k, v in req.header_items()},
                "body": body,
                "timeout": timeout,
            }
        )
        queue = self.script.get(body["model"]) or [ok()]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def calls_for(self, model_id: str) -> list[dict]:
        return [c for c in self.calls if c["body"]["model"] == model_id]


def _env(monkeypatch, targets=TARGETS):
    monkeypatch.setenv("SNS_TOPIC_ARN", TOPIC)
    monkeypatch.setenv("ANTHROPIC_KEY_PARAM", ANTHROPIC_PARAM)
    monkeypatch.setenv("OPENAI_KEY_PARAM", OPENAI_PARAM)
    monkeypatch.setenv("PROBE_TARGETS", json.dumps(targets))
    monkeypatch.delenv("REQUEST_TIMEOUT_SECONDS", raising=False)


def _exec(name: str):
    spec = importlib.util.spec_from_file_location(name, LAMBDA_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def probe(monkeypatch):
    _env(monkeypatch)
    clients = {"ssm": MagicMock(name="ssm"), "sns": MagicMock(name="sns")}
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: clients[name])
    mod = _exec("_lambda_provider_probe")

    keys = {ANTHROPIC_PARAM: ANTHROPIC_KEY, OPENAI_PARAM: OPENAI_KEY}
    clients["ssm"].get_parameter.side_effect = lambda Name, WithDecryption: {
        "Parameter": {"Value": keys[Name]}
    }

    fake = FakeHttp()
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    mod.sleeps = []
    monkeypatch.setattr(mod, "_sleep", mod.sleeps.append)
    mod.http = fake
    return mod


def _status(result, model_id: str) -> str:
    return next(r["status"] for r in result["results"] if r["model_id"] == model_id)


def _published(mod) -> tuple[str, str]:
    mod.sns.publish.assert_called_once()
    kwargs = mod.sns.publish.call_args.kwargs
    assert kwargs["TopicArn"] == TOPIC
    return kwargs["Subject"], kwargs["Message"]


# ---------- the healthy Sunday ----------------------------------------


def test_all_ok_sends_nothing(probe):
    """A probe that mails every week gets filtered, and then the one
    week it matters it is read by nobody."""
    result = probe.lambda_handler({}, None)

    assert result["ok"] is True
    assert result["level"] == "OK"
    assert result["published"] is False
    assert [r["status"] for r in result["results"]] == ["OK"] * 4
    assert len(probe.http.calls) == 4
    probe.sns.publish.assert_not_called()


def test_keys_are_read_once_per_provider_with_decryption(probe):
    probe.lambda_handler({}, None)

    names = [c.kwargs["Name"] for c in probe.ssm.get_parameter.call_args_list]
    assert sorted(names) == [ANTHROPIC_PARAM, OPENAI_PARAM]
    assert all(c.kwargs["WithDecryption"] is True for c in probe.ssm.get_parameter.call_args_list)


# ---------- request shape ---------------------------------------------


def test_anthropic_request_is_minimal_and_omits_temperature(probe):
    probe.lambda_handler({}, None)

    call = probe.http.calls_for("claude-opus-5")[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == ANTHROPIC_KEY
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["body"]["max_tokens"] == 16
    # Opus 4.7 onward rejects any non-default temperature.
    assert "temperature" not in call["body"]
    assert call["timeout"] == 30


def test_openai_request_uses_completion_tokens_and_omits_temperature(probe):
    """gpt-5.5 rejects max_tokens and any non-default temperature; the
    probe must send what the runner sends or it reports a 400 that
    Monday would never see."""
    probe.lambda_handler({}, None)

    call = probe.http.calls_for("gpt-5.5")[0]
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["authorization"] == f"Bearer {OPENAI_KEY}"
    assert call["body"]["max_completion_tokens"] == 16
    assert "max_tokens" not in call["body"]
    assert "temperature" not in call["body"]


@pytest.mark.parametrize(
    "model_id",
    ["gpt-5.5", "gpt-6-astra", "gpt-5", "o3-mini", "gpt-4o", "gpt-4.1", "o4-mini"],
)
def test_token_kwarg_matches_the_runner(probe, model_id):
    req = probe._build_request("openai", model_id, "k")
    body = json.loads(req.data.decode())
    expected = openai_runner._token_kwarg_for(model_id)
    other = ({"max_tokens", "max_completion_tokens"} - {expected}).pop()
    assert body[expected] == 16
    assert other not in body


# ---------- classification --------------------------------------------


def test_anthropic_credit_balance_400_is_billing_and_pages(probe):
    """2026-W36 verbatim: a 400 invalid_request_error whose only tell is
    the sentence. Every Anthropic target shares the account, so all
    three fail together and the subject names them all."""
    for model in ("claude-opus-4-8", "claude-opus-5", "claude-haiku-4-5-20251001"):
        probe.http.answer(
            model,
            anthropic_error(
                400,
                "invalid_request_error",
                "Your credit balance is too low to access the Anthropic API. "
                "Please go to Plans & Billing to upgrade or purchase credits.",
            ),
        )

    result = probe.lambda_handler({}, None)

    assert result["level"] == "PAGE"
    assert _status(result, "claude-opus-4-8") == "BILLING"
    assert _status(result, "gpt-5.5") == "OK"
    subject, message = _published(probe)
    assert subject == "PAGE provider-probe anthropic BILLING (opus-4-8, opus-5, haiku-4-5)"
    assert "Top up Anthropic credit before Monday 09:00 UTC" in message
    assert "HTTP 400" in message
    # Billing is not retryable: one request per target.
    assert len(probe.http.calls_for("claude-opus-5")) == 1


@pytest.mark.parametrize(
    "error",
    [
        lambda: anthropic_error(402, "invalid_request_error", "payment required"),
        lambda: anthropic_error(400, "billing_error", "billing problem"),
        lambda: anthropic_error(403, "billing_error", "billing problem"),
    ],
    ids=["402", "billing_error-400", "billing_error-beats-403"],
)
def test_anthropic_other_billing_shapes(probe, error):
    probe.http.answer("claude-opus-5", error())

    result = probe.lambda_handler({}, None)

    assert _status(result, "claude-opus-5") == "BILLING"


def test_ordinary_anthropic_400_is_inconclusive_not_billing(probe):
    """Positive evidence only, as in the runner: a false BILLING page
    sends the operator to top up an account that is fine."""
    probe.http.answer(
        "claude-opus-5",
        anthropic_error(400, "invalid_request_error", "max_tokens: too small"),
    )

    result = probe.lambda_handler({}, None)

    assert _status(result, "claude-opus-5") == "INCONCLUSIVE"
    assert result["level"] == "WARN"
    subject, _ = _published(probe)
    assert subject == "WARN provider-probe anthropic INCONCLUSIVE (opus-5)"
    # A 400 is not transient; retrying it is noise.
    assert len(probe.http.calls_for("claude-opus-5")) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, "insufficient_quota"),
        (400, "billing_hard_limit_reached"),
        (400, "billing_not_active"),
    ],
)
def test_openai_billing_codes(probe, status, code):
    """insufficient_quota arrives as a 429. Treated as a rate limit it
    would be retried and then reported INCONCLUSIVE, which is the
    silent-week failure in a different costume."""
    probe.http.answer(
        "gpt-5.5", openai_error(status, "You exceeded your current quota.", code, code)
    )

    result = probe.lambda_handler({}, None)

    assert _status(result, "gpt-5.5") == "BILLING"
    assert len(probe.http.calls_for("gpt-5.5")) == 1
    subject, message = _published(probe)
    assert subject == "PAGE provider-probe openai BILLING (gpt-5.5)"
    assert "OpenAI credit" in message


def test_openai_billing_prose_without_code_is_not_billing(probe):
    """The runner trusts only the machine-readable code; so does this."""
    probe.http.answer(
        "gpt-5.5", openai_error(400, "insufficient_quota mentioned in prose", "invalid_request_error")
    )

    result = probe.lambda_handler({}, None)

    assert _status(result, "gpt-5.5") == "INCONCLUSIVE"


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize("model", ["claude-opus-4-8", "gpt-5.5"])
def test_auth_failures(probe, code, model):
    probe.http.answer(model, http(code, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}}))

    result = probe.lambda_handler({}, None)

    assert _status(result, model) == "AUTH"
    assert result["level"] == "PAGE"
    _, message = _published(probe)
    assert "aws ssm put-parameter" in message


@pytest.mark.parametrize(
    ("model", "error"),
    [
        ("claude-opus-4-8", lambda: anthropic_error(404, "not_found_error", "model: claude-opus-4-8")),
        ("gpt-5.5", lambda: openai_error(404, "The model `gpt-5.5` does not exist", "invalid_request_error", "model_not_found")),
        ("gpt-5.5", lambda: openai_error(400, "The model `gpt-5.5` does not exist", "invalid_request_error", "model_not_found")),
        # A project that has lost access to a model: a 403, but the key
        # is fine, so replacing it (AUTH's action) would be the wrong fix.
        ("gpt-5.5", lambda: openai_error(403, "Project `proj_abc` does not have access to model `gpt-5.5`", "invalid_request_error", "model_not_found")),
    ],
    ids=["anthropic-404", "openai-404", "openai-model_not_found-400", "openai-model_not_found-403"],
)
def test_model_gone(probe, model, error):
    probe.http.answer(model, error())

    result = probe.lambda_handler({}, None)

    assert _status(result, model) == "MODEL-GONE"
    _, message = _published(probe)
    assert "meridian/config.yaml" in message
    assert "aws ssm put-parameter" not in message


def test_page_subject_lists_page_groups_before_warn_groups(probe):
    probe.http.answer("gpt-5.5", http(503, "upstream down"), http(503, "upstream down"))
    probe.http.answer("claude-opus-5", anthropic_error(404, "not_found_error", "gone"))

    result = probe.lambda_handler({}, None)

    assert result["level"] == "PAGE"
    subject, _ = _published(probe)
    assert subject == (
        "PAGE provider-probe anthropic MODEL-GONE (opus-5); openai INCONCLUSIVE (gpt-5.5)"
    )


# ---------- retry ------------------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        lambda: http(529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
        lambda: http(500, "internal"),
        lambda: http(429, {"error": {"type": "rate_limit_error", "message": "slow down"}}),
        lambda: urllib.error.URLError("connection reset"),
        lambda: TimeoutError("timed out"),
    ],
    ids=["529", "500", "429", "urlerror", "timeout"],
)
def test_one_retry_recovers_a_transient_failure(probe, first):
    probe.http.answer("claude-opus-5", first(), ok())

    result = probe.lambda_handler({}, None)

    r = next(r for r in result["results"] if r["model_id"] == "claude-opus-5")
    assert r["status"] == "OK"
    assert r["attempts"] == 2
    assert len(probe.sleeps) == 1
    probe.sns.publish.assert_not_called()


def test_retry_is_bounded_to_one(probe):
    probe.http.answer("gpt-5.5", http(502, "bad gateway"))  # answers 502 forever

    result = probe.lambda_handler({}, None)

    assert _status(result, "gpt-5.5") == "INCONCLUSIVE"
    assert len(probe.http.calls_for("gpt-5.5")) == 2
    assert result["level"] == "WARN"


def test_network_failure_reports_no_http_status(probe):
    probe.http.answer("gpt-5.5", urllib.error.URLError("Name or service not known"))

    result = probe.lambda_handler({}, None)

    r = next(r for r in result["results"] if r["model_id"] == "gpt-5.5")
    assert r["status"] == "INCONCLUSIVE"
    assert r["http_status"] is None
    _, message = _published(probe)
    assert "no response" in message


def test_retry_after_is_honoured_but_capped(probe):
    probe.http.answer(
        "claude-opus-5", http(429, {"error": {"type": "rate_limit_error"}}, {"retry-after": "120"}), ok()
    )
    probe.lambda_handler({}, None)
    assert probe.sleeps == [probe.MAX_BACKOFF_SECONDS]


# ---------- secrets ----------------------------------------------------


def _assert_no_key(text: str) -> None:
    for key in (ANTHROPIC_KEY, OPENAI_KEY):
        assert key not in text
        # A tail is still key material; OpenAI echoes masked fragments.
        assert key[-12:] not in text
    assert "TESTSECRET" not in text


def test_no_key_material_in_email_logs_or_result(probe, caplog):
    """OpenAI echoes the rejected key in its 401 body. That body is
    exactly the text the alert quotes, so it is the realistic leak."""
    probe.http.answer(
        "gpt-5.5",
        openai_error(
            401,
            f"Incorrect API key provided: {OPENAI_KEY}. You can find your API key at ...",
            "invalid_request_error",
            "invalid_api_key",
        ),
    )
    probe.http.answer(
        "claude-opus-5",
        anthropic_error(400, "invalid_request_error", f"echo {ANTHROPIC_KEY} and sk-ant-oth3rKEYmaterial"),
    )

    with caplog.at_level(logging.DEBUG):
        result = probe.lambda_handler({}, None)

    subject, message = _published(probe)
    _assert_no_key(subject)
    _assert_no_key(message)
    _assert_no_key(caplog.text)
    _assert_no_key(json.dumps(result))
    assert "[REDACTED]" in message
    assert "sk-ant-oth3r" not in message


def test_sanitize_redacts_masked_keys_and_truncates(probe):
    out = probe._sanitize("key sk-proj-****abcd rejected " + "x" * 500)
    assert "sk-proj" not in out
    assert len(out) <= 200
    assert out.isascii()


# ---------- SSM --------------------------------------------------------


def test_missing_parameter_is_auth_without_a_request(probe):
    def get_parameter(Name, WithDecryption):
        if Name == OPENAI_PARAM:
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter")
        return {"Parameter": {"Value": ANTHROPIC_KEY}}

    probe.ssm.get_parameter.side_effect = get_parameter

    result = probe.lambda_handler({}, None)

    assert _status(result, "gpt-5.5") == "AUTH"
    assert probe.http.calls_for("gpt-5.5") == []
    subject, message = _published(probe)
    assert subject == "PAGE provider-probe openai AUTH (gpt-5.5)"
    assert OPENAI_PARAM in message


def test_ssm_access_denied_on_one_provider_is_a_warn_naming_the_grant(probe):
    """The probe's own IAM being wrong is not a dead credential, but the
    email must point at the grant rather than at the provider."""

    def get_parameter(Name, WithDecryption):
        if Name == OPENAI_PARAM:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetParameter")
        return {"Parameter": {"Value": ANTHROPIC_KEY}}

    probe.ssm.get_parameter.side_effect = get_parameter

    result = probe.lambda_handler({}, None)

    assert _status(result, "gpt-5.5") == "INCONCLUSIVE"
    assert _status(result, "claude-opus-4-8") == "OK"
    assert probe.http.calls_for("gpt-5.5") == []
    assert result["level"] == "WARN"
    _, message = _published(probe)
    assert f"could not read {OPENAI_PARAM}" in message
    assert "ssm:GetParameter" in message
    assert "Not a known failure shape" not in message


def test_ssm_access_denied_everywhere_pages(probe):
    """No key read means no target was checked: the Sunday the probe
    exists for passed unexamined, which is a page, not a warning."""
    probe.ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetParameter"
    )

    result = probe.lambda_handler({}, None)

    assert {r["status"] for r in result["results"]} == {"INCONCLUSIVE"}
    assert probe.http.calls == []
    assert result["level"] == "PAGE"
    subject, message = _published(probe)
    assert subject.startswith("PAGE provider-probe ")
    assert "meridian-provider-probe role" in message
    assert "Not a known failure shape" not in message


def test_provider_inconclusive_keeps_the_generic_action(probe):
    """The grant advice is only for key reads; a real request that came
    back strange still gets the re-run advice, and stays a WARN."""
    probe.http.answer("gpt-5.5", http(400, {"error": {"message": "odd", "type": "invalid_request_error"}}))

    result = probe.lambda_handler({}, None)

    assert result["level"] == "WARN"
    _, message = _published(probe)
    assert "Not a known failure shape" in message
    assert "ssm:GetParameter" not in message


# ---------- subject ----------------------------------------------------


def test_subject_fits_sns_limit_and_is_ascii(monkeypatch):
    _env(
        monkeypatch,
        targets=[
            {"provider": p, "model_id": f"{'claude-' if p == 'anthropic' else ''}model-with-a-long-name-{i:02d}",
             "role": "runner", "cadence": "every_week"}
            for i, p in enumerate(["anthropic", "openai"] * 6)
        ],
    )
    sns = MagicMock(name="sns")
    ssm = MagicMock(name="ssm")
    ssm.get_parameter.return_value = {"Parameter": {"Value": "sk-x"}}
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: {"sns": sns, "ssm": ssm}[name])
    mod = _exec("_lambda_provider_probe_long")
    fake = FakeHttp()
    for t in mod.TARGETS:
        fake.answer(t["model_id"], http(401, "nope"))
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)

    mod.lambda_handler({}, None)

    subject = sns.publish.call_args.kwargs["Subject"]
    assert 0 < len(subject) <= 100
    assert subject.isascii()
    assert subject.startswith("PAGE provider-probe anthropic AUTH (")


# ---------- escalation and config --------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "AuthorizationError", "Message": "denied"}}, "Publish"),
        EndpointConnectionError(endpoint_url="https://sns.us-east-2.amazonaws.com"),
    ],
)
def test_failed_publish_escalates(probe, error):
    """Same contract as the canary: a finding that could not be sent
    must become an Errors datapoint, not a log line."""
    probe.http.answer("gpt-5.5", http(401, "nope"))
    probe.sns.publish.side_effect = error

    with pytest.raises(type(error)):
        probe.lambda_handler({}, None)


@pytest.mark.parametrize(
    "raw", ["", "[]", "{}", json.dumps([{"provider": "google", "model_id": "gemini"}]),
            json.dumps([{"provider": "openai"}])],
)
def test_bad_targets_fail_at_import(monkeypatch, raw):
    _env(monkeypatch)
    monkeypatch.setenv("PROBE_TARGETS", raw)
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: MagicMock())
    with pytest.raises(ValueError):
        _exec("_lambda_provider_probe_bad")


def test_missing_required_env_fails_at_import(monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv("PROBE_TARGETS")
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: MagicMock())
    with pytest.raises(KeyError):
        _exec("_lambda_provider_probe_noenv")


# ---------- schedule awareness -----------------------------------------


@pytest.mark.parametrize(
    ("now", "week"),
    [
        (dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc), "2026-W40"),  # scheduled Sunday
        (dt.datetime(2026, 10, 1, 9, 0, tzinfo=dt.timezone.utc), "2026-W40"),  # by hand, Thursday
        (dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc), "2026-W41"),  # Monday, after the run
        (dt.datetime(2026, 12, 27, 12, 0, tzinfo=dt.timezone.utc), "2026-W52"),
        (dt.datetime(2027, 1, 3, 12, 0, tzinfo=dt.timezone.utc), "2026-W53"),  # ISO year boundary
    ],
)
def test_next_run_week_matches_run_weekly_labelling(probe, now, week):
    """run-weekly.sh labels Monday's run `date -u --date=yesterday +%G-W%V`."""
    assert probe._next_run_week(now)[0] == week


def test_body_says_which_models_are_in_mondays_run(probe):
    probe.http.answer("gpt-5.5", http(401, "nope"))
    probe.http.answer("claude-opus-5", http(401, "nope"))
    probe._next_run_week = lambda now: ("2026-W40", 40)

    result = probe.lambda_handler({}, None)

    by_model = {r["model_id"]: r for r in result["results"]}
    assert by_model["claude-opus-5"]["in_next_run"] is True
    assert by_model["gpt-5.5"]["in_next_run"] is False
    assert by_model["claude-haiku-4-5-20251001"]["in_next_run"] is True
    _, message = _published(probe)
    assert "anthropic/claude-opus-5 (runner, in Monday's run)" in message
    assert "openai/gpt-5.5 (runner, not in Monday's run)" in message


# ---------- parity with the pipeline -----------------------------------


def test_billing_rules_mirror_the_runners(probe):
    """The Lambda copies these because it cannot import meridian. If a
    runner learns a new billing shape, the probe must learn it too."""
    assert probe._ANTHROPIC_CREDIT_MARKER == anthropic_runner._CREDIT_BALANCE_MARKER
    assert probe._OPENAI_BILLING_CODES == openai_runner._BILLING_CODES


_WEEK_OR_NULL = r'(?:"([^"]+)"|null)'
_TARGET_RE = re.compile(
    r'\{\s*provider\s*=\s*"([^"]+)"\s*,\s*model_id\s*=\s*"([^"]+)"\s*,'
    r'\s*role\s*=\s*"([^"]+)"\s*,\s*cadence\s*=\s*"([^"]+)"\s*,'
    r'\s*first_week\s*=\s*' + _WEEK_OR_NULL + r'\s*,'
    r'\s*last_week\s*=\s*' + _WEEK_OR_NULL + r'\s*\}'
)


def _terraform_default_targets() -> list[tuple]:
    text = (MODULE_DIR / "variables.tf").read_text()
    block = text.split('variable "provider_probe_targets"', 1)[1]
    default = block.split("default = [", 1)[1].split("\n  ]", 1)[0]
    # Every entry states both bounds, null when unset, so the parse is
    # strict and an entry that omits one fails loudly below.
    found = [
        tuple(None if v == "" else v for v in m)
        for m in _TARGET_RE.findall(default)
    ]
    # Guard against a vacuous pass: every entry in the default must have
    # been understood, or a reformatted entry would silently drop out.
    assert len(found) == default.count("model_id"), "unparsed provider_probe_targets entry"
    assert found, "provider_probe_targets default not found"
    return found


def _config_targets() -> list[tuple]:
    cfg = load_config()
    out = [
        (r.provider, r.model_id, "runner", r.cadence, r.first_week, r.last_week)
        for r in cfg.runners
        # Every provider that needs a paid key. Ollama is local and free.
        # Retired runners stay listed with their last_week, in both.
        if r.enabled and r.provider != "ollama"
    ]
    if cfg.stance.enabled:
        out.append(
            (cfg.stance.provider, cfg.stance.model_id, "stance", "every_week", None, None)
        )
    return out


def test_terraform_targets_match_pipeline_config():
    """The roster cannot drift silently.

    Adding a model to config.yaml without adding it here leaves it
    unprobed, which is the exact gap that lost two weeks. Removing one
    without removing it here pages MODEL-GONE forever or, worse, gets
    the alert muted.
    """
    tf = _terraform_default_targets()
    cfg = _config_targets()
    key = lambda t: tuple("" if v is None else v for v in t)  # noqa: E731
    assert sorted(tf, key=key) == sorted(cfg, key=key)
    assert len(tf) == len(set(tf)), "duplicate probe target"


# ---------- roster succession (first_week / last_week) -----------------

SUCCESSION_TARGETS = [
    {"provider": "anthropic", "model_id": "claude-opus-4-8", "role": "runner",
     "cadence": "even_weeks", "first_week": None, "last_week": "2026-W42"},
    {"provider": "anthropic", "model_id": "claude-opus-5", "role": "runner",
     "cadence": "even_weeks", "first_week": None, "last_week": "2026-W42"},
    {"provider": "anthropic", "model_id": "claude-opus-5-5", "role": "runner",
     "cadence": "even_weeks", "first_week": "2026-W42", "last_week": None},
    {"provider": "openai", "model_id": "gpt-5.5", "role": "runner",
     "cadence": "odd_weeks", "first_week": None, "last_week": "2026-W41"},
    {"provider": "openai", "model_id": "gpt-6-astra", "role": "runner",
     "cadence": "odd_weeks", "first_week": "2026-W41", "last_week": None},
    {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001", "role": "stance",
     "cadence": "every_week", "first_week": None, "last_week": None},
]


@pytest.fixture
def succession_probe(monkeypatch):
    """The probe loaded with the 2026-10 succession roster, shaped as
    Terraform's jsonencode emits it (unset optional bounds are null)."""
    _env(monkeypatch, targets=SUCCESSION_TARGETS)
    clients = {"ssm": MagicMock(name="ssm"), "sns": MagicMock(name="sns")}
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: clients[name])
    mod = _exec("_lambda_provider_probe_succession")
    keys = {ANTHROPIC_PARAM: ANTHROPIC_KEY, OPENAI_PARAM: OPENAI_KEY}
    clients["ssm"].get_parameter.side_effect = lambda Name, WithDecryption: {
        "Parameter": {"Value": keys[Name]}
    }
    fake = FakeHttp()
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    monkeypatch.setattr(mod, "_sleep", lambda s: None)
    mod.http = fake
    return mod


def _probed(mod) -> set[str]:
    return {c["body"]["model"] for c in mod.http.calls}


def test_sunday_before_w41_probes_everything_and_marks_the_overlap(succession_probe):
    """Sunday 2026-10-11: gpt-5.5's last week and gpt-6-astra's first.
    claude-opus-5-5 starts a week later but is probed now, so its access
    is proven before its first Monday."""
    succession_probe._next_run_week = lambda now: ("2026-W41", 41)
    result = succession_probe.lambda_handler({}, None)

    assert _probed(succession_probe) == {t["model_id"] for t in SUCCESSION_TARGETS}
    assert result["retired"] == []
    in_run = {r["model_id"]: r["in_next_run"] for r in result["results"]}
    assert in_run == {
        "claude-opus-4-8": False,
        "claude-opus-5": False,
        "claude-opus-5-5": False,
        "gpt-5.5": True,
        "gpt-6-astra": True,
        "claude-haiku-4-5-20251001": True,
    }


def test_sunday_before_w42_runs_three_opus_models(succession_probe):
    succession_probe._next_run_week = lambda now: ("2026-W42", 42)
    result = succession_probe.lambda_handler({}, None)

    in_run = {r["model_id"]: r["in_next_run"] for r in result["results"]}
    assert in_run["claude-opus-4-8"] is True
    assert in_run["claude-opus-5"] is True
    assert in_run["claude-opus-5-5"] is True
    assert in_run["gpt-6-astra"] is False
    # gpt-5.5 ran its last week in 2026-W41.
    assert "gpt-5.5" not in in_run
    assert result["retired"] == ["openai/gpt-5.5"]


def test_retired_models_are_not_probed_and_never_page(succession_probe):
    """After 2026-W42 the three retired models are off the roster on
    purpose. Even if their ids now 404, Sunday must stay quiet."""
    succession_probe._next_run_week = lambda now: ("2026-W43", 43)
    for gone in ("claude-opus-4-8", "claude-opus-5", "gpt-5.5"):
        succession_probe.http.answer(gone, http(404, {"error": {"type": "not_found_error"}}))

    result = succession_probe.lambda_handler({}, None)

    assert _probed(succession_probe) == {
        "claude-opus-5-5", "gpt-6-astra", "claude-haiku-4-5-20251001",
    }
    assert result["level"] == "OK"
    assert result["published"] is False
    succession_probe.sns.publish.assert_not_called()
    assert sorted(result["retired"]) == [
        "anthropic/claude-opus-4-8", "anthropic/claude-opus-5", "openai/gpt-5.5",
    ]
    in_run = {r["model_id"]: r["in_next_run"] for r in result["results"]}
    assert in_run == {
        "claude-opus-5-5": False,
        "gpt-6-astra": True,
        "claude-haiku-4-5-20251001": True,
    }


def test_new_models_requests_are_minimal(succession_probe):
    succession_probe._next_run_week = lambda now: ("2026-W41", 41)
    succession_probe.lambda_handler({}, None)

    opus = succession_probe.http.calls_for("claude-opus-5-5")[0]["body"]
    # No thinking (cannot be disabled on this model), no sampling
    # params, and never a fallback model.
    assert set(opus) == {"model", "max_tokens", "messages"}
    astra = succession_probe.http.calls_for("gpt-6-astra")[0]["body"]
    assert set(astra) == {"model", "messages", "max_completion_tokens"}


@pytest.mark.parametrize(
    "bad",
    [
        {"first_week": "2026-W4"},
        {"last_week": "2026-41"},
        {"last_week": "2026-W54"},
        {"first_week": 41},
        {"first_week": "2026-W43", "last_week": "2026-W42"},
    ],
)
def test_bad_week_bounds_fail_at_import(monkeypatch, bad):
    target = {"provider": "openai", "model_id": "gpt-6-astra", "role": "runner",
              "cadence": "odd_weeks", **bad}
    _env(monkeypatch, targets=[target])
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: MagicMock())
    with pytest.raises(ValueError):
        _exec("_lambda_provider_probe_badweek")


def test_bounds_without_bound_keys_still_load(monkeypatch):
    """Targets written before the bounds existed carry no bound keys."""
    _env(monkeypatch)
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: MagicMock())
    mod = _exec("_lambda_provider_probe_nobounds")
    assert not mod._retired(TARGETS[0], "2099-W01")


def test_terraform_passes_the_variable_to_the_function():
    tf = (MODULE_DIR / "provider_probe.tf").read_text()
    assert "PROBE_TARGETS       = jsonencode(var.provider_probe_targets)" in tf
    assert "aws_ssm_parameter.anthropic_api_key.name" in tf
    assert "aws_ssm_parameter.openai_api_key.name" in tf
    sched = (MODULE_DIR / "scheduler.tf").read_text()
    # A schedule whose function is missing from the invoke policy fires
    # on time and is denied, silently.
    assert "aws_lambda_function.provider_probe.arn" in sched
