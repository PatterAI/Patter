"""Shared helpers for every notebook in examples/notebooks/python/.

Public surface (mirrored in typescript/_setup.ts):
    NotebookEnv      — frozen dataclass holding every env var the series reads
    load()           — parse .env and return NotebookEnv
    has_key()        — booleanise a key
    print_key_matrix() — render a ✅/⚪️ table at notebook open
    cell()           — context manager wrapping every feature cell
    skip()           — raise NotebookSkip inside a cell
    skip_section()   — same, for whole sections (live appendix gate)
    load_fixture()   — load bytes from examples/notebooks/fixtures/
    run_stt()        — standardised STT roundtrip helper
    run_tts()        — standardised TTS roundtrip helper
    hangup_leftover_calls() — safety sweep for live appendix teardown
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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
