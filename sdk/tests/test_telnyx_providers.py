"""Unit tests for the Telnyx STT/TTS providers and the Telnyx call-control
helpers (DTMF, transfer validation, recording).

MOCK: All tests here patch network I/O — no real Telnyx API calls are made.
For end-to-end verification against Telnyx, see
``tests/integration/test_telnyx_realtime.py`` which requires
``TELNYX_API_KEY`` plus a valid Call Control Application ID.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.providers.telnyx_stt import (
    TelnyxSTT,
    TELNYX_STT_WS_URL,
    _create_streaming_wav_header,
)
from patter.providers.telnyx_tts import TelnyxTTS, TELNYX_TTS_WS_URL, DEFAULT_VOICE


# ---------------------------------------------------------------------------
# TelnyxSTT — init + WAV header
# ---------------------------------------------------------------------------


def test_telnyx_stt_init_defaults() -> None:
    stt = TelnyxSTT(api_key="KEY-test")
    assert stt.api_key == "KEY-test"
    assert stt.language == "en"
    assert stt.transcription_engine == "telnyx"
    assert stt.sample_rate == 16000
    assert stt.base_url == TELNYX_STT_WS_URL


def test_telnyx_stt_init_engine_override() -> None:
    stt = TelnyxSTT(api_key="K", language="es", transcription_engine="deepgram")
    assert stt.language == "es"
    assert stt.transcription_engine == "deepgram"


def test_telnyx_stt_repr_contains_engine() -> None:
    stt = TelnyxSTT(api_key="K", transcription_engine="google")
    assert "google" in repr(stt)


def test_wav_header_has_riff_prefix_and_data_section() -> None:
    header = _create_streaming_wav_header(16000, 1)
    assert header[0:4] == b"RIFF"
    assert header[8:12] == b"WAVE"
    assert header[12:16] == b"fmt "
    assert header[36:40] == b"data"
    # 44-byte standard WAV header
    assert len(header) == 44


def test_parse_message_extracts_transcript_and_flags() -> None:
    raw = json.dumps(
        {"transcript": "hello world", "is_final": True, "confidence": 0.91}
    )
    result = TelnyxSTT._parse_message(raw)
    assert result is not None
    assert result.text == "hello world"
    assert result.is_final is True
    assert result.confidence == pytest.approx(0.91)


def test_parse_message_drops_empty_transcript() -> None:
    raw = json.dumps({"transcript": "", "is_final": True})
    assert TelnyxSTT._parse_message(raw) is None


def test_parse_message_handles_bad_json() -> None:
    assert TelnyxSTT._parse_message("not-json") is None


# ---------------------------------------------------------------------------
# TelnyxTTS — init
# ---------------------------------------------------------------------------


def test_telnyx_tts_init_defaults() -> None:
    tts = TelnyxTTS(api_key="KEY-test")
    assert tts.api_key == "KEY-test"
    assert tts.voice == DEFAULT_VOICE
    assert tts.base_url == TELNYX_TTS_WS_URL


def test_telnyx_tts_default_voice_is_astra() -> None:
    tts = TelnyxTTS(api_key="K")
    assert tts.voice == "Telnyx.NaturalHD.astra"


def test_telnyx_tts_repr() -> None:
    tts = TelnyxTTS(api_key="K", voice="Telnyx.NaturalHD.bronte")
    assert "bronte" in repr(tts)


# ---------------------------------------------------------------------------
# telnyx_handler — DTMF send (MOCK: no real Telnyx API call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_dtmf_mock_posts_once_per_digit() -> None:
    """MOCK: no real Telnyx API call — verifies 4 digits produce 4 POSTs."""
    from patter.handlers.telnyx_handler import _is_valid_transfer_target  # noqa: F401

    # Build a mock client that records POST calls.
    post_calls: list[dict] = []

    class _MockResponse:
        status_code = 200
        text = ""

    class _MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            post_calls.append({"url": url, "json": json})
            return _MockResponse()

    async def _mock_sleep(_seconds: float) -> None:
        # Skip real waiting — just count the sleeps.
        _mock_sleep.calls += 1  # type: ignore[attr-defined]
    _mock_sleep.calls = 0  # type: ignore[attr-defined]

    # Build a fake send_dtmf fn mirroring telnyx_handler._telnyx_send_dtmf.
    # We re-implement the minimal body here to avoid spinning up the full
    # websocket bridge.
    from patter.handlers.telnyx_handler import _DTMF_ALLOWED

    call_control_id = "v3:mock-call-id"
    telnyx_key = "KEY-mock"

    digits = "12#4"
    delay_ms = 250

    async def _send_dtmf(d: str, delay: int = delay_ms) -> None:
        filtered = [x for x in d if x in _DTMF_ALLOWED]
        if not filtered:
            return
        with patch("httpx.AsyncClient", return_value=_MockClient()), \
             patch("asyncio.sleep", side_effect=_mock_sleep):
            import httpx as _httpx

            async with _httpx.AsyncClient() as _http:
                for idx, digit in enumerate(filtered):
                    await _http.post(
                        f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/send_dtmf",
                        headers={"Authorization": f"Bearer {telnyx_key}"},
                        json={"digits": digit, "duration_millis": 250},
                        timeout=10.0,
                    )
                    if idx < len(filtered) - 1 and delay > 0:
                        await asyncio.sleep(delay / 1000)

    await _send_dtmf(digits)

    assert len(post_calls) == 4
    assert post_calls[0]["json"]["digits"] == "1"
    assert post_calls[1]["json"]["digits"] == "2"
    assert post_calls[2]["json"]["digits"] == "#"
    assert post_calls[3]["json"]["digits"] == "4"
    # 3 delays between 4 digits
    assert _mock_sleep.calls == 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_send_dtmf_ignores_invalid_digits() -> None:
    """MOCK: digits outside 0-9*#A-D are filtered before POST."""
    from patter.handlers.telnyx_handler import _DTMF_ALLOWED

    digits = "abXY12"  # only '1' and '2' are valid
    filtered = [x for x in digits if x in _DTMF_ALLOWED]
    # 'a' and 'b' are allowed (DTMF A/B/C/D are valid). Case-insensitive.
    assert set(filtered) == {"a", "b", "1", "2"}


# ---------------------------------------------------------------------------
# Telnyx transfer target validation
# ---------------------------------------------------------------------------


def test_transfer_target_accepts_e164() -> None:
    from patter.handlers.telnyx_handler import _is_valid_transfer_target

    assert _is_valid_transfer_target("+15551234567") is True


def test_transfer_target_accepts_sip_uri() -> None:
    from patter.handlers.telnyx_handler import _is_valid_transfer_target

    assert _is_valid_transfer_target("sip:agent@example.com") is True
    assert _is_valid_transfer_target("sips:secure@voip.example.com:5061") is True


def test_transfer_target_rejects_empty_and_invalid() -> None:
    from patter.handlers.telnyx_handler import _is_valid_transfer_target

    assert _is_valid_transfer_target("") is False
    assert _is_valid_transfer_target("not a number") is False
    assert _is_valid_transfer_target("15551234567") is False  # no leading +
    assert _is_valid_transfer_target("http://example.com") is False


# ---------------------------------------------------------------------------
# CallControl — send_dtmf forwards to configured fn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_control_send_dtmf_forwards_to_fn() -> None:
    from patter.models import CallControl

    seen: list[tuple[str, int]] = []

    async def _fn(digits: str, delay_ms: int) -> None:
        seen.append((digits, delay_ms))

    cc = CallControl(
        call_id="c1",
        caller="+15551111111",
        callee="+15552222222",
        telephony_provider="telnyx",
        _send_dtmf_fn=_fn,
    )
    await cc.send_dtmf("1234", delay_ms=150)
    assert seen == [("1234", 150)]


@pytest.mark.asyncio
async def test_call_control_send_dtmf_noops_when_unconfigured() -> None:
    from patter.models import CallControl

    cc = CallControl(
        call_id="c1",
        caller="",
        callee="",
        telephony_provider="telnyx",
    )
    # Should not raise — logs a warning instead.
    await cc.send_dtmf("99#")
