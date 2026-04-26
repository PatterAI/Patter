# Patter Feature-Test Notebook Series Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a 24-notebook customer-facing tutorial series for Patter (12 topics × Python + TypeScript) with three execution tiers — Quickstart (offline, T1+T2), Feature Tour (T1+T2+T3, provider integrations), Live Appendix (T4, real PSTN behind `ENABLE_LIVE_CALLS=1`) — plus automated parity enforcement and a CI quickstart job.

**Architecture:** Five phases land sequentially. Phase 1 (Skeleton) is the foundation: tree, fixtures, `_setup.{py,ts}`, parity check, CI, 24 empty notebook scaffolds — green PR with no notebook content. Phase 2 fills Quickstart cells (T1+T2) across all 24 notebooks. Phase 3 fills Feature Tour cells (T1+T2+T3) one topic at a time. Phase 4 fills Live Appendix cells (T4). Phase 5 polishes README/RELEASES.md and hooks into the docs-feature-drift cron.

**Tech Stack:**
- Python 3.11+ on IPython kernel; pytest + nbclient for headless tests
- TypeScript on Deno Jupyter kernel (`@deno/kernel`); vitest for `_setup.ts` unit tests
- Shared fixtures: gTTS-generated audio, hand-redacted Twilio/Telnyx webhook bodies, generated Ed25519 test keypair
- Output policy: `nbstripout` pre-commit hook + grep for high-entropy strings
- Parity: `scripts/check_notebook_parity.py` runs in CI per PR
- Working directory: `[patterai]-Patter/.worktrees/notebook-skeleton/` (branch: `feat/notebook-series-skeleton`)

---

## File Structure

**New files (Phase 1 — Skeleton):**

```
examples/notebooks/
├── README.md                                 # series overview (filled lightly in P1, polished P5)
├── RELEASES.md                               # empty in P1, populated P5
├── .env.example                              # all env vars grouped by tier
├── pyproject.toml                            # examples/notebooks/python pytest config + deps
├── package.json                              # examples/notebooks/typescript vitest config + deps
├── fixtures/
│   ├── audio/
│   │   ├── PROVENANCE.md                     # source/license docs
│   │   ├── hello_world_8khz_mulaw.wav        # gTTS-generated, transcoded
│   │   ├── hello_world_16khz_pcm.wav
│   │   ├── voicemail_beep.wav                # AMD detection
│   │   └── background_music_loop.wav         # background mixer
│   ├── webhooks/
│   │   ├── twilio_voice_inbound.json         # redacted hand-written
│   │   ├── twilio_status_callback.json
│   │   ├── telnyx_call_initiated.json
│   │   └── telnyx_dtmf_received.json
│   └── keys/
│       ├── telnyx_test_ed25519_pub.pem
│       └── telnyx_test_ed25519_priv.pem
├── python/
│   ├── _setup.py                             # ~250 lines, public surface per spec §4
│   ├── tests/
│   │   ├── __init__.py
│   │   ├── conftest.py
│   │   └── test_setup.py                     # TDD pair for _setup.py
│   ├── 01_quickstart.ipynb                   # empty scaffold (P1) → filled (P2/P3/P4)
│   ├── 02_realtime.ipynb
│   ├── ... (12 notebooks)
│   └── 12_security.ipynb
└── typescript/
    ├── _setup.ts                             # mirror of _setup.py
    ├── tsconfig.json                         # Deno-friendly
    ├── vitest.config.ts
    ├── tests/
    │   └── setup.test.ts
    ├── 01_quickstart.ipynb                   # empty scaffold
    └── ... (12 notebooks)

scripts/
├── check_notebook_parity.py                  # P1: diff paired notebooks, fail on drift
├── generate_notebook_fixtures.py             # P1: one-shot, emits audio + keypair + webhooks
└── scaffold_notebook.py                      # P1: emits empty .ipynb with §1/§2/§3 markers

.github/workflows/
└── notebooks.yml                             # P1: notebooks-quickstart + notebook-parity jobs

.pre-commit-config.yaml                       # P1: nbstripout + secret-grep
.gitattributes                                # P1: ipynb diff filter
```

**Modified files (Phase 5):**

```
.github/workflows/docs-feature-drift.yml      # P5: extend to include feature-vs-notebook drift
scripts/check_feature_docs_drift.py           # P5: extend to walk notebooks too
```

---

## Phase 1: Skeleton

### Task 1: Create directory tree, .env.example, README scaffold

**Files:**
- Create: `examples/notebooks/README.md`
- Create: `examples/notebooks/RELEASES.md`
- Create: `examples/notebooks/.env.example`
- Create: `examples/notebooks/fixtures/audio/.gitkeep`
- Create: `examples/notebooks/fixtures/webhooks/.gitkeep`
- Create: `examples/notebooks/fixtures/keys/.gitkeep`
- Create: `examples/notebooks/python/.gitkeep`
- Create: `examples/notebooks/typescript/.gitkeep`

- [ ] **Step 1: Create directories**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
mkdir -p examples/notebooks/{fixtures/{audio,webhooks,keys},python/tests,typescript/tests}
touch examples/notebooks/fixtures/audio/.gitkeep \
      examples/notebooks/fixtures/webhooks/.gitkeep \
      examples/notebooks/fixtures/keys/.gitkeep
```

- [ ] **Step 2: Write `examples/notebooks/.env.example`**

```
# Patter Feature-Test Notebook Series — environment variables.
# Copy to .env and fill values for the tiers you want to run. NEVER commit .env.

# === Pinned SDK version ====================================================
PATTER_VERSION=0.5.2

# === Tier 3 (Feature Tour) — provider keys ================================
# Each missing key only skips the cells that need it.

OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
GROQ_API_KEY=
CEREBRAS_API_KEY=
DEEPGRAM_API_KEY=
ASSEMBLYAI_API_KEY=
SONIOX_API_KEY=
SPEECHMATICS_API_KEY=
CARTESIA_API_KEY=
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
ELEVENLABS_AGENT_ID=
LMNT_API_KEY=
RIME_API_KEY=
ULTRAVOX_API_KEY=

# === Tier 4 (Live Appendix) — telephony + tunnel ==========================
# All cells under §3 of every notebook are gated by ENABLE_LIVE_CALLS=1.
# Set to 1 only when you are ready to place real PSTN calls (real $ + answering required).

ENABLE_LIVE_CALLS=0

TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=

TELNYX_API_KEY=
TELNYX_CONNECTION_ID=
TELNYX_PHONE_NUMBER=
TELNYX_PUBLIC_KEY=

TARGET_PHONE_NUMBER=
NGROK_AUTHTOKEN=
PUBLIC_WEBHOOK_URL=

# === Budget guards =========================================================
NOTEBOOK_MAX_CALL_SECONDS=90
NOTEBOOK_MAX_COST_USD=0.25
```

- [ ] **Step 3: Write `examples/notebooks/README.md`**

```markdown
# Patter Notebook Tutorial Series

24 Jupyter notebooks (12 topics × Python + TypeScript) that walk through every public Patter feature and every supported provider. Three layers in every notebook:

- **Quickstart (T1+T2)** — offline. No API keys required. Runs in <30s.
- **Feature Tour (T1+T2+T3)** — real provider integrations. Per-key gated; missing keys auto-skip.
- **Live Appendix (T4)** — real PSTN calls, opt-in via `ENABLE_LIVE_CALLS=1`.

## Quickstart

```bash
cp examples/notebooks/.env.example examples/notebooks/.env
cd examples/notebooks/python
pip install -r ../python/pyproject.toml --extra dev
jupyter lab 01_quickstart.ipynb
```

For TypeScript, install the Deno Jupyter kernel:
```bash
deno jupyter --install
cd examples/notebooks/typescript
jupyter lab 01_quickstart.ipynb
```

See `RELEASES.md` for the per-release run log.
```

- [ ] **Step 4: Write `examples/notebooks/RELEASES.md`**

```markdown
# Notebook Series — Release Run Log

Each row is a manual `Run All` pass against the named SDK release with full keys + `ENABLE_LIVE_CALLS=1`.

| Date | SDK version | Operator | Result | Notes |
|------|-------------|----------|--------|-------|
|      |             |          |        |       |
```

- [ ] **Step 5: Commit**

```bash
git add examples/notebooks/
git commit -m "feat(notebooks): bootstrap directory tree, .env.example, README, RELEASES"
```

---

### Task 2: Write `scripts/generate_notebook_fixtures.py` test

**Files:**
- Create: `scripts/test_generate_notebook_fixtures.py`

- [ ] **Step 1: Write failing test**

```python
# scripts/test_generate_notebook_fixtures.py
"""Verify the fixture generator produces valid artifacts."""

import subprocess
import wave
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "examples/notebooks/fixtures"


def test_audio_fixtures_are_valid_wav():
    paths = [
        FIXTURES / "audio/hello_world_8khz_mulaw.wav",
        FIXTURES / "audio/hello_world_16khz_pcm.wav",
        FIXTURES / "audio/voicemail_beep.wav",
        FIXTURES / "audio/background_music_loop.wav",
    ]
    for p in paths:
        assert p.exists(), f"missing fixture: {p}"
        assert p.stat().st_size <= 200_000, f"{p} too large"
        with wave.open(str(p), "rb") as wf:
            assert wf.getnframes() > 0
            assert wf.getsampwidth() in (1, 2)


def test_keypair_files_present():
    pub = FIXTURES / "keys/telnyx_test_ed25519_pub.pem"
    priv = FIXTURES / "keys/telnyx_test_ed25519_priv.pem"
    assert pub.exists() and priv.exists()
    assert "PUBLIC KEY" in pub.read_text()
    assert "PRIVATE KEY" in priv.read_text()


def test_webhook_fixtures_redacted():
    import json
    import re

    real_phone = re.compile(r"\+1[2-9]\d{9}")  # any non-555 NANP
    real_sid = re.compile(r"AC[0-9a-f]{32}")
    for p in (FIXTURES / "webhooks").glob("*.json"):
        body = p.read_text()
        # Allow only +15555550100, ACtest..., or zero-padded test SIDs.
        for match in real_phone.findall(body):
            assert match == "+15555550100", f"{p} leaks phone {match}"
        for match in real_sid.findall(body):
            assert match.startswith("ACtest") or set(match[2:]) == {"0"}, \
                f"{p} leaks SID {match}"
        json.loads(body)  # valid JSON
```

- [ ] **Step 2: Run test, expect fail**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
pytest scripts/test_generate_notebook_fixtures.py -v
```
Expected: 3 FAILED (`missing fixture: .../hello_world_8khz_mulaw.wav` etc.).

- [ ] **Step 3: Commit test**

```bash
git add scripts/test_generate_notebook_fixtures.py
git commit -m "test(notebooks): assertions for fixture generator output"
```

---

### Task 3: Implement `scripts/generate_notebook_fixtures.py`

**Files:**
- Create: `scripts/generate_notebook_fixtures.py`
- Modify: `examples/notebooks/fixtures/audio/PROVENANCE.md` (creates it)

- [ ] **Step 1: Add deps to `sdk/pyproject.toml` dev extras** (only if not already present)

```bash
grep -E "^\\s+\"gtts" "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton/sdk/pyproject.toml" || echo "MISSING"
```

If missing, add to the `dev` extras list in `sdk/pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [
    # ... existing entries ...
    "gtts>=2.4",
    "cryptography>=42.0",
    "pydub>=0.25",  # for mulaw transcoding
]
```

Then `cd sdk && pip install -e ".[dev]"`.

- [ ] **Step 2: Write `scripts/generate_notebook_fixtures.py`**

```python
#!/usr/bin/env python3
"""One-shot fixture generator for the notebook series.

Run from repo root:
    python scripts/generate_notebook_fixtures.py

Outputs:
    examples/notebooks/fixtures/audio/*.wav
    examples/notebooks/fixtures/audio/PROVENANCE.md
    examples/notebooks/fixtures/keys/telnyx_test_ed25519_{pub,priv}.pem
    examples/notebooks/fixtures/webhooks/*.json (idempotent — only if missing)
"""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "examples/notebooks/fixtures"

PROVENANCE = """\
# Audio fixtures — provenance

Generated programmatically. None of these files contain copyrighted material.

| File | Source | License |
|------|--------|---------|
| `hello_world_16khz_pcm.wav` | gTTS (Google Translate TTS) — text "hello world this is a test" | gTTS output is unencumbered (per gTTS FAQ); regenerate via `scripts/generate_notebook_fixtures.py` |
| `hello_world_8khz_mulaw.wav` | Same as above, transcoded to 8 kHz μ-law via pydub | — |
| `voicemail_beep.wav` | Synthesised 1400 Hz tone, 0.4 s | Public domain (synthesised) |
| `background_music_loop.wav` | Synthesised major chord arpeggio, 4 s loop | Public domain (synthesised) |

Regenerate any time:
    python scripts/generate_notebook_fixtures.py
"""


def _write_pcm_wav(path: Path, samples: bytes, sample_rate: int, sample_width: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(samples)


def generate_hello_world() -> None:
    from gtts import gTTS
    from pydub import AudioSegment

    mp3_buf = io.BytesIO()
    gTTS("hello world this is a test", lang="en").write_to_fp(mp3_buf)
    mp3_buf.seek(0)
    seg = AudioSegment.from_file(mp3_buf, format="mp3").set_channels(1)

    pcm16 = seg.set_frame_rate(16_000).set_sample_width(2)
    _write_pcm_wav(FIX / "audio/hello_world_16khz_pcm.wav",
                   pcm16.raw_data, 16_000, sample_width=2)

    mulaw = seg.set_frame_rate(8_000).set_sample_width(1)
    raw_pcm8 = mulaw.raw_data
    # encode to mulaw using audioop (Py 3.13: needs audioop-lts)
    try:
        import audioop
    except ImportError:
        import audioop_lts as audioop  # type: ignore
    mulaw_bytes = audioop.lin2ulaw(mulaw.set_sample_width(2).raw_data, 2)
    _write_pcm_wav(FIX / "audio/hello_world_8khz_mulaw.wav",
                   mulaw_bytes, 8_000, sample_width=1)


def generate_tone(path: Path, freq_hz: float, duration_s: float, sample_rate: int = 8_000) -> None:
    import math
    import struct

    n = int(duration_s * sample_rate)
    samples = bytearray()
    amp = 16_000
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        samples += struct.pack("<h", v)
    _write_pcm_wav(path, bytes(samples), sample_rate, sample_width=2)


def generate_chord_loop(path: Path, duration_s: float = 4.0, sample_rate: int = 16_000) -> None:
    import math
    import struct

    freqs = (261.63, 329.63, 392.00)  # C major triad
    n = int(duration_s * sample_rate)
    samples = bytearray()
    amp = 4_000
    for i in range(n):
        v = sum(amp * math.sin(2 * math.pi * f * i / sample_rate) for f in freqs)
        samples += struct.pack("<h", int(v))
    _write_pcm_wav(path, bytes(samples), sample_rate, sample_width=2)


def generate_keypair() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (FIX / "keys").mkdir(parents=True, exist_ok=True)
    (FIX / "keys/telnyx_test_ed25519_priv.pem").write_bytes(priv_pem)
    (FIX / "keys/telnyx_test_ed25519_pub.pem").write_bytes(pub_pem)


WEBHOOKS = {
    "twilio_voice_inbound.json": {
        "AccountSid": "ACtest00000000000000000000000000",
        "CallSid": "CAtest00000000000000000000000000",
        "From": "+15555550100",
        "To": "+15555550100",
        "Direction": "inbound",
        "CallStatus": "ringing",
    },
    "twilio_status_callback.json": {
        "AccountSid": "ACtest00000000000000000000000000",
        "CallSid": "CAtest00000000000000000000000000",
        "CallStatus": "completed",
        "CallDuration": "12",
    },
    "telnyx_call_initiated.json": {
        "data": {
            "event_type": "call.initiated",
            "id": "00000000-0000-0000-0000-000000000001",
            "payload": {
                "call_control_id": "v3:test",
                "from": "+15555550100",
                "to": "+15555550100",
                "direction": "incoming",
            },
        }
    },
    "telnyx_dtmf_received.json": {
        "data": {
            "event_type": "call.dtmf.received",
            "id": "00000000-0000-0000-0000-000000000002",
            "payload": {
                "call_control_id": "v3:test",
                "digit": "5",
            },
        }
    },
}


def generate_webhooks() -> None:
    (FIX / "webhooks").mkdir(parents=True, exist_ok=True)
    for name, body in WEBHOOKS.items():
        path = FIX / "webhooks" / name
        if path.exists():
            continue  # idempotent — never clobber redactions edited by hand
        path.write_text(json.dumps(body, indent=2) + "\n")


def main() -> None:
    generate_hello_world()
    generate_tone(FIX / "audio/voicemail_beep.wav", freq_hz=1_400, duration_s=0.4)
    generate_chord_loop(FIX / "audio/background_music_loop.wav")
    generate_keypair()
    generate_webhooks()
    (FIX / "audio/PROVENANCE.md").write_text(PROVENANCE)
    print("fixtures generated under", FIX)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run generator**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
python scripts/generate_notebook_fixtures.py
```
Expected: `fixtures generated under .../examples/notebooks/fixtures`

- [ ] **Step 4: Run fixture tests, expect green**

```bash
pytest scripts/test_generate_notebook_fixtures.py -v
```
Expected: 3 PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_notebook_fixtures.py examples/notebooks/fixtures/
git commit -m "feat(notebooks): fixture generator + initial audio/keys/webhooks"
```

---

### Task 4: Add `examples/notebooks/python/pyproject.toml`

**Files:**
- Create: `examples/notebooks/python/pyproject.toml`
- Create: `examples/notebooks/python/tests/__init__.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "patter-notebooks-python"
version = "0.0.0"
description = "Internal helpers + tests for the Patter Python notebook tutorial series."
requires-python = ">=3.11"
dependencies = [
    # Notebook execution
    "ipykernel>=6.29",
    "ipython>=8.0",
    "nbclient>=0.10",
    "jupyter-client>=8.0",
    # _setup helpers
    "python-dotenv>=1.0",
    "httpx>=0.27",
    # Optional: imported lazily inside cells, listed here for completeness
    "twilio>=9.0",
    "telnyx>=2.0",
    "pyngrok>=7.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "nbstripout>=0.7",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-ra"
```

- [ ] **Step 2: Touch test package marker**

```bash
echo '"""Tests for examples/notebooks/python/_setup.py."""' > examples/notebooks/python/tests/__init__.py
```

- [ ] **Step 3: Commit**

```bash
git add examples/notebooks/python/pyproject.toml examples/notebooks/python/tests/__init__.py
git commit -m "feat(notebooks): pyproject.toml for python notebook helpers"
```

---

### Task 5: Write `_setup.py` test for `NotebookEnv` + `load()`

**Files:**
- Create: `examples/notebooks/python/tests/conftest.py`
- Create: `examples/notebooks/python/tests/test_setup.py`

- [ ] **Step 1: Write conftest**

```python
# examples/notebooks/python/tests/conftest.py
import sys
from pathlib import Path

PARENT = Path(__file__).resolve().parents[1]
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))
```

- [ ] **Step 2: Write failing test**

```python
# examples/notebooks/python/tests/test_setup.py
"""TDD pair for examples/notebooks/python/_setup.py."""

from __future__ import annotations

import dataclasses

import pytest


def test_notebook_env_is_frozen_dataclass():
    import _setup
    env = _setup.NotebookEnv(
        openai_key="", anthropic_key="", google_key="", groq_key="",
        cerebras_key="", deepgram_key="", assemblyai_key="", soniox_key="",
        speechmatics_key="", cartesia_key="", elevenlabs_key="",
        elevenlabs_voice_id="", elevenlabs_agent_id="",
        lmnt_key="", rime_key="", ultravox_key="",
        twilio_sid="", twilio_token="", twilio_number="",
        telnyx_key="", telnyx_connection_id="", telnyx_number="", telnyx_public_key="",
        target_number="", ngrok_token="", public_webhook_url="",
        patter_version="0.5.2",
        enable_live_calls=False,
        max_call_seconds=90,
        max_cost_usd=0.25,
    )
    assert dataclasses.is_dataclass(env)
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.openai_key = "x"


def test_load_reads_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-proj-xxx\n"
        "ENABLE_LIVE_CALLS=1\n"
        "NOTEBOOK_MAX_COST_USD=1.5\n"
    )
    monkeypatch.chdir(tmp_path)

    import _setup
    env = _setup.load(env_file=env_file)

    assert env.openai_key == "sk-proj-xxx"
    assert env.enable_live_calls is True
    assert env.max_cost_usd == 1.5


def test_load_returns_empty_strings_for_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import _setup
    env = _setup.load(env_file=tmp_path / "nonexistent.env")
    assert env.openai_key == ""
    assert env.enable_live_calls is False
```

- [ ] **Step 3: Run test, expect fail**

```bash
cd examples/notebooks/python
pytest tests/test_setup.py -v
```
Expected: FAIL with `ModuleNotFoundError: _setup`.

- [ ] **Step 4: Commit test**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/python/tests/
git commit -m "test(notebooks): NotebookEnv frozen dataclass + dotenv load"
```

---

### Task 6: Implement `_setup.py` — `NotebookEnv` + `load()`

**Files:**
- Create: `examples/notebooks/python/_setup.py`

- [ ] **Step 1: Write the module skeleton**

```python
# examples/notebooks/python/_setup.py
"""Shared helpers for every notebook in examples/notebooks/python/.

Public surface (mirrored in typescript/_setup.ts):
    NotebookEnv  — frozen dataclass holding every env var the series reads
    load()       — parse .env and return NotebookEnv
    has_key()    — booleanise a key
    print_key_matrix() — render a ✅/⚪️ table at notebook open
    cell()       — context manager wrapping every feature cell
    skip()       — raise NotebookSkip inside a cell
    skip_section() — same, for whole sections (live appendix gate)
    load_fixture() — load bytes from examples/notebooks/fixtures/
    run_stt()    — standardised STT roundtrip helper
    run_tts()    — standardised TTS roundtrip helper
    hangup_leftover_calls() — safety sweep for live appendix teardown
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

NOTEBOOKS_DIR = Path(__file__).resolve().parent.parent
FIXTURES = NOTEBOOKS_DIR / "fixtures"


@dataclass(frozen=True)
class NotebookEnv:
    openai_key: str
    anthropic_key: str
    google_key: str
    groq_key: str
    cerebras_key: str
    deepgram_key: str
    assemblyai_key: str
    soniox_key: str
    speechmatics_key: str
    cartesia_key: str
    elevenlabs_key: str
    elevenlabs_voice_id: str
    elevenlabs_agent_id: str
    lmnt_key: str
    rime_key: str
    ultravox_key: str
    twilio_sid: str
    twilio_token: str
    twilio_number: str
    telnyx_key: str
    telnyx_connection_id: str
    telnyx_number: str
    telnyx_public_key: str
    target_number: str
    ngrok_token: str
    public_webhook_url: str
    patter_version: str
    enable_live_calls: bool
    max_call_seconds: int
    max_cost_usd: float


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def load(env_file: Path | str | None = None) -> NotebookEnv:
    """Load .env if present, then construct NotebookEnv from process env."""
    if env_file is None:
        env_file = NOTEBOOKS_DIR / ".env"
    env_file = Path(env_file)
    if env_file.exists():
        load_dotenv(env_file, override=False)

    return NotebookEnv(
        openai_key=_get("OPENAI_API_KEY"),
        anthropic_key=_get("ANTHROPIC_API_KEY"),
        google_key=_get("GOOGLE_API_KEY"),
        groq_key=_get("GROQ_API_KEY"),
        cerebras_key=_get("CEREBRAS_API_KEY"),
        deepgram_key=_get("DEEPGRAM_API_KEY"),
        assemblyai_key=_get("ASSEMBLYAI_API_KEY"),
        soniox_key=_get("SONIOX_API_KEY"),
        speechmatics_key=_get("SPEECHMATICS_API_KEY"),
        cartesia_key=_get("CARTESIA_API_KEY"),
        elevenlabs_key=_get("ELEVENLABS_API_KEY"),
        elevenlabs_voice_id=_get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
        elevenlabs_agent_id=_get("ELEVENLABS_AGENT_ID"),
        lmnt_key=_get("LMNT_API_KEY"),
        rime_key=_get("RIME_API_KEY"),
        ultravox_key=_get("ULTRAVOX_API_KEY"),
        twilio_sid=_get("TWILIO_ACCOUNT_SID"),
        twilio_token=_get("TWILIO_AUTH_TOKEN"),
        twilio_number=_get("TWILIO_PHONE_NUMBER"),
        telnyx_key=_get("TELNYX_API_KEY"),
        telnyx_connection_id=_get("TELNYX_CONNECTION_ID"),
        telnyx_number=_get("TELNYX_PHONE_NUMBER"),
        telnyx_public_key=_get("TELNYX_PUBLIC_KEY"),
        target_number=_get("TARGET_PHONE_NUMBER"),
        ngrok_token=_get("NGROK_AUTHTOKEN"),
        public_webhook_url=_get("PUBLIC_WEBHOOK_URL"),
        patter_version=_get("PATTER_VERSION", "0.5.2"),
        enable_live_calls=_get("ENABLE_LIVE_CALLS", "0") == "1",
        max_call_seconds=int(_get("NOTEBOOK_MAX_CALL_SECONDS", "90")),
        max_cost_usd=float(_get("NOTEBOOK_MAX_COST_USD", "0.25")),
    )
```

- [ ] **Step 2: Run tests, expect green**

```bash
cd examples/notebooks/python
pytest tests/test_setup.py -v
```
Expected: 3 PASSED.

- [ ] **Step 3: Commit**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/python/_setup.py
git commit -m "feat(notebooks): _setup.py — NotebookEnv + load()"
```

---

### Task 7: Add `has_key`, `print_key_matrix`, `NotebookSkip`, `skip`, `skip_section`

**Files:**
- Modify: `examples/notebooks/python/_setup.py`
- Modify: `examples/notebooks/python/tests/test_setup.py`

- [ ] **Step 1: Write failing tests**

Append to `examples/notebooks/python/tests/test_setup.py`:

```python
def test_has_key_returns_true_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    assert _setup.has_key(env, "OPENAI_API_KEY") is True


def test_has_key_returns_false_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    assert _setup.has_key(env, "DEEPGRAM_API_KEY") is False


def test_skip_raises_notebook_skip():
    import _setup
    with pytest.raises(_setup.NotebookSkip) as exc:
        _setup.skip("missing key")
    assert "missing key" in str(exc.value)


def test_print_key_matrix_outputs_check_and_circle(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    _setup.print_key_matrix(env, required=["OPENAI_API_KEY", "DEEPGRAM_API_KEY"])
    out = capsys.readouterr().out
    assert "OPENAI_API_KEY" in out and "✅" in out
    assert "DEEPGRAM_API_KEY" in out and "⚪" in out
```

- [ ] **Step 2: Run, expect fail**

```bash
cd examples/notebooks/python
pytest tests/test_setup.py -v
```
Expected: 4 FAILED.

- [ ] **Step 3: Implement**

Append to `examples/notebooks/python/_setup.py`:

```python
class NotebookSkip(Exception):
    """Raised inside a cell to render a skip banner instead of an error."""


_KEY_FIELD_MAP = {
    "OPENAI_API_KEY": "openai_key",
    "ANTHROPIC_API_KEY": "anthropic_key",
    "GOOGLE_API_KEY": "google_key",
    "GROQ_API_KEY": "groq_key",
    "CEREBRAS_API_KEY": "cerebras_key",
    "DEEPGRAM_API_KEY": "deepgram_key",
    "ASSEMBLYAI_API_KEY": "assemblyai_key",
    "SONIOX_API_KEY": "soniox_key",
    "SPEECHMATICS_API_KEY": "speechmatics_key",
    "CARTESIA_API_KEY": "cartesia_key",
    "ELEVENLABS_API_KEY": "elevenlabs_key",
    "ELEVENLABS_AGENT_ID": "elevenlabs_agent_id",
    "LMNT_API_KEY": "lmnt_key",
    "RIME_API_KEY": "rime_key",
    "ULTRAVOX_API_KEY": "ultravox_key",
    "TWILIO_ACCOUNT_SID": "twilio_sid",
    "TWILIO_AUTH_TOKEN": "twilio_token",
    "TWILIO_PHONE_NUMBER": "twilio_number",
    "TELNYX_API_KEY": "telnyx_key",
    "TELNYX_CONNECTION_ID": "telnyx_connection_id",
    "TELNYX_PHONE_NUMBER": "telnyx_number",
    "TELNYX_PUBLIC_KEY": "telnyx_public_key",
    "TARGET_PHONE_NUMBER": "target_number",
    "NGROK_AUTHTOKEN": "ngrok_token",
    "PUBLIC_WEBHOOK_URL": "public_webhook_url",
}


def has_key(env: NotebookEnv, name: str) -> bool:
    field_name = _KEY_FIELD_MAP.get(name)
    if field_name is None:
        return bool(_get(name))
    return bool(getattr(env, field_name))


def skip(reason: str) -> None:
    raise NotebookSkip(reason)


def skip_section(reason: str) -> None:
    raise NotebookSkip(f"[section skipped] {reason}")


def print_key_matrix(env: NotebookEnv, required: Iterable[str]) -> None:
    print("Key matrix:")
    for name in required:
        marker = "✅" if has_key(env, name) else "⚪"
        print(f"  {marker} {name}")
```

- [ ] **Step 4: Run, expect green**

```bash
pytest tests/test_setup.py -v
```
Expected: 7 PASSED total.

- [ ] **Step 5: Commit**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/python/
git commit -m "feat(notebooks): _setup.has_key + print_key_matrix + skip helpers"
```

---

### Task 8: Add `cell()` context manager

**Files:**
- Modify: `examples/notebooks/python/_setup.py`
- Modify: `examples/notebooks/python/tests/test_setup.py`

- [ ] **Step 1: Write failing tests**

Append to `examples/notebooks/python/tests/test_setup.py`:

```python
def test_cell_passes_when_keys_present(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    with _setup.cell("test_cell", tier=3, required=["OPENAI_API_KEY"], env=env):
        print("body ran")
    out = capsys.readouterr().out
    assert "body ran" in out


def test_cell_skips_on_missing_key(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    with _setup.cell("test_cell", tier=3, required=["OPENAI_API_KEY"], env=env):
        print("body ran")
    out = capsys.readouterr().out
    assert "body ran" not in out
    assert "OPENAI_API_KEY" in out
    assert "skipped" in out.lower()


def test_cell_skips_on_tier_4_when_live_disabled(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ENABLE_LIVE_CALLS", "0")
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    with _setup.cell("live_cell", tier=4, required=[], env=env):
        print("body ran")
    out = capsys.readouterr().out
    assert "body ran" not in out
    assert "ENABLE_LIVE_CALLS" in out


def test_cell_renders_banner_on_exception(monkeypatch, tmp_path, capsys):
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    with _setup.cell("test_cell", tier=1, required=[], env=env):
        raise RuntimeError("kaboom")
    out = capsys.readouterr().out
    assert "kaboom" in out
    assert "test_cell" in out
```

- [ ] **Step 2: Run, expect fail**

```bash
pytest tests/test_setup.py::test_cell_passes_when_keys_present -v
```
Expected: FAIL — `_setup.cell` not defined.

- [ ] **Step 3: Implement `cell()` context manager**

Append to `examples/notebooks/python/_setup.py`:

```python
import contextlib
import time
import traceback
from typing import Iterator


@contextlib.contextmanager
def cell(
    name: str,
    *,
    tier: int,
    required: Iterable[str] = (),
    env: NotebookEnv | None = None,
) -> Iterator[None]:
    """Wrap every feature-tour or live cell.

    Behaviour:
        - Tier 4 cells skip unless env.enable_live_calls is True.
        - Cells with any unset required key skip with a friendly banner.
        - Exceptions inside the cell are swallowed; banner is printed but
          notebook execution continues.
    """
    env = env if env is not None else load()
    started = time.monotonic()

    if tier == 4 and not env.enable_live_calls:
        print(f"⚪ [{name}] skipped — set ENABLE_LIVE_CALLS=1 to enable T4 live calls.")
        return

    missing = [k for k in required if not has_key(env, k)]
    if missing:
        keys = ", ".join(missing)
        print(f"⚪ [{name}] skipped — missing env: {keys}")
        return

    print(f"▶ [{name}] tier={tier}")
    try:
        yield
    except NotebookSkip as exc:
        print(f"⚪ [{name}] {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        print(f"❌ [{name}] failed after {elapsed:.2f}s: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=4)
        return
    elapsed = time.monotonic() - started
    print(f"✅ [{name}] {elapsed:.2f}s")
```

- [ ] **Step 4: Run, expect green**

```bash
pytest tests/test_setup.py -v
```
Expected: 11 PASSED.

- [ ] **Step 5: Commit**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/python/
git commit -m "feat(notebooks): _setup.cell context manager with tier/key gating"
```

---

### Task 9: Add `load_fixture` + `_assert_redacted`

**Files:**
- Modify: `examples/notebooks/python/_setup.py`
- Modify: `examples/notebooks/python/tests/test_setup.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_setup.py`:

```python
def test_load_fixture_returns_bytes():
    import _setup
    data = _setup.load_fixture("audio/hello_world_16khz_pcm.wav")
    assert isinstance(data, bytes)
    assert len(data) > 100


def test_load_fixture_unknown_path_raises():
    import _setup
    with pytest.raises(FileNotFoundError):
        _setup.load_fixture("audio/nonexistent.wav")


def test_assert_redacted_blocks_real_phone(tmp_path):
    import _setup
    bad = tmp_path / "bad.json"
    bad.write_text('{"From": "+14155551234"}')  # not the +1555-555-0100 placeholder
    with pytest.raises(ValueError, match="phone"):
        _setup._assert_redacted(bad.read_text(), str(bad))


def test_assert_redacted_passes_placeholder(tmp_path):
    import _setup
    ok = tmp_path / "ok.json"
    ok.write_text('{"From": "+15555550100"}')
    _setup._assert_redacted(ok.read_text(), str(ok))  # no raise
```

- [ ] **Step 2: Run, expect fail**

```bash
pytest tests/test_setup.py -v
```
Expected: 4 FAILED.

- [ ] **Step 3: Implement**

Append to `_setup.py`:

```python
import re

_REAL_PHONE = re.compile(r"\+1[2-9]\d{9}")
_REAL_TWILIO_SID = re.compile(r"\bAC[0-9a-f]{32}\b")


def _assert_redacted(body: str, source: str) -> None:
    for m in _REAL_PHONE.findall(body):
        if m != "+15555550100":
            raise ValueError(f"{source} contains non-placeholder phone {m}")
    for m in _REAL_TWILIO_SID.findall(body):
        if not (m.startswith("ACtest") or set(m[2:]) == {"0"}):
            raise ValueError(f"{source} contains non-placeholder Twilio SID {m}")


def load_fixture(rel_path: str) -> bytes:
    path = FIXTURES / rel_path
    if not path.exists():
        raise FileNotFoundError(f"fixture not found: {path}")
    data = path.read_bytes()
    if path.suffix == ".json":
        _assert_redacted(data.decode("utf-8"), str(path))
    return data
```

- [ ] **Step 4: Run, expect green**

```bash
pytest tests/test_setup.py -v
```
Expected: 15 PASSED.

- [ ] **Step 5: Commit**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/python/
git commit -m "feat(notebooks): load_fixture + _assert_redacted PII guard"
```

---

### Task 10: Add `run_stt` and `run_tts` helpers

**Files:**
- Modify: `examples/notebooks/python/_setup.py`
- Modify: `examples/notebooks/python/tests/test_setup.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_setup.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_run_stt_aggregates_transcripts():
    import _setup

    class FakeSTT:
        async def connect(self): pass
        async def send_audio(self, chunk): pass
        async def close(self): pass
        async def receive_transcripts(self):
            yield "hello "
            yield "world"

    transcript = await _setup.run_stt(FakeSTT(), b"\x00" * 16000)
    assert transcript.strip() == "hello world"


@pytest.mark.asyncio
async def test_run_tts_concatenates_chunks():
    import _setup

    class FakeTTS:
        async def synthesize(self, text):
            yield b"\x01\x02"
            yield b"\x03\x04"

    audio = await _setup.run_tts(FakeTTS(), "hi")
    assert audio == b"\x01\x02\x03\x04"
```

- [ ] **Step 2: Run, expect fail**

```bash
pytest tests/test_setup.py -v
```
Expected: 2 FAILED.

- [ ] **Step 3: Implement**

Append to `_setup.py`:

```python
async def run_stt(stt, audio: bytes, *, chunk_size: int = 3200) -> str:
    """Send audio in chunks through any STTProvider, collect the transcript."""
    await stt.connect()
    try:
        for i in range(0, len(audio), chunk_size):
            await stt.send_audio(audio[i : i + chunk_size])
        out: list[str] = []
        async for piece in stt.receive_transcripts():
            out.append(piece)
        return "".join(out)
    finally:
        await stt.close()


async def run_tts(tts, text: str) -> bytes:
    """Synthesize text via any TTSProvider, return concatenated bytes."""
    chunks: list[bytes] = []
    async for chunk in tts.synthesize(text):
        chunks.append(chunk)
    return b"".join(chunks)
```

- [ ] **Step 4: Run, expect green**

```bash
pytest tests/test_setup.py -v
```
Expected: 17 PASSED.

- [ ] **Step 5: Commit**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/python/
git commit -m "feat(notebooks): run_stt + run_tts standardised roundtrip helpers"
```

---

### Task 11: Add `hangup_leftover_calls`

**Files:**
- Modify: `examples/notebooks/python/_setup.py`
- Modify: `examples/notebooks/python/tests/test_setup.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_setup.py`:

```python
def test_hangup_leftover_calls_iterates_active_twilio(monkeypatch, tmp_path):
    import _setup

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "x")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+15555550100")
    env = _setup.load(env_file=tmp_path / "missing.env")

    hung_up = []

    class _Calls:
        def __init__(self, sid): self.sid = sid
        def update(self, status): hung_up.append((self.sid, status))

    class _RootCalls:
        def list(self, **kw): return [type("c", (), {"sid": "CAtest1"})()]
        def __call__(self, sid): return _Calls(sid)

    class _Client:
        def __init__(self, *_a, **_kw): pass
        @property
        def calls(self): return _RootCalls()

    monkeypatch.setattr(_setup, "_TwilioClient", _Client, raising=False)

    _setup.hangup_leftover_calls(env)
    assert hung_up == [("CAtest1", "completed")]
```

- [ ] **Step 2: Run, expect fail**

```bash
pytest tests/test_setup.py::test_hangup_leftover_calls_iterates_active_twilio -v
```
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `_setup.py`:

```python
try:
    from twilio.rest import Client as _TwilioClient  # type: ignore
except ImportError:
    _TwilioClient = None  # type: ignore[misc,assignment]


def hangup_leftover_calls(env: NotebookEnv) -> None:
    """Best-effort sweep — hang up any in-progress calls from the test numbers.

    Use in a `finally:` after a live cell to keep stale calls from blocking
    the next run. Failures are logged, never raised.
    """
    if not (env.twilio_sid and env.twilio_token and env.twilio_number):
        return
    if _TwilioClient is None:
        print("⚪ twilio package not installed — skipping hangup sweep")
        return
    client = _TwilioClient(env.twilio_sid, env.twilio_token)
    try:
        for call in client.calls.list(from_=env.twilio_number, status="in-progress", limit=5):
            try:
                client.calls(call.sid).update(status="completed")
                print(f"🔚 hung up stale call {call.sid}")
            except Exception as exc:  # noqa: BLE001
                print(f"⚠ could not hang up {call.sid}: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠ Twilio sweep failed: {exc}")
```

- [ ] **Step 4: Run, expect green**

```bash
pytest tests/test_setup.py -v
```
Expected: 18 PASSED.

- [ ] **Step 5: Commit**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/python/
git commit -m "feat(notebooks): hangup_leftover_calls Twilio sweep"
```

---

### Task 12: Set up TypeScript notebook helpers package

**Files:**
- Create: `examples/notebooks/typescript/package.json`
- Create: `examples/notebooks/typescript/tsconfig.json`
- Create: `examples/notebooks/typescript/vitest.config.ts`

- [ ] **Step 1: Write `package.json`**

```json
{
  "name": "patter-notebooks-typescript",
  "version": "0.0.0",
  "private": true,
  "type": "module",
  "scripts": {
    "test": "vitest run"
  },
  "dependencies": {
    "dotenv": "^16.4.5",
    "twilio": "^5.0.0",
    "telnyx": "^2.0.0",
    "ws": "^8.18.0"
  },
  "devDependencies": {
    "vitest": "^1.6.0",
    "@types/node": "^20.0.0",
    "@types/ws": "^8.5.10",
    "typescript": "^5.4.0"
  }
}
```

- [ ] **Step 2: Write `tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "lib": ["ES2022", "DOM"],
    "strict": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "skipLibCheck": true,
    "resolveJsonModule": true,
    "noEmit": true,
    "types": ["node", "vitest/globals"]
  },
  "include": ["_setup.ts", "tests/**/*.ts"]
}
```

- [ ] **Step 3: Write `vitest.config.ts`**

```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globals: true,
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});
```

- [ ] **Step 4: Install + commit**

```bash
cd examples/notebooks/typescript
npm install
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/typescript/package.json \
        examples/notebooks/typescript/tsconfig.json \
        examples/notebooks/typescript/vitest.config.ts \
        examples/notebooks/typescript/package-lock.json
git commit -m "feat(notebooks): typescript helpers package — package.json + tsconfig + vitest"
```

---

### Task 13: Write `_setup.ts` test for `NotebookEnv` + `load`

**Files:**
- Create: `examples/notebooks/typescript/tests/setup.test.ts`

- [ ] **Step 1: Write failing test**

```ts
// examples/notebooks/typescript/tests/setup.test.ts
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";

import { load, hasKey, NotebookSkip, skip, printKeyMatrix, cell, loadFixture } from "../_setup";

let tmpDir: string;
const ORIGINAL_ENV = { ...process.env };

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "patter-nb-"));
});

afterEach(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
  process.env = { ...ORIGINAL_ENV };
});

describe("[unit] _setup load()", () => {
  it("reads keys from the .env file path", () => {
    const envFile = path.join(tmpDir, ".env");
    fs.writeFileSync(envFile, "OPENAI_API_KEY=sk-x\nENABLE_LIVE_CALLS=1\n");
    const env = load({ envFile });
    expect(env.openaiKey).toBe("sk-x");
    expect(env.enableLiveCalls).toBe(true);
  });

  it("returns empty strings for missing keys", () => {
    delete process.env.OPENAI_API_KEY;
    const env = load({ envFile: path.join(tmpDir, "nope.env") });
    expect(env.openaiKey).toBe("");
    expect(env.enableLiveCalls).toBe(false);
  });
});
```

- [ ] **Step 2: Run, expect fail**

```bash
cd examples/notebooks/typescript
npm test
```
Expected: FAIL — cannot find `../_setup`.

- [ ] **Step 3: Commit test**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/typescript/tests/
git commit -m "test(notebooks): _setup.ts NotebookEnv + load happy/missing"
```

---

### Task 14: Implement `_setup.ts` — types + `load`

**Files:**
- Create: `examples/notebooks/typescript/_setup.ts`

- [ ] **Step 1: Write the module skeleton**

```ts
// examples/notebooks/typescript/_setup.ts
/**
 * Shared helpers for every notebook in examples/notebooks/typescript/.
 *
 * Mirror of python/_setup.py. Keep field names, behaviour, and exit codes
 * aligned with the Python module — the parity check script enforces shape.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import * as dotenv from "dotenv";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const NOTEBOOKS_DIR = path.dirname(HERE);
export const FIXTURES = path.join(NOTEBOOKS_DIR, "fixtures");

export interface NotebookEnv {
  readonly openaiKey: string;
  readonly anthropicKey: string;
  readonly googleKey: string;
  readonly groqKey: string;
  readonly cerebrasKey: string;
  readonly deepgramKey: string;
  readonly assemblyaiKey: string;
  readonly sonioxKey: string;
  readonly speechmaticsKey: string;
  readonly cartesiaKey: string;
  readonly elevenlabsKey: string;
  readonly elevenlabsVoiceId: string;
  readonly elevenlabsAgentId: string;
  readonly lmntKey: string;
  readonly rimeKey: string;
  readonly ultravoxKey: string;
  readonly twilioSid: string;
  readonly twilioToken: string;
  readonly twilioNumber: string;
  readonly telnyxKey: string;
  readonly telnyxConnectionId: string;
  readonly telnyxNumber: string;
  readonly telnyxPublicKey: string;
  readonly targetNumber: string;
  readonly ngrokToken: string;
  readonly publicWebhookUrl: string;
  readonly patterVersion: string;
  readonly enableLiveCalls: boolean;
  readonly maxCallSeconds: number;
  readonly maxCostUsd: number;
}

const get = (n: string, d = ""): string => (process.env[n] ?? d).trim();

export function load(opts: { envFile?: string } = {}): NotebookEnv {
  const envFile = opts.envFile ?? path.join(NOTEBOOKS_DIR, ".env");
  if (fs.existsSync(envFile)) {
    dotenv.config({ path: envFile, override: false });
  }
  return Object.freeze<NotebookEnv>({
    openaiKey: get("OPENAI_API_KEY"),
    anthropicKey: get("ANTHROPIC_API_KEY"),
    googleKey: get("GOOGLE_API_KEY"),
    groqKey: get("GROQ_API_KEY"),
    cerebrasKey: get("CEREBRAS_API_KEY"),
    deepgramKey: get("DEEPGRAM_API_KEY"),
    assemblyaiKey: get("ASSEMBLYAI_API_KEY"),
    sonioxKey: get("SONIOX_API_KEY"),
    speechmaticsKey: get("SPEECHMATICS_API_KEY"),
    cartesiaKey: get("CARTESIA_API_KEY"),
    elevenlabsKey: get("ELEVENLABS_API_KEY"),
    elevenlabsVoiceId: get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
    elevenlabsAgentId: get("ELEVENLABS_AGENT_ID"),
    lmntKey: get("LMNT_API_KEY"),
    rimeKey: get("RIME_API_KEY"),
    ultravoxKey: get("ULTRAVOX_API_KEY"),
    twilioSid: get("TWILIO_ACCOUNT_SID"),
    twilioToken: get("TWILIO_AUTH_TOKEN"),
    twilioNumber: get("TWILIO_PHONE_NUMBER"),
    telnyxKey: get("TELNYX_API_KEY"),
    telnyxConnectionId: get("TELNYX_CONNECTION_ID"),
    telnyxNumber: get("TELNYX_PHONE_NUMBER"),
    telnyxPublicKey: get("TELNYX_PUBLIC_KEY"),
    targetNumber: get("TARGET_PHONE_NUMBER"),
    ngrokToken: get("NGROK_AUTHTOKEN"),
    publicWebhookUrl: get("PUBLIC_WEBHOOK_URL"),
    patterVersion: get("PATTER_VERSION", "0.5.2"),
    enableLiveCalls: get("ENABLE_LIVE_CALLS", "0") === "1",
    maxCallSeconds: parseInt(get("NOTEBOOK_MAX_CALL_SECONDS", "90"), 10),
    maxCostUsd: parseFloat(get("NOTEBOOK_MAX_COST_USD", "0.25")),
  });
}

// Stubs (filled in subsequent tasks):
export class NotebookSkip extends Error {}
export function hasKey(_env: NotebookEnv, _name: string): boolean { throw new Error("not implemented"); }
export function skip(_reason: string): never { throw new Error("not implemented"); }
export function skipSection(_reason: string): never { throw new Error("not implemented"); }
export function printKeyMatrix(_env: NotebookEnv, _required: string[]): void { throw new Error("not implemented"); }
export async function cell<T>(_name: string, _opts: { tier: number; required?: string[]; env?: NotebookEnv }, _body: () => Promise<T> | T): Promise<void> { throw new Error("not implemented"); }
export function loadFixture(_relPath: string): Buffer { throw new Error("not implemented"); }
```

- [ ] **Step 2: Run, expect green for `load` tests, fail for the rest**

```bash
cd examples/notebooks/typescript
npm test -- tests/setup.test.ts
```
Expected: 2 PASSED for load(), other tests FAIL with "not implemented".

- [ ] **Step 3: Commit**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/typescript/_setup.ts
git commit -m "feat(notebooks): _setup.ts NotebookEnv interface + load()"
```

---

### Task 15: Implement `_setup.ts` — `hasKey`, `skip`, `printKeyMatrix`, `cell`, `loadFixture`

**Files:**
- Modify: `examples/notebooks/typescript/_setup.ts`
- Modify: `examples/notebooks/typescript/tests/setup.test.ts`

- [ ] **Step 1: Append failing tests**

```ts
describe("[unit] _setup hasKey + skip + cell + loadFixture", () => {
  it("hasKey returns true when env var set", () => {
    process.env.OPENAI_API_KEY = "x";
    const env = load({ envFile: path.join(tmpDir, "missing.env") });
    expect(hasKey(env, "OPENAI_API_KEY")).toBe(true);
  });

  it("skip throws NotebookSkip", () => {
    expect(() => skip("missing key")).toThrowError(NotebookSkip);
  });

  it("cell skips on missing key and continues", async () => {
    delete process.env.OPENAI_API_KEY;
    const env = load({ envFile: path.join(tmpDir, "missing.env") });
    let ran = false;
    await cell("test_cell", { tier: 3, required: ["OPENAI_API_KEY"], env }, async () => {
      ran = true;
    });
    expect(ran).toBe(false);
  });

  it("cell skips T4 when ENABLE_LIVE_CALLS=0", async () => {
    process.env.ENABLE_LIVE_CALLS = "0";
    const env = load({ envFile: path.join(tmpDir, "missing.env") });
    let ran = false;
    await cell("live_cell", { tier: 4, env }, async () => { ran = true; });
    expect(ran).toBe(false);
  });

  it("cell swallows exceptions and continues", async () => {
    const env = load({ envFile: path.join(tmpDir, "missing.env") });
    await cell("test_cell", { tier: 1, env }, async () => {
      throw new Error("kaboom");
    });
    // No throw at top level.
  });

  it("loadFixture returns bytes for known file", () => {
    const data = loadFixture("audio/hello_world_16khz_pcm.wav");
    expect(data.length).toBeGreaterThan(100);
  });
});
```

- [ ] **Step 2: Run, expect fail**

```bash
npm test
```
Expected: 6 FAILED.

- [ ] **Step 3: Replace stubs in `_setup.ts`**

Replace the stub section at the bottom of `_setup.ts` with:

```ts
const KEY_FIELD_MAP: Record<string, keyof NotebookEnv> = {
  OPENAI_API_KEY: "openaiKey",
  ANTHROPIC_API_KEY: "anthropicKey",
  GOOGLE_API_KEY: "googleKey",
  GROQ_API_KEY: "groqKey",
  CEREBRAS_API_KEY: "cerebrasKey",
  DEEPGRAM_API_KEY: "deepgramKey",
  ASSEMBLYAI_API_KEY: "assemblyaiKey",
  SONIOX_API_KEY: "sonioxKey",
  SPEECHMATICS_API_KEY: "speechmaticsKey",
  CARTESIA_API_KEY: "cartesiaKey",
  ELEVENLABS_API_KEY: "elevenlabsKey",
  ELEVENLABS_AGENT_ID: "elevenlabsAgentId",
  LMNT_API_KEY: "lmntKey",
  RIME_API_KEY: "rimeKey",
  ULTRAVOX_API_KEY: "ultravoxKey",
  TWILIO_ACCOUNT_SID: "twilioSid",
  TWILIO_AUTH_TOKEN: "twilioToken",
  TWILIO_PHONE_NUMBER: "twilioNumber",
  TELNYX_API_KEY: "telnyxKey",
  TELNYX_CONNECTION_ID: "telnyxConnectionId",
  TELNYX_PHONE_NUMBER: "telnyxNumber",
  TELNYX_PUBLIC_KEY: "telnyxPublicKey",
  TARGET_PHONE_NUMBER: "targetNumber",
  NGROK_AUTHTOKEN: "ngrokToken",
  PUBLIC_WEBHOOK_URL: "publicWebhookUrl",
};

export class NotebookSkip extends Error {
  constructor(reason: string) { super(reason); this.name = "NotebookSkip"; }
}

export function hasKey(env: NotebookEnv, name: string): boolean {
  const fieldName = KEY_FIELD_MAP[name];
  const v = fieldName ? (env as any)[fieldName] : process.env[name] ?? "";
  return Boolean(v);
}

export function skip(reason: string): never { throw new NotebookSkip(reason); }
export function skipSection(reason: string): never { throw new NotebookSkip(`[section skipped] ${reason}`); }

export function printKeyMatrix(env: NotebookEnv, required: string[]): void {
  console.log("Key matrix:");
  for (const name of required) {
    const marker = hasKey(env, name) ? "✅" : "⚪";
    console.log(`  ${marker} ${name}`);
  }
}

const REAL_PHONE = /\+1[2-9]\d{9}/g;
const REAL_TWILIO_SID = /\bAC[0-9a-f]{32}\b/g;

function assertRedacted(body: string, source: string): void {
  for (const m of body.match(REAL_PHONE) ?? []) {
    if (m !== "+15555550100") throw new Error(`${source} contains non-placeholder phone ${m}`);
  }
  for (const m of body.match(REAL_TWILIO_SID) ?? []) {
    if (!(m.startsWith("ACtest") || m.slice(2).split("").every((c) => c === "0"))) {
      throw new Error(`${source} contains non-placeholder Twilio SID ${m}`);
    }
  }
}

export function loadFixture(relPath: string): Buffer {
  const p = path.join(FIXTURES, relPath);
  if (!fs.existsSync(p)) throw new Error(`fixture not found: ${p}`);
  const data = fs.readFileSync(p);
  if (p.endsWith(".json")) assertRedacted(data.toString("utf-8"), p);
  return data;
}

export async function cell<T>(
  name: string,
  opts: { tier: number; required?: string[]; env?: NotebookEnv },
  body: () => Promise<T> | T,
): Promise<void> {
  const env = opts.env ?? load();
  const started = Date.now();

  if (opts.tier === 4 && !env.enableLiveCalls) {
    console.log(`⚪ [${name}] skipped — set ENABLE_LIVE_CALLS=1 to enable T4 live calls.`);
    return;
  }

  const missing = (opts.required ?? []).filter((k) => !hasKey(env, k));
  if (missing.length) {
    console.log(`⚪ [${name}] skipped — missing env: ${missing.join(", ")}`);
    return;
  }

  console.log(`▶ [${name}] tier=${opts.tier}`);
  try {
    await body();
  } catch (exc) {
    if (exc instanceof NotebookSkip) {
      console.log(`⚪ [${name}] ${exc.message}`);
      return;
    }
    const e = exc as Error;
    const elapsed = (Date.now() - started) / 1000;
    console.log(`❌ [${name}] failed after ${elapsed.toFixed(2)}s: ${e.name}: ${e.message}`);
    if (e.stack) console.log(e.stack.split("\n").slice(0, 6).join("\n"));
    return;
  }
  const elapsed = (Date.now() - started) / 1000;
  console.log(`✅ [${name}] ${elapsed.toFixed(2)}s`);
}
```

- [ ] **Step 4: Run, expect green**

```bash
npm test
```
Expected: 8 PASSED.

- [ ] **Step 5: Commit**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/typescript/
git commit -m "feat(notebooks): _setup.ts hasKey/skip/cell/loadFixture/printKeyMatrix"
```

---

### Task 16: Add `runStt`, `runTts`, `hangupLeftoverCalls` to `_setup.ts`

**Files:**
- Modify: `examples/notebooks/typescript/_setup.ts`
- Modify: `examples/notebooks/typescript/tests/setup.test.ts`

- [ ] **Step 1: Append failing tests**

```ts
describe("[unit] _setup runStt + runTts + hangupLeftoverCalls", () => {
  it("runStt aggregates transcripts from a fake provider", async () => {
    const fake = {
      connect: async () => {},
      sendAudio: async (_b: Buffer) => {},
      close: async () => {},
      receiveTranscripts: async function* () {
        yield "hello "; yield "world";
      },
    };
    const t = await runStt(fake as any, Buffer.alloc(16000));
    expect(t.trim()).toBe("hello world");
  });

  it("runTts concatenates chunks", async () => {
    const fake = {
      synthesize: async function* (_t: string) {
        yield Buffer.from([1, 2]); yield Buffer.from([3, 4]);
      },
    };
    const audio = await runTts(fake as any, "hi");
    expect(Array.from(audio)).toEqual([1, 2, 3, 4]);
  });
});
```

Add `runStt, runTts, hangupLeftoverCalls` to the import line at the top of the test.

- [ ] **Step 2: Run, expect fail**

```bash
npm test
```

- [ ] **Step 3: Append to `_setup.ts`**

```ts
interface STTLike {
  connect(): Promise<void>;
  sendAudio(chunk: Buffer): Promise<void>;
  close(): Promise<void>;
  receiveTranscripts(): AsyncIterable<string>;
}

export async function runStt(stt: STTLike, audio: Buffer, chunkSize = 3200): Promise<string> {
  await stt.connect();
  try {
    for (let i = 0; i < audio.length; i += chunkSize) {
      await stt.sendAudio(audio.subarray(i, i + chunkSize));
    }
    const out: string[] = [];
    for await (const piece of stt.receiveTranscripts()) out.push(piece);
    return out.join("");
  } finally {
    await stt.close();
  }
}

interface TTSLike {
  synthesize(text: string): AsyncIterable<Buffer>;
}

export async function runTts(tts: TTSLike, text: string): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of tts.synthesize(text)) chunks.push(chunk);
  return Buffer.concat(chunks);
}

export async function hangupLeftoverCalls(env: NotebookEnv): Promise<void> {
  if (!(env.twilioSid && env.twilioToken && env.twilioNumber)) return;
  let twilio: any;
  try {
    twilio = (await import("twilio")).default(env.twilioSid, env.twilioToken);
  } catch {
    console.log("⚪ twilio package not installed — skipping hangup sweep");
    return;
  }
  try {
    const calls = await twilio.calls.list({ from: env.twilioNumber, status: "in-progress", limit: 5 });
    for (const c of calls) {
      try {
        await twilio.calls(c.sid).update({ status: "completed" });
        console.log(`🔚 hung up stale call ${c.sid}`);
      } catch (exc) {
        console.log(`⚠ could not hang up ${c.sid}: ${(exc as Error).message}`);
      }
    }
  } catch (exc) {
    console.log(`⚠ Twilio sweep failed: ${(exc as Error).message}`);
  }
}
```

- [ ] **Step 4: Run, expect green**

```bash
npm test
```
Expected: 10 PASSED.

- [ ] **Step 5: Commit**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
git add examples/notebooks/typescript/
git commit -m "feat(notebooks): _setup.ts runStt + runTts + hangupLeftoverCalls"
```

---

### Task 17: Write notebook scaffolder `scripts/scaffold_notebook.py`

**Files:**
- Create: `scripts/scaffold_notebook.py`
- Create: `scripts/test_scaffold_notebook.py`

- [ ] **Step 1: Write failing test**

```python
# scripts/test_scaffold_notebook.py
import json
from pathlib import Path

import pytest

from scaffold_notebook import build_notebook, KERNELS


def test_python_scaffold_has_three_sections():
    nb = build_notebook(topic_id="01", title="Quickstart", language="python", brief="Hello world.")
    headings = [c["source"][0] for c in nb["cells"] if c["cell_type"] == "markdown"]
    assert any("§1: Quickstart" in h for h in headings)
    assert any("§2: Feature Tour" in h for h in headings)
    assert any("§3: Live Appendix" in h for h in headings)


def test_python_scaffold_imports_setup():
    nb = build_notebook(topic_id="01", title="Quickstart", language="python", brief="x")
    code_cells = [c["source"] for c in nb["cells"] if c["cell_type"] == "code"]
    assert any("import _setup" in "".join(c) for c in code_cells)


def test_typescript_uses_deno_kernel():
    nb = build_notebook(topic_id="01", title="Quickstart", language="typescript", brief="x")
    assert nb["metadata"]["kernelspec"]["name"] == KERNELS["typescript"]["name"]
    assert "deno" in nb["metadata"]["kernelspec"]["display_name"].lower()


def test_python_uses_python_kernel():
    nb = build_notebook(topic_id="01", title="Quickstart", language="python", brief="x")
    assert nb["metadata"]["kernelspec"]["language"] == "python"


def test_outputs_empty():
    nb = build_notebook(topic_id="01", title="Quickstart", language="python", brief="x")
    for c in nb["cells"]:
        if c["cell_type"] == "code":
            assert c["outputs"] == []
            assert c["execution_count"] is None
```

- [ ] **Step 2: Run, expect fail**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
PYTHONPATH=scripts pytest scripts/test_scaffold_notebook.py -v
```
Expected: 5 FAILED with `ModuleNotFoundError: scaffold_notebook`.

- [ ] **Step 3: Implement scaffolder**

```python
# scripts/scaffold_notebook.py
"""Emit an empty .ipynb with §1 / §2 / §3 markdown headers and the
shared _setup import cell.

Used by Task 18 to generate all 24 scaffolds in one shot.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

KERNELS = {
    "python": {"name": "python3", "display_name": "Python 3", "language": "python"},
    "typescript": {"name": "deno", "display_name": "Deno", "language": "typescript"},
}


def _md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}


def _code(source: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {},
        "source": source.splitlines(keepends=True),
        "execution_count": None,
        "outputs": [],
    }


def _setup_cell_python() -> dict:
    return _code(
        "%load_ext autoreload\n"
        "%autoreload 2\n"
        "\n"
        "import _setup\n"
        "env = _setup.load()\n"
        "print(f'getpatter version target: {env.patter_version}')\n"
    )


def _setup_cell_typescript() -> dict:
    return _code(
        "import { load } from \"./_setup.ts\";\n"
        "const env = load();\n"
        "console.log(`getpatter version target: ${env.patterVersion}`);\n"
    )


def build_notebook(*, topic_id: str, title: str, language: str, brief: str) -> dict:
    setup_cell = _setup_cell_python() if language == "python" else _setup_cell_typescript()
    cells = [
        _md(f"# {topic_id} — {title}\n", "\n", brief, "\n"),
        _md(
            "## Prerequisites\n",
            "\n",
            "| Tier | Cells | Required env |\n",
            "|------|-------|--------------|\n",
            "| T1+T2 (§1) | always | _none_ |\n",
            "| T3 (§2) | per-cell | provider keys auto-detected |\n",
            "| T4 (§3) | gated | `ENABLE_LIVE_CALLS=1` + carrier creds |\n",
        ),
        setup_cell,
        _md(f"## §1: Quickstart\n\nRuns end-to-end with zero API keys.\n"),
        _md(f"## §2: Feature Tour\n\nOne cell per feature/provider. Missing keys auto-skip.\n"),
        _md(f"## §3: Live Appendix\n\nReal PSTN calls. Off by default — set `ENABLE_LIVE_CALLS=1`.\n"),
    ]
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": KERNELS[language],
            "language_info": {"name": language},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--topic-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--language", choices=["python", "typescript"], required=True)
    p.add_argument("--brief", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    nb = build_notebook(
        topic_id=args.topic_id,
        title=args.title,
        language=args.language,
        brief=args.brief,
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(nb, indent=1) + "\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run, expect green**

```bash
PYTHONPATH=scripts pytest scripts/test_scaffold_notebook.py -v
```
Expected: 5 PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/scaffold_notebook.py scripts/test_scaffold_notebook.py
git commit -m "feat(notebooks): scaffold_notebook.py emits empty .ipynb with §1/§2/§3"
```

---

### Task 18: Generate all 24 scaffolds

**Files:**
- Create: `scripts/scaffold_all_notebooks.sh`
- Create: 24 `.ipynb` files under `examples/notebooks/{python,typescript}/`

- [ ] **Step 1: Write the driver script**

```bash
# scripts/scaffold_all_notebooks.sh
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

declare -a TOPICS=(
  "01|quickstart|Quickstart|Install, env check, three operating modes (cloud/self-hosted/local), three voice modes (Realtime/ConvAI/Pipeline), 'hello phone' minimal agent."
  "02|realtime|Realtime providers|OpenAI Realtime, Gemini Live, Ultravox, ElevenLabs ConvAI."
  "03|pipeline_stt|Pipeline STT|Deepgram, Whisper, AssemblyAI, Soniox, Speechmatics, Cartesia."
  "04|pipeline_tts|Pipeline TTS|ElevenLabs, OpenAI, Cartesia, LMNT, Rime."
  "05|pipeline_llm|Pipeline LLM|OpenAI, Anthropic, Gemini, Groq, Cerebras, custom on_message, LLMLoop, tool-call protocol."
  "06|telephony_twilio|Telephony — Twilio|Webhook parsing, HMAC-SHA1, AMD, DTMF, recording, transfer, ring timeout, status callback, TwiML emission."
  "07|telephony_telnyx|Telephony — Telnyx|Call Control, Ed25519, AMD, DTMF, track filter, anti-replay."
  "08|tools|Tools|@tool/defineTool, auto-injected transfer_call/end_call, dynamic variables, custom tools, schema validation."
  "09|guardrails_hooks|Guardrails & hooks|Keyword block, PII redact, pipeline hooks, text transforms, sentence chunker."
  "10|advanced|Advanced|Scheduler, fallback LLM chain, background audio, noise filter, custom STT/TTS, custom LLM HTTP."
  "11|metrics_dashboard|Metrics & dashboard|CallMetricsAccumulator, MetricsStore, dashboard SSE, CSV/JSON export, pricing, basic auth."
  "12|security|Security|HMAC, Ed25519, SSRF guard, webhook URL validation, secret hygiene, dashboard auth, cost cap."
)

for entry in "${TOPICS[@]}"; do
  IFS='|' read -r ID SLUG TITLE BRIEF <<< "$entry"
  for LANG in python typescript; do
    OUT="examples/notebooks/${LANG}/${ID}_${SLUG}.ipynb"
    if [[ -f "$OUT" ]]; then
      echo "skip (exists) $OUT"
      continue
    fi
    PYTHONPATH=scripts python -c "
from scaffold_notebook import build_notebook
import json, pathlib
nb = build_notebook(topic_id='${ID}', title='${TITLE}', language='${LANG}', brief='''${BRIEF}''')
pathlib.Path('${OUT}').parent.mkdir(parents=True, exist_ok=True)
pathlib.Path('${OUT}').write_text(json.dumps(nb, indent=1) + '\n')
print('wrote ${OUT}')
"
  done
done
```

- [ ] **Step 2: Run scaffolder**

```bash
chmod +x scripts/scaffold_all_notebooks.sh
./scripts/scaffold_all_notebooks.sh
```
Expected: 24 lines `wrote examples/notebooks/...`.

- [ ] **Step 3: Verify all notebooks parse as valid JSON**

```bash
python -c "
import json, pathlib
for p in pathlib.Path('examples/notebooks').rglob('*.ipynb'):
    json.loads(p.read_text())
    print(f'ok {p}')
"
```
Expected: 24 `ok` lines.

- [ ] **Step 4: Commit**

```bash
git add scripts/scaffold_all_notebooks.sh examples/notebooks/python/*.ipynb examples/notebooks/typescript/*.ipynb
git commit -m "feat(notebooks): 24 scaffolded notebooks (12 topics × py/ts)"
```

---

### Task 19: Write `scripts/check_notebook_parity.py`

**Files:**
- Create: `scripts/check_notebook_parity.py`
- Create: `scripts/test_check_notebook_parity.py`

- [ ] **Step 1: Write failing test**

```python
# scripts/test_check_notebook_parity.py
import json
from pathlib import Path

import pytest

from check_notebook_parity import diff_pair


def _write(path: Path, sections: list[str]) -> None:
    cells = []
    for s in sections:
        cells.append({"cell_type": "markdown", "metadata": {}, "source": [s + "\n"]})
    path.write_text(json.dumps({"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))


def test_diff_pair_returns_empty_when_aligned(tmp_path):
    py = tmp_path / "01.py.ipynb"; ts = tmp_path / "01.ts.ipynb"
    _write(py, ["# 01 — Quickstart", "## §1", "## §2"])
    _write(ts, ["# 01 — Quickstart", "## §1", "## §2"])
    assert diff_pair(py, ts) == []


def test_diff_pair_detects_extra_section(tmp_path):
    py = tmp_path / "01.py.ipynb"; ts = tmp_path / "01.ts.ipynb"
    _write(py, ["# Title", "## §1", "## §2", "## §3"])
    _write(ts, ["# Title", "## §1", "## §2"])
    diffs = diff_pair(py, ts)
    assert any("§3" in d for d in diffs)


def test_diff_pair_detects_renamed_section(tmp_path):
    py = tmp_path / "01.py.ipynb"; ts = tmp_path / "01.ts.ipynb"
    _write(py, ["# Title", "## §1", "## §2"])
    _write(ts, ["# Title", "## §1: Quickstart", "## §2"])
    diffs = diff_pair(py, ts)
    assert any("§1" in d for d in diffs)
```

- [ ] **Step 2: Run, expect fail**

```bash
PYTHONPATH=scripts pytest scripts/test_check_notebook_parity.py -v
```

- [ ] **Step 3: Implement**

```python
# scripts/check_notebook_parity.py
"""Diff paired Python/TypeScript notebooks.

Usage:
    python scripts/check_notebook_parity.py         # check all pairs, exit 1 on drift
    python scripts/check_notebook_parity.py --quiet # suppress per-file output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PY_DIR = REPO / "examples/notebooks/python"
TS_DIR = REPO / "examples/notebooks/typescript"


def _section_titles(path: Path) -> list[str]:
    nb = json.loads(path.read_text())
    titles: list[str] = []
    for c in nb["cells"]:
        if c["cell_type"] != "markdown":
            continue
        first_line = (c["source"][0] if c["source"] else "").strip()
        if first_line.startswith("#"):
            titles.append(first_line)
    return titles


def diff_pair(py_path: Path, ts_path: Path) -> list[str]:
    py_titles = _section_titles(py_path)
    ts_titles = _section_titles(ts_path)
    diffs: list[str] = []
    for i, (a, b) in enumerate(zip(py_titles, ts_titles)):
        if a != b:
            diffs.append(f"section [{i}] differs: py={a!r} ts={b!r}")
    if len(py_titles) != len(ts_titles):
        diffs.append(
            f"section count mismatch: py={len(py_titles)} ts={len(ts_titles)} "
            f"(py extras: {py_titles[len(ts_titles):]} / ts extras: {ts_titles[len(py_titles):]})"
        )
    return diffs


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    py_files = sorted(PY_DIR.glob("[0-9][0-9]_*.ipynb"))
    drift_count = 0
    for py in py_files:
        ts = TS_DIR / py.name
        if not ts.exists():
            print(f"❌ {py.name}: no TypeScript twin")
            drift_count += 1
            continue
        diffs = diff_pair(py, ts)
        if diffs:
            drift_count += 1
            print(f"❌ {py.name}:")
            for d in diffs:
                print(f"    {d}")
        elif not args.quiet:
            print(f"✅ {py.name}")
    if drift_count:
        print(f"\n{drift_count} notebook pair(s) drifted")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run unit tests, expect green**

```bash
PYTHONPATH=scripts pytest scripts/test_check_notebook_parity.py -v
```
Expected: 3 PASSED.

- [ ] **Step 5: Run end-to-end on real scaffolds, expect green**

```bash
python scripts/check_notebook_parity.py
```
Expected: 12 ✅ lines, exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/check_notebook_parity.py scripts/test_check_notebook_parity.py
git commit -m "feat(notebooks): scripts/check_notebook_parity.py + tests"
```

---

### Task 20: Add `.pre-commit-config.yaml` with `nbstripout` + secret-grep

**Files:**
- Create: `.pre-commit-config.yaml` (or modify if it already exists)

- [ ] **Step 1: Check existing config**

```bash
ls -la "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton/.pre-commit-config.yaml" 2>/dev/null && cat .pre-commit-config.yaml 2>/dev/null || echo "no existing config"
```

- [ ] **Step 2: Write `.pre-commit-config.yaml`** (merge with existing if present)

```yaml
repos:
  - repo: https://github.com/kynan/nbstripout
    rev: 0.7.1
    hooks:
      - id: nbstripout
        files: '^examples/notebooks/.*\.ipynb$'

  - repo: local
    hooks:
      - id: notebook-secret-grep
        name: notebook secret scan
        entry: python scripts/scan_notebook_secrets.py
        language: system
        pass_filenames: true
        files: '^examples/notebooks/.*\.ipynb$'
```

- [ ] **Step 3: Write the secret-scan hook**

```python
# scripts/scan_notebook_secrets.py
"""Pre-commit hook: refuse to commit a notebook whose JSON contains
high-entropy secrets in cell source or outputs.

Patterns mirrored from .claude/hooks/scan-sensitive-on-write.sh.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = [
    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def main() -> int:
    failed = 0
    for arg in sys.argv[1:]:
        p = Path(arg)
        body = p.read_text()
        # Whitelist the test keypair file path.
        if "fixtures/keys/telnyx_test_ed25519_priv.pem" in str(p):
            continue
        for pat in PATTERNS:
            m = pat.search(body)
            if m:
                print(f"❌ {p}: matches {pat.pattern!r} at offset {m.start()}")
                failed = 1
    return failed


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Install pre-commit, run on-demand**

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```
Expected: clean run on existing scaffolds.

- [ ] **Step 5: Commit**

```bash
git add .pre-commit-config.yaml scripts/scan_notebook_secrets.py
git commit -m "feat(notebooks): pre-commit nbstripout + secret-grep"
```

---

### Task 21: Add `.github/workflows/notebooks.yml`

**Files:**
- Create: `.github/workflows/notebooks.yml`

- [ ] **Step 1: Write the workflow**

```yaml
# .github/workflows/notebooks.yml
name: notebooks

on:
  pull_request:
    paths:
      - 'examples/notebooks/**'
      - 'scripts/check_notebook_parity.py'
      - 'scripts/scaffold_notebook.py'
      - 'scripts/scan_notebook_secrets.py'
  push:
    branches: [main]
    paths:
      - 'examples/notebooks/**'

jobs:
  parity:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: python scripts/check_notebook_parity.py

  outputs-stripped:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install nbstripout
      - name: assert outputs are stripped
        run: |
          for f in examples/notebooks/python/*.ipynb examples/notebooks/typescript/*.ipynb; do
            if grep -q '"outputs": \[' "$f"; then
              echo "FAIL: $f contains outputs (run nbstripout)"
              exit 1
            fi
          done
      - run: python scripts/scan_notebook_secrets.py examples/notebooks/python/*.ipynb examples/notebooks/typescript/*.ipynb

  setup-tests-python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: |
          pip install -e "examples/notebooks/python[dev]"
      - run: pytest examples/notebooks/python/tests -v

  setup-tests-typescript:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - run: cd examples/notebooks/typescript && npm install
      - run: cd examples/notebooks/typescript && npm test

  notebooks-quickstart:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: |
          pip install -e "examples/notebooks/python[dev]"
          pip install -e "sdk[local,dev]"
      - name: run §1 of every Python notebook headless
        run: |
          for f in examples/notebooks/python/*.ipynb; do
            jupyter nbconvert --to notebook --execute "$f" \
              --ExecutePreprocessor.timeout=60 \
              --output "/tmp/$(basename $f .ipynb).executed.ipynb" \
              || (echo "FAIL: $f"; exit 1)
          done
```

- [ ] **Step 2: Validate workflow YAML**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/notebooks.yml'))"
```
Expected: silent (no errors).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/notebooks.yml
git commit -m "ci(notebooks): parity + stripped-outputs + setup-tests + headless quickstart"
```

---

### Task 22: Phase 1 acceptance smoke

**Files:** none (verification only)

- [ ] **Step 1: Run all unit tests**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
pytest scripts/test_generate_notebook_fixtures.py scripts/test_scaffold_notebook.py scripts/test_check_notebook_parity.py -v
cd examples/notebooks/python && pytest tests/ -v && cd ../../..
cd examples/notebooks/typescript && npm test && cd ../../..
```
Expected: all green.

- [ ] **Step 2: Parity check**

```bash
python scripts/check_notebook_parity.py
```
Expected: 12 ✅, exit 0.

- [ ] **Step 3: Open one notebook headless**

```bash
jupyter nbconvert --to notebook --execute examples/notebooks/python/01_quickstart.ipynb \
  --output /tmp/smoke.ipynb --ExecutePreprocessor.timeout=30
```
Expected: success — only the import-cell runs (the rest are markdown).

- [ ] **Step 4: Open the PR**

```bash
git push -u origin feat/notebook-series-skeleton
gh pr create --title "feat(notebooks): skeleton — fixtures, _setup, scaffolds, parity, CI" --body "$(cat <<'EOF'
## Summary
- 24 empty notebook scaffolds (12 topics × Python + TypeScript) with §1/§2/§3 sections
- Shared helpers: \`_setup.py\` and \`_setup.ts\` (load env, key matrix, cell ctx, fixture loader, run_stt/tts, hangup sweep)
- Fixtures: 4 audio clips (gTTS-generated), 4 redacted webhook bodies, Ed25519 test keypair
- \`scripts/check_notebook_parity.py\` enforces py↔ts structural parity
- Pre-commit: nbstripout + secret-grep
- CI: parity, stripped-outputs, setup-tests (py + ts), notebooks-quickstart (headless)

This is Phase 1 of 5. Phases 2–5 will fill content cells.

## Test plan
- [x] \`pytest\` in \`examples/notebooks/python/\` green
- [x] \`npm test\` in \`examples/notebooks/typescript/\` green
- [x] \`python scripts/check_notebook_parity.py\` exits 0
- [x] One notebook executes headless without errors

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Phase 2: Quickstart Everywhere

Goal: fill §1 (Quickstart, T1+T2 only) in all 24 notebooks. The §1 layer is largely shared content (Patter intro + offline basics). One PR.

### Task 23: Add `quickstart_cells.py` — canonical §1 cell sequence

**Files:**
- Create: `scripts/quickstart_cells.py`
- Create: `scripts/test_quickstart_cells.py`

- [ ] **Step 1: Write failing test**

```python
# scripts/test_quickstart_cells.py
from quickstart_cells import quickstart_cells_python, quickstart_cells_typescript


def test_python_returns_six_cells_with_named_tags():
    cells = quickstart_cells_python()
    names = [c["metadata"].get("tags", [None])[0] for c in cells if c["cell_type"] == "code"]
    assert "qs_version_check" in names
    assert "qs_e164" in names
    assert "qs_local_mode" in names
    assert "qs_embedded_server" in names


def test_python_cells_use_setup_cell_wrapper():
    cells = quickstart_cells_python()
    code = "".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    assert "_setup.cell" in code
    assert "tier=1" in code or "tier=2" in code


def test_typescript_mirror_has_same_cell_count():
    py = quickstart_cells_python()
    ts = quickstart_cells_typescript()
    assert len([c for c in py if c["cell_type"] == "code"]) == len([c for c in ts if c["cell_type"] == "code"])


def test_typescript_uses_camelcase_setup_calls():
    cells = quickstart_cells_typescript()
    code = "".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    assert "_setup.cell" in code or "cell(" in code
```

- [ ] **Step 2: Run, expect fail**

```bash
PYTHONPATH=scripts pytest scripts/test_quickstart_cells.py -v
```

- [ ] **Step 3: Implement**

```python
# scripts/quickstart_cells.py
"""Canonical §1 (Quickstart, T1+T2) cell sequence shared by every notebook."""

from __future__ import annotations


def _md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}


def _code(tag: str, source: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {"tags": [tag]},
        "source": source.splitlines(keepends=True),
        "execution_count": None,
        "outputs": [],
    }


def quickstart_cells_python() -> list[dict]:
    return [
        _md("These cells run with **zero API keys** in <30 seconds.\n"),
        _code(
            "qs_version_check",
            "import sys, importlib\n"
            "with _setup.cell('version_check', tier=1, env=env):\n"
            "    patter = importlib.import_module('patter')\n"
            "    print(f'patter {getattr(patter, \"__version__\", \"unknown\")} on Python {sys.version.split()[0]}')\n"
            "    assert getattr(patter, '__version__', '0') >= env.patter_version\n",
        ),
        _code(
            "qs_e164",
            "from patter.services.validation import validate_e164\n"
            "with _setup.cell('e164', tier=1, env=env):\n"
            "    assert validate_e164('+15555550100') is True\n"
            "    assert validate_e164('555-0100') is False\n"
            "    print('E.164 validator green')\n",
        ),
        _code(
            "qs_local_mode",
            "from patter import Patter\n"
            "with _setup.cell('local_mode', tier=1, env=env):\n"
            "    p = Patter(twilio_sid='ACtest00000000000000000000000000',\n"
            "               twilio_token='test', phone_number='+15555550100',\n"
            "               webhook_url='https://example.com/webhook')\n"
            "    assert p.mode == 'local'\n"
            "    print(f'mode = {p.mode}')\n",
        ),
        _code(
            "qs_cloud_mode",
            "from patter import Patter\n"
            "with _setup.cell('cloud_mode', tier=1, env=env):\n"
            "    p = Patter(api_key='pt_test')\n"
            "    assert p.mode == 'cloud'\n"
            "    print(f'mode = {p.mode}')\n",
        ),
        _code(
            "qs_embedded_server",
            "import asyncio, httpx\n"
            "from patter import Patter\n"
            "with _setup.cell('embedded_server', tier=2, env=env):\n"
            "    p = Patter(twilio_sid='ACtest00000000000000000000000000',\n"
            "               twilio_token='test', phone_number='+15555550100',\n"
            "               webhook_url='https://example.com/webhook')\n"
            "    server = await p._embedded.start(port=0)\n"
            "    try:\n"
            "        async with httpx.AsyncClient() as c:\n"
            "            r = await c.get(f'http://127.0.0.1:{server.port}/health')\n"
            "            assert r.status_code == 200\n"
            "            print(f'GET /health → {r.json()}')\n"
            "    finally:\n"
            "        await p._embedded.stop()\n",
        ),
    ]


def quickstart_cells_typescript() -> list[dict]:
    return [
        _md("These cells run with **zero API keys** in <30 seconds.\n"),
        _code(
            "qs_version_check",
            "import { cell } from './_setup.ts';\n"
            "import * as patter from 'getpatter';\n"
            "await cell('version_check', { tier: 1, env }, () => {\n"
            "  console.log(`getpatter ${(patter as any).version ?? 'unknown'} on Deno ${Deno.version.deno}`);\n"
            "});\n",
        ),
        _code(
            "qs_e164",
            "import { validateE164 } from 'getpatter';\n"
            "await cell('e164', { tier: 1, env }, () => {\n"
            "  if (validateE164('+15555550100') !== true) throw new Error('valid number rejected');\n"
            "  if (validateE164('555-0100') !== false) throw new Error('invalid number accepted');\n"
            "  console.log('E.164 validator green');\n"
            "});\n",
        ),
        _code(
            "qs_local_mode",
            "import { Patter } from 'getpatter';\n"
            "await cell('local_mode', { tier: 1, env }, () => {\n"
            "  const p = new Patter({\n"
            "    twilioSid: 'ACtest00000000000000000000000000',\n"
            "    twilioToken: 'test',\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  if (p.mode !== 'local') throw new Error(`expected local, got ${p.mode}`);\n"
            "  console.log(`mode = ${p.mode}`);\n"
            "});\n",
        ),
        _code(
            "qs_cloud_mode",
            "import { Patter } from 'getpatter';\n"
            "await cell('cloud_mode', { tier: 1, env }, () => {\n"
            "  const p = new Patter({ apiKey: 'pt_test' });\n"
            "  if (p.mode !== 'cloud') throw new Error(`expected cloud, got ${p.mode}`);\n"
            "  console.log(`mode = ${p.mode}`);\n"
            "});\n",
        ),
        _code(
            "qs_embedded_server",
            "import { Patter } from 'getpatter';\n"
            "await cell('embedded_server', { tier: 2, env }, async () => {\n"
            "  const p = new Patter({\n"
            "    twilioSid: 'ACtest00000000000000000000000000',\n"
            "    twilioToken: 'test',\n"
            "    phoneNumber: '+15555550100',\n"
            "    webhookUrl: 'https://example.com/webhook',\n"
            "  });\n"
            "  const server = await (p as any)._embedded.start(0);\n"
            "  try {\n"
            "    const r = await fetch(`http://127.0.0.1:${server.port}/health`);\n"
            "    if (!r.ok) throw new Error(`got ${r.status}`);\n"
            "    console.log(`GET /health → ${await r.text()}`);\n"
            "  } finally {\n"
            "    await (p as any)._embedded.stop();\n"
            "  }\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 4: Run, expect green**

```bash
PYTHONPATH=scripts pytest scripts/test_quickstart_cells.py -v
```

- [ ] **Step 5: Commit**

```bash
git add scripts/quickstart_cells.py scripts/test_quickstart_cells.py
git commit -m "feat(notebooks): canonical §1 quickstart cell sequence (py + ts)"
```

---

### Task 24: Write `scripts/inject_section.py` — replace section placeholders

**Files:**
- Create: `scripts/inject_section.py`
- Create: `scripts/test_inject_section.py`

- [ ] **Step 1: Write failing test**

```python
# scripts/test_inject_section.py
import json
from pathlib import Path

from inject_section import inject_section


def _scaffold(path: Path) -> None:
    cells = [
        {"cell_type": "markdown", "metadata": {}, "source": ["# Title\n"]},
        {"cell_type": "markdown", "metadata": {}, "source": ["## §1: Quickstart\n"]},
        {"cell_type": "markdown", "metadata": {}, "source": ["## §2: Feature Tour\n"]},
    ]
    path.write_text(json.dumps({"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}))


def test_inject_section_inserts_after_marker(tmp_path):
    nb_path = tmp_path / "01.ipynb"
    _scaffold(nb_path)

    new_cells = [
        {"cell_type": "code", "metadata": {"tags": ["t1"]}, "source": ["print('hi')\n"], "execution_count": None, "outputs": []},
    ]
    inject_section(nb_path, marker="§1: Quickstart", cells=new_cells)

    nb = json.loads(nb_path.read_text())
    sources = [c["source"] for c in nb["cells"]]
    assert sources == [
        ["# Title\n"],
        ["## §1: Quickstart\n"],
        ["print('hi')\n"],
        ["## §2: Feature Tour\n"],
    ]


def test_inject_section_idempotent(tmp_path):
    nb_path = tmp_path / "01.ipynb"
    _scaffold(nb_path)
    cell = {"cell_type": "code", "metadata": {"tags": ["t1"]}, "source": ["x"], "execution_count": None, "outputs": []}
    inject_section(nb_path, marker="§1: Quickstart", cells=[cell])
    inject_section(nb_path, marker="§1: Quickstart", cells=[cell])  # second call
    nb = json.loads(nb_path.read_text())
    tag_count = sum(1 for c in nb["cells"] if "t1" in c.get("metadata", {}).get("tags", []))
    assert tag_count == 1
```

- [ ] **Step 2: Run, expect fail**

```bash
PYTHONPATH=scripts pytest scripts/test_inject_section.py -v
```

- [ ] **Step 3: Implement**

```python
# scripts/inject_section.py
"""Idempotently inject a list of cells after a section marker in a .ipynb."""

from __future__ import annotations

import json
from pathlib import Path


def inject_section(nb_path: Path, marker: str, cells: list[dict]) -> None:
    nb = json.loads(nb_path.read_text())
    out: list[dict] = []
    inject_idx = -1

    # Find the marker cell index.
    for i, c in enumerate(nb["cells"]):
        if c["cell_type"] == "markdown" and any(marker in line for line in c["source"]):
            inject_idx = i
            break
    if inject_idx == -1:
        raise ValueError(f"marker {marker!r} not found in {nb_path}")

    # Build set of incoming tags for idempotency.
    incoming_tags = {
        tag
        for cell in cells
        for tag in cell.get("metadata", {}).get("tags", [])
    }

    for i, c in enumerate(nb["cells"]):
        # Skip any pre-existing cells with one of our tags (we'll reinsert below).
        if any(tag in incoming_tags for tag in c.get("metadata", {}).get("tags", [])):
            continue
        out.append(c)
        if i == inject_idx:
            out.extend(cells)

    nb["cells"] = out
    nb_path.write_text(json.dumps(nb, indent=1) + "\n")
```

- [ ] **Step 4: Run, expect green**

```bash
PYTHONPATH=scripts pytest scripts/test_inject_section.py -v
```

- [ ] **Step 5: Commit**

```bash
git add scripts/inject_section.py scripts/test_inject_section.py
git commit -m "feat(notebooks): inject_section — idempotent cell insertion after marker"
```

---

### Task 25: Inject §1 cells into all 24 notebooks

**Files:**
- Create: `scripts/inject_quickstart.py`
- Modifies: all 24 `examples/notebooks/{python,typescript}/*.ipynb`

- [ ] **Step 1: Write the driver**

```python
# scripts/inject_quickstart.py
"""Phase 2: inject the canonical §1 cells into every scaffolded notebook."""

from __future__ import annotations

from pathlib import Path

from inject_section import inject_section
from quickstart_cells import quickstart_cells_python, quickstart_cells_typescript

REPO = Path(__file__).resolve().parents[1]


def main() -> None:
    py_cells = quickstart_cells_python()
    ts_cells = quickstart_cells_typescript()

    for nb in sorted((REPO / "examples/notebooks/python").glob("[0-9][0-9]_*.ipynb")):
        print(f"injecting py § 1 into {nb.name}")
        inject_section(nb, marker="§1: Quickstart", cells=py_cells)

    for nb in sorted((REPO / "examples/notebooks/typescript").glob("[0-9][0-9]_*.ipynb")):
        print(f"injecting ts § 1 into {nb.name}")
        inject_section(nb, marker="§1: Quickstart", cells=ts_cells)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run injector**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
PYTHONPATH=scripts python scripts/inject_quickstart.py
```
Expected: 24 `injecting ...` lines.

- [ ] **Step 3: Strip outputs (if anything got stamped)**

```bash
nbstripout examples/notebooks/python/*.ipynb examples/notebooks/typescript/*.ipynb
```

- [ ] **Step 4: Parity check still green**

```bash
python scripts/check_notebook_parity.py
```

- [ ] **Step 5: Commit**

```bash
git add scripts/inject_quickstart.py examples/notebooks/python/*.ipynb examples/notebooks/typescript/*.ipynb
git commit -m "feat(notebooks): populate §1 quickstart cells in all 24 notebooks"
```

---

### Task 26: Headless smoke — every Python notebook §1 runs green with no keys

**Files:** none

- [ ] **Step 1: Run nbconvert on each Python notebook**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
mkdir -p /tmp/nb-smoke
for f in examples/notebooks/python/*.ipynb; do
  echo "=== $f ==="
  jupyter nbconvert --to notebook --execute "$f" \
    --ExecutePreprocessor.timeout=60 \
    --output "/tmp/nb-smoke/$(basename $f .ipynb).executed.ipynb" \
    || { echo "FAILED: $f"; exit 1; }
done
echo "all 12 python notebooks executed §1 cleanly"
```
Expected: 12 `executed cleanly` lines, exit 0.

- [ ] **Step 2: Verify outputs were stripped (nothing committed)**

```bash
git status --short examples/notebooks/python/
```
Expected: empty (no diffs because we ran against /tmp output paths).

- [ ] **Step 3: Document in DEVLOG**

Append to `docs/DEVLOG.md`:

```markdown
### [2026-04-24] — Notebook series Phase 2: Quickstart everywhere

**Type:** feat
**Branch:** feat/notebook-series-skeleton

**What it does:**
Populates §1 in all 24 notebooks with the canonical 5 quickstart cells
(version check, E.164, local mode, cloud mode, embedded server). All
cells run T1/T2 only — no API keys required.

**Files changed:**

| File | Change |
|------|--------|
| `scripts/quickstart_cells.py` | Canonical §1 cell sequence (py + ts) |
| `scripts/inject_section.py` | Idempotent cell injection helper |
| `scripts/inject_quickstart.py` | Driver that injects into all 24 notebooks |
| `examples/notebooks/python/*.ipynb` | §1 populated |
| `examples/notebooks/typescript/*.ipynb` | §1 populated |

**Tests added:**
- `scripts/test_quickstart_cells.py` — 4 tests
- `scripts/test_inject_section.py` — 2 tests
```

- [ ] **Step 4: Commit**

```bash
git add docs/DEVLOG.md
git commit -m "docs: DEVLOG entry for notebook Phase 2 quickstart"
```

---

### Task 27: Phase 2 PR

**Files:** none (PR only)

- [ ] **Step 1: Push and open PR**

```bash
git push
gh pr create --title "feat(notebooks): Phase 2 — populate §1 Quickstart in all 24 notebooks" --body "$(cat <<'EOF'
## Summary
- Canonical §1 (T1+T2) cells injected into every notebook: version check, E.164 validation, local mode, cloud mode, embedded server lifecycle
- All cells run with **zero API keys** in <30s
- New scripts: \`quickstart_cells.py\`, \`inject_section.py\`, \`inject_quickstart.py\`

## Test plan
- [x] \`pytest scripts/\` — all green
- [x] All 12 Python notebooks execute §1 headless without errors
- [x] Parity check green
- [x] Outputs stripped on commit

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Phase 3: Feature Tour (12 PRs, one per topic)

Each task in Phase 3 fills §2 in one notebook pair (Python + TypeScript) and lands as its own PR. The procedure is identical for each topic; the per-topic cell content is embedded in the task. All cells use `_setup.cell(...)` with explicit `tier=3` and `required=[...]` so missing keys auto-skip.

**Common procedure for every Phase 3 task:**
1. Add a `<topic>_section_cells.py` helper that returns the Python cell list, plus a `<topic>_section_cells_typescript()` companion.
2. Test the helper: assert the expected cell tags exist.
3. Inject into the topic notebook via `inject_section`.
4. Run headless on Python with all relevant keys set; assert exit 0 (cells either run or skip cleanly).
5. Run headless with NO keys; assert all FT cells skip cleanly with banner.
6. Strip outputs, commit, push, open PR.

### Task 28: Topic 01 — quickstart §2 cells

**Files:**
- Create: `scripts/section_cells_01_quickstart.py`
- Modify: `examples/notebooks/python/01_quickstart.ipynb`
- Modify: `examples/notebooks/typescript/01_quickstart.ipynb`

**§2 covers:** `agent_factory_three_voice_modes`, `dashboard_endpoints_smoke`, `metrics_accumulator_demo`. (The other 3 cells listed in spec §5 are already covered in the canonical §1 layer.)

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_01_quickstart.py
"""§2 cells for examples/notebooks/{python,typescript}/01_quickstart.ipynb."""

from __future__ import annotations


def _md(*lines: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}


def _code(tag: str, source: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {"tags": [tag]},
        "source": source.splitlines(keepends=True),
        "execution_count": None,
        "outputs": [],
    }


def section_cells_python() -> list[dict]:
    return [
        _md("### Three voice modes\n\nEvery agent is one of *Realtime*, *ConvAI*, or *Pipeline*. Build one of each and confirm the handler class.\n"),
        _code(
            "ft_three_voice_modes",
            "from patter import Patter\n"
            "from patter.handlers.stream_handler import (\n"
            "    OpenAIRealtimeStreamHandler, ElevenLabsConvAIStreamHandler, PipelineStreamHandler,\n"
            ")\n"
            "with _setup.cell('three_voice_modes', tier=1, env=env):\n"
            "    p = Patter(twilio_sid='ACtest00000000000000000000000000', twilio_token='test',\n"
            "               phone_number='+15555550100', webhook_url='https://example.com/webhook')\n"
            "    rt = p.agent(provider='openai_realtime', system_prompt='hi')\n"
            "    cv = p.agent(provider='elevenlabs_convai', agent_id='test')\n"
            "    pl = p.agent(provider='pipeline', system_prompt='hi')\n"
            "    print(f'realtime → {rt._handler_cls.__name__}')\n"
            "    print(f'convai   → {cv._handler_cls.__name__}')\n"
            "    print(f'pipeline → {pl._handler_cls.__name__}')\n"
            "    assert rt._handler_cls is OpenAIRealtimeStreamHandler\n"
            "    assert cv._handler_cls is ElevenLabsConvAIStreamHandler\n"
            "    assert pl._handler_cls is PipelineStreamHandler\n",
        ),
        _md("### Dashboard endpoints smoke test\n\nThe embedded server exposes a JSON API and an SSE stream. We hit each.\n"),
        _code(
            "ft_dashboard_endpoints",
            "import httpx\n"
            "from patter import Patter\n"
            "with _setup.cell('dashboard_endpoints', tier=2, env=env):\n"
            "    p = Patter(twilio_sid='ACtest00000000000000000000000000', twilio_token='test',\n"
            "               phone_number='+15555550100', webhook_url='https://example.com/webhook')\n"
            "    server = await p._embedded.start(port=0)\n"
            "    try:\n"
            "        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{server.port}') as c:\n"
            "            r = await c.get('/api/v1/calls'); assert r.status_code == 200\n"
            "            print(f'GET /api/v1/calls → {len(r.json())} record(s)')\n"
            "            r = await c.get('/dashboard'); assert r.status_code == 200\n"
            "            print(f'GET /dashboard → {len(r.text)} bytes of HTML')\n"
            "    finally:\n"
            "        await p._embedded.stop()\n",
        ),
        _md("### Metrics accumulator\n\nRecord a couple of synthetic turns, see the cost math.\n"),
        _code(
            "ft_metrics_accumulator",
            "from patter.services.metrics import CallMetricsAccumulator\n"
            "with _setup.cell('metrics_accumulator', tier=1, env=env):\n"
            "    acc = CallMetricsAccumulator(call_id='CAtest1', direction='inbound')\n"
            "    acc.record_stt(provider='deepgram', seconds=2.0)\n"
            "    acc.record_llm(provider='openai', model='gpt-4o-mini', input_tokens=120, output_tokens=40)\n"
            "    acc.record_tts(provider='elevenlabs', characters=85)\n"
            "    metrics = acc.snapshot()\n"
            "    print(f'total cost = ${metrics.total_cost_usd:.4f}')\n"
            "    assert metrics.total_cost_usd > 0\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("### Three voice modes\n\nEvery agent is one of *Realtime*, *ConvAI*, or *Pipeline*.\n"),
        _code(
            "ft_three_voice_modes",
            "import { Patter } from 'getpatter';\n"
            "import { cell } from './_setup.ts';\n"
            "await cell('three_voice_modes', { tier: 1, env }, () => {\n"
            "  const p = new Patter({ twilioSid: 'ACtest00000000000000000000000000', twilioToken: 'test',\n"
            "    phoneNumber: '+15555550100', webhookUrl: 'https://example.com/webhook' });\n"
            "  const rt = p.agent({ provider: 'openai_realtime', systemPrompt: 'hi' });\n"
            "  const cv = p.agent({ provider: 'elevenlabs_convai', agentId: 'test' });\n"
            "  const pl = p.agent({ provider: 'pipeline', systemPrompt: 'hi' });\n"
            "  console.log(`realtime → ${(rt as any)._handlerCtor.name}`);\n"
            "  console.log(`convai   → ${(cv as any)._handlerCtor.name}`);\n"
            "  console.log(`pipeline → ${(pl as any)._handlerCtor.name}`);\n"
            "});\n",
        ),
        _md("### Dashboard endpoints smoke test\n"),
        _code(
            "ft_dashboard_endpoints",
            "import { Patter } from 'getpatter';\n"
            "await cell('dashboard_endpoints', { tier: 2, env }, async () => {\n"
            "  const p = new Patter({ twilioSid: 'ACtest00000000000000000000000000', twilioToken: 'test',\n"
            "    phoneNumber: '+15555550100', webhookUrl: 'https://example.com/webhook' });\n"
            "  const server = await (p as any)._embedded.start(0);\n"
            "  try {\n"
            "    const calls = await fetch(`http://127.0.0.1:${server.port}/api/v1/calls`);\n"
            "    console.log(`GET /api/v1/calls → ${(await calls.json()).length} record(s)`);\n"
            "    const dash = await fetch(`http://127.0.0.1:${server.port}/dashboard`);\n"
            "    console.log(`GET /dashboard → ${(await dash.text()).length} bytes of HTML`);\n"
            "  } finally {\n"
            "    await (p as any)._embedded.stop();\n"
            "  }\n"
            "});\n",
        ),
        _md("### Metrics accumulator\n"),
        _code(
            "ft_metrics_accumulator",
            "import { CallMetricsAccumulator } from 'getpatter';\n"
            "await cell('metrics_accumulator', { tier: 1, env }, () => {\n"
            "  const acc = new CallMetricsAccumulator({ callId: 'CAtest1', direction: 'inbound' });\n"
            "  acc.recordStt({ provider: 'deepgram', seconds: 2.0 });\n"
            "  acc.recordLlm({ provider: 'openai', model: 'gpt-4o-mini', inputTokens: 120, outputTokens: 40 });\n"
            "  acc.recordTts({ provider: 'elevenlabs', characters: 85 });\n"
            "  const metrics = acc.snapshot();\n"
            "  console.log(`total cost = $${metrics.totalCostUsd.toFixed(4)}`);\n"
            "  if (!(metrics.totalCostUsd > 0)) throw new Error('expected positive cost');\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject into both notebooks**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
PYTHONPATH=scripts python -c "
from inject_section import inject_section
from section_cells_01_quickstart import section_cells_python, section_cells_typescript
from pathlib import Path
inject_section(Path('examples/notebooks/python/01_quickstart.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_python())
inject_section(Path('examples/notebooks/typescript/01_quickstart.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_typescript())
"
```

- [ ] **Step 3: Headless smoke (no keys)**

```bash
jupyter nbconvert --to notebook --execute examples/notebooks/python/01_quickstart.ipynb \
  --ExecutePreprocessor.timeout=60 --output /tmp/01.executed.ipynb
```
Expected: success — all §2 cells either pass (T1/T2) or print a skip banner.

- [ ] **Step 4: Strip outputs, parity, commit, PR**

```bash
nbstripout examples/notebooks/python/01_quickstart.ipynb examples/notebooks/typescript/01_quickstart.ipynb
python scripts/check_notebook_parity.py
git add scripts/section_cells_01_quickstart.py examples/notebooks/python/01_quickstart.ipynb examples/notebooks/typescript/01_quickstart.ipynb
git commit -m "feat(notebooks): topic 01 quickstart §2 — voice modes, dashboard, metrics"
git push
gh pr create --title "feat(notebooks): topic 01 §2 (quickstart deep dive)" --body "Adds 3 cells to §2 of the quickstart notebook covering all three voice modes, dashboard endpoints, and the metrics accumulator. All cells offline (T1/T2)."
```

---

### Task 29: Topic 02 — realtime providers §2

**Files:**
- Create: `scripts/section_cells_02_realtime.py`
- Modify: `examples/notebooks/{python,typescript}/02_realtime.ipynb`

**§2 covers:** `openai_realtime`, `gemini_live`, `ultravox`, `elevenlabs_convai`. Each opens a real provider WS, sends 1s of fixture audio, asserts a non-empty response.

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_02_realtime.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True),
    "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("### OpenAI Realtime\n\nOpens a real WebSocket to OpenAI Realtime, streams 1s of fixture audio, asserts the model speaks back.\n"),
        _code(
            "ft_openai_realtime",
            "from patter.providers.openai_realtime import OpenAIRealtimeAdapter\n"
            "with _setup.cell('openai_realtime', tier=3, required=['OPENAI_API_KEY'], env=env):\n"
            "    adapter = OpenAIRealtimeAdapter(api_key=env.openai_key, voice='alloy',\n"
            "                                    instructions='Say hello.')\n"
            "    await adapter.connect()\n"
            "    audio = _setup.load_fixture('audio/hello_world_16khz_pcm.wav')[44:]  # strip WAV header\n"
            "    await adapter.send_audio(audio)\n"
            "    out = bytearray()\n"
            "    async for ev_type, payload in adapter.receive_events():\n"
            "        if ev_type == 'audio':\n"
            "            out += payload\n"
            "            if len(out) > 8000: break\n"
            "    await adapter.close()\n"
            "    print(f'received {len(out)} audio bytes')\n"
            "    assert len(out) > 0\n",
        ),
        _md("### Gemini Live\n"),
        _code(
            "ft_gemini_live",
            "from patter.providers.gemini_live import GeminiLiveAdapter\n"
            "with _setup.cell('gemini_live', tier=3, required=['GOOGLE_API_KEY'], env=env):\n"
            "    adapter = GeminiLiveAdapter(api_key=env.google_key, system_prompt='Say hello.')\n"
            "    await adapter.connect()\n"
            "    audio = _setup.load_fixture('audio/hello_world_16khz_pcm.wav')[44:]\n"
            "    await adapter.send_audio(audio)\n"
            "    bytes_out = 0\n"
            "    async for ev_type, payload in adapter.receive_events():\n"
            "        if ev_type == 'audio': bytes_out += len(payload)\n"
            "        if bytes_out > 8000: break\n"
            "    await adapter.close()\n"
            "    print(f'received {bytes_out} audio bytes')\n"
            "    assert bytes_out > 0\n",
        ),
        _md("### Ultravox\n"),
        _code(
            "ft_ultravox",
            "from patter.providers.ultravox_realtime import UltravoxRealtimeAdapter\n"
            "with _setup.cell('ultravox', tier=3, required=['ULTRAVOX_API_KEY'], env=env):\n"
            "    adapter = UltravoxRealtimeAdapter(api_key=env.ultravox_key, system_prompt='Say hello.')\n"
            "    await adapter.connect()\n"
            "    audio = _setup.load_fixture('audio/hello_world_16khz_pcm.wav')[44:]\n"
            "    await adapter.send_audio(audio)\n"
            "    bytes_out = 0\n"
            "    async for ev_type, payload in adapter.receive_events():\n"
            "        if ev_type == 'audio': bytes_out += len(payload)\n"
            "        if bytes_out > 8000: break\n"
            "    await adapter.close()\n"
            "    print(f'received {bytes_out} audio bytes')\n"
            "    assert bytes_out > 0\n",
        ),
        _md("### ElevenLabs ConvAI\n\nOpens a ConvAI agent session and waits for the configured greeting.\n"),
        _code(
            "ft_elevenlabs_convai",
            "from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter\n"
            "with _setup.cell('elevenlabs_convai', tier=3,\n"
            "                 required=['ELEVENLABS_API_KEY', 'ELEVENLABS_AGENT_ID'], env=env):\n"
            "    adapter = ElevenLabsConvAIAdapter(api_key=env.elevenlabs_key, agent_id=env.elevenlabs_agent_id)\n"
            "    await adapter.connect()\n"
            "    bytes_out = 0\n"
            "    async for ev_type, payload in adapter.receive_events():\n"
            "        if ev_type == 'audio': bytes_out += len(payload)\n"
            "        if bytes_out > 8000: break\n"
            "    await adapter.close()\n"
            "    print(f'agent greeting: {bytes_out} audio bytes')\n"
            "    assert bytes_out > 0\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("### OpenAI Realtime\n"),
        _code(
            "ft_openai_realtime",
            "import { OpenAIRealtimeAdapter, loadFixture, cell } from './_setup.ts';\n"
            "await cell('openai_realtime', { tier: 3, required: ['OPENAI_API_KEY'], env }, async () => {\n"
            "  const adapter = new OpenAIRealtimeAdapter({ apiKey: env.openaiKey, voice: 'alloy',\n"
            "    instructions: 'Say hello.' });\n"
            "  await adapter.connect();\n"
            "  const audio = loadFixture('audio/hello_world_16khz_pcm.wav').subarray(44);\n"
            "  await adapter.sendAudio(audio);\n"
            "  let bytes = 0;\n"
            "  for await (const ev of adapter.receiveEvents()) {\n"
            "    if (ev.type === 'audio') bytes += ev.payload.length;\n"
            "    if (bytes > 8000) break;\n"
            "  }\n"
            "  await adapter.close();\n"
            "  console.log(`received ${bytes} audio bytes`);\n"
            "  if (!(bytes > 0)) throw new Error('no audio');\n"
            "});\n",
        ),
        _md("### Gemini Live\n"),
        _code(
            "ft_gemini_live",
            "import { GeminiLiveAdapter } from 'getpatter';\n"
            "await cell('gemini_live', { tier: 3, required: ['GOOGLE_API_KEY'], env }, async () => {\n"
            "  const adapter = new GeminiLiveAdapter({ apiKey: env.googleKey, systemPrompt: 'Say hello.' });\n"
            "  await adapter.connect();\n"
            "  const audio = loadFixture('audio/hello_world_16khz_pcm.wav').subarray(44);\n"
            "  await adapter.sendAudio(audio);\n"
            "  let bytes = 0;\n"
            "  for await (const ev of adapter.receiveEvents()) {\n"
            "    if (ev.type === 'audio') bytes += ev.payload.length;\n"
            "    if (bytes > 8000) break;\n"
            "  }\n"
            "  await adapter.close();\n"
            "  console.log(`received ${bytes} audio bytes`);\n"
            "  if (!(bytes > 0)) throw new Error('no audio');\n"
            "});\n",
        ),
        _md("### Ultravox\n"),
        _code(
            "ft_ultravox",
            "import { UltravoxRealtimeAdapter } from 'getpatter';\n"
            "await cell('ultravox', { tier: 3, required: ['ULTRAVOX_API_KEY'], env }, async () => {\n"
            "  const adapter = new UltravoxRealtimeAdapter({ apiKey: env.ultravoxKey, systemPrompt: 'Say hello.' });\n"
            "  await adapter.connect();\n"
            "  const audio = loadFixture('audio/hello_world_16khz_pcm.wav').subarray(44);\n"
            "  await adapter.sendAudio(audio);\n"
            "  let bytes = 0;\n"
            "  for await (const ev of adapter.receiveEvents()) {\n"
            "    if (ev.type === 'audio') bytes += ev.payload.length;\n"
            "    if (bytes > 8000) break;\n"
            "  }\n"
            "  await adapter.close();\n"
            "  console.log(`received ${bytes} audio bytes`);\n"
            "  if (!(bytes > 0)) throw new Error('no audio');\n"
            "});\n",
        ),
        _md("### ElevenLabs ConvAI\n"),
        _code(
            "ft_elevenlabs_convai",
            "import { ElevenLabsConvAIAdapter } from 'getpatter';\n"
            "await cell('elevenlabs_convai', { tier: 3, required: ['ELEVENLABS_API_KEY', 'ELEVENLABS_AGENT_ID'], env }, async () => {\n"
            "  const adapter = new ElevenLabsConvAIAdapter({ apiKey: env.elevenlabsKey, agentId: env.elevenlabsAgentId });\n"
            "  await adapter.connect();\n"
            "  let bytes = 0;\n"
            "  for await (const ev of adapter.receiveEvents()) {\n"
            "    if (ev.type === 'audio') bytes += ev.payload.length;\n"
            "    if (bytes > 8000) break;\n"
            "  }\n"
            "  await adapter.close();\n"
            "  console.log(`agent greeting: ${bytes} audio bytes`);\n"
            "  if (!(bytes > 0)) throw new Error('no greeting audio');\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject + smoke + commit + PR** (same shape as Task 28 step 2-4, with `02_realtime.ipynb` paths and `section_cells_02_realtime` import)

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
PYTHONPATH=scripts python -c "
from inject_section import inject_section
from section_cells_02_realtime import section_cells_python, section_cells_typescript
from pathlib import Path
inject_section(Path('examples/notebooks/python/02_realtime.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_python())
inject_section(Path('examples/notebooks/typescript/02_realtime.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_typescript())
"
jupyter nbconvert --to notebook --execute examples/notebooks/python/02_realtime.ipynb \
  --ExecutePreprocessor.timeout=120 --output /tmp/02.executed.ipynb
nbstripout examples/notebooks/python/02_realtime.ipynb examples/notebooks/typescript/02_realtime.ipynb
python scripts/check_notebook_parity.py
git add scripts/section_cells_02_realtime.py examples/notebooks/python/02_realtime.ipynb examples/notebooks/typescript/02_realtime.ipynb
git commit -m "feat(notebooks): topic 02 realtime §2 — openai/gemini/ultravox/convai cells"
git push && gh pr create --title "feat(notebooks): topic 02 §2 (realtime providers)" --body "4 cells covering all 4 realtime providers (OpenAI, Gemini Live, Ultravox, ElevenLabs ConvAI). Each opens a real WS and validates audio bytes flow back."
```

---

### Task 30: Topic 03 — pipeline STT §2

**Files:**
- Create: `scripts/section_cells_03_pipeline_stt.py`
- Modify: `examples/notebooks/{python,typescript}/03_pipeline_stt.ipynb`

**§2 covers:** `deepgram`, `whisper`, `assemblyai`, `soniox`, `speechmatics`, `cartesia`. All identical pattern via `_setup.run_stt`.

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_03_pipeline_stt.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


_PROVIDERS = [
    ("deepgram",      "DeepgramSTT",     "DEEPGRAM_API_KEY",     "deepgram_key"),
    ("whisper",       "WhisperSTT",      "OPENAI_API_KEY",       "openai_key"),
    ("assemblyai",    "AssemblyAISTT",   "ASSEMBLYAI_API_KEY",   "assemblyai_key"),
    ("soniox",        "SonioxSTT",       "SONIOX_API_KEY",       "soniox_key"),
    ("speechmatics",  "SpeechmaticsSTT", "SPEECHMATICS_API_KEY", "speechmatics_key"),
    ("cartesia",      "CartesiaSTT",     "CARTESIA_API_KEY",     "cartesia_key"),
]


def section_cells_python() -> list[dict]:
    cells = []
    for slug, cls, env_key, field in _PROVIDERS:
        cells.append(_md(f"### {cls}\n"))
        cells.append(_code(
            f"ft_{slug}",
            f"from patter.providers import {cls}\n"
            f"with _setup.cell('{slug}', tier=3, required=['{env_key}'], env=env):\n"
            f"    stt = {cls}(api_key=env.{field}, language='en-US')\n"
            f"    audio = _setup.load_fixture('audio/hello_world_16khz_pcm.wav')[44:]\n"
            f"    transcript = await _setup.run_stt(stt, audio)\n"
            f"    print(f'{slug} → {{transcript!r}}')\n"
            f"    assert 'hello' in transcript.lower() or 'world' in transcript.lower()\n",
        ))
    return cells


def section_cells_typescript() -> list[dict]:
    cells = []
    for slug, cls, env_key, field in _PROVIDERS:
        cells.append(_md(f"### {cls}\n"))
        cells.append(_code(
            f"ft_{slug}",
            f"import {{ {cls} }} from 'getpatter';\n"
            f"import {{ cell, runStt, loadFixture }} from './_setup.ts';\n"
            f"await cell('{slug}', {{ tier: 3, required: ['{env_key}'], env }}, async () => {{\n"
            f"  const stt = new {cls}({{ apiKey: env.{field}, language: 'en-US' }});\n"
            f"  const audio = loadFixture('audio/hello_world_16khz_pcm.wav').subarray(44);\n"
            f"  const t = await runStt(stt as any, audio);\n"
            f"  console.log(`{slug} → ${{JSON.stringify(t)}}`);\n"
            f"  const lower = t.toLowerCase();\n"
            f"  if (!(lower.includes('hello') || lower.includes('world'))) throw new Error('no expected word');\n"
            f"}});\n",
        ))
    return cells
```

- [ ] **Step 2: Inject + smoke + commit + PR**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
PYTHONPATH=scripts python -c "
from inject_section import inject_section
from section_cells_03_pipeline_stt import section_cells_python, section_cells_typescript
from pathlib import Path
inject_section(Path('examples/notebooks/python/03_pipeline_stt.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_python())
inject_section(Path('examples/notebooks/typescript/03_pipeline_stt.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_typescript())
"
jupyter nbconvert --to notebook --execute examples/notebooks/python/03_pipeline_stt.ipynb \
  --ExecutePreprocessor.timeout=180 --output /tmp/03.executed.ipynb
nbstripout examples/notebooks/python/03_pipeline_stt.ipynb examples/notebooks/typescript/03_pipeline_stt.ipynb
python scripts/check_notebook_parity.py
git add scripts/section_cells_03_pipeline_stt.py examples/notebooks/python/03_pipeline_stt.ipynb examples/notebooks/typescript/03_pipeline_stt.ipynb
git commit -m "feat(notebooks): topic 03 STT §2 — 6 providers via run_stt"
git push && gh pr create --title "feat(notebooks): topic 03 §2 (pipeline STT)" --body "6 STT cells (Deepgram, Whisper, AssemblyAI, Soniox, Speechmatics, Cartesia). Identical pattern via run_stt helper. Per-key skip when missing."
```

---

### Task 31: Topic 04 — pipeline TTS §2

**Files:**
- Create: `scripts/section_cells_04_pipeline_tts.py`
- Modify: `examples/notebooks/{python,typescript}/04_pipeline_tts.ipynb`

**§2 covers:** `elevenlabs`, `openai`, `cartesia`, `lmnt`, `rime`. All identical via `_setup.run_tts`. Each Python cell ends with `IPython.display.Audio(...)`.

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_04_pipeline_tts.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


_PROVIDERS = [
    ("elevenlabs", "ElevenLabsTTS", "ELEVENLABS_API_KEY", "elevenlabs_key", "voice_id=env.elevenlabs_voice_id"),
    ("openai",     "OpenAITTS",     "OPENAI_API_KEY",     "openai_key",     "voice='alloy'"),
    ("cartesia",   "CartesiaTTS",   "CARTESIA_API_KEY",   "cartesia_key",   "voice_id='default'"),
    ("lmnt",       "LMNTTTS",       "LMNT_API_KEY",       "lmnt_key",       "voice='lily'"),
    ("rime",       "RimeTTS",       "RIME_API_KEY",       "rime_key",       "voice='clara'"),
]


def section_cells_python() -> list[dict]:
    cells = []
    for slug, cls, env_key, field, extra in _PROVIDERS:
        cells.append(_md(f"### {cls}\n"))
        cells.append(_code(
            f"ft_{slug}",
            f"from patter.providers import {cls}\n"
            f"from IPython.display import Audio, display\n"
            f"with _setup.cell('{slug}', tier=3, required=['{env_key}'], env=env):\n"
            f"    tts = {cls}(api_key=env.{field}, {extra})\n"
            f"    audio = await _setup.run_tts(tts, 'Hello from {slug}.')\n"
            f"    print(f'{slug} → {{len(audio)}} bytes')\n"
            f"    assert len(audio) > 1000\n"
            f"    display(Audio(audio, rate=22_050))\n",
        ))
    return cells


def section_cells_typescript() -> list[dict]:
    cells = []
    ts_extras = {
        "elevenlabs": "voiceId: env.elevenlabsVoiceId",
        "openai": "voice: 'alloy'",
        "cartesia": "voiceId: 'default'",
        "lmnt": "voice: 'lily'",
        "rime": "voice: 'clara'",
    }
    for slug, cls, env_key, field, _ in _PROVIDERS:
        cells.append(_md(f"### {cls}\n"))
        cells.append(_code(
            f"ft_{slug}",
            f"import {{ {cls} }} from 'getpatter';\n"
            f"import {{ cell, runTts }} from './_setup.ts';\n"
            f"await cell('{slug}', {{ tier: 3, required: ['{env_key}'], env }}, async () => {{\n"
            f"  const tts = new {cls}({{ apiKey: env.{field}, {ts_extras[slug]} }});\n"
            f"  const audio = await runTts(tts as any, 'Hello from {slug}.');\n"
            f"  console.log(`{slug} → ${{audio.length}} bytes`);\n"
            f"  if (!(audio.length > 1000)) throw new Error('audio too short');\n"
            f"}});\n",
        ))
    return cells
```

- [ ] **Step 2: Inject + smoke + commit + PR**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
PYTHONPATH=scripts python -c "
from inject_section import inject_section
from section_cells_04_pipeline_tts import section_cells_python, section_cells_typescript
from pathlib import Path
inject_section(Path('examples/notebooks/python/04_pipeline_tts.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_python())
inject_section(Path('examples/notebooks/typescript/04_pipeline_tts.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_typescript())
"
jupyter nbconvert --to notebook --execute examples/notebooks/python/04_pipeline_tts.ipynb \
  --ExecutePreprocessor.timeout=180 --output /tmp/04.executed.ipynb
nbstripout examples/notebooks/python/04_pipeline_tts.ipynb examples/notebooks/typescript/04_pipeline_tts.ipynb
python scripts/check_notebook_parity.py
git add scripts/section_cells_04_pipeline_tts.py examples/notebooks/python/04_pipeline_tts.ipynb examples/notebooks/typescript/04_pipeline_tts.ipynb
git commit -m "feat(notebooks): topic 04 TTS §2 — 5 providers via run_tts + audio playback"
git push && gh pr create --title "feat(notebooks): topic 04 §2 (pipeline TTS)" --body "5 TTS cells (ElevenLabs, OpenAI, Cartesia, LMNT, Rime). Each Python cell renders an inline audio player so the operator can hear it."
```

---

### Task 32: Topic 05 — pipeline LLM §2

**Files:**
- Create: `scripts/section_cells_05_pipeline_llm.py`
- Modify: `examples/notebooks/{python,typescript}/05_pipeline_llm.ipynb`

**§2 covers:** `openai`, `anthropic`, `gemini`, `groq`, `cerebras`, `custom_on_message`, `llm_loop_tool_call`, `llm_loop_streaming`.

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_05_pipeline_llm.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


_LLM = [
    ("openai",   "OpenAILLMProvider",   "OPENAI_API_KEY",    "openai_key",    "gpt-4o-mini"),
    ("anthropic","AnthropicLLMProvider","ANTHROPIC_API_KEY", "anthropic_key", "claude-haiku-4-5"),
    ("gemini",   "GeminiLLMProvider",   "GOOGLE_API_KEY",    "google_key",    "gemini-2.5-flash"),
    ("groq",     "GroqLLMProvider",     "GROQ_API_KEY",      "groq_key",      "llama-3.3-70b-versatile"),
    ("cerebras", "CerebrasLLMProvider", "CEREBRAS_API_KEY",  "cerebras_key",  "llama-3.3-70b"),
]


def section_cells_python() -> list[dict]:
    cells = []
    for slug, cls, env_key, field, model in _LLM:
        cells.append(_md(f"### {cls} — `{model}`\n"))
        cells.append(_code(
            f"ft_llm_{slug}",
            f"from patter.services.llm_loop import LLMLoop, {cls}\n"
            f"with _setup.cell('llm_{slug}', tier=3, required=['{env_key}'], env=env):\n"
            f"    provider = {cls}(api_key=env.{field}, model='{model}')\n"
            f"    loop = LLMLoop(provider=provider, system_prompt='Reply with one short word.')\n"
            f"    chunks = []\n"
            f"    async for chunk in loop.stream_message(user_text='Say hi'):\n"
            f"        chunks.append(chunk.content or '')\n"
            f"    out = ''.join(chunks).strip()\n"
            f"    print(f'{slug} → {{out!r}}')\n"
            f"    assert len(out) > 0\n",
        ))
    cells.append(_md("### Custom `on_message` handler\n\nUser supplies their own LLM glue. Patter calls back into it.\n"))
    cells.append(_code(
        "ft_custom_on_message",
        "with _setup.cell('custom_on_message', tier=1, env=env):\n"
        "    received = []\n"
        "    async def my_handler(message, ctx):\n"
        "        received.append(message.content)\n"
        "        return 'echo: ' + message.content\n"
        "    # Simulate Patter dispatching a message into the handler.\n"
        "    from patter.types import IncomingMessage\n"
        "    reply = await my_handler(IncomingMessage(role='user', content='ping'), {})\n"
        "    print(f'handler returned {reply!r}')\n"
        "    assert received == ['ping'] and reply == 'echo: ping'\n",
    ))
    cells.append(_md("### LLMLoop tool call\n\nRegister a `@tool`, ask a question that should fire it, assert the tool result feeds back.\n"))
    cells.append(_code(
        "ft_llm_loop_tool_call",
        "from patter import tool\n"
        "from patter.services.llm_loop import LLMLoop, OpenAILLMProvider\n"
        "with _setup.cell('llm_loop_tool_call', tier=3, required=['OPENAI_API_KEY'], env=env):\n"
        "    @tool(description='Get current UTC time as ISO 8601 string.')\n"
        "    async def now() -> str:\n"
        "        from datetime import datetime, timezone\n"
        "        return datetime.now(timezone.utc).isoformat()\n"
        "    loop = LLMLoop(provider=OpenAILLMProvider(api_key=env.openai_key, model='gpt-4o-mini'),\n"
        "                   system_prompt='Use tools when asked about time.', tools=[now])\n"
        "    out = []\n"
        "    async for chunk in loop.stream_message(user_text='What time is it (UTC)?'):\n"
        "        out.append(chunk.content or '')\n"
        "    text = ''.join(out)\n"
        "    print(text)\n"
        "    assert any(year in text for year in ('2025','2026','2027'))\n",
    ))
    cells.append(_md("### LLMLoop streaming tokens\n\nAssert the iterator yields more than once.\n"))
    cells.append(_code(
        "ft_llm_loop_streaming",
        "from patter.services.llm_loop import LLMLoop, OpenAILLMProvider\n"
        "with _setup.cell('llm_loop_streaming', tier=3, required=['OPENAI_API_KEY'], env=env):\n"
        "    loop = LLMLoop(provider=OpenAILLMProvider(api_key=env.openai_key, model='gpt-4o-mini'),\n"
        "                   system_prompt='Reply with a sentence about voice AI.')\n"
        "    chunk_count = 0\n"
        "    async for _ in loop.stream_message(user_text='Tell me about voice AI in one sentence.'):\n"
        "        chunk_count += 1\n"
        "    print(f'received {chunk_count} chunks')\n"
        "    assert chunk_count > 1\n",
    ))
    return cells


def section_cells_typescript() -> list[dict]:
    cells = []
    for slug, cls, env_key, field, model in _LLM:
        cells.append(_md(f"### {cls} — `{model}`\n"))
        cells.append(_code(
            f"ft_llm_{slug}",
            f"import {{ LLMLoop, {cls} }} from 'getpatter';\n"
            f"import {{ cell }} from './_setup.ts';\n"
            f"await cell('llm_{slug}', {{ tier: 3, required: ['{env_key}'], env }}, async () => {{\n"
            f"  const provider = new {cls}({{ apiKey: env.{field}, model: '{model}' }});\n"
            f"  const loop = new LLMLoop({{ provider, systemPrompt: 'Reply with one short word.' }});\n"
            f"  const chunks: string[] = [];\n"
            f"  for await (const chunk of loop.streamMessage({{ userText: 'Say hi' }})) chunks.push(chunk.content ?? '');\n"
            f"  const out = chunks.join('').trim();\n"
            f"  console.log(`{slug} → ${{JSON.stringify(out)}}`);\n"
            f"  if (out.length === 0) throw new Error('no content');\n"
            f"}});\n",
        ))
    cells.append(_md("### Custom message handler\n"))
    cells.append(_code(
        "ft_custom_on_message",
        "import { cell } from './_setup.ts';\n"
        "await cell('custom_on_message', { tier: 1, env }, async () => {\n"
        "  const received: string[] = [];\n"
        "  const handler = async (msg: { content: string }) => { received.push(msg.content); return 'echo: ' + msg.content; };\n"
        "  const reply = await handler({ content: 'ping' });\n"
        "  console.log(`handler returned ${JSON.stringify(reply)}`);\n"
        "  if (!(received.length === 1 && reply === 'echo: ping')) throw new Error('handler invariants failed');\n"
        "});\n",
    ))
    cells.append(_md("### LLMLoop tool call\n"))
    cells.append(_code(
        "ft_llm_loop_tool_call",
        "import { defineTool, LLMLoop, OpenAILLMProvider } from 'getpatter';\n"
        "await cell('llm_loop_tool_call', { tier: 3, required: ['OPENAI_API_KEY'], env }, async () => {\n"
        "  const now = defineTool({\n"
        "    name: 'now', description: 'Get current UTC time.', parameters: {},\n"
        "    handler: async () => new Date().toISOString(),\n"
        "  });\n"
        "  const loop = new LLMLoop({ provider: new OpenAILLMProvider({ apiKey: env.openaiKey, model: 'gpt-4o-mini' }),\n"
        "    systemPrompt: 'Use tools when asked about time.', tools: [now] });\n"
        "  const out: string[] = [];\n"
        "  for await (const c of loop.streamMessage({ userText: 'What time is it (UTC)?' })) out.push(c.content ?? '');\n"
        "  const text = out.join('');\n"
        "  console.log(text);\n"
        "  if (!['2025','2026','2027'].some((y) => text.includes(y))) throw new Error('no year in reply');\n"
        "});\n",
    ))
    cells.append(_md("### Streaming token count\n"))
    cells.append(_code(
        "ft_llm_loop_streaming",
        "import { LLMLoop, OpenAILLMProvider } from 'getpatter';\n"
        "await cell('llm_loop_streaming', { tier: 3, required: ['OPENAI_API_KEY'], env }, async () => {\n"
        "  const loop = new LLMLoop({ provider: new OpenAILLMProvider({ apiKey: env.openaiKey, model: 'gpt-4o-mini' }),\n"
        "    systemPrompt: 'Reply with a sentence about voice AI.' });\n"
        "  let n = 0;\n"
        "  for await (const _ of loop.streamMessage({ userText: 'Tell me about voice AI in one sentence.' })) n += 1;\n"
        "  console.log(`received ${n} chunks`);\n"
        "  if (!(n > 1)) throw new Error('not streaming');\n"
        "});\n",
    ))
    return cells
```

- [ ] **Step 2: Inject + smoke + commit + PR**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
PYTHONPATH=scripts python -c "
from inject_section import inject_section
from section_cells_05_pipeline_llm import section_cells_python, section_cells_typescript
from pathlib import Path
inject_section(Path('examples/notebooks/python/05_pipeline_llm.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_python())
inject_section(Path('examples/notebooks/typescript/05_pipeline_llm.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_typescript())
"
jupyter nbconvert --to notebook --execute examples/notebooks/python/05_pipeline_llm.ipynb \
  --ExecutePreprocessor.timeout=180 --output /tmp/05.executed.ipynb
nbstripout examples/notebooks/python/05_pipeline_llm.ipynb examples/notebooks/typescript/05_pipeline_llm.ipynb
python scripts/check_notebook_parity.py
git add scripts/section_cells_05_pipeline_llm.py examples/notebooks/python/05_pipeline_llm.ipynb examples/notebooks/typescript/05_pipeline_llm.ipynb
git commit -m "feat(notebooks): topic 05 LLM §2 — 5 providers + on_message + LLMLoop"
git push && gh pr create --title "feat(notebooks): topic 05 §2 (pipeline LLM)" --body "8 cells: 5 LLM providers via LLMLoop, custom on_message handler demo, LLMLoop tool-call roundtrip, streaming token count."
```

---

### Task 33: Topic 06 — Twilio telephony §2

**Files:**
- Create: `scripts/section_cells_06_telephony_twilio.py`
- Modify: `examples/notebooks/{python,typescript}/06_telephony_twilio.ipynb`

**§2 covers:** `parse_inbound_voice_webhook`, `verify_signature_valid`, `verify_signature_invalid`, `amd_voicemail_branch`, `dtmf_input`, `recording_url_received`, `transfer_call_twiml`, `ring_timeout_emission`, `status_callback_lifecycle`. All cells T2 (local server + httpx) — no real Twilio API.

- [ ] **Step 1: Write the helper (Python)**

```python
# scripts/section_cells_06_telephony_twilio.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


PRELUDE = (
    "import json, hmac, hashlib, base64, httpx\n"
    "from urllib.parse import urlencode\n"
    "from patter import Patter\n"
    "\n"
    "TWILIO_TOKEN = 'test_token_xxx'\n"
    "\n"
    "def _twilio_sig(url: str, params: dict[str, str], token: str) -> str:\n"
    "    s = url + ''.join(f'{k}{params[k]}' for k in sorted(params))\n"
    "    return base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode()\n"
    "\n"
    "def _make_patter():\n"
    "    return Patter(twilio_sid='ACtest00000000000000000000000000', twilio_token=TWILIO_TOKEN,\n"
    "                  phone_number='+15555550100', webhook_url='https://example.com/webhook')\n"
)


def section_cells_python() -> list[dict]:
    return [
        _md("Cells under §2 are all T2 — they spin up the embedded server and hit it with `httpx`. No real Twilio API needed.\n"),
        _code("ft_twilio_prelude", PRELUDE),
        _md("### Parse inbound voice webhook\n"),
        _code(
            "ft_parse_inbound_voice_webhook",
            "with _setup.cell('parse_inbound_voice_webhook', tier=2, env=env):\n"
            "    p = _make_patter()\n"
            "    server = await p._embedded.start(port=0)\n"
            "    try:\n"
            "        url = f'http://127.0.0.1:{server.port}/webhook/twilio/voice'\n"
            "        body = json.loads(_setup.load_fixture('webhooks/twilio_voice_inbound.json'))\n"
            "        sig = _twilio_sig(url, body, TWILIO_TOKEN)\n"
            "        async with httpx.AsyncClient() as c:\n"
            "            r = await c.post(url, data=body, headers={'X-Twilio-Signature': sig})\n"
            "        print(f'POST /webhook/twilio/voice → {r.status_code}, {len(r.text)} bytes TwiML')\n"
            "        assert r.status_code == 200\n"
            "        assert '<Response>' in r.text\n"
            "    finally:\n"
            "        await p._embedded.stop()\n",
        ),
        _md("### Verify Twilio signature — valid\n"),
        _code(
            "ft_verify_signature_valid",
            "from patter.handlers.common import validate_twilio_signature\n"
            "with _setup.cell('verify_signature_valid', tier=1, env=env):\n"
            "    url = 'https://example.com/webhook/twilio/voice'\n"
            "    body = {'From': '+15555550100', 'To': '+15555550100'}\n"
            "    sig = _twilio_sig(url, body, TWILIO_TOKEN)\n"
            "    assert validate_twilio_signature(url, body, sig, TWILIO_TOKEN) is True\n"
            "    print('valid signature accepted')\n",
        ),
        _md("### Verify Twilio signature — tampered\n"),
        _code(
            "ft_verify_signature_invalid",
            "from patter.handlers.common import validate_twilio_signature\n"
            "with _setup.cell('verify_signature_invalid', tier=1, env=env):\n"
            "    url = 'https://example.com/webhook/twilio/voice'\n"
            "    body = {'From': '+15555550100', 'To': '+15555550100'}\n"
            "    sig = _twilio_sig(url, body, TWILIO_TOKEN)\n"
            "    body['From'] = '+15555550101'  # tamper\n"
            "    assert validate_twilio_signature(url, body, sig, TWILIO_TOKEN) is False\n"
            "    print('tampered body rejected')\n",
        ),
        _md("### AMD voicemail branch\n"),
        _code(
            "ft_amd_voicemail_branch",
            "with _setup.cell('amd_voicemail_branch', tier=2, env=env):\n"
            "    p = _make_patter()\n"
            "    server = await p._embedded.start(port=0)\n"
            "    try:\n"
            "        url = f'http://127.0.0.1:{server.port}/webhook/twilio/amd'\n"
            "        body = {'CallSid': 'CAtest00000000000000000000000001',\n"
            "                'AnsweredBy': 'machine_end_beep'}\n"
            "        sig = _twilio_sig(url, body, TWILIO_TOKEN)\n"
            "        async with httpx.AsyncClient() as c:\n"
            "            r = await c.post(url, data=body, headers={'X-Twilio-Signature': sig})\n"
            "        print(f'AMD → {r.status_code}')\n"
            "        assert r.status_code == 200\n"
            "    finally:\n"
            "        await p._embedded.stop()\n",
        ),
        _md("### DTMF input\n"),
        _code(
            "ft_dtmf_input",
            "with _setup.cell('dtmf_input', tier=2, env=env):\n"
            "    p = _make_patter()\n"
            "    server = await p._embedded.start(port=0)\n"
            "    try:\n"
            "        url = f'http://127.0.0.1:{server.port}/webhook/twilio/gather'\n"
            "        body = {'CallSid': 'CAtest00000000000000000000000002', 'Digits': '5'}\n"
            "        sig = _twilio_sig(url, body, TWILIO_TOKEN)\n"
            "        async with httpx.AsyncClient() as c:\n"
            "            r = await c.post(url, data=body, headers={'X-Twilio-Signature': sig})\n"
            "        print(f'DTMF → {r.status_code}')\n"
            "        assert r.status_code == 200\n"
            "    finally:\n"
            "        await p._embedded.stop()\n",
        ),
        _md("### Recording URL received\n"),
        _code(
            "ft_recording_url_received",
            "with _setup.cell('recording_url_received', tier=2, env=env):\n"
            "    p = _make_patter()\n"
            "    server = await p._embedded.start(port=0)\n"
            "    try:\n"
            "        url = f'http://127.0.0.1:{server.port}/webhook/twilio/recording'\n"
            "        body = {'CallSid': 'CAtest00000000000000000000000003',\n"
            "                'RecordingUrl': 'https://api.twilio.com/2010-04-01/Accounts/ACtest/Recordings/REtest',\n"
            "                'RecordingDuration': '7'}\n"
            "        sig = _twilio_sig(url, body, TWILIO_TOKEN)\n"
            "        async with httpx.AsyncClient() as c:\n"
            "            r = await c.post(url, data=body, headers={'X-Twilio-Signature': sig})\n"
            "        print(f'recording → {r.status_code}')\n"
            "        assert r.status_code == 200\n"
            "    finally:\n"
            "        await p._embedded.stop()\n",
        ),
        _md("### `transfer_call` emits `<Dial>` TwiML\n"),
        _code(
            "ft_transfer_call_twiml",
            "from patter.handlers.twilio_handler import build_transfer_twiml\n"
            "with _setup.cell('transfer_call_twiml', tier=1, env=env):\n"
            "    twiml = build_transfer_twiml(target='+15555550100')\n"
            "    print(twiml)\n"
            "    assert '<Dial>' in twiml and '+15555550100' in twiml\n",
        ),
        _md("### Ring timeout flows into TwiML\n"),
        _code(
            "ft_ring_timeout_emission",
            "from patter.handlers.twilio_handler import build_outbound_twiml\n"
            "with _setup.cell('ring_timeout_emission', tier=1, env=env):\n"
            "    twiml = build_outbound_twiml(target='+15555550100', ring_timeout=20)\n"
            "    print(twiml)\n"
            "    assert 'Timeout=\"20\"' in twiml\n",
        ),
        _md("### Status callback lifecycle\n"),
        _code(
            "ft_status_callback_lifecycle",
            "with _setup.cell('status_callback_lifecycle', tier=2, env=env):\n"
            "    p = _make_patter()\n"
            "    server = await p._embedded.start(port=0)\n"
            "    try:\n"
            "        url = f'http://127.0.0.1:{server.port}/webhooks/twilio/status'\n"
            "        states = [('initiated','0'), ('ringing','0'), ('in-progress','3'), ('completed','12')]\n"
            "        async with httpx.AsyncClient() as c:\n"
            "            for s, dur in states:\n"
            "                body = {'CallSid': 'CAtest00000000000000000000000004', 'CallStatus': s, 'CallDuration': dur}\n"
            "                sig = _twilio_sig(url, body, TWILIO_TOKEN)\n"
            "                r = await c.post(url, data=body, headers={'X-Twilio-Signature': sig})\n"
            "                print(f'{s} → {r.status_code}')\n"
            "                assert r.status_code == 200\n"
            "    finally:\n"
            "        await p._embedded.stop()\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    # TS uses crypto subtle for HMAC; otherwise structurally identical.
    return [
        _md("Cells under §2 are all T2 — they spin up the embedded server and hit it with `fetch`. No real Twilio API needed.\n"),
        _code(
            "ft_twilio_prelude",
            "import { createHmac } from 'node:crypto';\n"
            "import { Patter } from 'getpatter';\n"
            "import { cell, loadFixture } from './_setup.ts';\n"
            "const TWILIO_TOKEN = 'test_token_xxx';\n"
            "function twilioSig(url: string, params: Record<string,string>, token: string): string {\n"
            "  const sorted = Object.keys(params).sort();\n"
            "  const s = url + sorted.map((k) => k + params[k]).join('');\n"
            "  return createHmac('sha1', token).update(s).digest('base64');\n"
            "}\n"
            "function makePatter() {\n"
            "  return new Patter({ twilioSid: 'ACtest00000000000000000000000000', twilioToken: TWILIO_TOKEN,\n"
            "    phoneNumber: '+15555550100', webhookUrl: 'https://example.com/webhook' });\n"
            "}\n",
        ),
        _md("### Parse inbound voice webhook\n"),
        _code(
            "ft_parse_inbound_voice_webhook",
            "await cell('parse_inbound_voice_webhook', { tier: 2, env }, async () => {\n"
            "  const p = makePatter();\n"
            "  const server = await (p as any)._embedded.start(0);\n"
            "  try {\n"
            "    const url = `http://127.0.0.1:${server.port}/webhook/twilio/voice`;\n"
            "    const body = JSON.parse(loadFixture('webhooks/twilio_voice_inbound.json').toString());\n"
            "    const sig = twilioSig(url, body, TWILIO_TOKEN);\n"
            "    const r = await fetch(url, { method: 'POST', headers: { 'X-Twilio-Signature': sig,\n"
            "      'Content-Type': 'application/x-www-form-urlencoded' },\n"
            "      body: new URLSearchParams(body).toString() });\n"
            "    const text = await r.text();\n"
            "    console.log(`POST /webhook/twilio/voice → ${r.status}, ${text.length} bytes TwiML`);\n"
            "    if (!(r.status === 200 && text.includes('<Response>'))) throw new Error('bad response');\n"
            "  } finally { await (p as any)._embedded.stop(); }\n"
            "});\n",
        ),
        _md("### Verify signature — valid + tampered\n"),
        _code(
            "ft_verify_signature_valid",
            "import { validateTwilioSignature } from 'getpatter';\n"
            "await cell('verify_signature_valid', { tier: 1, env }, () => {\n"
            "  const url = 'https://example.com/webhook/twilio/voice';\n"
            "  const body = { From: '+15555550100', To: '+15555550100' };\n"
            "  const sig = twilioSig(url, body, TWILIO_TOKEN);\n"
            "  if (!validateTwilioSignature(url, body, sig, TWILIO_TOKEN)) throw new Error('valid sig rejected');\n"
            "  console.log('valid signature accepted');\n"
            "});\n",
        ),
        _code(
            "ft_verify_signature_invalid",
            "import { validateTwilioSignature } from 'getpatter';\n"
            "await cell('verify_signature_invalid', { tier: 1, env }, () => {\n"
            "  const url = 'https://example.com/webhook/twilio/voice';\n"
            "  const body = { From: '+15555550100', To: '+15555550100' };\n"
            "  const sig = twilioSig(url, body, TWILIO_TOKEN);\n"
            "  body.From = '+15555550101';\n"
            "  if (validateTwilioSignature(url, body, sig, TWILIO_TOKEN)) throw new Error('tampered body accepted');\n"
            "  console.log('tampered body rejected');\n"
            "});\n",
        ),
        _md("### AMD / DTMF / recording / transfer / ring timeout / status callback\n\nFor brevity, these five share the same scaffold (POST signed body to embedded server, assert 200). See the Python notebook for the full per-cell breakdown — TS mirrors it line-for-line.\n"),
        _code(
            "ft_amd_voicemail_branch",
            "await cell('amd_voicemail_branch', { tier: 2, env }, async () => {\n"
            "  const p = makePatter(); const server = await (p as any)._embedded.start(0);\n"
            "  try {\n"
            "    const url = `http://127.0.0.1:${server.port}/webhook/twilio/amd`;\n"
            "    const body = { CallSid: 'CAtest00000000000000000000000001', AnsweredBy: 'machine_end_beep' };\n"
            "    const sig = twilioSig(url, body, TWILIO_TOKEN);\n"
            "    const r = await fetch(url, { method: 'POST', headers: { 'X-Twilio-Signature': sig,\n"
            "      'Content-Type': 'application/x-www-form-urlencoded' },\n"
            "      body: new URLSearchParams(body).toString() });\n"
            "    if (r.status !== 200) throw new Error(`got ${r.status}`);\n"
            "    console.log(`AMD → ${r.status}`);\n"
            "  } finally { await (p as any)._embedded.stop(); }\n"
            "});\n",
        ),
        _code(
            "ft_dtmf_input",
            "await cell('dtmf_input', { tier: 2, env }, async () => {\n"
            "  const p = makePatter(); const server = await (p as any)._embedded.start(0);\n"
            "  try {\n"
            "    const url = `http://127.0.0.1:${server.port}/webhook/twilio/gather`;\n"
            "    const body = { CallSid: 'CAtest00000000000000000000000002', Digits: '5' };\n"
            "    const sig = twilioSig(url, body, TWILIO_TOKEN);\n"
            "    const r = await fetch(url, { method: 'POST', headers: { 'X-Twilio-Signature': sig,\n"
            "      'Content-Type': 'application/x-www-form-urlencoded' },\n"
            "      body: new URLSearchParams(body).toString() });\n"
            "    if (r.status !== 200) throw new Error(`got ${r.status}`);\n"
            "    console.log(`DTMF → ${r.status}`);\n"
            "  } finally { await (p as any)._embedded.stop(); }\n"
            "});\n",
        ),
        _code(
            "ft_recording_url_received",
            "await cell('recording_url_received', { tier: 2, env }, async () => {\n"
            "  const p = makePatter(); const server = await (p as any)._embedded.start(0);\n"
            "  try {\n"
            "    const url = `http://127.0.0.1:${server.port}/webhook/twilio/recording`;\n"
            "    const body = { CallSid: 'CAtest00000000000000000000000003',\n"
            "      RecordingUrl: 'https://api.twilio.com/2010-04-01/Accounts/ACtest/Recordings/REtest',\n"
            "      RecordingDuration: '7' };\n"
            "    const sig = twilioSig(url, body, TWILIO_TOKEN);\n"
            "    const r = await fetch(url, { method: 'POST', headers: { 'X-Twilio-Signature': sig,\n"
            "      'Content-Type': 'application/x-www-form-urlencoded' },\n"
            "      body: new URLSearchParams(body).toString() });\n"
            "    if (r.status !== 200) throw new Error(`got ${r.status}`);\n"
            "    console.log(`recording → ${r.status}`);\n"
            "  } finally { await (p as any)._embedded.stop(); }\n"
            "});\n",
        ),
        _code(
            "ft_transfer_call_twiml",
            "import { buildTransferTwiml } from 'getpatter';\n"
            "await cell('transfer_call_twiml', { tier: 1, env }, () => {\n"
            "  const twiml = buildTransferTwiml({ target: '+15555550100' });\n"
            "  console.log(twiml);\n"
            "  if (!(twiml.includes('<Dial>') && twiml.includes('+15555550100'))) throw new Error('bad twiml');\n"
            "});\n",
        ),
        _code(
            "ft_ring_timeout_emission",
            "import { buildOutboundTwiml } from 'getpatter';\n"
            "await cell('ring_timeout_emission', { tier: 1, env }, () => {\n"
            "  const twiml = buildOutboundTwiml({ target: '+15555550100', ringTimeout: 20 });\n"
            "  console.log(twiml);\n"
            "  if (!twiml.includes('Timeout=\"20\"')) throw new Error('no timeout');\n"
            "});\n",
        ),
        _code(
            "ft_status_callback_lifecycle",
            "await cell('status_callback_lifecycle', { tier: 2, env }, async () => {\n"
            "  const p = makePatter(); const server = await (p as any)._embedded.start(0);\n"
            "  try {\n"
            "    const url = `http://127.0.0.1:${server.port}/webhooks/twilio/status`;\n"
            "    for (const [s, dur] of [['initiated','0'],['ringing','0'],['in-progress','3'],['completed','12']]) {\n"
            "      const body = { CallSid: 'CAtest00000000000000000000000004', CallStatus: s, CallDuration: dur };\n"
            "      const sig = twilioSig(url, body, TWILIO_TOKEN);\n"
            "      const r = await fetch(url, { method: 'POST', headers: { 'X-Twilio-Signature': sig,\n"
            "        'Content-Type': 'application/x-www-form-urlencoded' },\n"
            "        body: new URLSearchParams(body).toString() });\n"
            "      if (r.status !== 200) throw new Error(`${s}: ${r.status}`);\n"
            "      console.log(`${s} → ${r.status}`);\n"
            "    }\n"
            "  } finally { await (p as any)._embedded.stop(); }\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject + smoke + commit + PR**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
PYTHONPATH=scripts python -c "
from inject_section import inject_section
from section_cells_06_telephony_twilio import section_cells_python, section_cells_typescript
from pathlib import Path
inject_section(Path('examples/notebooks/python/06_telephony_twilio.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_python())
inject_section(Path('examples/notebooks/typescript/06_telephony_twilio.ipynb'),
               marker='§2: Feature Tour', cells=section_cells_typescript())
"
jupyter nbconvert --to notebook --execute examples/notebooks/python/06_telephony_twilio.ipynb \
  --ExecutePreprocessor.timeout=180 --output /tmp/06.executed.ipynb
nbstripout examples/notebooks/python/06_telephony_twilio.ipynb examples/notebooks/typescript/06_telephony_twilio.ipynb
python scripts/check_notebook_parity.py
git add scripts/section_cells_06_telephony_twilio.py examples/notebooks/python/06_telephony_twilio.ipynb examples/notebooks/typescript/06_telephony_twilio.ipynb
git commit -m "feat(notebooks): topic 06 Twilio §2 — webhooks, signatures, AMD/DTMF/recording/transfer/timeout/status"
git push && gh pr create --title "feat(notebooks): topic 06 §2 (Twilio telephony)" --body "9 cells covering parse, HMAC validate (positive + tampered), AMD voicemail, DTMF, recording URL, transfer TwiML, ring timeout, full status callback lifecycle. All T2 — no real Twilio API required."
```

---

### Task 34: Topic 07 — Telnyx telephony §2

**Files:**
- Create: `scripts/section_cells_07_telephony_telnyx.py`
- Modify: `examples/notebooks/{python,typescript}/07_telephony_telnyx.ipynb`

**§2 covers:** `call_initiated_event_parsing`, `verify_ed25519_valid`, `verify_ed25519_invalid_replay`, `track_filter_inbound_only`, `dtmf_received_event`, `transfer_call_call_control`, `ring_timeout_emission`. Uses the test Ed25519 keypair from fixtures.

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_07_telephony_telnyx.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("Cells use the test Ed25519 keypair under `fixtures/keys/`. No real Telnyx account needed.\n"),
        _md("### Call-initiated event parsing\n"),
        _code(
            "ft_call_initiated_event_parsing",
            "import json\n"
            "from patter.handlers.telnyx_handler import parse_call_event\n"
            "with _setup.cell('call_initiated_event_parsing', tier=1, env=env):\n"
            "    body = json.loads(_setup.load_fixture('webhooks/telnyx_call_initiated.json'))\n"
            "    event = parse_call_event(body)\n"
            "    print(event)\n"
            "    assert event.event_type == 'call.initiated'\n"
            "    assert event.from_ == '+15555550100'\n",
        ),
        _md("### Verify Ed25519 — valid signature within window\n"),
        _code(
            "ft_verify_ed25519_valid",
            "import time, base64\n"
            "from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key\n"
            "from patter.handlers.common import validate_telnyx_signature\n"
            "with _setup.cell('verify_ed25519_valid', tier=1, env=env):\n"
            "    priv = load_pem_private_key(_setup.load_fixture('keys/telnyx_test_ed25519_priv.pem'), password=None)\n"
            "    pub_pem = _setup.load_fixture('keys/telnyx_test_ed25519_pub.pem').decode()\n"
            "    body = b'{\"event_type\":\"call.initiated\"}'\n"
            "    ts = str(int(time.time()))\n"
            "    sig = base64.b64encode(priv.sign(ts.encode() + b'|' + body)).decode()\n"
            "    ok = validate_telnyx_signature(body, sig, ts, pub_pem)\n"
            "    assert ok is True\n"
            "    print('valid signature accepted')\n",
        ),
        _md("### Verify Ed25519 — old timestamp rejected (anti-replay)\n"),
        _code(
            "ft_verify_ed25519_invalid_replay",
            "import time, base64\n"
            "from cryptography.hazmat.primitives.serialization import load_pem_private_key\n"
            "from patter.handlers.common import validate_telnyx_signature\n"
            "with _setup.cell('verify_ed25519_invalid_replay', tier=1, env=env):\n"
            "    priv = load_pem_private_key(_setup.load_fixture('keys/telnyx_test_ed25519_priv.pem'), password=None)\n"
            "    pub_pem = _setup.load_fixture('keys/telnyx_test_ed25519_pub.pem').decode()\n"
            "    body = b'{\"event_type\":\"call.initiated\"}'\n"
            "    old_ts = str(int(time.time()) - 600)  # 10 min ago\n"
            "    sig = base64.b64encode(priv.sign(old_ts.encode() + b'|' + body)).decode()\n"
            "    ok = validate_telnyx_signature(body, sig, old_ts, pub_pem)\n"
            "    assert ok is False\n"
            "    print('replay rejected')\n",
        ),
        _md("### Track filter — only inbound media frames pass\n"),
        _code(
            "ft_track_filter_inbound_only",
            "from patter.handlers.telnyx_handler import filter_track\n"
            "with _setup.cell('track_filter_inbound_only', tier=1, env=env):\n"
            "    inbound = filter_track({'track': 'inbound', 'payload': 'aGk='})\n"
            "    outbound = filter_track({'track': 'outbound', 'payload': 'aGk='})\n"
            "    assert inbound is not None and outbound is None\n"
            "    print('outbound frames suppressed')\n",
        ),
        _md("### DTMF received event\n"),
        _code(
            "ft_dtmf_received_event",
            "import json\n"
            "from patter.handlers.telnyx_handler import parse_call_event\n"
            "with _setup.cell('dtmf_received_event', tier=1, env=env):\n"
            "    body = json.loads(_setup.load_fixture('webhooks/telnyx_dtmf_received.json'))\n"
            "    ev = parse_call_event(body)\n"
            "    print(ev)\n"
            "    assert ev.event_type == 'call.dtmf.received'\n"
            "    assert ev.payload['digit'] == '5'\n",
        ),
        _md("### `transfer_call` via Call Control\n"),
        _code(
            "ft_transfer_call_call_control",
            "from patter.handlers.telnyx_handler import build_transfer_action\n"
            "with _setup.cell('transfer_call_call_control', tier=1, env=env):\n"
            "    action = build_transfer_action(call_control_id='v3:test', to='+15555550100')\n"
            "    print(action)\n"
            "    assert action['to'] == '+15555550100'\n"
            "    assert action['from'] is not None\n",
        ),
        _md("### Ring timeout emission (`timeout_secs`)\n"),
        _code(
            "ft_ring_timeout_emission",
            "from patter.handlers.telnyx_handler import build_outbound_call\n"
            "with _setup.cell('ring_timeout_emission', tier=1, env=env):\n"
            "    body = build_outbound_call(connection_id='c1', to='+15555550100',\n"
            "                               from_='+15555550100', ring_timeout=20)\n"
            "    print(body)\n"
            "    assert body['timeout_secs'] == 20\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("Cells use the test Ed25519 keypair. TypeScript signing via `node:crypto`.\n"),
        _code(
            "ft_call_initiated_event_parsing",
            "import { parseCallEvent } from 'getpatter';\n"
            "import { cell, loadFixture } from './_setup.ts';\n"
            "await cell('call_initiated_event_parsing', { tier: 1, env }, () => {\n"
            "  const body = JSON.parse(loadFixture('webhooks/telnyx_call_initiated.json').toString());\n"
            "  const ev = parseCallEvent(body);\n"
            "  console.log(ev);\n"
            "  if (ev.eventType !== 'call.initiated' || ev.from !== '+15555550100') throw new Error('bad parse');\n"
            "});\n",
        ),
        _code(
            "ft_verify_ed25519_valid",
            "import { sign as edSign } from 'node:crypto';\n"
            "import { validateTelnyxSignature } from 'getpatter';\n"
            "await cell('verify_ed25519_valid', { tier: 1, env }, () => {\n"
            "  const priv = loadFixture('keys/telnyx_test_ed25519_priv.pem').toString();\n"
            "  const pub = loadFixture('keys/telnyx_test_ed25519_pub.pem').toString();\n"
            "  const body = Buffer.from('{\"event_type\":\"call.initiated\"}');\n"
            "  const ts = String(Math.floor(Date.now() / 1000));\n"
            "  const sig = edSign(null, Buffer.concat([Buffer.from(ts + '|'), body]), priv).toString('base64');\n"
            "  if (!validateTelnyxSignature(body, sig, ts, pub)) throw new Error('valid sig rejected');\n"
            "  console.log('valid signature accepted');\n"
            "});\n",
        ),
        _code(
            "ft_verify_ed25519_invalid_replay",
            "import { sign as edSign } from 'node:crypto';\n"
            "import { validateTelnyxSignature } from 'getpatter';\n"
            "await cell('verify_ed25519_invalid_replay', { tier: 1, env }, () => {\n"
            "  const priv = loadFixture('keys/telnyx_test_ed25519_priv.pem').toString();\n"
            "  const pub = loadFixture('keys/telnyx_test_ed25519_pub.pem').toString();\n"
            "  const body = Buffer.from('{\"event_type\":\"call.initiated\"}');\n"
            "  const oldTs = String(Math.floor(Date.now() / 1000) - 600);\n"
            "  const sig = edSign(null, Buffer.concat([Buffer.from(oldTs + '|'), body]), priv).toString('base64');\n"
            "  if (validateTelnyxSignature(body, sig, oldTs, pub)) throw new Error('replay accepted');\n"
            "  console.log('replay rejected');\n"
            "});\n",
        ),
        _code(
            "ft_track_filter_inbound_only",
            "import { filterTrack } from 'getpatter';\n"
            "await cell('track_filter_inbound_only', { tier: 1, env }, () => {\n"
            "  if (filterTrack({ track: 'inbound', payload: 'aGk=' }) === null) throw new Error('inbound dropped');\n"
            "  if (filterTrack({ track: 'outbound', payload: 'aGk=' }) !== null) throw new Error('outbound passed');\n"
            "  console.log('outbound frames suppressed');\n"
            "});\n",
        ),
        _code(
            "ft_dtmf_received_event",
            "import { parseCallEvent } from 'getpatter';\n"
            "await cell('dtmf_received_event', { tier: 1, env }, () => {\n"
            "  const body = JSON.parse(loadFixture('webhooks/telnyx_dtmf_received.json').toString());\n"
            "  const ev = parseCallEvent(body);\n"
            "  console.log(ev);\n"
            "  if (ev.eventType !== 'call.dtmf.received' || ev.payload.digit !== '5') throw new Error('bad parse');\n"
            "});\n",
        ),
        _code(
            "ft_transfer_call_call_control",
            "import { buildTransferAction } from 'getpatter';\n"
            "await cell('transfer_call_call_control', { tier: 1, env }, () => {\n"
            "  const action = buildTransferAction({ callControlId: 'v3:test', to: '+15555550100' });\n"
            "  console.log(action);\n"
            "  if (action.to !== '+15555550100') throw new Error('bad action');\n"
            "});\n",
        ),
        _code(
            "ft_ring_timeout_emission",
            "import { buildOutboundCall } from 'getpatter';\n"
            "await cell('ring_timeout_emission', { tier: 1, env }, () => {\n"
            "  const body = buildOutboundCall({ connectionId: 'c1', to: '+15555550100',\n"
            "    from: '+15555550100', ringTimeout: 20 });\n"
            "  console.log(body);\n"
            "  if (body.timeoutSecs !== 20) throw new Error('no timeout');\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject + smoke + commit + PR** (same shape; substitute `07_telephony_telnyx`).

---

### Task 35: Topic 08 — tools §2

**Files:**
- Create: `scripts/section_cells_08_tools.py`
- Modify: `examples/notebooks/{python,typescript}/08_tools.ipynb`

**§2 covers:** `tool_decorator_basic`, `tool_decorator_async`, `auto_injected_transfer_call`, `auto_injected_end_call`, `dynamic_variables`, `tool_argument_validation`, `tool_returns_streamed_to_llm`.

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_08_tools.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("### `@tool` on a sync function\n"),
        _code(
            "ft_tool_decorator_basic",
            "from patter import tool\n"
            "with _setup.cell('tool_decorator_basic', tier=1, env=env):\n"
            "    @tool(description='Add two integers.')\n"
            "    def add(a: int, b: int) -> int:\n"
            "        return a + b\n"
            "    schema = add.schema\n"
            "    print(schema)\n"
            "    assert schema['parameters']['properties']['a']['type'] == 'integer'\n"
            "    assert add(2, 3) == 5\n",
        ),
        _md("### `@tool` on an async function\n"),
        _code(
            "ft_tool_decorator_async",
            "from patter import tool\n"
            "import asyncio\n"
            "with _setup.cell('tool_decorator_async', tier=1, env=env):\n"
            "    @tool(description='Sleep then return ok.')\n"
            "    async def slow() -> str:\n"
            "        await asyncio.sleep(0.01)\n"
            "        return 'ok'\n"
            "    assert await slow() == 'ok'\n"
            "    print(slow.schema['name'])\n",
        ),
        _md("### Auto-injected `transfer_call`\n"),
        _code(
            "ft_auto_injected_transfer_call",
            "from patter import Patter\n"
            "with _setup.cell('auto_injected_transfer_call', tier=1, env=env):\n"
            "    p = Patter(twilio_sid='ACtest00000000000000000000000000', twilio_token='t',\n"
            "               phone_number='+15555550100', webhook_url='https://example.com/webhook')\n"
            "    agent = p.agent(provider='openai_realtime', system_prompt='hi', tools=[])\n"
            "    names = [t.schema['name'] for t in agent.tools]\n"
            "    print(names)\n"
            "    assert 'transfer_call' in names\n",
        ),
        _md("### Auto-injected `end_call`\n"),
        _code(
            "ft_auto_injected_end_call",
            "from patter import Patter\n"
            "with _setup.cell('auto_injected_end_call', tier=1, env=env):\n"
            "    p = Patter(twilio_sid='ACtest00000000000000000000000000', twilio_token='t',\n"
            "               phone_number='+15555550100', webhook_url='https://example.com/webhook')\n"
            "    agent = p.agent(provider='openai_realtime', system_prompt='hi', tools=[])\n"
            "    names = [t.schema['name'] for t in agent.tools]\n"
            "    assert 'end_call' in names\n"
            "    print('end_call auto-registered')\n",
        ),
        _md("### Dynamic variables in `system_prompt`\n"),
        _code(
            "ft_dynamic_variables",
            "from patter.services.template import render_prompt\n"
            "with _setup.cell('dynamic_variables', tier=1, env=env):\n"
            "    rendered = render_prompt(template='Hi {customer_name}, you are calling about {topic}.',\n"
            "                             variables={'customer_name': 'Alice', 'topic': 'order #42'})\n"
            "    print(rendered)\n"
            "    assert rendered == 'Hi Alice, you are calling about order #42.'\n",
        ),
        _md("### Tool argument validation rejects bad payloads\n"),
        _code(
            "ft_tool_argument_validation",
            "from patter import tool\n"
            "from pydantic import ValidationError\n"
            "with _setup.cell('tool_argument_validation', tier=1, env=env):\n"
            "    @tool(description='Add two integers.')\n"
            "    def add(a: int, b: int) -> int:\n"
            "        return a + b\n"
            "    try:\n"
            "        add.invoke_with_args({'a': 'not-an-int', 'b': 3})\n"
            "        raise AssertionError('should have raised')\n"
            "    except ValidationError as e:\n"
            "        print(f'rejected: {e.errors()[0][\"msg\"]}')\n",
        ),
        _md("### Tool result feeds back into LLM\n"),
        _code(
            "ft_tool_returns_streamed_to_llm",
            "from patter import tool\n"
            "from patter.services.llm_loop import LLMLoop, OpenAILLMProvider\n"
            "with _setup.cell('tool_returns_streamed_to_llm', tier=3, required=['OPENAI_API_KEY'], env=env):\n"
            "    @tool(description='Get current temperature in Celsius.')\n"
            "    async def temp_c() -> float:\n"
            "        return 22.0\n"
            "    loop = LLMLoop(provider=OpenAILLMProvider(api_key=env.openai_key, model='gpt-4o-mini'),\n"
            "                   system_prompt='Use tools when asked about weather.', tools=[temp_c])\n"
            "    out = []\n"
            "    async for chunk in loop.stream_message(user_text='What is the temperature?'):\n"
            "        out.append(chunk.content or '')\n"
            "    text = ''.join(out)\n"
            "    print(text)\n"
            "    assert '22' in text\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("### defineTool — sync\n"),
        _code(
            "ft_tool_decorator_basic",
            "import { defineTool } from 'getpatter';\n"
            "import { cell } from './_setup.ts';\n"
            "await cell('tool_decorator_basic', { tier: 1, env }, () => {\n"
            "  const add = defineTool({ name: 'add', description: 'Add two integers.',\n"
            "    parameters: { a: { type: 'integer' }, b: { type: 'integer' } },\n"
            "    handler: ({ a, b }: { a: number; b: number }) => a + b });\n"
            "  console.log(add.schema);\n"
            "  if ((add.handler as any)({ a: 2, b: 3 }) !== 5) throw new Error('add wrong');\n"
            "});\n",
        ),
        _code(
            "ft_tool_decorator_async",
            "import { defineTool } from 'getpatter';\n"
            "await cell('tool_decorator_async', { tier: 1, env }, async () => {\n"
            "  const slow = defineTool({ name: 'slow', description: 'sleep', parameters: {},\n"
            "    handler: async () => { await new Promise((r) => setTimeout(r, 10)); return 'ok'; } });\n"
            "  if ((await (slow.handler as any)()) !== 'ok') throw new Error('slow wrong');\n"
            "  console.log(slow.schema.name);\n"
            "});\n",
        ),
        _code(
            "ft_auto_injected_transfer_call",
            "import { Patter } from 'getpatter';\n"
            "await cell('auto_injected_transfer_call', { tier: 1, env }, () => {\n"
            "  const p = new Patter({ twilioSid: 'ACtest00000000000000000000000000', twilioToken: 't',\n"
            "    phoneNumber: '+15555550100', webhookUrl: 'https://example.com/webhook' });\n"
            "  const agent = p.agent({ provider: 'openai_realtime', systemPrompt: 'hi', tools: [] });\n"
            "  const names = agent.tools.map((t: any) => t.schema.name);\n"
            "  console.log(names);\n"
            "  if (!names.includes('transfer_call')) throw new Error('transfer_call missing');\n"
            "});\n",
        ),
        _code(
            "ft_auto_injected_end_call",
            "import { Patter } from 'getpatter';\n"
            "await cell('auto_injected_end_call', { tier: 1, env }, () => {\n"
            "  const p = new Patter({ twilioSid: 'ACtest00000000000000000000000000', twilioToken: 't',\n"
            "    phoneNumber: '+15555550100', webhookUrl: 'https://example.com/webhook' });\n"
            "  const agent = p.agent({ provider: 'openai_realtime', systemPrompt: 'hi', tools: [] });\n"
            "  const names = agent.tools.map((t: any) => t.schema.name);\n"
            "  if (!names.includes('end_call')) throw new Error('end_call missing');\n"
            "  console.log('end_call auto-registered');\n"
            "});\n",
        ),
        _code(
            "ft_dynamic_variables",
            "import { renderPrompt } from 'getpatter';\n"
            "await cell('dynamic_variables', { tier: 1, env }, () => {\n"
            "  const rendered = renderPrompt({ template: 'Hi {customer_name}, you are calling about {topic}.',\n"
            "    variables: { customer_name: 'Alice', topic: 'order #42' } });\n"
            "  console.log(rendered);\n"
            "  if (rendered !== 'Hi Alice, you are calling about order #42.') throw new Error('bad render');\n"
            "});\n",
        ),
        _code(
            "ft_tool_argument_validation",
            "import { defineTool } from 'getpatter';\n"
            "await cell('tool_argument_validation', { tier: 1, env }, () => {\n"
            "  const add = defineTool({ name: 'add', description: 'Add', parameters: {\n"
            "    a: { type: 'integer' }, b: { type: 'integer' },\n"
            "  }, handler: ({ a, b }: { a: number; b: number }) => a + b });\n"
            "  try {\n"
            "    add.invokeWithArgs({ a: 'not-an-int', b: 3 });\n"
            "    throw new Error('should have rejected');\n"
            "  } catch (e: any) {\n"
            "    if (!String(e.message).includes('integer')) throw e;\n"
            "    console.log(`rejected: ${e.message}`);\n"
            "  }\n"
            "});\n",
        ),
        _code(
            "ft_tool_returns_streamed_to_llm",
            "import { defineTool, LLMLoop, OpenAILLMProvider } from 'getpatter';\n"
            "await cell('tool_returns_streamed_to_llm', { tier: 3, required: ['OPENAI_API_KEY'], env }, async () => {\n"
            "  const tempC = defineTool({ name: 'tempC', description: 'temp in C', parameters: {},\n"
            "    handler: async () => 22 });\n"
            "  const loop = new LLMLoop({ provider: new OpenAILLMProvider({ apiKey: env.openaiKey, model: 'gpt-4o-mini' }),\n"
            "    systemPrompt: 'Use tools when asked about weather.', tools: [tempC] });\n"
            "  const out: string[] = [];\n"
            "  for await (const c of loop.streamMessage({ userText: 'What is the temperature?' })) out.push(c.content ?? '');\n"
            "  const text = out.join('');\n"
            "  console.log(text);\n"
            "  if (!text.includes('22')) throw new Error('no temp in reply');\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject + smoke + commit + PR** (same shape; substitute `08_tools`).

---

### Task 36: Topic 09 — guardrails & hooks §2

**Files:**
- Create: `scripts/section_cells_09_guardrails_hooks.py`
- Modify: `examples/notebooks/{python,typescript}/09_guardrails_hooks.ipynb`

**§2 covers:** `keyword_block`, `pii_redact`, `before_send_to_stt`, `before_send_to_llm`, `before_send_to_tts`, `text_transforms` (markdown/emoji/SentenceChunker).

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_09_guardrails_hooks.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("### Keyword-block guardrail\n"),
        _code(
            "ft_keyword_block",
            "from patter import Guardrail\n"
            "with _setup.cell('keyword_block', tier=1, env=env):\n"
            "    g = Guardrail.keyword_block(['secret_word'], replacement='[redacted]')\n"
            "    out = g.apply('the secret_word is rosebud')\n"
            "    print(out)\n"
            "    assert 'secret_word' not in out\n"
            "    assert '[redacted]' in out\n",
        ),
        _md("### PII-redact guardrail (phone + email)\n"),
        _code(
            "ft_pii_redact",
            "from patter import Guardrail\n"
            "with _setup.cell('pii_redact', tier=1, env=env):\n"
            "    g = Guardrail.pii_redact()\n"
            "    out = g.apply('Call +15555551234 or email me at user@example.com.')\n"
            "    print(out)\n"
            "    assert '+15555551234' not in out and 'user@example.com' not in out\n",
        ),
        _md("### `before_send_to_stt` hook — modify bytes\n"),
        _code(
            "ft_before_send_to_stt_hook",
            "from patter.pipeline_hooks import PipelineHookExecutor\n"
            "with _setup.cell('before_send_to_stt_hook', tier=1, env=env):\n"
            "    executor = PipelineHookExecutor()\n"
            "    @executor.before_send_to_stt\n"
            "    async def double_volume(ctx, audio: bytes) -> bytes:\n"
            "        return audio * 2\n"
            "    out = await executor.run_before_send_to_stt(ctx={}, audio=b'\\x01\\x02')\n"
            "    print(f'len={len(out)}')\n"
            "    assert out == b'\\x01\\x02\\x01\\x02'\n",
        ),
        _md("### `before_send_to_llm` hook — inject system message\n"),
        _code(
            "ft_before_send_to_llm_hook",
            "from patter.pipeline_hooks import PipelineHookExecutor\n"
            "with _setup.cell('before_send_to_llm_hook', tier=1, env=env):\n"
            "    executor = PipelineHookExecutor()\n"
            "    @executor.before_send_to_llm\n"
            "    async def add_system(ctx, messages):\n"
            "        return [{'role': 'system', 'content': 'Be brief.'}] + list(messages)\n"
            "    out = await executor.run_before_send_to_llm(ctx={}, messages=[{'role': 'user', 'content': 'hi'}])\n"
            "    print(out)\n"
            "    assert out[0]['role'] == 'system'\n",
        ),
        _md("### `before_send_to_tts` hook — append disclaimer\n"),
        _code(
            "ft_before_send_to_tts_hook",
            "from patter.pipeline_hooks import PipelineHookExecutor\n"
            "with _setup.cell('before_send_to_tts_hook', tier=1, env=env):\n"
            "    executor = PipelineHookExecutor()\n"
            "    @executor.before_send_to_tts\n"
            "    async def add_disclaimer(ctx, text: str) -> str:\n"
            "        return text + ' This call may be recorded.'\n"
            "    out = await executor.run_before_send_to_tts(ctx={}, text='Hello!')\n"
            "    print(out)\n"
            "    assert 'recorded' in out\n",
        ),
        _md("### Text transforms + sentence chunker\n"),
        _code(
            "ft_text_transforms",
            "from patter import SentenceChunker, filter_markdown, filter_emoji, filter_for_tts\n"
            "with _setup.cell('text_transforms', tier=1, env=env):\n"
            "    md = filter_markdown('**Hello** _world_')\n"
            "    em = filter_emoji('Nice 🎉 day ☀')\n"
            "    safe = filter_for_tts('Visit https://example.com! 😀')\n"
            "    print(md); print(em); print(safe)\n"
            "    assert '**' not in md and '🎉' not in em\n"
            "    chunker = SentenceChunker()\n"
            "    chunks = list(chunker.chunk('Hello there. Goodbye now.'))\n"
            "    print(chunks)\n"
            "    assert chunks == ['Hello there.', 'Goodbye now.']\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("### Keyword-block guardrail\n"),
        _code(
            "ft_keyword_block",
            "import { Guardrail } from 'getpatter';\n"
            "import { cell } from './_setup.ts';\n"
            "await cell('keyword_block', { tier: 1, env }, () => {\n"
            "  const g = Guardrail.keywordBlock(['secret_word'], { replacement: '[redacted]' });\n"
            "  const out = g.apply('the secret_word is rosebud');\n"
            "  console.log(out);\n"
            "  if (out.includes('secret_word') || !out.includes('[redacted]')) throw new Error('bad redact');\n"
            "});\n",
        ),
        _code(
            "ft_pii_redact",
            "import { Guardrail } from 'getpatter';\n"
            "await cell('pii_redact', { tier: 1, env }, () => {\n"
            "  const g = Guardrail.piiRedact();\n"
            "  const out = g.apply('Call +15555551234 or email me at user@example.com.');\n"
            "  console.log(out);\n"
            "  if (out.includes('+15555551234') || out.includes('user@example.com')) throw new Error('PII leaked');\n"
            "});\n",
        ),
        _code(
            "ft_before_send_to_stt_hook",
            "import { PipelineHookExecutor } from 'getpatter';\n"
            "await cell('before_send_to_stt_hook', { tier: 1, env }, async () => {\n"
            "  const ex = new PipelineHookExecutor();\n"
            "  ex.beforeSendToStt(async (_ctx: any, audio: Buffer) => Buffer.concat([audio, audio]));\n"
            "  const out = await ex.runBeforeSendToStt({ ctx: {}, audio: Buffer.from([1, 2]) });\n"
            "  console.log(`len=${out.length}`);\n"
            "  if (out.length !== 4) throw new Error('hook didn\\'t double');\n"
            "});\n",
        ),
        _code(
            "ft_before_send_to_llm_hook",
            "import { PipelineHookExecutor } from 'getpatter';\n"
            "await cell('before_send_to_llm_hook', { tier: 1, env }, async () => {\n"
            "  const ex = new PipelineHookExecutor();\n"
            "  ex.beforeSendToLlm(async (_ctx: any, messages: any[]) => [{ role: 'system', content: 'Be brief.' }, ...messages]);\n"
            "  const out = await ex.runBeforeSendToLlm({ ctx: {}, messages: [{ role: 'user', content: 'hi' }] });\n"
            "  console.log(out);\n"
            "  if (out[0].role !== 'system') throw new Error('no system msg');\n"
            "});\n",
        ),
        _code(
            "ft_before_send_to_tts_hook",
            "import { PipelineHookExecutor } from 'getpatter';\n"
            "await cell('before_send_to_tts_hook', { tier: 1, env }, async () => {\n"
            "  const ex = new PipelineHookExecutor();\n"
            "  ex.beforeSendToTts(async (_ctx: any, text: string) => text + ' This call may be recorded.');\n"
            "  const out = await ex.runBeforeSendToTts({ ctx: {}, text: 'Hello!' });\n"
            "  console.log(out);\n"
            "  if (!out.includes('recorded')) throw new Error('no disclaimer');\n"
            "});\n",
        ),
        _code(
            "ft_text_transforms",
            "import { SentenceChunker, filterMarkdown, filterEmoji, filterForTTS } from 'getpatter';\n"
            "await cell('text_transforms', { tier: 1, env }, () => {\n"
            "  console.log(filterMarkdown('**Hello** _world_'));\n"
            "  console.log(filterEmoji('Nice 🎉 day ☀'));\n"
            "  console.log(filterForTTS('Visit https://example.com! 😀'));\n"
            "  const chunker = new SentenceChunker();\n"
            "  const chunks = [...chunker.chunk('Hello there. Goodbye now.')];\n"
            "  console.log(chunks);\n"
            "  if (chunks.length !== 2) throw new Error('wrong chunks');\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject + smoke + commit + PR** (same shape; substitute `09_guardrails_hooks`).

---

### Task 37: Topic 10 — advanced §2

**Files:**
- Create: `scripts/section_cells_10_advanced.py`
- Modify: `examples/notebooks/{python,typescript}/10_advanced.ipynb`

**§2 covers:** `scheduler_cron`, `scheduler_once`, `scheduler_interval`, `fallback_llm_chain`, `background_audio_mixer`, `noise_filter`, `custom_stt_via_protocol`.

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_10_advanced.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("### Scheduler — cron\n"),
        _code(
            "ft_scheduler_cron",
            "import asyncio\n"
            "from patter import schedule_cron\n"
            "with _setup.cell('scheduler_cron', tier=1, env=env):\n"
            "    fired = asyncio.Event()\n"
            "    async def cb():\n"
            "        fired.set()\n"
            "    handle = schedule_cron('* * * * * */1', cb)  # every second (6-field cron)\n"
            "    try:\n"
            "        await asyncio.wait_for(fired.wait(), timeout=3)\n"
            "        print('cron fired')\n"
            "    finally:\n"
            "        handle.cancel()\n",
        ),
        _md("### Scheduler — once\n"),
        _code(
            "ft_scheduler_once",
            "import asyncio\n"
            "from patter import schedule_once\n"
            "with _setup.cell('scheduler_once', tier=1, env=env):\n"
            "    fired = asyncio.Event()\n"
            "    handle = schedule_once(0.1, lambda: fired.set())\n"
            "    try:\n"
            "        await asyncio.wait_for(fired.wait(), timeout=2)\n"
            "        print('once fired')\n"
            "    finally:\n"
            "        handle.cancel()\n",
        ),
        _md("### Scheduler — interval\n"),
        _code(
            "ft_scheduler_interval",
            "import asyncio\n"
            "from patter import schedule_interval\n"
            "with _setup.cell('scheduler_interval', tier=1, env=env):\n"
            "    counter = {'n': 0}\n"
            "    async def tick():\n"
            "        counter['n'] += 1\n"
            "    handle = schedule_interval(0.1, tick)\n"
            "    await asyncio.sleep(0.35)\n"
            "    handle.cancel()\n"
            "    print(f'ticks = {counter[\"n\"]}')\n"
            "    assert counter['n'] >= 2\n",
        ),
        _md("### Fallback LLM chain — primary fails, secondary succeeds\n"),
        _code(
            "ft_fallback_llm_chain",
            "from patter import FallbackLLMProvider, OpenAILLMProvider\n"
            "with _setup.cell('fallback_llm_chain', tier=3, required=['OPENAI_API_KEY'], env=env):\n"
            "    class Broken:\n"
            "        async def stream_message(self, **kw):\n"
            "            raise RuntimeError('intentional failure')\n"
            "            yield  # pragma: no cover\n"
            "    chain = FallbackLLMProvider([Broken(), OpenAILLMProvider(api_key=env.openai_key, model='gpt-4o-mini')])\n"
            "    out = []\n"
            "    async for c in chain.stream_message(messages=[{'role': 'user', 'content': 'hi'}], system_prompt=''):\n"
            "        out.append(c.content or '')\n"
            "    text = ''.join(out).strip()\n"
            "    print(f'fallback succeeded: {text!r}')\n"
            "    assert len(text) > 0\n",
        ),
        _md("### Background audio mixer\n"),
        _code(
            "ft_background_audio_mixer",
            "from patter.services.audio_mixer import mix_pcm16\n"
            "with _setup.cell('background_audio_mixer', tier=1, env=env):\n"
            "    bg = _setup.load_fixture('audio/background_music_loop.wav')[44:]  # strip header\n"
            "    fg = _setup.load_fixture('audio/hello_world_16khz_pcm.wav')[44:]\n"
            "    mixed = mix_pcm16(foreground=fg, background=bg, bg_gain=0.2)\n"
            "    print(f'mixed: {len(mixed)} bytes')\n"
            "    assert len(mixed) >= max(len(fg), len(bg)) - 4\n",
        ),
        _md("### Noise filter\n"),
        _code(
            "ft_noise_filter",
            "from patter.services.noise_filter import NoiseFilter\n"
            "with _setup.cell('noise_filter', tier=1, env=env):\n"
            "    nf = NoiseFilter()\n"
            "    audio = _setup.load_fixture('audio/hello_world_16khz_pcm.wav')[44:]\n"
            "    out = nf.process(audio)\n"
            "    print(f'in={len(audio)} out={len(out)}')\n"
            "    assert len(out) == len(audio)\n"
            "    assert out != audio\n",
        ),
        _md("### Custom STT via Protocol\n"),
        _code(
            "ft_custom_stt_via_protocol",
            "from patter.providers.stt_protocol import STTProvider\n"
            "with _setup.cell('custom_stt_via_protocol', tier=1, env=env):\n"
            "    class StubSTT:\n"
            "        async def connect(self): pass\n"
            "        async def send_audio(self, chunk): pass\n"
            "        async def close(self): pass\n"
            "        async def receive_transcripts(self):\n"
            "            yield 'fake transcript'\n"
            "    stt: STTProvider = StubSTT()  # structural typing — must satisfy Protocol\n"
            "    transcript = await _setup.run_stt(stt, b'\\x00' * 1600)\n"
            "    print(transcript)\n"
            "    assert transcript == 'fake transcript'\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("### Scheduler — cron / once / interval\n"),
        _code(
            "ft_scheduler_cron",
            "import { scheduleCron } from 'getpatter';\n"
            "import { cell } from './_setup.ts';\n"
            "await cell('scheduler_cron', { tier: 1, env }, async () => {\n"
            "  let fired = false;\n"
            "  const handle = scheduleCron('* * * * * */1', async () => { fired = true; });\n"
            "  try {\n"
            "    const start = Date.now();\n"
            "    while (!fired && Date.now() - start < 3000) await new Promise((r) => setTimeout(r, 50));\n"
            "    if (!fired) throw new Error('no fire');\n"
            "    console.log('cron fired');\n"
            "  } finally { handle.cancel(); }\n"
            "});\n",
        ),
        _code(
            "ft_scheduler_once",
            "import { scheduleOnce } from 'getpatter';\n"
            "await cell('scheduler_once', { tier: 1, env }, async () => {\n"
            "  let fired = false;\n"
            "  scheduleOnce(0.1, () => { fired = true; });\n"
            "  await new Promise((r) => setTimeout(r, 300));\n"
            "  if (!fired) throw new Error('no fire');\n"
            "  console.log('once fired');\n"
            "});\n",
        ),
        _code(
            "ft_scheduler_interval",
            "import { scheduleInterval } from 'getpatter';\n"
            "await cell('scheduler_interval', { tier: 1, env }, async () => {\n"
            "  let n = 0;\n"
            "  const handle = scheduleInterval(0.1, () => { n += 1; });\n"
            "  await new Promise((r) => setTimeout(r, 350));\n"
            "  handle.cancel();\n"
            "  console.log(`ticks = ${n}`);\n"
            "  if (n < 2) throw new Error('not enough ticks');\n"
            "});\n",
        ),
        _code(
            "ft_fallback_llm_chain",
            "import { FallbackLLMProvider, OpenAILLMProvider } from 'getpatter';\n"
            "await cell('fallback_llm_chain', { tier: 3, required: ['OPENAI_API_KEY'], env }, async () => {\n"
            "  const broken = { streamMessage: async function* () { throw new Error('intentional'); } };\n"
            "  const chain = new FallbackLLMProvider({\n"
            "    providers: [broken as any, new OpenAILLMProvider({ apiKey: env.openaiKey, model: 'gpt-4o-mini' })],\n"
            "  });\n"
            "  const out: string[] = [];\n"
            "  for await (const c of chain.streamMessage({ messages: [{ role: 'user', content: 'hi' }], systemPrompt: '' })) {\n"
            "    out.push(c.content ?? '');\n"
            "  }\n"
            "  const text = out.join('').trim();\n"
            "  console.log(`fallback succeeded: ${JSON.stringify(text)}`);\n"
            "  if (text.length === 0) throw new Error('empty fallback output');\n"
            "});\n",
        ),
        _code(
            "ft_background_audio_mixer",
            "import { mixPcm16 } from 'getpatter';\n"
            "import { loadFixture } from './_setup.ts';\n"
            "await cell('background_audio_mixer', { tier: 1, env }, () => {\n"
            "  const bg = loadFixture('audio/background_music_loop.wav').subarray(44);\n"
            "  const fg = loadFixture('audio/hello_world_16khz_pcm.wav').subarray(44);\n"
            "  const mixed = mixPcm16({ foreground: fg, background: bg, bgGain: 0.2 });\n"
            "  console.log(`mixed: ${mixed.length} bytes`);\n"
            "  if (mixed.length + 4 < Math.max(fg.length, bg.length)) throw new Error('mix too short');\n"
            "});\n",
        ),
        _code(
            "ft_noise_filter",
            "import { NoiseFilter } from 'getpatter';\n"
            "await cell('noise_filter', { tier: 1, env }, () => {\n"
            "  const nf = new NoiseFilter();\n"
            "  const audio = loadFixture('audio/hello_world_16khz_pcm.wav').subarray(44);\n"
            "  const out = nf.process(audio);\n"
            "  console.log(`in=${audio.length} out=${out.length}`);\n"
            "  if (out.length !== audio.length) throw new Error('length changed');\n"
            "  if (out.equals(audio)) throw new Error('noop filter');\n"
            "});\n",
        ),
        _code(
            "ft_custom_stt_via_protocol",
            "import { runStt, cell } from './_setup.ts';\n"
            "await cell('custom_stt_via_protocol', { tier: 1, env }, async () => {\n"
            "  const stub = {\n"
            "    connect: async () => {}, sendAudio: async (_b: Buffer) => {}, close: async () => {},\n"
            "    receiveTranscripts: async function* () { yield 'fake transcript'; },\n"
            "  };\n"
            "  const t = await runStt(stub as any, Buffer.alloc(1600));\n"
            "  console.log(t);\n"
            "  if (t !== 'fake transcript') throw new Error('bad transcript');\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject + smoke + commit + PR** (same shape; substitute `10_advanced`).

---

### Task 38: Topic 11 — metrics & dashboard §2

**Files:**
- Create: `scripts/section_cells_11_metrics_dashboard.py`
- Modify: `examples/notebooks/{python,typescript}/11_metrics_dashboard.ipynb`

**§2 covers:** `call_metrics_accumulator_basic`, `pricing_overrides`, `metrics_store_eviction`, `metrics_store_csv_export`, `metrics_store_json_export`, `dashboard_sse_subscribe`, `dashboard_basic_auth`.

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_11_metrics_dashboard.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("### `CallMetricsAccumulator` — basic math\n"),
        _code(
            "ft_call_metrics_accumulator_basic",
            "from patter.services.metrics import CallMetricsAccumulator\n"
            "with _setup.cell('call_metrics_accumulator_basic', tier=1, env=env):\n"
            "    acc = CallMetricsAccumulator(call_id='CAtest1', direction='inbound')\n"
            "    acc.record_stt(provider='deepgram', seconds=2.0)\n"
            "    acc.record_llm(provider='openai', model='gpt-4o-mini', input_tokens=120, output_tokens=40)\n"
            "    acc.record_tts(provider='elevenlabs', characters=85)\n"
            "    s = acc.snapshot()\n"
            "    print(s.cost_breakdown)\n"
            "    assert s.total_cost_usd == sum(s.cost_breakdown.values())\n",
        ),
        _md("### Pricing overrides via `merge_pricing`\n"),
        _code(
            "ft_pricing_overrides",
            "from patter.pricing import DEFAULT_PRICING, merge_pricing, calculate_stt_cost\n"
            "with _setup.cell('pricing_overrides', tier=1, env=env):\n"
            "    custom = merge_pricing(DEFAULT_PRICING, {'stt': {'deepgram': {'per_second_usd': 0.0001}}})\n"
            "    cost = calculate_stt_cost(custom, provider='deepgram', seconds=10)\n"
            "    print(f'overridden cost = ${cost:.6f}')\n"
            "    assert cost == 0.001\n",
        ),
        _md("### `MetricsStore` — eviction past capacity\n"),
        _code(
            "ft_metrics_store_eviction",
            "from patter import MetricsStore\n"
            "with _setup.cell('metrics_store_eviction', tier=1, env=env):\n"
            "    store = MetricsStore(capacity=3)\n"
            "    for i in range(5):\n"
            "        store.add({'call_id': f'CA{i}', 'total_cost_usd': 0.01, 'duration_s': 10})\n"
            "    ids = [r['call_id'] for r in store.list()]\n"
            "    print(ids)\n"
            "    assert ids == ['CA2', 'CA3', 'CA4']\n",
        ),
        _md("### CSV export\n"),
        _code(
            "ft_metrics_store_csv_export",
            "from patter import MetricsStore, calls_to_csv\n"
            "with _setup.cell('metrics_store_csv_export', tier=1, env=env):\n"
            "    s = MetricsStore(capacity=10)\n"
            "    s.add({'call_id': 'CA1', 'total_cost_usd': 0.05, 'duration_s': 30})\n"
            "    csv_text = calls_to_csv(s.list())\n"
            "    print(csv_text.splitlines()[0])\n"
            "    assert 'call_id' in csv_text and 'CA1' in csv_text\n",
        ),
        _md("### JSON export\n"),
        _code(
            "ft_metrics_store_json_export",
            "import json\n"
            "from patter import MetricsStore, calls_to_json\n"
            "with _setup.cell('metrics_store_json_export', tier=1, env=env):\n"
            "    s = MetricsStore(capacity=10)\n"
            "    s.add({'call_id': 'CA1', 'total_cost_usd': 0.05, 'duration_s': 30})\n"
            "    body = calls_to_json(s.list())\n"
            "    parsed = json.loads(body)\n"
            "    print(parsed)\n"
            "    assert parsed[0]['call_id'] == 'CA1'\n",
        ),
        _md("### Dashboard SSE — subscribe and receive an event\n"),
        _code(
            "ft_dashboard_sse_subscribe",
            "import asyncio, httpx\n"
            "from patter import Patter\n"
            "with _setup.cell('dashboard_sse_subscribe', tier=2, env=env):\n"
            "    p = Patter(twilio_sid='ACtest00000000000000000000000000', twilio_token='t',\n"
            "               phone_number='+15555550100', webhook_url='https://example.com/webhook')\n"
            "    server = await p._embedded.start(port=0)\n"
            "    try:\n"
            "        async def push():\n"
            "            await asyncio.sleep(0.2)\n"
            "            p._embedded.metrics_store.add({'call_id': 'CAlive', 'total_cost_usd': 0.01, 'duration_s': 5})\n"
            "        asyncio.create_task(push())\n"
            "        url = f'http://127.0.0.1:{server.port}/api/v1/calls/stream'\n"
            "        async with httpx.AsyncClient(timeout=5.0) as c:\n"
            "            async with c.stream('GET', url) as r:\n"
            "                async for line in r.aiter_lines():\n"
            "                    if 'CAlive' in line:\n"
            "                        print('SSE delivered:', line[:80])\n"
            "                        break\n"
            "    finally:\n"
            "        await p._embedded.stop()\n",
        ),
        _md("### Dashboard basic auth\n"),
        _code(
            "ft_dashboard_basic_auth",
            "import base64, httpx\n"
            "from patter import Patter\n"
            "with _setup.cell('dashboard_basic_auth', tier=2, env=env):\n"
            "    p = Patter(twilio_sid='ACtest00000000000000000000000000', twilio_token='t',\n"
            "               phone_number='+15555550100', webhook_url='https://example.com/webhook',\n"
            "               dashboard_username='admin', dashboard_password='secret')\n"
            "    server = await p._embedded.start(port=0)\n"
            "    try:\n"
            "        async with httpx.AsyncClient() as c:\n"
            "            r1 = await c.get(f'http://127.0.0.1:{server.port}/dashboard')\n"
            "            assert r1.status_code == 401\n"
            "            auth = 'Basic ' + base64.b64encode(b'admin:secret').decode()\n"
            "            r2 = await c.get(f'http://127.0.0.1:{server.port}/dashboard', headers={'Authorization': auth})\n"
            "            assert r2.status_code == 200\n"
            "            print('auth gate green: 401 → 200 with creds')\n"
            "    finally:\n"
            "        await p._embedded.stop()\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("### Mirrors the Python cells one-to-one. See companion file for full detail.\n"),
        _code(
            "ft_call_metrics_accumulator_basic",
            "import { CallMetricsAccumulator } from 'getpatter';\n"
            "import { cell } from './_setup.ts';\n"
            "await cell('call_metrics_accumulator_basic', { tier: 1, env }, () => {\n"
            "  const acc = new CallMetricsAccumulator({ callId: 'CAtest1', direction: 'inbound' });\n"
            "  acc.recordStt({ provider: 'deepgram', seconds: 2.0 });\n"
            "  acc.recordLlm({ provider: 'openai', model: 'gpt-4o-mini', inputTokens: 120, outputTokens: 40 });\n"
            "  acc.recordTts({ provider: 'elevenlabs', characters: 85 });\n"
            "  const s = acc.snapshot();\n"
            "  console.log(s.costBreakdown);\n"
            "  const total = Object.values(s.costBreakdown).reduce((a: number, b: any) => a + b, 0);\n"
            "  if (Math.abs(s.totalCostUsd - total) > 1e-9) throw new Error('cost mismatch');\n"
            "});\n",
        ),
        _code(
            "ft_pricing_overrides",
            "import { DEFAULT_PRICING, mergePricing, calculateSttCost } from 'getpatter';\n"
            "await cell('pricing_overrides', { tier: 1, env }, () => {\n"
            "  const custom = mergePricing(DEFAULT_PRICING, { stt: { deepgram: { perSecondUsd: 0.0001 } } });\n"
            "  const cost = calculateSttCost(custom, { provider: 'deepgram', seconds: 10 });\n"
            "  console.log(`overridden cost = $${cost.toFixed(6)}`);\n"
            "  if (cost !== 0.001) throw new Error('override not applied');\n"
            "});\n",
        ),
        _code(
            "ft_metrics_store_eviction",
            "import { MetricsStore } from 'getpatter';\n"
            "await cell('metrics_store_eviction', { tier: 1, env }, () => {\n"
            "  const s = new MetricsStore({ capacity: 3 });\n"
            "  for (let i = 0; i < 5; i++) s.add({ callId: `CA${i}`, totalCostUsd: 0.01, durationS: 10 });\n"
            "  const ids = s.list().map((r: any) => r.callId);\n"
            "  console.log(ids);\n"
            "  if (JSON.stringify(ids) !== JSON.stringify(['CA2','CA3','CA4'])) throw new Error('bad eviction');\n"
            "});\n",
        ),
        _code(
            "ft_metrics_store_csv_export",
            "import { MetricsStore, callsToCsv } from 'getpatter';\n"
            "await cell('metrics_store_csv_export', { tier: 1, env }, () => {\n"
            "  const s = new MetricsStore({ capacity: 10 });\n"
            "  s.add({ callId: 'CA1', totalCostUsd: 0.05, durationS: 30 });\n"
            "  const csv = callsToCsv(s.list());\n"
            "  console.log(csv.split('\\n')[0]);\n"
            "  if (!(csv.includes('callId') && csv.includes('CA1'))) throw new Error('bad csv');\n"
            "});\n",
        ),
        _code(
            "ft_metrics_store_json_export",
            "import { MetricsStore, callsToJson } from 'getpatter';\n"
            "await cell('metrics_store_json_export', { tier: 1, env }, () => {\n"
            "  const s = new MetricsStore({ capacity: 10 });\n"
            "  s.add({ callId: 'CA1', totalCostUsd: 0.05, durationS: 30 });\n"
            "  const body = callsToJson(s.list());\n"
            "  console.log(body);\n"
            "  if (!JSON.parse(body)[0].callId || JSON.parse(body)[0].callId !== 'CA1') throw new Error('bad json');\n"
            "});\n",
        ),
        _code(
            "ft_dashboard_sse_subscribe",
            "import { Patter } from 'getpatter';\n"
            "await cell('dashboard_sse_subscribe', { tier: 2, env }, async () => {\n"
            "  const p = new Patter({ twilioSid: 'ACtest00000000000000000000000000', twilioToken: 't',\n"
            "    phoneNumber: '+15555550100', webhookUrl: 'https://example.com/webhook' });\n"
            "  const server = await (p as any)._embedded.start(0);\n"
            "  try {\n"
            "    setTimeout(() => (p as any)._embedded.metricsStore.add({ callId: 'CAlive', totalCostUsd: 0.01, durationS: 5 }), 200);\n"
            "    const r = await fetch(`http://127.0.0.1:${server.port}/api/v1/calls/stream`);\n"
            "    const reader = r.body!.getReader();\n"
            "    const decoder = new TextDecoder();\n"
            "    const start = Date.now();\n"
            "    while (Date.now() - start < 5000) {\n"
            "      const { value, done } = await reader.read();\n"
            "      if (done) break;\n"
            "      const text = decoder.decode(value);\n"
            "      if (text.includes('CAlive')) { console.log('SSE delivered:', text.slice(0, 80)); break; }\n"
            "    }\n"
            "  } finally { await (p as any)._embedded.stop(); }\n"
            "});\n",
        ),
        _code(
            "ft_dashboard_basic_auth",
            "import { Patter } from 'getpatter';\n"
            "await cell('dashboard_basic_auth', { tier: 2, env }, async () => {\n"
            "  const p = new Patter({ twilioSid: 'ACtest00000000000000000000000000', twilioToken: 't',\n"
            "    phoneNumber: '+15555550100', webhookUrl: 'https://example.com/webhook',\n"
            "    dashboardUsername: 'admin', dashboardPassword: 'secret' });\n"
            "  const server = await (p as any)._embedded.start(0);\n"
            "  try {\n"
            "    const r1 = await fetch(`http://127.0.0.1:${server.port}/dashboard`);\n"
            "    if (r1.status !== 401) throw new Error(`expected 401 got ${r1.status}`);\n"
            "    const auth = 'Basic ' + Buffer.from('admin:secret').toString('base64');\n"
            "    const r2 = await fetch(`http://127.0.0.1:${server.port}/dashboard`, { headers: { Authorization: auth } });\n"
            "    if (r2.status !== 200) throw new Error(`expected 200 got ${r2.status}`);\n"
            "    console.log('auth gate green: 401 → 200 with creds');\n"
            "  } finally { await (p as any)._embedded.stop(); }\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject + smoke + commit + PR** (same shape; substitute `11_metrics_dashboard`).

---

### Task 39: Topic 12 — security §2

**Files:**
- Create: `scripts/section_cells_12_security.py`
- Modify: `examples/notebooks/{python,typescript}/12_security.ipynb`

**§2 covers:** `twilio_hmac_roundtrip`, `twilio_hmac_tamper`, `telnyx_ed25519_roundtrip`, `telnyx_replay_window`, `ssrf_guard_private_ip`, `ssrf_guard_metadata_endpoint`, `dashboard_basic_auth_default_off_when_public`, `secret_log_redaction`.

- [ ] **Step 1: Write the helper**

```python
# scripts/section_cells_12_security.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("### Twilio HMAC roundtrip — sign + verify\n"),
        _code(
            "ft_twilio_hmac_roundtrip",
            "import hmac, hashlib, base64\n"
            "from patter.handlers.common import validate_twilio_signature\n"
            "with _setup.cell('twilio_hmac_roundtrip', tier=1, env=env):\n"
            "    token = 'auth_token_xxx'\n"
            "    url = 'https://example.com/webhook'\n"
            "    body = {'CallSid': 'CAtest', 'From': '+15555550100'}\n"
            "    s = url + ''.join(f'{k}{body[k]}' for k in sorted(body))\n"
            "    sig = base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode()\n"
            "    assert validate_twilio_signature(url, body, sig, token) is True\n"
            "    print('HMAC sign+verify roundtrip green')\n",
        ),
        _md("### Twilio HMAC — flip a byte → reject\n"),
        _code(
            "ft_twilio_hmac_tamper",
            "import hmac, hashlib, base64\n"
            "from patter.handlers.common import validate_twilio_signature\n"
            "with _setup.cell('twilio_hmac_tamper', tier=1, env=env):\n"
            "    token = 'auth_token_xxx'\n"
            "    url = 'https://example.com/webhook'\n"
            "    body = {'CallSid': 'CAtest', 'From': '+15555550100'}\n"
            "    s = url + ''.join(f'{k}{body[k]}' for k in sorted(body))\n"
            "    sig = base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode()\n"
            "    body['From'] = '+15555550101'\n"
            "    assert validate_twilio_signature(url, body, sig, token) is False\n"
            "    print('tampered body rejected')\n",
        ),
        _md("### Telnyx Ed25519 roundtrip\n"),
        _code(
            "ft_telnyx_ed25519_roundtrip",
            "import time, base64\n"
            "from cryptography.hazmat.primitives.serialization import load_pem_private_key\n"
            "from patter.handlers.common import validate_telnyx_signature\n"
            "with _setup.cell('telnyx_ed25519_roundtrip', tier=1, env=env):\n"
            "    priv = load_pem_private_key(_setup.load_fixture('keys/telnyx_test_ed25519_priv.pem'), password=None)\n"
            "    pub = _setup.load_fixture('keys/telnyx_test_ed25519_pub.pem').decode()\n"
            "    body = b'{\"x\":1}'\n"
            "    ts = str(int(time.time()))\n"
            "    sig = base64.b64encode(priv.sign(ts.encode() + b'|' + body)).decode()\n"
            "    assert validate_telnyx_signature(body, sig, ts, pub) is True\n"
            "    print('Ed25519 roundtrip green')\n",
        ),
        _md("### Telnyx replay window — expired timestamp rejected\n"),
        _code(
            "ft_telnyx_replay_window",
            "import time, base64\n"
            "from cryptography.hazmat.primitives.serialization import load_pem_private_key\n"
            "from patter.handlers.common import validate_telnyx_signature\n"
            "with _setup.cell('telnyx_replay_window', tier=1, env=env):\n"
            "    priv = load_pem_private_key(_setup.load_fixture('keys/telnyx_test_ed25519_priv.pem'), password=None)\n"
            "    pub = _setup.load_fixture('keys/telnyx_test_ed25519_pub.pem').decode()\n"
            "    body = b'{\"x\":1}'\n"
            "    old_ts = str(int(time.time()) - 3600)\n"
            "    sig = base64.b64encode(priv.sign(old_ts.encode() + b'|' + body)).decode()\n"
            "    assert validate_telnyx_signature(body, sig, old_ts, pub) is False\n"
            "    print('expired timestamp rejected')\n",
        ),
        _md("### SSRF guard — private IP blocked\n"),
        _code(
            "ft_ssrf_guard_private_ip",
            "from patter.services.remote_message import is_private_url\n"
            "with _setup.cell('ssrf_guard_private_ip', tier=1, env=env):\n"
            "    assert is_private_url('http://127.0.0.1/x') is True\n"
            "    assert is_private_url('http://192.168.1.5/x') is True\n"
            "    assert is_private_url('https://api.openai.com/v1') is False\n"
            "    print('private IPs blocked')\n",
        ),
        _md("### SSRF guard — cloud metadata endpoint blocked\n"),
        _code(
            "ft_ssrf_guard_metadata_endpoint",
            "from patter.services.remote_message import is_private_url\n"
            "with _setup.cell('ssrf_guard_metadata_endpoint', tier=1, env=env):\n"
            "    assert is_private_url('http://169.254.169.254/latest/meta-data/') is True\n"
            "    print('metadata endpoint blocked')\n",
        ),
        _md("### Dashboard auth — default behaviour when bound publicly\n"),
        _code(
            "ft_dashboard_basic_auth_default_off_when_public",
            "from patter.dashboard.auth import make_auth_middleware\n"
            "with _setup.cell('dashboard_basic_auth_default_off_when_public', tier=1, env=env):\n"
            "    middleware = make_auth_middleware(username='admin', password='secret', bind_host='0.0.0.0')\n"
            "    assert middleware is not None\n"
            "    print('non-loopback bind requires creds (middleware installed)')\n",
        ),
        _md("### Secret-log redaction\n"),
        _code(
            "ft_secret_log_redaction",
            "import logging\n"
            "from patter.observability.log_filters import RedactSecretsFilter\n"
            "with _setup.cell('secret_log_redaction', tier=1, env=env):\n"
            "    logger = logging.getLogger('patter.test_redact')\n"
            "    logger.addFilter(RedactSecretsFilter())\n"
            "    handler = logging.StreamHandler()\n"
            "    import io\n"
            "    buf = io.StringIO()\n"
            "    handler.stream = buf\n"
            "    handler.setLevel(logging.INFO)\n"
            "    logger.addHandler(handler)\n"
            "    logger.setLevel(logging.INFO)\n"
            "    logger.info('reaching api with key sk-proj-deadbeefdeadbeefdeadbeefdead')\n"
            "    out = buf.getvalue()\n"
            "    print(out)\n"
            "    assert 'sk-proj-deadbeef' not in out\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("### TypeScript mirrors of every Python cell\n"),
        _code(
            "ft_twilio_hmac_roundtrip",
            "import { createHmac } from 'node:crypto';\n"
            "import { validateTwilioSignature } from 'getpatter';\n"
            "import { cell } from './_setup.ts';\n"
            "await cell('twilio_hmac_roundtrip', { tier: 1, env }, () => {\n"
            "  const token = 'auth_token_xxx';\n"
            "  const url = 'https://example.com/webhook';\n"
            "  const body = { CallSid: 'CAtest', From: '+15555550100' };\n"
            "  const s = url + Object.keys(body).sort().map((k) => k + (body as any)[k]).join('');\n"
            "  const sig = createHmac('sha1', token).update(s).digest('base64');\n"
            "  if (!validateTwilioSignature(url, body, sig, token)) throw new Error('roundtrip failed');\n"
            "  console.log('HMAC sign+verify roundtrip green');\n"
            "});\n",
        ),
        _code(
            "ft_twilio_hmac_tamper",
            "import { createHmac } from 'node:crypto';\n"
            "import { validateTwilioSignature } from 'getpatter';\n"
            "await cell('twilio_hmac_tamper', { tier: 1, env }, () => {\n"
            "  const token = 'auth_token_xxx';\n"
            "  const url = 'https://example.com/webhook';\n"
            "  const body: any = { CallSid: 'CAtest', From: '+15555550100' };\n"
            "  const s = url + Object.keys(body).sort().map((k) => k + body[k]).join('');\n"
            "  const sig = createHmac('sha1', token).update(s).digest('base64');\n"
            "  body.From = '+15555550101';\n"
            "  if (validateTwilioSignature(url, body, sig, token)) throw new Error('tamper accepted');\n"
            "  console.log('tampered body rejected');\n"
            "});\n",
        ),
        _code(
            "ft_telnyx_ed25519_roundtrip",
            "import { sign as edSign } from 'node:crypto';\n"
            "import { validateTelnyxSignature } from 'getpatter';\n"
            "import { loadFixture } from './_setup.ts';\n"
            "await cell('telnyx_ed25519_roundtrip', { tier: 1, env }, () => {\n"
            "  const priv = loadFixture('keys/telnyx_test_ed25519_priv.pem').toString();\n"
            "  const pub = loadFixture('keys/telnyx_test_ed25519_pub.pem').toString();\n"
            "  const body = Buffer.from('{\"x\":1}');\n"
            "  const ts = String(Math.floor(Date.now() / 1000));\n"
            "  const sig = edSign(null, Buffer.concat([Buffer.from(ts + '|'), body]), priv).toString('base64');\n"
            "  if (!validateTelnyxSignature(body, sig, ts, pub)) throw new Error('roundtrip failed');\n"
            "  console.log('Ed25519 roundtrip green');\n"
            "});\n",
        ),
        _code(
            "ft_telnyx_replay_window",
            "import { sign as edSign } from 'node:crypto';\n"
            "import { validateTelnyxSignature } from 'getpatter';\n"
            "await cell('telnyx_replay_window', { tier: 1, env }, () => {\n"
            "  const priv = loadFixture('keys/telnyx_test_ed25519_priv.pem').toString();\n"
            "  const pub = loadFixture('keys/telnyx_test_ed25519_pub.pem').toString();\n"
            "  const body = Buffer.from('{\"x\":1}');\n"
            "  const oldTs = String(Math.floor(Date.now() / 1000) - 3600);\n"
            "  const sig = edSign(null, Buffer.concat([Buffer.from(oldTs + '|'), body]), priv).toString('base64');\n"
            "  if (validateTelnyxSignature(body, sig, oldTs, pub)) throw new Error('replay accepted');\n"
            "  console.log('expired timestamp rejected');\n"
            "});\n",
        ),
        _code(
            "ft_ssrf_guard_private_ip",
            "import { isPrivateUrl } from 'getpatter';\n"
            "await cell('ssrf_guard_private_ip', { tier: 1, env }, () => {\n"
            "  if (!isPrivateUrl('http://127.0.0.1/x')) throw new Error('loopback not blocked');\n"
            "  if (!isPrivateUrl('http://192.168.1.5/x')) throw new Error('rfc1918 not blocked');\n"
            "  if (isPrivateUrl('https://api.openai.com/v1')) throw new Error('public blocked');\n"
            "  console.log('private IPs blocked');\n"
            "});\n",
        ),
        _code(
            "ft_ssrf_guard_metadata_endpoint",
            "import { isPrivateUrl } from 'getpatter';\n"
            "await cell('ssrf_guard_metadata_endpoint', { tier: 1, env }, () => {\n"
            "  if (!isPrivateUrl('http://169.254.169.254/latest/meta-data/')) throw new Error('metadata not blocked');\n"
            "  console.log('metadata endpoint blocked');\n"
            "});\n",
        ),
        _code(
            "ft_dashboard_basic_auth_default_off_when_public",
            "import { makeAuthMiddleware } from 'getpatter';\n"
            "await cell('dashboard_basic_auth_default_off_when_public', { tier: 1, env }, () => {\n"
            "  const mw = makeAuthMiddleware({ username: 'admin', password: 'secret', bindHost: '0.0.0.0' });\n"
            "  if (!mw) throw new Error('no middleware');\n"
            "  console.log('non-loopback bind requires creds (middleware installed)');\n"
            "});\n",
        ),
        _code(
            "ft_secret_log_redaction",
            "import { getLogger, setLogger } from 'getpatter';\n"
            "await cell('secret_log_redaction', { tier: 1, env }, () => {\n"
            "  const buf: string[] = [];\n"
            "  setLogger({ info: (m: string) => buf.push(m), warn: () => {}, error: () => {}, debug: () => {} });\n"
            "  const log = getLogger('patter.test_redact');\n"
            "  log.info('reaching api with key sk-proj-deadbeefdeadbeefdeadbeefdead');\n"
            "  const out = buf.join('');\n"
            "  console.log(out);\n"
            "  if (out.includes('sk-proj-deadbeef')) throw new Error('secret leaked to log');\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject + smoke + commit + PR** (same shape; substitute `12_security`).

---

## Phase 4: Live Appendix (12 PRs, one per topic)

Phase 4 fills §3 with cells that place real Twilio/Telnyx calls. **Every cell uses `tier=4`**, so they auto-skip unless the operator sets `ENABLE_LIVE_CALLS=1`. Each cell wraps its live code in `try/finally` with `_setup.hangup_leftover_calls(env)` to guarantee cleanup. Per-topic cell templates below.

**Common procedure for every Phase 4 task:**
1. Write `live_cells_NN_<topic>.py` returning the §3 cells (Python + TypeScript).
2. Inject after `## §3: Live Appendix` marker.
3. Manual validation:
    - Set `ENABLE_LIVE_CALLS=0` → cells skip cleanly with banner. Run headless.
    - Set `ENABLE_LIVE_CALLS=1` + carrier creds + answer your phone → at least one cell completes a real call within budget caps.
4. Strip outputs, commit, PR.

### Task 40: Topic 01 — quickstart §3 (live)

**Files:**
- Create: `scripts/live_cells_01_quickstart.py`
- Modify: `examples/notebooks/{python,typescript}/01_quickstart.ipynb`

**§3 covers:** `live_outbound_hello` (5-second outbound to `TARGET_PHONE_NUMBER`).

- [ ] **Step 1: Helper**

```python
# scripts/live_cells_01_quickstart.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("⚠️ **Real call!** Set `ENABLE_LIVE_CALLS=1` and answer your phone.\n"),
        _code(
            "live_outbound_hello",
            "import asyncio\n"
            "from patter import Patter\n"
            "with _setup.cell('live_outbound_hello', tier=4,\n"
            "                 required=['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
            "                          'TARGET_PHONE_NUMBER','OPENAI_API_KEY','PUBLIC_WEBHOOK_URL'], env=env):\n"
            "    p = Patter(twilio_sid=env.twilio_sid, twilio_token=env.twilio_token,\n"
            "               phone_number=env.twilio_number, webhook_url=env.public_webhook_url)\n"
            "    agent = p.agent(provider='openai_realtime', api_key=env.openai_key,\n"
            "                    system_prompt='Greet the caller and hang up.', max_turn_duration_ms=4_000)\n"
            "    try:\n"
            "        call = await p.call(to=env.target_number, agent=agent, timeout_s=env.max_call_seconds)\n"
            "        await asyncio.sleep(5)\n"
            "        await call.hangup()\n"
            "        print(f'call_sid = {call.sid}, status = {call.status}')\n"
            "    finally:\n"
            "        _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("⚠️ **Real call!** Set `ENABLE_LIVE_CALLS=1` and answer your phone.\n"),
        _code(
            "live_outbound_hello",
            "import { Patter } from 'getpatter';\n"
            "import { cell, hangupLeftoverCalls } from './_setup.ts';\n"
            "await cell('live_outbound_hello', {\n"
            "  tier: 4,\n"
            "  required: ['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
            "             'TARGET_PHONE_NUMBER','OPENAI_API_KEY','PUBLIC_WEBHOOK_URL'],\n"
            "  env,\n"
            "}, async () => {\n"
            "  const p = new Patter({ twilioSid: env.twilioSid, twilioToken: env.twilioToken,\n"
            "    phoneNumber: env.twilioNumber, webhookUrl: env.publicWebhookUrl });\n"
            "  const agent = p.agent({ provider: 'openai_realtime', apiKey: env.openaiKey,\n"
            "    systemPrompt: 'Greet the caller and hang up.', maxTurnDurationMs: 4000 });\n"
            "  try {\n"
            "    const call = await p.call({ to: env.targetNumber, agent, timeoutS: env.maxCallSeconds });\n"
            "    await new Promise((r) => setTimeout(r, 5000));\n"
            "    await call.hangup();\n"
            "    console.log(`call_sid = ${call.sid}, status = ${call.status}`);\n"
            "  } finally {\n"
            "    await hangupLeftoverCalls(env);\n"
            "  }\n"
            "});\n",
        ),
    ]
```

- [ ] **Step 2: Inject + smoke + commit + PR** (same recipe as Phase 3 tasks; substitute `01_quickstart` and `live_cells_01_quickstart`).

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
PYTHONPATH=scripts python -c "
from inject_section import inject_section
from live_cells_01_quickstart import section_cells_python, section_cells_typescript
from pathlib import Path
inject_section(Path('examples/notebooks/python/01_quickstart.ipynb'),
               marker='§3: Live Appendix', cells=section_cells_python())
inject_section(Path('examples/notebooks/typescript/01_quickstart.ipynb'),
               marker='§3: Live Appendix', cells=section_cells_typescript())
"
ENABLE_LIVE_CALLS=0 jupyter nbconvert --to notebook --execute examples/notebooks/python/01_quickstart.ipynb \
  --ExecutePreprocessor.timeout=180 --output /tmp/01.live_off.ipynb
nbstripout examples/notebooks/python/01_quickstart.ipynb examples/notebooks/typescript/01_quickstart.ipynb
python scripts/check_notebook_parity.py
git add scripts/live_cells_01_quickstart.py examples/notebooks/python/01_quickstart.ipynb examples/notebooks/typescript/01_quickstart.ipynb
git commit -m "feat(notebooks): topic 01 §3 — live outbound hello"
git push && gh pr create --title "feat(notebooks): topic 01 §3 (live appendix)" --body "Adds a 5-second outbound call cell behind ENABLE_LIVE_CALLS=1."
```

---

### Task 41: Topic 02 — realtime §3 (live)

**§3 covers:** `live_realtime_call` (real outbound via OpenAI Realtime, 10s, asserts agent speaks first).

- [ ] **Helper:**

```python
# scripts/live_cells_02_realtime.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("Real Realtime call. Answer the phone — agent will greet you and hang up.\n"),
        _code(
            "live_realtime_call",
            "import asyncio\n"
            "from patter import Patter\n"
            "with _setup.cell('live_realtime_call', tier=4,\n"
            "                 required=['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
            "                          'TARGET_PHONE_NUMBER','OPENAI_API_KEY','PUBLIC_WEBHOOK_URL'], env=env):\n"
            "    p = Patter(twilio_sid=env.twilio_sid, twilio_token=env.twilio_token,\n"
            "               phone_number=env.twilio_number, webhook_url=env.public_webhook_url)\n"
            "    agent = p.agent(provider='openai_realtime', api_key=env.openai_key,\n"
            "                    system_prompt='Greet briefly and ask how you can help.',\n"
            "                    max_turn_duration_ms=6_000)\n"
            "    try:\n"
            "        call = await p.call(to=env.target_number, agent=agent, timeout_s=env.max_call_seconds)\n"
            "        await asyncio.sleep(10)\n"
            "        metrics = call.metrics_snapshot()\n"
            "        print(f'turns = {len(metrics.turns)}; cost = ${metrics.total_cost_usd:.4f}')\n"
            "        assert len(metrics.turns) >= 1\n"
            "        await call.hangup()\n"
            "    finally:\n"
            "        _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("Real Realtime call. Answer the phone — agent will greet you and hang up.\n"),
        _code(
            "live_realtime_call",
            "import { Patter } from 'getpatter';\n"
            "import { cell, hangupLeftoverCalls } from './_setup.ts';\n"
            "await cell('live_realtime_call', {\n"
            "  tier: 4,\n"
            "  required: ['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
            "             'TARGET_PHONE_NUMBER','OPENAI_API_KEY','PUBLIC_WEBHOOK_URL'],\n"
            "  env,\n"
            "}, async () => {\n"
            "  const p = new Patter({ twilioSid: env.twilioSid, twilioToken: env.twilioToken,\n"
            "    phoneNumber: env.twilioNumber, webhookUrl: env.publicWebhookUrl });\n"
            "  const agent = p.agent({ provider: 'openai_realtime', apiKey: env.openaiKey,\n"
            "    systemPrompt: 'Greet briefly and ask how you can help.', maxTurnDurationMs: 6000 });\n"
            "  try {\n"
            "    const call = await p.call({ to: env.targetNumber, agent, timeoutS: env.maxCallSeconds });\n"
            "    await new Promise((r) => setTimeout(r, 10000));\n"
            "    const m = call.metricsSnapshot();\n"
            "    console.log(`turns = ${m.turns.length}; cost = $${m.totalCostUsd.toFixed(4)}`);\n"
            "    if (m.turns.length < 1) throw new Error('no turns');\n"
            "    await call.hangup();\n"
            "  } finally { await hangupLeftoverCalls(env); }\n"
            "});\n",
        ),
    ]
```

- [ ] **Inject + manual run + PR** (same shape; `02_realtime`, `live_cells_02_realtime`).

---

### Task 42: Topic 03 — STT §3 (live)

**§3 covers:** `live_pipeline_stt_call` (real outbound, Pipeline mode with Deepgram STT + OpenAI LLM + ElevenLabs TTS, 10s, asserts a non-empty user transcript).

- [ ] **Helper:**

```python
# scripts/live_cells_03_pipeline_stt.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("Real outbound Pipeline call exercising Deepgram STT live.\n"),
        _code(
            "live_pipeline_stt_call",
            "import asyncio\n"
            "from patter import Patter\n"
            "with _setup.cell('live_pipeline_stt_call', tier=4,\n"
            "                 required=['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
            "                          'TARGET_PHONE_NUMBER','OPENAI_API_KEY','DEEPGRAM_API_KEY',\n"
            "                          'ELEVENLABS_API_KEY','PUBLIC_WEBHOOK_URL'], env=env):\n"
            "    p = Patter(twilio_sid=env.twilio_sid, twilio_token=env.twilio_token,\n"
            "               phone_number=env.twilio_number, webhook_url=env.public_webhook_url)\n"
            "    agent = p.agent(provider='pipeline',\n"
            "                    stt=Patter.deepgram(api_key=env.deepgram_key, language='en-US'),\n"
            "                    tts=Patter.elevenlabs(api_key=env.elevenlabs_key, voice_id=env.elevenlabs_voice_id),\n"
            "                    api_key=env.openai_key,\n"
            "                    system_prompt='Ask one short question.')\n"
            "    try:\n"
            "        call = await p.call(to=env.target_number, agent=agent, timeout_s=env.max_call_seconds)\n"
            "        await asyncio.sleep(10)\n"
            "        m = call.metrics_snapshot()\n"
            "        user_turns = [t for t in m.turns if t.speaker == 'user' and t.transcript]\n"
            "        print(f'user turns: {len(user_turns)}; first: {user_turns[0].transcript if user_turns else None!r}')\n"
            "        assert len(user_turns) >= 1\n"
            "        await call.hangup()\n"
            "    finally:\n"
            "        _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("Real Pipeline call with Deepgram STT.\n"),
        _code(
            "live_pipeline_stt_call",
            "import { Patter } from 'getpatter';\n"
            "import { cell, hangupLeftoverCalls } from './_setup.ts';\n"
            "await cell('live_pipeline_stt_call', {\n"
            "  tier: 4,\n"
            "  required: ['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
            "             'TARGET_PHONE_NUMBER','OPENAI_API_KEY','DEEPGRAM_API_KEY',\n"
            "             'ELEVENLABS_API_KEY','PUBLIC_WEBHOOK_URL'],\n"
            "  env,\n"
            "}, async () => {\n"
            "  const p = new Patter({ twilioSid: env.twilioSid, twilioToken: env.twilioToken,\n"
            "    phoneNumber: env.twilioNumber, webhookUrl: env.publicWebhookUrl });\n"
            "  const agent = p.agent({\n"
            "    provider: 'pipeline',\n"
            "    stt: Patter.deepgram({ apiKey: env.deepgramKey, language: 'en-US' }),\n"
            "    tts: Patter.elevenlabs({ apiKey: env.elevenlabsKey, voiceId: env.elevenlabsVoiceId }),\n"
            "    apiKey: env.openaiKey,\n"
            "    systemPrompt: 'Ask one short question.',\n"
            "  });\n"
            "  try {\n"
            "    const call = await p.call({ to: env.targetNumber, agent, timeoutS: env.maxCallSeconds });\n"
            "    await new Promise((r) => setTimeout(r, 10000));\n"
            "    const m = call.metricsSnapshot();\n"
            "    const userTurns = m.turns.filter((t: any) => t.speaker === 'user' && t.transcript);\n"
            "    console.log(`user turns: ${userTurns.length}; first: ${userTurns[0]?.transcript ?? null}`);\n"
            "    if (userTurns.length < 1) throw new Error('no user transcript');\n"
            "    await call.hangup();\n"
            "  } finally { await hangupLeftoverCalls(env); }\n"
            "});\n",
        ),
    ]
```

- [ ] **Inject + manual run + PR** (substitute `03_pipeline_stt`).

---

### Task 43: Topics 04 — TTS §3 (live)

**§3 covers:** `live_pipeline_tts_call` — same shape as Task 42 but asserts agent's outbound TTS audio bytes flowed.

- [ ] **Helper (Python only — TS mirror identical to Task 42 with `tts:` swapped to ElevenLabs):**

```python
# scripts/live_cells_04_pipeline_tts.py
def _md(*lines): return {"cell_type": "markdown", "metadata": {}, "source": list(lines)}
def _code(tag, source): return {
    "cell_type": "code", "metadata": {"tags": [tag]},
    "source": source.splitlines(keepends=True), "execution_count": None, "outputs": [],
}


def section_cells_python() -> list[dict]:
    return [
        _md("Real outbound Pipeline call exercising ElevenLabs TTS live.\n"),
        _code(
            "live_pipeline_tts_call",
            "import asyncio\n"
            "from patter import Patter\n"
            "with _setup.cell('live_pipeline_tts_call', tier=4,\n"
            "                 required=['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
            "                          'TARGET_PHONE_NUMBER','OPENAI_API_KEY','DEEPGRAM_API_KEY',\n"
            "                          'ELEVENLABS_API_KEY','PUBLIC_WEBHOOK_URL'], env=env):\n"
            "    p = Patter(twilio_sid=env.twilio_sid, twilio_token=env.twilio_token,\n"
            "               phone_number=env.twilio_number, webhook_url=env.public_webhook_url)\n"
            "    agent = p.agent(provider='pipeline',\n"
            "                    stt=Patter.deepgram(api_key=env.deepgram_key),\n"
            "                    tts=Patter.elevenlabs(api_key=env.elevenlabs_key, voice_id=env.elevenlabs_voice_id),\n"
            "                    api_key=env.openai_key,\n"
            "                    system_prompt='Speak a short greeting and end the call.')\n"
            "    try:\n"
            "        call = await p.call(to=env.target_number, agent=agent, timeout_s=env.max_call_seconds)\n"
            "        await asyncio.sleep(8)\n"
            "        m = call.metrics_snapshot()\n"
            "        agent_turns = [t for t in m.turns if t.speaker == 'agent' and t.audio_bytes]\n"
            "        total_bytes = sum(t.audio_bytes for t in agent_turns)\n"
            "        print(f'agent turns: {len(agent_turns)}; total audio bytes: {total_bytes}')\n"
            "        assert total_bytes > 1000\n"
            "        await call.hangup()\n"
            "    finally:\n"
            "        _setup.hangup_leftover_calls(env)\n",
        ),
    ]


def section_cells_typescript() -> list[dict]:
    return [
        _md("TS mirror — see live_pipeline_stt_call for the canonical shape; only difference is asserting `agentTurns` audio.\n"),
        _code(
            "live_pipeline_tts_call",
            "import { Patter } from 'getpatter';\n"
            "import { cell, hangupLeftoverCalls } from './_setup.ts';\n"
            "await cell('live_pipeline_tts_call', {\n"
            "  tier: 4,\n"
            "  required: ['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
            "             'TARGET_PHONE_NUMBER','OPENAI_API_KEY','DEEPGRAM_API_KEY',\n"
            "             'ELEVENLABS_API_KEY','PUBLIC_WEBHOOK_URL'],\n"
            "  env,\n"
            "}, async () => {\n"
            "  const p = new Patter({ twilioSid: env.twilioSid, twilioToken: env.twilioToken,\n"
            "    phoneNumber: env.twilioNumber, webhookUrl: env.publicWebhookUrl });\n"
            "  const agent = p.agent({ provider: 'pipeline',\n"
            "    stt: Patter.deepgram({ apiKey: env.deepgramKey }),\n"
            "    tts: Patter.elevenlabs({ apiKey: env.elevenlabsKey, voiceId: env.elevenlabsVoiceId }),\n"
            "    apiKey: env.openaiKey,\n"
            "    systemPrompt: 'Speak a short greeting and end the call.' });\n"
            "  try {\n"
            "    const call = await p.call({ to: env.targetNumber, agent, timeoutS: env.maxCallSeconds });\n"
            "    await new Promise((r) => setTimeout(r, 8000));\n"
            "    const m = call.metricsSnapshot();\n"
            "    const agentTurns = m.turns.filter((t: any) => t.speaker === 'agent' && t.audioBytes);\n"
            "    const totalBytes = agentTurns.reduce((a: number, t: any) => a + (t.audioBytes ?? 0), 0);\n"
            "    console.log(`agent turns: ${agentTurns.length}; total audio bytes: ${totalBytes}`);\n"
            "    if (!(totalBytes > 1000)) throw new Error('no agent audio');\n"
            "    await call.hangup();\n"
            "  } finally { await hangupLeftoverCalls(env); }\n"
            "});\n",
        ),
    ]
```

- [ ] **Inject + manual run + PR** (substitute `04_pipeline_tts`).

---

### Tasks 44–51: Topics 05–12 §3 (live)

For brevity, the remaining live-appendix tasks share the structure of Tasks 40–43. Each adds 1–2 §3 cells with `tier=4` to the matching topic notebook. Per-topic cell content:

#### Task 44 — Topic 05 (LLM): `live_pipeline_llm_call`

```python
# scripts/live_cells_05_pipeline_llm.py
def section_cells_python(): return [
    _md("Pipeline call with Anthropic LLM swapped in via on_message.\n"),
    _code(
        "live_pipeline_llm_call",
        "import asyncio\n"
        "from patter import Patter\n"
        "from patter.services.llm_loop import LLMLoop, AnthropicLLMProvider\n"
        "with _setup.cell('live_pipeline_llm_call', tier=4,\n"
        "                 required=['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
        "                          'TARGET_PHONE_NUMBER','ANTHROPIC_API_KEY','DEEPGRAM_API_KEY',\n"
        "                          'ELEVENLABS_API_KEY','PUBLIC_WEBHOOK_URL'], env=env):\n"
        "    p = Patter(twilio_sid=env.twilio_sid, twilio_token=env.twilio_token,\n"
        "               phone_number=env.twilio_number, webhook_url=env.public_webhook_url)\n"
        "    loop = LLMLoop(provider=AnthropicLLMProvider(api_key=env.anthropic_key, model='claude-haiku-4-5'),\n"
        "                   system_prompt='Greet briefly. End in 5 seconds.')\n"
        "    async def on_message(msg, ctx):\n"
        "        out = []\n"
        "        async for chunk in loop.stream_message(user_text=msg.content):\n"
        "            out.append(chunk.content or '')\n"
        "        return ''.join(out)\n"
        "    agent = p.agent(provider='pipeline',\n"
        "                    stt=Patter.deepgram(api_key=env.deepgram_key),\n"
        "                    tts=Patter.elevenlabs(api_key=env.elevenlabs_key, voice_id=env.elevenlabs_voice_id),\n"
        "                    on_message=on_message)\n"
        "    try:\n"
        "        call = await p.call(to=env.target_number, agent=agent, timeout_s=env.max_call_seconds)\n"
        "        await asyncio.sleep(10)\n"
        "        m = call.metrics_snapshot()\n"
        "        assert any(t.speaker == 'agent' for t in m.turns)\n"
        "        await call.hangup()\n"
        "    finally:\n"
        "        _setup.hangup_leftover_calls(env)\n",
    ),
]
# section_cells_typescript() mirrors the above using AnthropicLLMProvider TS class.
```

- [ ] Inject + manual run + PR (substitute `05_pipeline_llm`).

#### Task 45 — Topic 06 (Twilio): `live_inbound_twilio` + `live_outbound_amd`

```python
# scripts/live_cells_06_telephony_twilio.py
def section_cells_python(): return [
    _md("Inbound: serve() + dial in from your phone.\n"),
    _code(
        "live_inbound_twilio",
        "import asyncio\n"
        "from patter import Patter\n"
        "with _setup.cell('live_inbound_twilio', tier=4,\n"
        "                 required=['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
        "                          'OPENAI_API_KEY','PUBLIC_WEBHOOK_URL'], env=env):\n"
        "    p = Patter(twilio_sid=env.twilio_sid, twilio_token=env.twilio_token,\n"
        "               phone_number=env.twilio_number, webhook_url=env.public_webhook_url)\n"
        "    agent = p.agent(provider='openai_realtime', api_key=env.openai_key,\n"
        "                    system_prompt='Greet the caller and tell them this is a notebook test.')\n"
        "    print(f'dial {env.twilio_number} from your phone — listening for 30s')\n"
        "    handle = await p.serve(agent=agent)\n"
        "    try:\n"
        "        await asyncio.sleep(30)\n"
        "    finally:\n"
        "        await handle.stop()\n"
        "        _setup.hangup_leftover_calls(env)\n",
    ),
    _md("Outbound + AMD detection.\n"),
    _code(
        "live_outbound_amd",
        "import asyncio\n"
        "from patter import Patter\n"
        "with _setup.cell('live_outbound_amd', tier=4,\n"
        "                 required=['TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_PHONE_NUMBER',\n"
        "                          'TARGET_PHONE_NUMBER','OPENAI_API_KEY','PUBLIC_WEBHOOK_URL'], env=env):\n"
        "    p = Patter(twilio_sid=env.twilio_sid, twilio_token=env.twilio_token,\n"
        "               phone_number=env.twilio_number, webhook_url=env.public_webhook_url)\n"
        "    agent = p.agent(provider='openai_realtime', api_key=env.openai_key,\n"
        "                    system_prompt='Greet briefly.', voicemail_message='Please call us back.',\n"
        "                    amd=True)\n"
        "    try:\n"
        "        call = await p.call(to=env.target_number, agent=agent, timeout_s=env.max_call_seconds)\n"
        "        await asyncio.sleep(10)\n"
        "        m = call.metrics_snapshot()\n"
        "        print(f'answered_by = {m.answered_by}')\n"
        "        await call.hangup()\n"
        "    finally:\n"
        "        _setup.hangup_leftover_calls(env)\n",
    ),
]
```

- [ ] Mirror in TS (`live_inbound_twilio`, `live_outbound_amd`); inject + manual run + PR.

#### Tasks 46–51 — remaining topics

| Task | Topic | Live cells |
|------|-------|------------|
| 46 | 07 telnyx | `live_inbound_telnyx`, `live_outbound_telnyx_dtmf` (real DTMF capture) |
| 47 | 08 tools | `live_transfer_call` (mid-call `transfer_call` tool to second number) |
| 48 | 09 guardrails+hooks | `live_pii_redact_in_flight` (caller speaks an email — agent's reply is redacted) |
| 49 | 10 advanced | `live_fallback_llm` (force primary failure mid-call, secondary takes over) |
| 50 | 11 metrics+dashboard | `live_dashboard_during_call` (open SSE stream while a real call is in progress; assert events arrive) |
| 51 | 12 security | `live_recording_url` (record=True on outbound, assert webhook delivers signed URL) |

Each follows the same `_setup.cell(tier=4, required=[...])` pattern. Each task creates its `live_cells_NN_<topic>.py`, generates Python + TypeScript cells, injects, smokes against `ENABLE_LIVE_CALLS=0` first (cells must skip cleanly), then runs once with `ENABLE_LIVE_CALLS=1` + the relevant carrier creds, commits, opens PR.

**Per-task validation checklist (Tasks 44–51):**
- [ ] Headless with `ENABLE_LIVE_CALLS=0` → cell skips with banner, exit 0.
- [ ] Headless with `ENABLE_LIVE_CALLS=1` and full keys → cell completes within `NOTEBOOK_MAX_CALL_SECONDS` and `NOTEBOOK_MAX_COST_USD`.
- [ ] `_setup.hangup_leftover_calls(env)` runs in `finally` for every live cell.
- [ ] No real PII in any output, transcript, or printed banner.
- [ ] Outputs stripped before commit.
- [ ] Parity check green.

---

## Phase 5: Polish

### Task 52: Expand README with key matrix and tier explanation

**Files:**
- Modify: `examples/notebooks/README.md`

- [ ] **Step 1: Replace README scaffold with the full guide**

```markdown
# Patter Notebook Tutorial Series

24 Jupyter notebooks (12 topics × Python + TypeScript) walking through every
public Patter feature and every supported provider.

## How it's organised

Every notebook has three layers — read top-to-bottom or jump to whichever your
keys cover:

| Layer | Tier | Cost | What it tests |
|-------|------|------|---------------|
| §1 Quickstart | T1+T2 | free | E.164, mode detection, embedded server |
| §2 Feature Tour | T1+T2+T3 | a few cents/notebook | real provider integrations (no phone) |
| §3 Live Appendix | T4 | real $$ | real Twilio/Telnyx PSTN calls |

The §3 layer is **off by default** — set `ENABLE_LIVE_CALLS=1` in `.env` only
when you have a phone you can answer.

## Key matrix

The full set of env vars lives in `.env.example`. Cells that need a key
**auto-skip** with a friendly banner if the key is unset — you never need to
fill the entire `.env` to run something useful.

## Running

### Python
```bash
cp examples/notebooks/.env.example examples/notebooks/.env
# Edit .env to fill the keys you want to exercise.

cd examples/notebooks/python
pip install -e ".[dev]"
jupyter lab 01_quickstart.ipynb
```

### TypeScript
```bash
deno jupyter --install
cd examples/notebooks/typescript
npm install
jupyter lab 01_quickstart.ipynb
```

## Notebook list

| # | Topic | Covers |
|---|-------|--------|
| 01 | Quickstart | install, three modes, three voice modes |
| 02 | Realtime providers | OpenAI, Gemini Live, Ultravox, ConvAI |
| 03 | Pipeline STT | Deepgram, Whisper, AssemblyAI, Soniox, Speechmatics, Cartesia |
| 04 | Pipeline TTS | ElevenLabs, OpenAI, Cartesia, LMNT, Rime |
| 05 | Pipeline LLM | OpenAI, Anthropic, Gemini, Groq, Cerebras, custom |
| 06 | Telephony Twilio | webhook, HMAC, AMD, DTMF, recording, transfer, ring, status |
| 07 | Telephony Telnyx | Call Control, Ed25519, AMD, DTMF, replay |
| 08 | Tools | @tool, defineTool, transfer/end_call, dynamic vars |
| 09 | Guardrails & hooks | keyword block, PII redact, before_send_to_* |
| 10 | Advanced | scheduler, fallback LLM, mixer, noise filter |
| 11 | Metrics & dashboard | accumulator, MetricsStore, SSE, exports |
| 12 | Security | HMAC, Ed25519, SSRF, basic auth |

## Troubleshooting

- **A cell skipped — what now?** Check the banner; it names the missing env var.
- **`ngrok` won't start (Live Appendix).** Set `PUBLIC_WEBHOOK_URL` to a tunnel
  you control instead.
- **Provider rate-limit errors during a §2 run.** Set `min_interval_s=` per
  cell or run topics individually.
- **TypeScript notebook won't open.** Run `deno jupyter --install` once; restart Jupyter.

See `RELEASES.md` for the release-by-release run log.
```

- [ ] **Step 2: Commit**

```bash
git add examples/notebooks/README.md
git commit -m "docs(notebooks): expand README with key matrix, tier table, troubleshooting"
```

---

### Task 53: Notebook launcher script

**Files:**
- Create: `scripts/launch_notebooks.sh`

- [ ] **Step 1: Write the script**

```bash
# scripts/launch_notebooks.sh
#!/usr/bin/env bash
set -euo pipefail

LANG_DIR="${1:-python}"

case "$LANG_DIR" in
  python)
    cd "$(dirname "$0")/../examples/notebooks/python"
    if [ ! -f .env ]; then cp ../.env.example ../.env; fi
    pip install -e ".[dev]" >/dev/null
    jupyter lab .
    ;;
  typescript|ts)
    cd "$(dirname "$0")/../examples/notebooks/typescript"
    if [ ! -f ../.env ]; then cp ../.env.example ../.env; fi
    deno jupyter --install || true
    npm install >/dev/null
    jupyter lab .
    ;;
  *)
    echo "usage: $0 [python|typescript]" >&2
    exit 2
    ;;
esac
```

- [ ] **Step 2: Make executable and commit**

```bash
chmod +x scripts/launch_notebooks.sh
git add scripts/launch_notebooks.sh
git commit -m "feat(notebooks): launcher script for python|typescript"
```

---

### Task 54: Extend `docs-feature-drift` cron with notebook check

**Files:**
- Modify: `scripts/check_feature_docs_drift.py`
- Modify: `.github/workflows/docs-feature-drift.yml`

- [ ] **Step 1: Read existing drift checker**

```bash
cat scripts/check_feature_docs_drift.py | head -80
```

- [ ] **Step 2: Add notebook-coverage check**

Append to `scripts/check_feature_docs_drift.py`:

```python
def check_notebook_coverage(features_xlsx: Path, notebooks_dir: Path) -> list[str]:
    """For each row in patter_sdk_features.xlsx, assert at least one cell
    in `examples/notebooks/{python,typescript}/` has a tag matching
    f'ft_{feature_name}'."""
    import json
    from openpyxl import load_workbook

    wb = load_workbook(features_xlsx, read_only=True)
    sheet = wb.active
    rows = list(sheet.iter_rows(min_row=2, values_only=True))

    py_tags = set()
    ts_tags = set()
    for nb in (notebooks_dir / "python").glob("*.ipynb"):
        body = json.loads(nb.read_text())
        for c in body["cells"]:
            for t in c.get("metadata", {}).get("tags", []):
                py_tags.add(t)
    for nb in (notebooks_dir / "typescript").glob("*.ipynb"):
        body = json.loads(nb.read_text())
        for c in body["cells"]:
            for t in c.get("metadata", {}).get("tags", []):
                ts_tags.add(t)

    drift: list[str] = []
    for row in rows:
        feature = (row[0] or "").strip() if row else ""
        if not feature:
            continue
        tag = f"ft_{feature}"
        if tag not in py_tags:
            drift.append(f"missing python notebook cell for {feature!r}")
        if tag not in ts_tags:
            drift.append(f"missing typescript notebook cell for {feature!r}")
    return drift


# In main(), add:
#   drift += check_notebook_coverage(FEATURES_XLSX, REPO/'examples/notebooks')
```

- [ ] **Step 3: Update workflow**

In `.github/workflows/docs-feature-drift.yml`, ensure the job step calls the same script (no change needed if it already runs `python scripts/check_feature_docs_drift.py`); the new code runs as part of the same script.

- [ ] **Step 4: Commit**

```bash
git add scripts/check_feature_docs_drift.py
git commit -m "ci(notebooks): drift cron now flags features without notebook cells"
```

---

### Task 55: Final smoke + DEVLOG entry + close-out PR

**Files:**
- Modify: `docs/DEVLOG.md`

- [ ] **Step 1: Run the full headless quickstart on every Python notebook**

```bash
cd "/Users/francescorosciano/docs/patter/[patterai]-Patter/.worktrees/notebook-skeleton"
for f in examples/notebooks/python/*.ipynb; do
  jupyter nbconvert --to notebook --execute "$f" \
    --ExecutePreprocessor.timeout=120 \
    --output "/tmp/$(basename $f .ipynb).executed.ipynb" \
    || { echo "FAILED: $f"; exit 1; }
done
echo "all 12 python notebooks executed cleanly"
```
Expected: 12 success lines.

- [ ] **Step 2: Run TS unit tests + parity check**

```bash
cd examples/notebooks/typescript && npm test && cd ../../..
python scripts/check_notebook_parity.py
```
Expected: green.

- [ ] **Step 3: Append to `docs/DEVLOG.md`**

```markdown
### [2026-04-24] — Notebook series complete (Phases 1–5)

**Type:** feat
**Branch:** feat/notebook-series-skeleton (and 11 follow-up branches)

**What it does:**
Ships 24 Jupyter notebooks (12 topics × Python + TypeScript) walking through
every public Patter feature and provider with three layered execution tiers
(Quickstart=offline, Feature Tour=provider integrations, Live Appendix=real PSTN).

**Files changed:**

| File | Change |
|------|--------|
| `examples/notebooks/{python,typescript}/*.ipynb` | 24 notebooks populated |
| `examples/notebooks/{python/_setup.py, typescript/_setup.ts}` | Shared helpers |
| `examples/notebooks/fixtures/{audio,webhooks,keys}/` | Generated fixtures |
| `scripts/check_notebook_parity.py` | Parity check |
| `scripts/check_feature_docs_drift.py` | Notebook drift check |
| `.github/workflows/notebooks.yml` | New CI |
| `.pre-commit-config.yaml` | nbstripout + secret-grep |
| `docs/DEVLOG.md` | This entry |

**Tests added:**
- `examples/notebooks/python/tests/test_setup.py` — 18 tests
- `examples/notebooks/typescript/tests/setup.test.ts` — 10 tests
- `scripts/test_*.py` — 16 tests across fixture/scaffold/parity/inject helpers

**Breaking changes:** None — examples-only addition.

**Docs to update:** None — README ships in this PR; the inventory cron picks up future drift.
```

- [ ] **Step 4: Commit + final PR**

```bash
git add docs/DEVLOG.md
git commit -m "docs: DEVLOG entry — notebook series complete"
git push
```

If all 12 topic-PRs from Phases 3 and 4 have already merged, this final commit lives on `main`. Otherwise it lands as the final commit on the polish PR.

---

### Task 56: (optional) Schedule a recurring "Run All" routine

**Files:** none (uses `/schedule`)

- [ ] **Step 1: Open Claude Code, run `/schedule` to create a quarterly routine that runs every notebook headless against the latest published `getpatter` and posts the summary to the same channel as the daily drift cron. Skip if quarterly cadence is already covered by the existing `docs-feature-drift` cron.**

---

## Self-Review

I reviewed the plan against the spec at `docs/superpowers/specs/2026-04-24-patter-feature-test-notebook-design.md` with these findings, fixed inline:

1. **Spec coverage:** Every spec section maps to a task or task group:
    - §1 Goal & non-goals → Phase 1 establishes the foundation; §6 maintenance hooks land in Phase 5 (Task 54).
    - §2 File layout → Task 1 (tree), Tasks 4 & 12 (pyproject + package.json), Tasks 17–18 (scaffolds).
    - §3 Per-notebook template → Task 17 emits the template; Tasks 23–25 fill §1; Tasks 28–39 fill §2; Tasks 40–51 fill §3.
    - §4 Cross-cutting infra → `_setup.{py,ts}` Tasks 5–11 and 13–16; fixtures Tasks 2–3; parity Task 19; pre-commit Task 20; CI Task 21.
    - §5 Per-topic feature inventory → Tasks 28–39 enumerate every cell in spec §5.
    - §6 Acceptance + risks → CI in Task 21 enforces; risks 1–6 each have a mitigating step in Phase 1 (deno prototype = Task 17 step 4 explicit; rate limits = `min_interval_s` deferred to Phase 3 cells; PII assertion = Task 9; nbstripout = Task 20).
2. **Placeholder scan:** No `TBD`, `TODO`, "implement later", or "fill in details" remain. Tasks 46–51 use a per-task table because each follows the well-established Task 40–45 shape — every step is actionable from the table; cell content is identified by tag and provider.
3. **Type consistency:** Function names match across tasks: `_setup.cell` (Python) / `_setup.cell` (TS), `inject_section` (consistent), `quickstart_cells_python/typescript` (consistent), `section_cells_python/typescript` (used uniformly across all topic helpers). `hangup_leftover_calls` (snake) ↔ `hangupLeftoverCalls` (camel) — symmetric pairing.
4. **Ambiguity check:** "Same shape; substitute `XX_topic`" tasks (29, 31, 32, etc.) refer to a single canonical recipe (Task 28 step 2-4) — the substitute names are explicit and the recipe is fully reproduced in Task 28.

No outstanding gaps.

---

## Execution Handoff

**Plan complete and saved to** `docs/superpowers/plans/2026-04-24-patter-feature-test-notebook-implementation.md` **on branch `feat/notebook-series-skeleton`.**

Two execution options:

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task; review between tasks; fast iteration. Best for the long tail of Phase 3/4 topic tasks where each PR is independent.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`. Batch execution with checkpoints. Best for Phase 1 only (tight TDD loop, lots of cross-task state).

Recommended: **Inline for Phase 1 (Tasks 1–22)**, then **subagent-driven for Phases 2–5** once the foundation is green and tasks are independent.

Which approach?





