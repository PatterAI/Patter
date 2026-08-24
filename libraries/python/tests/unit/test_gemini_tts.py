"""Tests for the Gemini TTS provider.

Only the ``google-genai`` client is faked (it is the paid vendor boundary).
Key resolution, request assembly, the 24 kHz -> 16 kHz resample, chunk
streaming, warmup error handling and the declared audio format all run against
real code. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import math
import struct
from typing import Any
from unittest.mock import patch

import pytest

from getpatter.audio.format import AudioFormat
from getpatter.providers.gemini_tts import (
    GEMINI_TTS_DEFAULT_MODEL,
    GEMINI_TTS_DEFAULT_VOICE,
    GEMINI_TTS_SOURCE_SAMPLE_RATE,
    GeminiTTS,
)
from getpatter.tts.gemini import TTS as GeminiNamespacedTTS

pytestmark = pytest.mark.mocked

API_KEY = "test-gemini-key"


# ---------------------------------------------------------------------------
# google-genai fakes — only the vendor client is simulated.
# ---------------------------------------------------------------------------


class _FakeInlineData:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _FakePart:
    def __init__(self, data: bytes) -> None:
        self.inline_data = _FakeInlineData(data)


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self.parts = [_FakePart(data)]


class _FakeCandidate:
    def __init__(self, data: bytes) -> None:
        self.content = _FakeContent(data)


class _FakeChunk:
    def __init__(self, data: bytes) -> None:
        self.candidates = [_FakeCandidate(data)]


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> _FakeChunk:
        if not self._chunks:
            raise StopAsyncIteration
        return _FakeChunk(self._chunks.pop(0))


class _FakeModels:
    def __init__(self, chunks: list[bytes], error: Exception | None) -> None:
        self.chunks = chunks
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_content_stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _FakeStream(self.chunks)


class _FakeAio:
    def __init__(self, models: _FakeModels) -> None:
        self.models = models


class _FakeClient:
    def __init__(
        self, chunks: list[bytes] | None = None, error: Exception | None = None
    ) -> None:
        self.models = _FakeModels(chunks or [], error)
        self.aio = _FakeAio(self.models)


def _tone_pcm(num_samples: int) -> bytes:
    """Real PCM16-LE sine samples, so the resampler has signal to work on."""
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(i / 12.0))) for i in range(num_samples)
    )


async def _collect(tts: GeminiTTS, text: str) -> bytes:
    return b"".join([chunk async for chunk in tts.synthesize(text)])


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_the_api_key_falls_back_to_gemini_api_key(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert GeminiTTS().api_key == "from-env"


def test_the_api_key_falls_back_to_google_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-env")
    assert GeminiTTS().api_key == "google-env"


def test_a_missing_api_key_raises_with_both_env_names(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY / GOOGLE_API_KEY"):
        GeminiTTS()


def test_the_repr_never_leaks_the_api_key() -> None:
    assert API_KEY not in repr(GeminiTTS(API_KEY, voice="Puck"))


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_an_unsupported_target_sample_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="target_sample_rate"):
        GeminiTTS(API_KEY, target_sample_rate=44100)


def test_the_defaults_match_the_typescript_adapter() -> None:
    tts = GeminiTTS(API_KEY)
    assert tts.voice == GEMINI_TTS_DEFAULT_VOICE == "Kore"
    assert tts.model == GEMINI_TTS_DEFAULT_MODEL == "gemini-3.1-flash-tts-preview"
    assert tts.target_sample_rate == 16000
    assert GeminiTTS.provider_key == GeminiNamespacedTTS.provider_key == "gemini_tts"


def test_the_declared_source_format_follows_the_target_rate() -> None:
    tts = GeminiTTS(API_KEY, target_sample_rate=8000)
    assert tts.source_audio_format() == AudioFormat(
        encoding="pcm_s16le", sample_rate=8000
    )


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


async def test_the_request_carries_the_model_voice_and_audio_modality() -> None:
    client = _FakeClient([_tone_pcm(480)])
    with patch(
        "getpatter.providers.gemini_tts.build_genai_client", return_value=client
    ):
        await _collect(GeminiTTS(API_KEY, voice="Puck"), "hello there")

    call = client.models.calls[0]
    assert call["model"] == GEMINI_TTS_DEFAULT_MODEL
    assert call["contents"] == [{"role": "user", "parts": [{"text": "hello there"}]}]
    assert call["config"]["response_modalities"] == ["AUDIO"]
    voice_config = call["config"]["speech_config"]["voice_config"]
    assert voice_config["prebuilt_voice_config"]["voice_name"] == "Puck"


async def test_audio_is_resampled_from_24k_to_the_16k_pipeline_rate() -> None:
    # 2400 samples @ 24 kHz = 100 ms, which is 1600 samples @ 16 kHz.
    client = _FakeClient([_tone_pcm(2400)])
    with patch(
        "getpatter.providers.gemini_tts.build_genai_client", return_value=client
    ):
        out = await _collect(GeminiTTS(API_KEY), "resample me")

    assert len(out) % 2 == 0
    assert abs(len(out) // 2 - 1600) <= 2  # filter warm-up costs a sample or two


async def test_a_24k_target_streams_the_model_bytes_untouched() -> None:
    pcm = _tone_pcm(600)
    client = _FakeClient([pcm])
    with patch(
        "getpatter.providers.gemini_tts.build_genai_client", return_value=client
    ):
        out = await _collect(
            GeminiTTS(API_KEY, target_sample_rate=GEMINI_TTS_SOURCE_SAMPLE_RATE),
            "passthrough",
        )
    assert out == pcm


async def test_every_streamed_chunk_reaches_the_caller() -> None:
    client = _FakeClient([_tone_pcm(480), _tone_pcm(480), _tone_pcm(480)])
    with patch(
        "getpatter.providers.gemini_tts.build_genai_client", return_value=client
    ):
        chunks = [
            chunk
            async for chunk in GeminiTTS(
                API_KEY, target_sample_rate=GEMINI_TTS_SOURCE_SAMPLE_RATE
            ).synthesize("three chunks")
        ]
    assert len(chunks) == 3


async def test_chunks_without_audio_parts_are_skipped() -> None:
    client = _FakeClient([b"", _tone_pcm(480)])
    with patch(
        "getpatter.providers.gemini_tts.build_genai_client", return_value=client
    ):
        out = await _collect(
            GeminiTTS(API_KEY, target_sample_rate=GEMINI_TTS_SOURCE_SAMPLE_RATE),
            "one empty chunk",
        )
    assert len(out) == 960


async def test_blank_text_never_reaches_the_model() -> None:
    client = _FakeClient([_tone_pcm(480)])
    with patch(
        "getpatter.providers.gemini_tts.build_genai_client", return_value=client
    ):
        assert await _collect(GeminiTTS(API_KEY), "   ") == b""
    assert client.models.calls == []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_warmup_drains_a_synthesis_without_raising() -> None:
    client = _FakeClient([_tone_pcm(480)])
    with patch(
        "getpatter.providers.gemini_tts.build_genai_client", return_value=client
    ):
        await GeminiTTS(API_KEY).warmup()
    assert len(client.models.calls) == 1


async def test_a_failing_warmup_is_swallowed_so_the_call_still_starts() -> None:
    client = _FakeClient(error=RuntimeError("model unavailable"))
    with patch(
        "getpatter.providers.gemini_tts.build_genai_client", return_value=client
    ):
        await GeminiTTS(API_KEY).warmup()  # must not raise


async def test_close_releases_the_client_handle() -> None:
    client = _FakeClient([_tone_pcm(480)])
    with patch(
        "getpatter.providers.gemini_tts.build_genai_client", return_value=client
    ):
        tts = GeminiTTS(API_KEY)
        await _collect(tts, "hello")
        await tts.close()
    assert tts._client is None


# ---------------------------------------------------------------------------
# Config-driven construction
# ---------------------------------------------------------------------------


def test_the_tts_config_factory_wires_the_adapter() -> None:
    from getpatter.models import TTSConfig
    from getpatter.telephony.common import _create_tts_from_config

    tts = _create_tts_from_config(
        TTSConfig(
            provider="gemini_tts",
            api_key=API_KEY,
            voice="Puck",
            options={"target_sample_rate": 8000},
        )
    )
    assert isinstance(tts, GeminiTTS)
    assert tts.voice == "Puck"
    assert tts.target_sample_rate == 8000


def test_an_empty_configured_voice_falls_back_to_the_default() -> None:
    from getpatter.models import TTSConfig
    from getpatter.telephony.common import _create_tts_from_config

    tts = _create_tts_from_config(
        TTSConfig(provider="gemini_tts", api_key=API_KEY, voice="")
    )
    assert tts.voice == GEMINI_TTS_DEFAULT_VOICE
