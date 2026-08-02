"""Tests for the Fish Audio batch STT (ASR) provider.

These run against a **real in-process aiohttp server** on 127.0.0.1 that parses
the real multipart body the adapter uploads — only Fish's transcription is
simulated. Buffering, WAV framing, multipart field names, tail padding,
transcript mapping, the queue/sentinel drain and the error path all execute for
real. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import io
import wave
from typing import Any, AsyncIterator

import pytest
from aiohttp import web

from getpatter.providers.base import Transcript
from getpatter.providers.fish_audio_stt import (
    BUFFER_SIZE_BYTES,
    MIN_AUDIO_BYTES,
    FishAudioSTT,
    _parse_asr_response,
)

pytestmark = pytest.mark.mocked


class _ASRRecorder:
    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.status = 200
        self.payload: dict[str, Any] = {"text": "ciao mondo", "duration": 2.0}
        self.error_body = ""


@pytest.fixture
async def fish_asr_server() -> AsyncIterator[tuple[str, _ASRRecorder]]:
    rec = _ASRRecorder()

    async def asr_handler(request: web.Request) -> web.Response:
        data = await request.post()
        audio_field = data.get("audio")
        raw = audio_field.file.read() if hasattr(audio_field, "file") else b""
        rec.uploads.append(
            {
                "authorization": request.headers.get("Authorization"),
                "language": data.get("language"),
                "ignore_timestamps": data.get("ignore_timestamps"),
                "filename": getattr(audio_field, "filename", None),
                "content_type": getattr(audio_field, "content_type", None),
                "wav": raw,
            }
        )
        if rec.status != 200:
            return web.Response(status=rec.status, text=rec.error_body)
        return web.json_response(rec.payload)

    app = web.Application()
    app.router.add_post("/v1/asr", asr_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    try:
        yield f"http://{host}:{port}/v1/asr", rec
    finally:
        await runner.cleanup()


def _stt(url: str, **kwargs: Any) -> FishAudioSTT:
    return FishAudioSTT(api_key="fish-test-key", base_url=url, **kwargs)


async def _drain(stt: FishAudioSTT) -> list[Transcript]:
    return [t async for t in stt.receive_transcripts()]


def _silence(num_bytes: int) -> bytes:
    return b"\x00" * num_bytes


# ---------------------------------------------------------------------------
# Upload shape
# ---------------------------------------------------------------------------


async def test_a_full_window_is_uploaded_and_yields_a_final_transcript(
    fish_asr_server,
) -> None:
    url, rec = fish_asr_server
    stt = _stt(url)
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()

    transcripts = await _drain(stt)
    assert [t.text for t in transcripts] == ["ciao mondo"]
    assert transcripts[0].is_final is True
    assert transcripts[0].speech_final is True
    assert len(rec.uploads) == 1


async def test_audio_is_uploaded_as_a_valid_16k_mono_pcm16_wav(fish_asr_server) -> None:
    url, rec = fish_asr_server
    stt = _stt(url)
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()
    await _drain(stt)

    with wave.open(io.BytesIO(rec.uploads[0]["wav"]), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        # Every PCM byte we buffered made it into the upload.
        assert wf.getnframes() == BUFFER_SIZE_BYTES // 2


async def test_multipart_uses_fishs_documented_field_names(fish_asr_server) -> None:
    url, rec = fish_asr_server
    stt = _stt(url, language="it")
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()
    await _drain(stt)

    upload = rec.uploads[0]
    assert upload["authorization"] == "Bearer fish-test-key"
    assert upload["language"] == "it"
    assert upload["ignore_timestamps"] == "true"
    assert upload["filename"] == "audio.wav"
    assert upload["content_type"] == "audio/wav"


async def test_ignore_timestamps_false_is_forwarded(fish_asr_server) -> None:
    url, rec = fish_asr_server
    stt = _stt(url, ignore_timestamps=False)
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()
    await _drain(stt)
    assert rec.uploads[0]["ignore_timestamps"] == "false"


async def test_language_is_omitted_when_none_so_fish_auto_detects(
    fish_asr_server,
) -> None:
    url, rec = fish_asr_server
    stt = _stt(url, language=None)
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()
    await _drain(stt)
    assert rec.uploads[0]["language"] is None


# ---------------------------------------------------------------------------
# Buffering / flush semantics
# ---------------------------------------------------------------------------


async def test_a_partial_window_is_not_uploaded_until_the_threshold(
    fish_asr_server,
) -> None:
    url, rec = fish_asr_server
    stt = _stt(url)
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES // 4))
    assert rec.uploads == []
    await stt.close()
    await _drain(stt)


async def test_a_short_tail_is_silence_padded_to_fishs_one_second_minimum(
    fish_asr_server,
) -> None:
    """Fish rejects clips under 1 s; dropping the tail would lose the last
    words of an utterance, so it is padded instead."""
    url, rec = fish_asr_server
    stt = _stt(url)
    await stt.connect()
    await stt.send_audio(_silence(1600))  # 50 ms — far under the 1 s minimum
    await stt.close()
    await _drain(stt)

    assert len(rec.uploads) == 1
    with wave.open(io.BytesIO(rec.uploads[0]["wav"]), "rb") as wf:
        assert wf.getnframes() == MIN_AUDIO_BYTES // 2


async def test_a_tail_already_over_the_minimum_is_not_padded(fish_asr_server) -> None:
    url, rec = fish_asr_server
    stt = _stt(url)
    await stt.connect()
    tail = MIN_AUDIO_BYTES + 4000
    await stt.send_audio(_silence(tail))
    await stt.close()
    await _drain(stt)
    with wave.open(io.BytesIO(rec.uploads[0]["wav"]), "rb") as wf:
        assert wf.getnframes() == tail // 2


async def test_multiple_windows_produce_multiple_transcripts(fish_asr_server) -> None:
    url, rec = fish_asr_server
    stt = _stt(url)
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()
    transcripts = await _drain(stt)
    assert len(transcripts) == 2
    assert len(rec.uploads) == 2


async def test_a_custom_buffer_size_changes_the_upload_cadence(fish_asr_server) -> None:
    url, rec = fish_asr_server
    stt = _stt(url, buffer_size_bytes=MIN_AUDIO_BYTES)
    await stt.connect()
    await stt.send_audio(_silence(MIN_AUDIO_BYTES * 2))
    await stt.close()
    await _drain(stt)
    assert len(rec.uploads) == 1  # one full window, nothing left over


# ---------------------------------------------------------------------------
# Failure handling — STT must never kill the call
# ---------------------------------------------------------------------------


async def test_a_server_error_is_logged_and_yields_no_transcript(
    fish_asr_server,
) -> None:
    url, rec = fish_asr_server
    rec.status = 402
    rec.error_body = "insufficient credit"
    stt = _stt(url)
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()
    assert await _drain(stt) == []


async def test_an_unreachable_host_does_not_raise() -> None:
    stt = _stt("http://127.0.0.1:1/v1/asr")
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()
    assert await _drain(stt) == []


async def test_an_empty_transcript_is_dropped(fish_asr_server) -> None:
    url, rec = fish_asr_server
    rec.payload = {"text": "   ", "duration": 2.0}
    stt = _stt(url)
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()
    assert await _drain(stt) == []


# ---------------------------------------------------------------------------
# Response mapping
# ---------------------------------------------------------------------------


async def test_segments_are_surfaced_on_the_transcript(fish_asr_server) -> None:
    url, rec = fish_asr_server
    rec.payload = {
        "text": "ciao mondo",
        "duration": 2.0,
        "segments": [
            {"text": "ciao", "start": 0.0, "end": 0.4},
            {"text": "mondo", "start": 0.4, "end": 1.1},
        ],
    }
    stt = _stt(url, ignore_timestamps=False)
    await stt.connect()
    await stt.send_audio(_silence(BUFFER_SIZE_BYTES))
    await stt.close()
    transcripts = await _drain(stt)
    assert len(transcripts[0].words) == 2
    assert transcripts[0].words[0]["text"] == "ciao"


def test_parse_maps_text_and_defaults_confidence_to_one() -> None:
    t = _parse_asr_response({"text": " hello ", "duration": 1.0})
    assert t is not None
    assert (t.text, t.is_final, t.confidence, t.words) == ("hello", True, 1.0, ())


def test_parse_returns_none_for_empty_or_malformed_payloads() -> None:
    assert _parse_asr_response({"text": ""}) is None
    assert _parse_asr_response({}) is None
    assert _parse_asr_response("not a dict") is None
    assert _parse_asr_response(None) is None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_api_key_falls_back_to_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "env-key")
    assert FishAudioSTT().api_key == "env-key"


def test_missing_api_key_raises_a_pointed_error(monkeypatch) -> None:
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="FISH_AUDIO_API_KEY"):
        FishAudioSTT()


def test_repr_never_leaks_the_api_key() -> None:
    assert "super-secret" not in repr(FishAudioSTT(api_key="super-secret"))


def test_for_twilio_matches_the_default_construction() -> None:
    stt = FishAudioSTT.for_twilio(api_key="k", language="fr")
    assert (stt.language, stt.sample_rate, stt.encoding) == ("fr", 16000, "linear16")


def test_default_window_is_two_seconds_clear_of_fishs_one_second_floor() -> None:
    assert BUFFER_SIZE_BYTES == MIN_AUDIO_BYTES * 2
