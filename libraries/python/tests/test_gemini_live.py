"""Mocked unit tests for GeminiLiveAdapter fixes.

Mocks the google.genai boundary only. Tests verify:
  1. send_audio sends audio= field (not deprecated media=)
  2. api_version auto-detection from model name
  3. GEMINI_LIVE_3_1_FLASH_PREVIEW constant value
  4. inbound/outbound audio-codec transcoding (REAL mu-law<->PCM + resample)
  5. GeminiLive engine marker + client dispatch
"""

import asyncio
import struct
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from getpatter.audio.transcoding import pcm16_to_mulaw
from getpatter.providers.gemini_live import (
    GeminiLiveAdapter,
    GeminiLiveModel,
    GEMINI_LIVE_3_1_FLASH_PREVIEW,
    MULAW_FRAME_BYTES,
)


def make_fake_session() -> MagicMock:
    async def fake_receive():
        yield MagicMock(server_content=None, go_away=None, tool_call=None)
        # Block forever (simulates open session)
        await asyncio.sleep(9999)

    session = MagicMock()
    session.send_realtime_input = AsyncMock()
    session.send_client_content = AsyncMock()
    session.send_tool_response = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.receive = fake_receive
    return session


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_send_audio_uses_audio_field_not_media():
    """send_audio must use audio= kwarg (media= is deprecated in google-genai >=2.x)."""
    fake_session = make_fake_session()

    with patch.dict("sys.modules", {
        "google": MagicMock(),
        "google.genai": MagicMock(),
        "google.genai.types": MagicMock(),
    }):
        import google.genai as genai_mod
        genai_mod.Client.return_value.aio.live.connect.return_value = fake_session

        adapter = GeminiLiveAdapter("test-key", model=GEMINI_LIVE_3_1_FLASH_PREVIEW)
        await adapter.connect()

        pcm = bytes([0x00, 0x01, 0x02])
        await adapter.send_audio(pcm)

        fake_session.send_realtime_input.assert_called_once()
        call_kwargs = fake_session.send_realtime_input.call_args
        # audio= kwarg must be present; media= must NOT be present
        assert "audio" in call_kwargs.kwargs or (
            len(call_kwargs.args) > 0 and "audio" in str(call_kwargs.args[0])
        ), f"Expected 'audio' kwarg; got: {call_kwargs}"
        assert "media" not in str(call_kwargs), f"Deprecated 'media' field found: {call_kwargs}"

        await adapter.close()


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_send_audio_transcodes_carrier_mulaw8_to_pcm16k():
    """send_audio decodes carrier mu-law 8 kHz and upsamples to 16 kHz PCM.

    REAL transcode (mu-law decode + audioop resample) — only google.genai is
    mocked. Discriminates against a raw pass-through: feeding 160 mu-law bytes
    must NOT send 160 bytes back, the mime rate must be 16000, and the sent
    payload must be a whole number of PCM16 samples (even length) far larger
    than the 160 raw mu-law bytes (~2x@16k of 160 8 kHz samples).
    """
    fake_session = make_fake_session()

    with patch.dict(
        "sys.modules",
        {
            "google": MagicMock(),
            "google.genai": MagicMock(),
            "google.genai.types": MagicMock(),
        },
    ):
        import google.genai as genai_mod

        genai_mod.Client.return_value.aio.live.connect.return_value = fake_session

        adapter = GeminiLiveAdapter("test-key", model=GEMINI_LIVE_3_1_FLASH_PREVIEW)
        await adapter.connect()

        mulaw = bytes(range(160))  # 160 mu-law bytes -> 160 PCM16@8k samples
        await adapter.send_audio(mulaw)

        fake_session.send_realtime_input.assert_called_once()
        kwargs = fake_session.send_realtime_input.call_args.kwargs
        audio = kwargs["audio"]
        assert audio["mime_type"] == "audio/pcm;rate=16000"
        sent = audio["data"]
        assert isinstance(sent, (bytes, bytearray))
        assert len(sent) % 2 == 0  # whole PCM16 samples
        # 160 8 kHz samples upsampled to 16 kHz ~= 320 samples = 640 bytes
        # (minus ratecv warmup). Must be far above the 320 raw PCM16@8k bytes
        # and never equal the 160 raw mu-law bytes.
        assert len(sent) != 160
        assert len(sent) > 320

        await adapter.close()


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_receive_transcodes_pcm24_to_mulaw8_20ms_frames():
    """receive_events resamples Gemini PCM-24k to mu-law 8 kHz 20 ms frames.

    REAL transcode (audioop resample + mu-law encode) — only google.genai is
    mocked. Pushes a 480-sample (20 ms) 200 Hz sine at 24 kHz (960 bytes);
    asserts each emitted ``audio`` frame is <=160 bytes (20 ms) and the total
    mu-law output is ~160 bytes — far below the 960 raw PCM-24k bytes.
    """
    import math

    # Build a 20 ms 200 Hz sine at 24 kHz: 480 samples, PCM16-LE.
    samples = [int(8000 * math.sin(2 * math.pi * 200 * n / 24000)) for n in range(480)]
    pcm24 = b"".join(struct.pack("<h", s) for s in samples)
    assert len(pcm24) == 960

    fake_part = MagicMock()
    fake_part.inline_data = MagicMock(data=pcm24)
    fake_part.text = None
    model_turn = MagicMock(parts=[fake_part])
    server_content = MagicMock(
        model_turn=model_turn,
        input_transcription=None,
        output_transcription=None,
        turn_complete=False,
        interrupted=False,
    )
    message = MagicMock(server_content=server_content, go_away=None, tool_call=None)

    async def one_then_block():
        yield message
        await asyncio.sleep(9999)

    fake_session = make_fake_session()
    fake_session.receive = one_then_block

    with patch.dict(
        "sys.modules",
        {
            "google": MagicMock(),
            "google.genai": MagicMock(),
            "google.genai.types": MagicMock(),
        },
    ):
        import google.genai as genai_mod

        genai_mod.Client.return_value.aio.live.connect.return_value = fake_session

        adapter = GeminiLiveAdapter("test-key", model=GEMINI_LIVE_3_1_FLASH_PREVIEW)
        await adapter.connect()

        frames = []
        gen = adapter.receive_events()
        try:
            while True:
                evt = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
                if evt[0] == "audio":
                    frames.append(evt[1])
        except (asyncio.TimeoutError, StopAsyncIteration):
            pass

        assert frames, "expected at least one audio frame"
        for frame in frames:
            assert len(frame) <= MULAW_FRAME_BYTES
        total = sum(len(f) for f in frames)
        # 480 samples @ 24k -> ~160 samples @ 8k -> ~160 mu-law bytes.
        # Far below the 960 raw PCM-24k bytes (the discriminator).
        assert 120 <= total <= 200, f"total mu-law {total} not in 120..200"
        assert total < 960

        await adapter.close()
        await gen.aclose()


@pytest.mark.mocked
def test_api_version_auto_detect_native_audio():
    """native-audio models → v1alpha; 3.1-flash-live → v1beta."""
    adapter_native = GeminiLiveAdapter(
        "test-key",
        model="gemini-2.5-flash-native-audio-preview-09-2025",
    )
    assert adapter_native._api_version == "v1alpha"

    adapter_new = GeminiLiveAdapter(
        "test-key",
        model=GEMINI_LIVE_3_1_FLASH_PREVIEW,
    )
    assert adapter_new._api_version == "v1beta"


@pytest.mark.mocked
def test_explicit_api_version_overrides_auto_detect():
    adapter = GeminiLiveAdapter(
        "test-key",
        model=GEMINI_LIVE_3_1_FLASH_PREVIEW,
        api_version="v1alpha",
    )
    assert adapter._api_version == "v1alpha"


@pytest.mark.mocked
def test_gemini_live_model_enum_has_3_1_flash():
    assert GeminiLiveModel.FLASH_3_1_LIVE_PREVIEW == "gemini-3.1-flash-live-preview"


@pytest.mark.mocked
def test_module_constant_value():
    assert GEMINI_LIVE_3_1_FLASH_PREVIEW == "gemini-3.1-flash-live-preview"


@pytest.mark.mocked
def test_gemini_live_engine_marker_and_dispatch(monkeypatch):
    """GeminiLive marker carries config and client._unpack_engine dispatches it.

    REAL marker + REAL Patter._unpack_engine — no mocks. Verifies the engine
    resolves to provider kind ``gemini_live`` with the marker's fields, and that
    the api_key falls back to GEMINI_API_KEY / GOOGLE_API_KEY.
    """
    from getpatter import GeminiLive
    from getpatter.client import Patter

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    marker = GeminiLive(
        api_key="explicit-key",
        model="gemini-3.1-flash-live-preview",
        voice="Kore",
        language="it-IT",
        temperature=0.5,
    )
    assert marker.kind == "gemini_live"

    kind, fields = Patter._unpack_engine(marker)
    assert kind == "gemini_live"
    assert fields == {
        "api_key": "explicit-key",
        "voice": "Kore",
        "model": "gemini-3.1-flash-live-preview",
        "language": "it-IT",
        "temperature": 0.5,
    }

    # GEMINI_API_KEY fallback when api_key omitted.
    monkeypatch.setenv("GEMINI_API_KEY", "env-gemini")
    assert GeminiLive().api_key == "env-gemini"

    # GOOGLE_API_KEY fallback when GEMINI_API_KEY absent.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "env-google")
    assert GeminiLive().api_key == "env-google"


@pytest.mark.mocked
def test_gemini_live_engine_requires_key(monkeypatch):
    """GeminiLive raises ValueError when no key is supplied or in the env."""
    from getpatter import GeminiLive

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="requires an api_key"):
        GeminiLive()
