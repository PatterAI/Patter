# Python Core — Test Report

**Run at:** 2026-04-12T03:23:14Z
**Working dir:** /Users/francescorosciano/docs/patter/Patter/sdk
**Command:** `python3 -m pytest tests/test_agent_model.py tests/test_api_routes.py tests/test_call_orchestrator.py tests/test_client.py tests/test_connection.py tests/test_cost_tracking.py tests/test_llm_loop.py tests/test_local_mode.py tests/test_metrics.py tests/test_models.py tests/test_new_agent_features.py tests/test_new_features.py tests/test_pricing.py tests/test_providers.py tests/test_providers_standalone.py tests/test_remote_message.py tests/test_server.py tests/test_session_manager.py tests/test_test_mode.py tests/test_tool_executor.py tests/test_twilio_handler.py tests/test_validation_guardrails.py -v --tb=short`
**Runtime:** 9.463s (wall clock; pytest self-reported 6.14s)

## Summary
- Total: 417
- Passed: 383
- Failed: 31
- Skipped: 3
- Errors: 0
- Exit code: 1

## Failures

### tests/test_local_mode.py::test_twilio_webhook_handler_url
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_local_mode.py:244: in test_twilio_webhook_handler_url
    with _patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
unittest/mock.py:1487: in __enter__
    self.target = self.getter()
pkgutil.py:473: in resolve_name
    result = getattr(result, p)
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_local_mode.py::test_mark_events_sent_after_audio
Reason: `AssertionError: Expected at least one mark event` (root cause: `audioop` missing — `patter[local]` extra not installed)
```
tests/test_local_mode.py:722: in test_mark_events_sent_after_audio
    assert len(mark_events) >= 1, f"Expected at least one mark event, got: {sent_events}"
E   AssertionError: Expected at least one mark event, got: []
E   assert 0 >= 1
ERROR patter:stream_handler.py:495 OpenAI Realtime forward error: audioop required: pip install patter[local]
  File "patter/handlers/twilio_handler.py", line 115, in send_audio
    resampled = self._resample_16k_to_8k(pcm_audio)
  File "patter/services/transcoding.py", line 35, in resample_16k_to_8k
    raise ImportError("audioop required: pip install patter[local]")
```

### tests/test_new_features.py::test_serve_passes_recording_to_server
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'` (Python 3.14 removed implicit loop creation in `asyncio.get_event_loop()`)
```
tests/test_new_features.py:122: in test_serve_passes_recording_to_server
    asyncio.get_event_loop().run_until_complete(
asyncio/events.py:715: in get_event_loop
    raise RuntimeError('There is no current event loop in thread %r.'
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_new_features.py::test_machine_detection_adds_params_to_twilio_call
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_new_features.py:183: in test_machine_detection_adds_params_to_twilio_call
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_new_features.py::test_amd_callback_url_uses_webhook_host
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_new_features.py:212: in test_amd_callback_url_uses_webhook_host
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_new_features.py::test_machine_detection_false_no_extra_params
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_new_features.py:257: in test_machine_detection_false_no_extra_params
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_new_features.py::test_serve_passes_voicemail_message_to_server
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'`
```
tests/test_new_features.py:399: in test_serve_passes_voicemail_message_to_server
    asyncio.get_event_loop().run_until_complete(
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_providers_standalone.py::test_mulaw_pcm16_roundtrip
Reason: `ImportError: audioop required: pip install patter[local]`
```
tests/test_providers_standalone.py:328: in test_mulaw_pcm16_roundtrip
    mulaw = pcm16_to_mulaw(samples)
patter/services/transcoding.py:18: in pcm16_to_mulaw
    raise ImportError("audioop required: pip install patter[local]")
E   ImportError: audioop required: pip install patter[local]
```

### tests/test_providers_standalone.py::test_pcm16_to_mulaw_output_is_bytes
Reason: `ImportError: audioop required: pip install patter[local]`
```
tests/test_providers_standalone.py:335: in test_pcm16_to_mulaw_output_is_bytes
    result = pcm16_to_mulaw(samples)
patter/services/transcoding.py:18: in pcm16_to_mulaw
    raise ImportError("audioop required: pip install patter[local]")
```

### tests/test_providers_standalone.py::test_mulaw_to_pcm16_output_is_bytes
Reason: `ImportError: audioop required: pip install patter[local]`
```
tests/test_providers_standalone.py:341: in test_mulaw_to_pcm16_output_is_bytes
    mulaw = pcm16_to_mulaw(samples)
patter/services/transcoding.py:18: in pcm16_to_mulaw
    raise ImportError("audioop required: pip install patter[local]")
```

### tests/test_providers_standalone.py::test_resample_8k_to_16k_empty
Reason: `ImportError: audioop required: pip install patter[local]`
```
tests/test_providers_standalone.py:347: in test_resample_8k_to_16k_empty
    assert resample_8k_to_16k(b"") == b""
patter/services/transcoding.py:25: in resample_8k_to_16k
    raise ImportError("audioop required: pip install patter[local]")
```

### tests/test_providers_standalone.py::test_resample_16k_to_8k_empty
Reason: `ImportError: audioop required: pip install patter[local]`
```
tests/test_providers_standalone.py:351: in test_resample_16k_to_8k_empty
    assert resample_16k_to_8k(b"") == b""
patter/services/transcoding.py:35: in resample_16k_to_8k
    raise ImportError("audioop required: pip install patter[local]")
```

### tests/test_providers_standalone.py::test_resample_8k_to_16k_doubles_length
Reason: `ImportError: audioop required: pip install patter[local]`
```
tests/test_providers_standalone.py:357: in test_resample_8k_to_16k_doubles_length
    result = resample_8k_to_16k(sample)
patter/services/transcoding.py:25: in resample_8k_to_16k
    raise ImportError("audioop required: pip install patter[local]")
```

### tests/test_providers_standalone.py::test_resample_16k_to_8k_halves_length
Reason: `ImportError: audioop required: pip install patter[local]`
```
tests/test_providers_standalone.py:365: in test_resample_16k_to_8k_halves_length
    result = resample_16k_to_8k(samples)
patter/services/transcoding.py:35: in resample_16k_to_8k
    raise ImportError("audioop required: pip install patter[local]")
```

### tests/test_twilio_handler.py::test_twilio_webhook_generates_string
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_twilio_handler.py:14: in test_twilio_webhook_generates_string
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_twilio_handler.py::test_twilio_webhook_calls_generate_stream_twiml
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_twilio_handler.py:24: in test_twilio_webhook_calls_generate_stream_twiml
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_twilio_handler.py::test_twilio_webhook_returns_adapter_output
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_twilio_handler.py:35: in test_twilio_webhook_returns_adapter_output
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_twilio_handler.py::test_stream_url_is_wss
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_twilio_handler.py:45: in test_stream_url_is_wss
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_twilio_handler.py::test_stream_url_contains_webhook_host
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_twilio_handler.py:56: in test_stream_url_contains_webhook_host
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_twilio_handler.py::test_stream_url_has_ws_stream_path
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_twilio_handler.py:67: in test_stream_url_has_ws_stream_path
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_twilio_handler.py::test_stream_url_contains_caller_param
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_twilio_handler.py:78: in test_stream_url_contains_caller_param
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_twilio_handler.py::test_stream_url_contains_callee_param
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_twilio_handler.py:89: in test_stream_url_contains_callee_param
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

### tests/test_validation_guardrails.py::test_serve_validates_agent_type
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'`
```
tests/test_validation_guardrails.py:75: in test_serve_validates_agent_type
    asyncio.get_event_loop().run_until_complete(phone.serve("not an agent"))
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_validation_guardrails.py::test_serve_validates_port_type
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'`
```
tests/test_validation_guardrails.py:83: in test_serve_validates_port_type
    asyncio.get_event_loop().run_until_complete(phone.serve(agent, port="8000"))
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_validation_guardrails.py::test_serve_validates_port_range_low
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'`
```
tests/test_validation_guardrails.py:91: in test_serve_validates_port_range_low
    asyncio.get_event_loop().run_until_complete(phone.serve(agent, port=0))
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_validation_guardrails.py::test_serve_validates_port_range_high
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'`
```
tests/test_validation_guardrails.py:99: in test_serve_validates_port_range_high
    asyncio.get_event_loop().run_until_complete(phone.serve(agent, port=99999))
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_validation_guardrails.py::test_serve_validates_recording_type
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'`
```
tests/test_validation_guardrails.py:107: in test_serve_validates_recording_type
    asyncio.get_event_loop().run_until_complete(phone.serve(agent, recording="yes"))
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_validation_guardrails.py::test_call_validates_e164_no_plus
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'`
```
tests/test_validation_guardrails.py:218: in test_call_validates_e164_no_plus
    asyncio.get_event_loop().run_until_complete(
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_validation_guardrails.py::test_call_validates_e164_empty
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'`
```
tests/test_validation_guardrails.py:228: in test_call_validates_e164_empty
    asyncio.get_event_loop().run_until_complete(
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_validation_guardrails.py::test_call_validates_e164_non_string
Reason: `RuntimeError: There is no current event loop in thread 'MainThread'`
```
tests/test_validation_guardrails.py:238: in test_call_validates_e164_non_string
    asyncio.get_event_loop().run_until_complete(
E   RuntimeError: There is no current event loop in thread 'MainThread'.
```

### tests/test_validation_guardrails.py::test_call_valid_e164_accepted
Reason: `AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'`
```
tests/test_validation_guardrails.py:250: in test_call_valid_e164_accepted
    with patch("patter.providers.twilio_adapter.TwilioAdapter") as MockAdapter:
E   AttributeError: module 'patter.providers' has no attribute 'twilio_adapter'. Did you mean: 'telnyx_adapter'?
```

## Full output (tail)
```
tests/test_api_routes.py: 12 warnings
tests/test_call_orchestrator.py: 13 warnings
tests/test_client.py: 8 warnings
tests/test_connection.py: 13 warnings
tests/test_llm_loop.py: 6 warnings
tests/test_local_mode.py: 9 warnings
tests/test_new_agent_features.py: 10 warnings
tests/test_new_features.py: 5 warnings
tests/test_providers_standalone.py: 2 warnings
tests/test_remote_message.py: 7 warnings
tests/test_server.py: 2 warnings
tests/test_session_manager.py: 3 warnings
tests/test_test_mode.py: 6 warnings
tests/test_validation_guardrails.py: 2 warnings
  /Library/Frameworks/Python.framework/Versions/3.14/lib/python3.14/site-packages/pytest_asyncio/plugin.py:978: DeprecationWarning: 'asyncio.get_event_loop_policy' is deprecated and slated for removal in Python 3.16
    policy = asyncio.get_event_loop_policy()

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/test_local_mode.py::test_twilio_webhook_handler_url - AttributeE...
FAILED tests/test_local_mode.py::test_mark_events_sent_after_audio - Assertio...
FAILED tests/test_new_features.py::test_serve_passes_recording_to_server - Ru...
FAILED tests/test_new_features.py::test_machine_detection_adds_params_to_twilio_call
FAILED tests/test_new_features.py::test_amd_callback_url_uses_webhook_host - ...
FAILED tests/test_new_features.py::test_machine_detection_false_no_extra_params
FAILED tests/test_new_features.py::test_serve_passes_voicemail_message_to_server
FAILED tests/test_providers_standalone.py::test_mulaw_pcm16_roundtrip - Impor...
FAILED tests/test_providers_standalone.py::test_pcm16_to_mulaw_output_is_bytes
FAILED tests/test_providers_standalone.py::test_mulaw_to_pcm16_output_is_bytes
FAILED tests/test_providers_standalone.py::test_resample_8k_to_16k_empty - Im...
FAILED tests/test_providers_standalone.py::test_resample_16k_to_8k_empty - Im...
FAILED tests/test_providers_standalone.py::test_resample_8k_to_16k_doubles_length
FAILED tests/test_providers_standalone.py::test_resample_16k_to_8k_halves_length
FAILED tests/test_twilio_handler.py::test_twilio_webhook_generates_string - A...
FAILED tests/test_twilio_handler.py::test_twilio_webhook_calls_generate_stream_twiml
FAILED tests/test_twilio_handler.py::test_twilio_webhook_returns_adapter_output
FAILED tests/test_twilio_handler.py::test_stream_url_is_wss - AttributeError:...
FAILED tests/test_twilio_handler.py::test_stream_url_contains_webhook_host - ...
FAILED tests/test_twilio_handler.py::test_stream_url_has_ws_stream_path - Att...
FAILED tests/test_twilio_handler.py::test_stream_url_contains_caller_param - ...
FAILED tests/test_twilio_handler.py::test_stream_url_contains_callee_param - ...
FAILED tests/test_validation_guardrails.py::test_serve_validates_agent_type
FAILED tests/test_validation_guardrails.py::test_serve_validates_port_type - ...
FAILED tests/test_validation_guardrails.py::test_serve_validates_port_range_low
FAILED tests/test_validation_guardrails.py::test_serve_validates_port_range_high
FAILED tests/test_validation_guardrails.py::test_serve_validates_recording_type
FAILED tests/test_validation_guardrails.py::test_call_validates_e164_no_plus
FAILED tests/test_validation_guardrails.py::test_call_validates_e164_empty - ...
FAILED tests/test_validation_guardrails.py::test_call_validates_e164_non_string
FAILED tests/test_validation_guardrails.py::test_call_valid_e164_accepted - A...
=========== 31 failed, 383 passed, 3 skipped, 883 warnings in 6.14s ============
```

## Notes
- Failures cluster into three root causes (not fixed per instructions):
  1. Missing module `patter.providers.twilio_adapter` — tests patch a path that no longer exists (18 failures). Sibling `telnyx_adapter` is present.
  2. Python 3.14 removed implicit event-loop creation; tests that call `asyncio.get_event_loop().run_until_complete(...)` now raise `RuntimeError: There is no current event loop` (11 failures).
  3. `audioop` stdlib module removed in Python 3.13+; `patter[local]` extra not installed, so `patter/services/transcoding.py` raises `ImportError` (7 failures, plus the `test_mark_events_sent_after_audio` assertion failure that is caused by the same import error surfacing at runtime).
- Environment: Python 3.14.2, pytest 8.4.2, pytest-asyncio 0.26.0 (mode=auto). Deprecation warnings from pytest-asyncio re: `get_event_loop_policy` are benign.
