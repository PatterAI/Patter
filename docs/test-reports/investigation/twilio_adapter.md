# Root Cause: `twilio_adapter` patch failures

**Last Updated:** 2026-04-11
**Investigator:** subagent `aa406f11ec052aa62`
**Cluster size:** 13 failures

## Verified failing tests

All fail at `with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:` with:

```
AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

1. `tests/test_local_mode.py::test_twilio_webhook_handler_url`
2. `tests/test_new_features.py::test_machine_detection_adds_params_to_twilio_call`
3. `tests/test_new_features.py::test_amd_callback_url_uses_webhook_host`
4. `tests/test_new_features.py::test_machine_detection_false_no_extra_params`
5. `tests/test_twilio_handler.py::test_twilio_webhook_generates_string`
6. `tests/test_twilio_handler.py::test_twilio_webhook_calls_generate_stream_twiml`
7. `tests/test_twilio_handler.py::test_twilio_webhook_returns_adapter_output`
8. `tests/test_twilio_handler.py::test_stream_url_is_wss`
9. `tests/test_twilio_handler.py::test_stream_url_contains_webhook_host`
10. `tests/test_twilio_handler.py::test_stream_url_has_ws_stream_path`
11. `tests/test_twilio_handler.py::test_stream_url_contains_caller_param`
12. `tests/test_twilio_handler.py::test_stream_url_contains_callee_param`
13. `tests/test_validation_guardrails.py::test_call_valid_e164_accepted`

## Root cause (confirmed)

The error message is misleading. `sdk/patter/providers/twilio_adapter.py` **exists** and defines `class TwilioAdapter`. The real failure is a **top-level import** inside that module:

```python
# sdk/patter/providers/twilio_adapter.py:3
from twilio.rest import Client as TwilioClient
```

The `twilio` pip package is not installed in the active Python 3.14.2 interpreter. When `unittest.mock.patch` resolves the dotted target it calls `importlib.import_module("patter.providers.twilio_adapter")`, which raises `ModuleNotFoundError: No module named 'twilio'`. The submodule is never bound as an attribute of `patter.providers`, and mock re-raises it as the confusing `AttributeError`. Python 3.14's "did you mean" hint suggests `telnyx_adapter` because — unlike `twilio_adapter` — `telnyx_adapter.py` only imports `httpx` (which is installed), so it loads cleanly and IS a bound attribute.

This is an **environment/install problem**, not a code bug. The `patter` SDK has never been `pip install -e`'d into the active interpreter — that's why `twilio` is absent even though `pyproject.toml` lists it as a required runtime dep (`twilio>=9.0.0`, line 39).

## Evidence

### Import test — `twilio_adapter`
```
$ cd /Users/francescorosciano/docs/patter/Patter/sdk && python3 -c "import patter.providers.twilio_adapter; print('OK')"
  File ".../sdk/patter/providers/twilio_adapter.py", line 3, in <module>
    from twilio.rest import Client as TwilioClient
ModuleNotFoundError: No module named 'twilio'
```

### Import test — `telnyx_adapter` (comparison)
```
$ python3 -c "import patter.providers.telnyx_adapter; print('OK')"
OK
```

### `sdk/patter/providers/__init__.py`
Innocent — no submodule imports, no try/except. Just re-exports a few helper functions from `patter.models`.

### `sdk/patter/providers/twilio_adapter.py:1-6`
```python
import asyncio
from functools import partial
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect
from patter.providers.base import TelephonyProvider
```

### Git history
```
$ git log --oneline -5 -- sdk/patter/providers/twilio_adapter.py
125ba47 Patter v0.3.0 — Voice AI SDK for Python & TypeScript
```
Single commit. No recent refactor.

### Environment
- `python3 --version` → `Python 3.14.2`
- `python3 -c "import telnyx"` → `ModuleNotFoundError` (confirming SDK extras were never installed here)
- CI matrix in `CLAUDE.md` covers 3.11/3.12/3.13 — **3.14 is not yet in CI**.

## Proposed minimal fix (not applied)

**Option 1 — Install fix (recommended, zero code change):**
```
cd /Users/francescorosciano/docs/patter/Patter/sdk && python3 -m pip install -e ".[local,dev]"
```
`twilio>=9.0.0` is declared as a required dep; tests are expected to run with it present.

**Option 2 — Make the telephony import lazy (long-term robustness):**
Wrap `sdk/patter/providers/twilio_adapter.py:3-4` in a `try/except ImportError` that sets `TwilioClient`, `VoiceResponse`, `Connect` to `None`, and raise a clear `RuntimeError` from `TwilioAdapter.__init__` if they're `None`. Move `twilio` from required to optional extra in `pyproject.toml`. Mirrors how `telnyx_adapter.py` already tolerates its underlying telephony dep being absent (it only uses `httpx`).

## Risk / side effects

- **Option 1**: pulls in `fastapi`, `uvicorn`, `twilio`, `openai`, `audioop-lts`. 3.14 is not in the official CI matrix — Twilio 9.x wheels may or may not have 3.14 wheels yet; worth spot-checking.
- **Option 2**: soft API break — users who `pip install patter` expecting Twilio to "just work" would need `patter[twilio]`. The 13 failing tests would still resolve `TwilioAdapter` symbolically and pass.
