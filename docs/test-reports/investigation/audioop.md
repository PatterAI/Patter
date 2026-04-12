# Root Cause: `audioop` ImportError failures

**Last Updated:** 2026-04-11
**Investigator:** subagent `a40e3f35d649c1c9b`
**Cluster size:** 8 failures

## Verified failing tests

All 8 trace back to `transcoding.py` raising `ImportError("audioop required: pip install patter[local]")`:

1. `tests/test_providers_standalone.py::test_mulaw_pcm16_roundtrip`
2. `tests/test_providers_standalone.py::test_pcm16_to_mulaw_output_is_bytes`
3. `tests/test_providers_standalone.py::test_mulaw_to_pcm16_output_is_bytes`
4. `tests/test_providers_standalone.py::test_resample_8k_to_16k_empty`
5. `tests/test_providers_standalone.py::test_resample_16k_to_8k_empty`
6. `tests/test_providers_standalone.py::test_resample_8k_to_16k_doubles_length`
7. `tests/test_providers_standalone.py::test_resample_16k_to_8k_halves_length`
8. `tests/test_local_mode.py::test_mark_events_sent_after_audio` — surfaces at runtime inside `twilio_handler.send_audio` → `pcm16_to_mulaw`

## Environment

- Python: **3.14.2**
- stdlib `audioop`: **absent** (removed in Python 3.13 per PEP 594)
- `audioop-lts`: **absent** — `python3 -c "import audioop_lts"` → `ModuleNotFoundError`
- `patter` SDK: **not installed** in the active interpreter (no `pip install -e .` has run here)

## Root cause (confirmed)

`patter[local]` extra in `sdk/pyproject.toml` correctly declares `audioop-lts>=0.2.1; python_version>='3.13'`. The guard code in `transcoding.py` is also correct — it tries stdlib `audioop`, then `audioop_lts`, then raises a helpful ImportError. **The failures are an installation gap, not a code bug.** Since neither the base package nor the `[local]` extra was installed into this Python 3.14.2 interpreter, both import branches fail and the fallback error is raised at call time.

A secondary issue: `test_providers_standalone.py` has **no** `importorskip("audioop")` guard for these 7 tests. It *does* use `pytest.importorskip("twilio", ...)` elsewhere but skipped audioop — so on any environment without `patter[local]`, the transcoding tests hard-fail instead of gracefully skipping.

## Evidence

### `sdk/pyproject.toml` → `[project.optional-dependencies]`
```toml
local = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "twilio>=9.0.0",
    "audioop-lts>=0.2.1; python_version>='3.13'",
    "openai>=1.0.0",
]
```
Note: `classifiers` lists Python 3.11/3.12/3.13 — **3.14 is not declared supported**.

### `sdk/patter/services/transcoding.py:1-19`
```python
try:
    import audioop
except ImportError:
    try:
        import audioop_lts as audioop  # type: ignore
    except ImportError:
        audioop = None  # type: ignore


def mulaw_to_pcm16(mulaw_data: bytes) -> bytes:
    if audioop is None:
        raise ImportError("audioop required: pip install patter[local]")
    return audioop.ulaw2lin(mulaw_data, 2)


def pcm16_to_mulaw(pcm_data: bytes) -> bytes:
    if audioop is None:
        raise ImportError("audioop required: pip install patter[local]")
    return audioop.lin2ulaw(pcm_data, 2)
```

### Test-side audioop handling
None. Only `pytest.importorskip("twilio", ...)` is present in `test_providers_standalone.py`.

### PyPI
`audioop-lts` is a real, maintained package (`curl -sI https://pypi.org/simple/audioop-lts/` → HTTP 200).

### Git history
`git log -S audioop -- sdk/` returns only `125ba47` (v0.3.0 initial commit). Both the conditional extra and the fallback import were present from day one.

## Proposed minimal fix (not applied)

Two independent levers:

**Option 1 — Install fix (smallest, unblocks this run):**
```
python3 -m pip install -e "sdk[local,dev]"
```
Pulls `audioop-lts>=0.2.1` because `python_version>='3.13'`. Both import branches then succeed and all 8 tests pass unchanged.

**Option 2 — Defensive test skip (makes bare `dev` install non-fatal):**
Add a module-level skip at the top of `sdk/tests/test_providers_standalone.py`:
```python
import pytest
try:
    import audioop  # noqa: F401
except ImportError:
    try:
        import audioop_lts  # noqa: F401
    except ImportError:
        pytest.skip("audioop/audioop_lts not installed (optional: patter[local])", allow_module_level=True)
```
Plus an equivalent `importorskip` guard for `tests/test_local_mode.py::test_mark_events_sent_after_audio`.

Options 1 and 2 are complementary — 1 fixes the run now, 2 prevents the same regression for contributors without the extra.

## Risk / side effects

- **Option 1**: zero code risk. Pulls in heavy optional deps (`fastapi`, `uvicorn`, `twilio`, `openai`). 3.14 wheels availability for transitive deps worth spot-checking.
- **Option 2**: masks transcoding coverage on machines without `[local]`. CI must still run with `[local,dev]` installed to preserve the 80% coverage bar.

## Follow-ups worth considering

- Add `3.14` to `pyproject.toml` classifiers and to the CI matrix once Twilio + audioop-lts wheels are verified.
- Consider promoting `audioop-lts` from `[local]` to core `dependencies` — any call path that touches telephony audio needs it, not just full local mode.
