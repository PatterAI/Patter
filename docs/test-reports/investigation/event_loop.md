# Root Cause: `asyncio.get_event_loop()` on Python 3.14

**Last Updated:** 2026-04-11
**Investigator:** subagent `a7337fbf650ca1e8a`
**Cluster size:** 10 failures

## Verified failing tests

All 10 are **sync** `def` functions that manually drive the event loop. Each fails with:

```
RuntimeError: There is no current event loop in thread 'MainThread'.
```

1. `tests/test_new_features.py::test_serve_passes_recording_to_server` (line 122)
2. `tests/test_new_features.py::test_serve_passes_voicemail_message_to_server` (line 399)
3. `tests/test_validation_guardrails.py::test_serve_validates_agent_type` (line 75)
4. `tests/test_validation_guardrails.py::test_serve_validates_port_type` (line 83)
5. `tests/test_validation_guardrails.py::test_serve_validates_port_range_low` (line 91)
6. `tests/test_validation_guardrails.py::test_serve_validates_port_range_high` (line 99)
7. `tests/test_validation_guardrails.py::test_serve_validates_recording_type` (line 107)
8. `tests/test_validation_guardrails.py::test_call_validates_e164_no_plus` (line 218)
9. `tests/test_validation_guardrails.py::test_call_validates_e164_empty` (line 228)
10. `tests/test_validation_guardrails.py::test_call_validates_e164_non_string` (line 238)

Note: `test_call_valid_e164_accepted` (line 250) uses the same `get_event_loop()` idiom on line 255, but its failure surfaces first as the `twilio_adapter` attribute error — it's tracked in the twilio cluster. If that fix is applied, line 255 will hit this same error and should be migrated together.

## Root cause (confirmed)

This is a **latent code bug exposed by a Python interpreter bump**, not an environment issue.

Every failing test is a sync function that does:
```python
asyncio.get_event_loop().run_until_complete(some_coroutine)
```

In Python ≤ 3.11, `asyncio.get_event_loop()` would implicitly create a new loop in the main thread when none was running. In Python 3.12 this started emitting a `DeprecationWarning`, and in **Python 3.14 the implicit creation was removed entirely** — `get_event_loop()` now raises `RuntimeError` when there is no running loop.

`pytest-asyncio` `asyncio_mode = "auto"` (set in `sdk/pyproject.toml`) **only wraps `async def` tests** in a managed loop. Sync tests get nothing. So these 10 sync tests, which were relying on the removed implicit-loop behavior, now blow up on Python 3.14.2.

## Evidence

### Representative failing test (`test_validation_guardrails.py:70-80`)
```python
def test_serve_validates_agent_type():
    phone = patter.Patter(api_key="test")
    with pytest.raises(TypeError, match="agent must be a Patter.Agent instance"):
        asyncio.get_event_loop().run_until_complete(phone.serve("not an agent"))
```

### Representative passing async test in same file
```python
@pytest.mark.asyncio
async def test_some_passing_case():
    phone = patter.Patter(api_key="test")
    ...
```
Passing tests use `async def` + the auto-mode wrapper. Sync + `get_event_loop()` is the failing pattern.

### `sdk/tests/conftest.py`
**0 bytes** (empty). No custom fixtures touching the event loop.

### Python 3.14 behavior
Documented in the Python 3.12 → 3.14 release notes and the `asyncio` API reference. `asyncio.get_event_loop()` no longer creates a new loop in the main thread; it raises `RuntimeError` when no loop is running. `asyncio.run(coro)` is the recommended replacement for one-shot loop drivers in sync code.

### Git history
```
$ git log --oneline -5 -- sdk/tests/test_validation_guardrails.py sdk/tests/test_new_features.py
e6af4b2 v0.3.1 ...
125ba47 Patter v0.3.0 — Voice AI SDK for Python & TypeScript
```
Tests were added in v0.3.0, last touched in v0.3.1. **Not recently written** — this is a latent bug exposed by running the suite on Python 3.14 for the first time.

## Proposed minimal fix (not applied)

Replace `asyncio.get_event_loop().run_until_complete(coro)` with `asyncio.run(coro)` at the 10 call sites (+1 adjacent site in `test_call_valid_e164_accepted` line 255 that is currently masked by the twilio cluster).

Example:
```diff
-    asyncio.get_event_loop().run_until_complete(phone.serve("not an agent"))
+    asyncio.run(phone.serve("not an agent"))
```

This is a **one-token textual change per line** — no signature changes, no decorators, no indentation, no new imports (both tests already `import asyncio`). `pytest.raises` semantics are preserved because `asyncio.run` propagates exceptions from the coroutine.

### Why not "convert to `async def` + `@pytest.mark.asyncio`"?
Also valid, but larger:
- Requires removing the `run_until_complete` wrapper AND adding the decorator AND changing `def` to `async def`.
- Affects indentation of the body under `with pytest.raises(...)` in a few places.
- `asyncio.run()` is the idiomatic replacement when the test deliberately wants to drive a fresh loop per test, which is exactly what these validation-guardrail tests want.

## Risk / side effects

- `asyncio.run()` creates and tears down a fresh event loop per call. For these tests that's already the expected behavior — each test drives one coroutine and exits. No shared-loop state across tests to worry about.
- No API surface change. Tests only.
- No interaction with pytest-asyncio auto mode (sync tests are still ignored by auto mode).
- Backwards compatible down to Python 3.7.

## Follow-ups worth considering

- Audit the rest of the Python SDK codebase (not just tests) for other `asyncio.get_event_loop()` call sites — they'll break under 3.14 too. Grep suggestion: `git grep -n "get_event_loop()" sdk/patter/`.
- Add Python 3.14 to the CI matrix once these 10 tests are migrated so this class of regression is caught upstream.
