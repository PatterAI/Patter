# Python Core — Test Report

**Run at:** 2026-04-12T03:45:53Z (post-fix)
**Working dir:** /Users/francescorosciano/docs/patter/Patter/sdk
**Command:** `python3 -m pytest tests/test_agent_model.py tests/test_api_routes.py tests/test_call_orchestrator.py tests/test_client.py tests/test_connection.py tests/test_cost_tracking.py tests/test_llm_loop.py tests/test_local_mode.py tests/test_metrics.py tests/test_models.py tests/test_new_agent_features.py tests/test_new_features.py tests/test_pricing.py tests/test_providers.py tests/test_providers_standalone.py tests/test_remote_message.py tests/test_server.py tests/test_session_manager.py tests/test_test_mode.py tests/test_tool_executor.py tests/test_twilio_handler.py tests/test_validation_guardrails.py --tb=line -q`
**Runtime:** 4.44s

## Summary
- Total: 417
- Passed: 417
- Failed: 0
- Skipped: 0
- Errors: 0
- Exit code: 0

## Failures
None.

## Delta from the previous run (2026-04-12T03:23:14Z)

| Metric | Before | After |
|---|---|---|
| Passed | 383 | **417** |
| Failed | **31** | **0** |
| Skipped | 3 | 0 |
| Exit code | 1 | **0** |

The 34-test improvement (+31 previously-failing + 3 previously-skipped tests that had conditional skip markers on missing deps) came from two orthogonal fixes:

1. **Environment fix** — `python3 -m pip install -e "sdk[local,dev]"` installed `twilio 9.10.4`, `audioop-lts 0.2.2`, `fastapi 0.128.0`, `uvicorn 0.40.0`, `openai 2.30.0`. This unblocked the 13 `twilio_adapter` `mock.patch` failures (the hard top-level `from twilio.rest import Client` was failing because the pip package was absent) and the 8 `audioop` `ImportError` failures (`audioop-lts` registers as the top-level `audioop` module on Python 3.13+, so `transcoding.py`'s `try: import audioop` branch succeeds once the package is installed).

2. **Code fix** — replaced `asyncio.get_event_loop().run_until_complete(...)` with `asyncio.run(...)` at **14 call sites** across `sdk/tests/test_new_features.py` (5 sites: lines 122, 189, 218, 263, 399) and `sdk/tests/test_validation_guardrails.py` (9 sites: lines 75, 83, 91, 99, 107, 218, 228, 238, 255). Python 3.14 removed the implicit main-thread event-loop creation from `get_event_loop()`, so any sync test that drove a coroutine with the old idiom now raises `RuntimeError`. `asyncio.run()` is the drop-in replacement — both files already imported `asyncio`, `pytest.raises` semantics are preserved, and the audit of `sdk/patter/` production code turned up zero additional call sites to worry about.

The original report audit over-counted cleared failures as 10 for the event-loop cluster; the true count is 14 because 3 sites in `test_new_features.py` (lines 189, 218, 263) were previously masked by twilio_adapter `mock.patch` errors and would have surfaced the same crash once the install fix landed.

## Full output (tail)
```
... 883 deprecation warnings (all from pytest-asyncio 0.26.0 using asyncio.get_event_loop_policy,
    slated for removal in Python 3.16; benign, upstream fix tracked in pytest-asyncio) ...

417 passed, 883 warnings in 4.44s
```

## Notes
- `pytest-asyncio` 0.26.0 itself emits 883 deprecation warnings on Python 3.14 because it still uses `asyncio.{get,set}_event_loop_policy`. This is upstream, benign, and will be fixed by upgrading pytest-asyncio once a 3.14-clean release ships.
- `CLAUDE.md`'s CI matrix still lists only 3.11/3.12/3.13. Adding 3.14 to CI once the pytest-asyncio warnings are resolved upstream would prevent regressions like this one from sliding past review again.
- Investigation notes for each failure cluster are preserved at `docs/test-reports/investigation/{twilio_adapter,audioop,event_loop}.md` for historical reference.
