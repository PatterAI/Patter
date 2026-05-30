# Authentic Tests Rule

Tests must verify that the code works in real scenarios. A green test suite that passes only because everything is faked is worse than no tests — it lies about the state of the system.

## The hard rule

**A test is authentic if, when the real dependency is swapped in, the test still passes without code changes.**

If that's not true, the test isn't verifying behaviour — it's verifying that the mock behaves like the mock.

## What IS allowed (in order of preference)

1. **Real code paths end-to-end** — use the actual functions, real audio transcoding, real WebSocket framing, real Ed25519 signature verification with a test keypair, real E.164 validation, real `CallMetricsAccumulator` accumulating real numbers.
2. **Real local services** — spin up a real in-process FastAPI/Express server, a real `websockets.serve()` test server, a real in-memory SQLite DB. These are NOT mocks — they're the same code that runs in production.
3. **Real provider SDK objects constructed from real schemas** — if OpenAI's client parses a response, use the real client with a captured real JSON body from a prior real call.
4. **Captured real fixtures** — a real-call recording committed to `tests/fixtures/` is real data. Prefer it over hand-written JSON.

## What is NOT allowed

- ❌ Fake functions that return the answer the test wants (e.g. `def transcode(x): return b"OK"` bypassing real mulaw→PCM).
- ❌ Fake results ("make the assertion match whatever the function returned").
- ❌ Fake conversions (stubbing `mulaw_to_pcm` to an identity function).
- ❌ Mocking the thing under test. If the test is about `StreamHandler`, `StreamHandler` is real.
- ❌ Patching your own module's internals to make a brittle test pass.
- ❌ Tests that assert `mock.called` but never exercise the real behaviour.

## What mocking IS permitted — and how to declare it

Mocks are allowed ONLY at the outer boundary, and ONLY when the real thing is:
- A paid external API (OpenAI Realtime WebSocket, ElevenLabs TTS stream)
- A real phone carrier (Twilio / Telnyx — you cannot place calls in CI)
- Non-deterministic external state (current time, random seeds, network)

Everything from the mock boundary INWARD must be real code.

### Examples of the ONLY acceptable mock surfaces

| Mock this (the last hop) | Keep real (everything else) |
|--------------------------|-----------------------------|
| OpenAI Realtime WebSocket server endpoint | Frame parsing, session state, tool dispatch, metrics |
| Twilio webhook HTTP endpoint | Signature verification, TwiML emission, audio transcoding |
| Telnyx Call Control POST | Ed25519 verify, event routing, hang-up logic |
| `time.time()` (for deterministic timestamps) | Cost calculation, TTL expiry logic |
| ElevenLabs TTS stream bytes | Chunking, barge-in logic, audio pacing |

## Required: mark mocked tests in a separate category

Every test file must classify itself:

### Python (`pytest`)
Use pytest markers already configured in `sdk/pyproject.toml`:

```python
import pytest

@pytest.mark.unit           # Real code, no external boundary mocked
def test_e164_validation(): ...

@pytest.mark.integration    # Real services locally (FastAPI server, websockets.serve)
def test_twilio_webhook_end_to_end(): ...

@pytest.mark.mocked         # MUST add this marker when mocking an external boundary
def test_openai_realtime_tool_dispatch_with_mock_ws(): ...
```

Add `mocked` to `markers` in `sdk/pyproject.toml` if not present. CI must run `unit` and `integration` by default; `mocked` runs as a separate stage.

### TypeScript (`vitest`)
Use the filename convention + a leading describe tag:

```ts
// sdk-ts/tests/stream-handler.test.ts
describe("[unit] StreamHandler", () => { ... });

// sdk-ts/tests/webhook.integration.test.ts
describe("[integration] Twilio webhook", () => { ... });

// sdk-ts/tests/realtime.mocked.test.ts
describe("[mocked] OpenAI Realtime — mock WS", () => { ... });
```

File suffix: `*.test.ts` (unit), `*.integration.test.ts`, `*.mocked.test.ts`. `vitest.config.ts` should be configured to report these buckets separately.

## End-to-end tests — must be real use cases

E2E tests simulate real customer flows. They may mock ONLY the provider WebSocket and the carrier, but everything else is real:

- Inbound call on Twilio → Pipeline (Deepgram STT → OpenAI → ElevenLabs TTS) → tool calls `transfer_call` → metrics land in dashboard
- Outbound call on Telnyx → OpenAI Realtime → AMD detects voicemail → `voicemail_message` played → call ends → cost computed

Every E2E must match a real use case documented in `examples/` or `docs/guides/`. If the E2E doesn't correspond to a real user scenario, delete it.

## Fixtures from real calls

- Put captured real provider payloads in `sdk/tests/fixtures/` or `sdk-ts/tests/fixtures/` — JSON/binary files only, one per scenario.
- Redact PII before committing: phone numbers → `+15555550100`, transcripts anonymised.
- Reference them in tests; never inline 200 lines of fake JSON in a test file.

## Test naming

Name tests after the behaviour, not the function:

- ❌ `test_transcode()`
- ✅ `test_mulaw_to_pcm_preserves_samples_at_8khz()`
- ❌ `test_handler()`
- ✅ `test_twilio_handler_closes_stream_on_hangup_event()`

If you can't describe the behaviour in the name, you haven't tested a behaviour.

## Verification

When reviewing a test, ask:
1. If I delete the implementation and replace with `raise NotImplementedError`, does the test fail? (If no → test is fake.)
2. Does the test import and exercise the real module under test? (If no → test is testing a mock.)
3. Are all mocks declared via the `mocked` marker/tag? (If no → fix it.)
4. Does the assertion check an observable outcome (output bytes, logged metric, HTTP response) — not `mock.called`? (If no → rewrite.)

If any answer is wrong, the test is not authentic. Fix it before landing.
