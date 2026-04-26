"""Verify the fixture generator produces valid artifacts."""

from __future__ import annotations

import json
import re
import wave
from pathlib import Path

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
    real_phone = re.compile(r"\+1[2-9]\d{9}")
    real_sid = re.compile(r"AC[0-9a-f]{32}")
    for p in (FIXTURES / "webhooks").glob("*.json"):
        body = p.read_text()
        for match in real_phone.findall(body):
            assert match == "+15555550100", f"{p} leaks phone {match}"
        for match in real_sid.findall(body):
            assert match.startswith("ACtest") or set(match[2:]) == {"0"}, (
                f"{p} leaks SID {match}"
            )
        json.loads(body)
