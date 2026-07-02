"""Unit tests for the Soniox TTS provider.

Mock the aiohttp boundary; everything else (payload assembly, Bearer auth,
telephony factories, source-format declaration, env-var fallback) runs against
real code. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

from typing import Any

import pytest

from getpatter.audio.format import AudioFormat
from getpatter.providers.soniox_tts import (
    SONIOX_TTS_REST_URL,
    SonioxTTS,
    SonioxTTSModel,
)
from getpatter.tts import soniox as soniox_ns


# ---------------------------------------------------------------------------
# aiohttp fakes — only the network boundary is mocked.
# ---------------------------------------------------------------------------


class _AsyncChunks:
    """Async iterator over a list of byte chunks (emulates iter_chunked)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> "_AsyncChunks":
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeContent:
    """Stand-in for ``aiohttp.StreamReader`` exposing ``iter_chunked``."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def iter_chunked(self, _size: int) -> _AsyncChunks:
        return _AsyncChunks(self._chunks)


class _FakeResponse:
    def __init__(self, status: int, chunks: list[bytes], body: str = "") -> None:
        self.status = status
        self.content = _FakeContent(chunks)
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.last_url: str | None = None
        self.last_json: dict[str, Any] | None = None
        self.last_headers: dict[str, str] | None = None
        self.closed = False

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],  # noqa: A002 - aiohttp signature compat
        timeout: Any = None,
    ) -> _FakeResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        return self.response

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Request shape + auth
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPayloadAndAuth:
    async def test_posts_to_rest_endpoint_with_bearer_auth(self) -> None:
        fake = _FakeSession(_FakeResponse(200, [b"hello", b"world"]))
        tts = SonioxTTS(api_key="sk-key", session=fake)  # type: ignore[arg-type]

        out = b"".join([c async for c in tts.synthesize("ciao")])

        assert out == b"helloworld"
        assert fake.last_url == SONIOX_TTS_REST_URL
        assert fake.last_headers is not None
        assert fake.last_headers["Authorization"] == "Bearer sk-key"
        assert fake.last_headers["Content-Type"] == "application/json"

    async def test_default_payload_uses_v1_pcm16_16k_adrian(self) -> None:
        fake = _FakeSession(_FakeResponse(200, []))
        tts = SonioxTTS(api_key="sk-key", session=fake)  # type: ignore[arg-type]
        async for _ in tts.synthesize("hi"):
            pass
        body = fake.last_json
        assert body is not None
        assert body["model"] == SonioxTTSModel.TTS_RT_V1.value
        assert body["voice"] == "Adrian"
        assert body["language"] == "en"
        assert body["audio_format"] == "pcm_s16le"
        assert body["sample_rate"] == 16000
        assert "bitrate" not in body
        assert "speed" not in body

    async def test_optional_fields_only_added_when_set(self) -> None:
        fake = _FakeSession(_FakeResponse(200, []))
        tts = SonioxTTS(
            api_key="sk-key",
            session=fake,  # type: ignore[arg-type]
            language="it",
            voice="Maya",
            bitrate=128000,
            speed=1.1,
        )
        async for _ in tts.synthesize("ciao"):
            pass
        body = fake.last_json
        assert body is not None
        assert body["language"] == "it"
        assert body["voice"] == "Maya"
        assert body["bitrate"] == 128000
        assert body["speed"] == 1.1

    async def test_non_200_raises_with_body_excerpt(self) -> None:
        fake = _FakeSession(_FakeResponse(429, [], body="rate limited"))
        tts = SonioxTTS(api_key="sk-key", session=fake)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match=r"Soniox TTS error 429"):
            async for _ in tts.synthesize("hi"):
                pass

    def test_provider_key_is_soniox_tts(self) -> None:
        assert SonioxTTS.provider_key == "soniox_tts"


# ---------------------------------------------------------------------------
# Source-format declaration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSourceAudioFormat:
    def test_default_declares_pcm16_at_configured_rate(self) -> None:
        tts = SonioxTTS(api_key="x", sample_rate=24000)
        assert tts.source_audio_format() == AudioFormat(
            encoding="pcm_s16le", sample_rate=24000
        )

    def test_mulaw_declares_mulaw(self) -> None:
        tts = SonioxTTS(api_key="x", audio_format="pcm_mulaw", sample_rate=8000)
        assert tts.source_audio_format() == AudioFormat(
            encoding="mulaw", sample_rate=8000
        )

    def test_alaw_declares_alaw(self) -> None:
        tts = SonioxTTS(api_key="x", audio_format="pcm_alaw", sample_rate=8000)
        assert tts.source_audio_format() == AudioFormat(
            encoding="alaw", sample_rate=8000
        )


# ---------------------------------------------------------------------------
# Telephony factories (provider-level)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProviderTelephonyFactories:
    def test_for_twilio_emits_mulaw_8k_natively(self) -> None:
        tts = SonioxTTS.for_twilio(api_key="x")
        assert tts.sample_rate == 8000
        assert tts.audio_format == "pcm_mulaw"

    def test_for_twilio_payload_requests_mulaw_8k(self) -> None:
        tts = SonioxTTS.for_twilio(api_key="x")
        payload = tts._build_payload("hello")
        assert payload["sample_rate"] == 8000
        assert payload["audio_format"] == "pcm_mulaw"

    def test_for_twilio_declares_mulaw_source_format(self) -> None:
        tts = SonioxTTS.for_twilio(api_key="x")
        assert tts.source_audio_format() == AudioFormat(
            encoding="mulaw", sample_rate=8000
        )

    def test_for_twilio_respects_overrides(self) -> None:
        tts = SonioxTTS.for_twilio(
            api_key="x", voice="Maya", language="es", speed=1.2
        )
        assert tts.sample_rate == 8000
        assert tts.audio_format == "pcm_mulaw"
        assert tts.voice == "Maya"
        assert tts.language == "es"
        assert tts.speed == 1.2

    def test_for_twilio_ignores_caller_sample_rate_and_format(self) -> None:
        tts = SonioxTTS.for_twilio(
            api_key="x", sample_rate=24000, audio_format="mp3"
        )
        assert tts.sample_rate == 8000
        assert tts.audio_format == "pcm_mulaw"

    def test_for_telnyx_emits_mulaw_8k_natively(self) -> None:
        tts = SonioxTTS.for_telnyx(api_key="x")
        assert tts.sample_rate == 8000
        assert tts.audio_format == "pcm_mulaw"

    def test_constructor_default_unchanged(self) -> None:
        tts = SonioxTTS(api_key="x")
        assert tts.sample_rate == 16000
        assert tts.audio_format == "pcm_s16le"


# ---------------------------------------------------------------------------
# Pipeline-mode public TTS wrapper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNamespacePublicTTS:
    def test_requires_api_key_or_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SONIOX_API_KEY", raising=False)
        with pytest.raises(ValueError, match=r"SONIOX_API_KEY"):
            soniox_ns.TTS()

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SONIOX_API_KEY", "env-key")
        tts = soniox_ns.TTS()
        assert tts.api_key == "env-key"

    def test_explicit_api_key_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SONIOX_API_KEY", "env-key")
        tts = soniox_ns.TTS(api_key="explicit")
        assert tts.api_key == "explicit"

    def test_provider_key_is_soniox_tts(self) -> None:
        assert soniox_ns.TTS.provider_key == "soniox_tts"

    def test_for_twilio_emits_mulaw_8k(self) -> None:
        tts = soniox_ns.TTS.for_twilio(api_key="x")
        assert tts.sample_rate == 8000
        assert tts.audio_format == "pcm_mulaw"

    def test_for_telnyx_emits_mulaw_8k(self) -> None:
        tts = soniox_ns.TTS.for_telnyx(api_key="x")
        assert tts.sample_rate == 8000
        assert tts.audio_format == "pcm_mulaw"

    def test_for_twilio_requires_api_key_or_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SONIOX_API_KEY", raising=False)
        with pytest.raises(ValueError, match="SONIOX_API_KEY"):
            soniox_ns.TTS.for_twilio()

    def test_for_twilio_uses_env_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SONIOX_API_KEY", "env-key")
        tts = soniox_ns.TTS.for_twilio()
        assert tts.sample_rate == 8000
        assert tts.api_key == "env-key"
