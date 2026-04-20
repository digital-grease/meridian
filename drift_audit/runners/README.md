# runners/ — LLM adapters

Each file in this directory is one provider adapter. Adapters share a
single contract (`Runner` in `base.py`) so the orchestrator never needs to
know which provider it is talking to.

## Adding a new provider

A new provider is a new file, a new class, and a new entry in
`drift_audit/config.py::build_runners`. Nothing else changes.

Concretely, a new runner must:

1. **Subclass `Runner`** (from `drift_audit.runners.base`).
2. **Set `provider`** (a stable string identifier like `"anthropic"` or
   `"ollama"`) and **set `model_id`** in `__init__` (the stable slug we
   carry through storage and the manifest).
3. **Implement `async def sample(...)`**: return one `Sample` or raise a
   `RunnerError` subclass.
   - Wrap the call in `with_retry()` from `drift_audit.runners._retry` so
     transient upstream failures don't abort the week.
   - Translate SDK exceptions into the right `RunnerError` subclass:
     - `AuthError` for missing/invalid credentials (not retryable).
     - `RateLimitError(msg, retry_after_s=...)` for 429s.
     - `UpstreamError` for everything else.
   - Populate the `Sample` with **as much provider metadata as the SDK
     exposes**. At minimum: `model_version_string`, input/output token
     counts, stop/finish reason, request id, latency.

4. **Do NOT override `async def batch(...)`**: the default bounded-
   concurrency implementation in `base.py` is the correct behavior for
   every known provider.

5. **Register in config**: add your provider to the `Provider` literal in
   `drift_audit/config.py` and extend `build_runners()` with the mapping
   from `RunnerSpec` to your class.

## Tests for a new runner

Two flavors, minimum:

- **Constructor test** (`test_runner_wiring.py` pattern): instantiate with
  a dummy API key and assert `provider` / `model_id` / `client` are set.

- **HTTP-level test with `respx`** (see `test_runner_ollama.py`): mock the
  endpoint, call `sample()` with a fake prompt, assert the returned
  `Sample` has the fields you expect. Include at least one error path
  (5xx or rate-limit) to verify the retry wrapper is wired in.

HTTP-level coverage is possible even when a provider ships its own SDK,
as long as that SDK uses `httpx` under the hood (both Anthropic and
OpenAI do). Where the SDK hides the transport, fall back to constructor
tests and trust the shared retry layer.

## Testing locally against a real provider

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run python -c "
import asyncio
from drift_audit.runners.anthropic import AnthropicRunner

async def main():
    r = AnthropicRunner('claude-haiku-4-5-20251001')
    s = await r.sample('Say hi.', prompt_id='debug', request_index=0, temperature=0.5)
    print(s.text[:200])
    print('tokens:', s.input_tokens, '/', s.output_tokens)

asyncio.run(main())
"
```

## Hard rules

- **Append-only storage**: runners must not modify or rewrite prior
  samples. The storage layer enforces this, but a runner that re-uses
  a `request_index` from an earlier run will produce confusing data —
  always derive indices from `start_index` argument to `batch()`.
- **No secrets in the repo**: API keys come from environment variables.
  Config files reference providers by name, never key.
- **Faithful metadata**: do not paper over SDK quirks. If a provider
  reports an unexpected stop reason, surface it verbatim in
  `Sample.stop_reason`; downstream analysis normalizes.
