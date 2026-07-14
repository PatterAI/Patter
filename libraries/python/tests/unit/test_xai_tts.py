"""Unit tests for the xAI TTS provider + custom-voice helper.

Mock the aiohttp boundary; payload assembly, Bearer auth, telephony factories,
source-format declaration, byte streaming, and multipart ordering run against
real code. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from getpatter.audio.format import AudioFormat
from getpatter.providers.xai_tts import (
    XAI_CUSTOM_VOICES_URL,
    XAI_TTS_REST_URL,
    XaiTTS,
    create_custom_voice,
)

# ---------------------------------------------------------------------------
# aiohttp fakes — only the network boundary is mocked.
# ---------------------------------------------------------------------------


class _AsyncChunks:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> "_AsyncChunks":
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeContent:
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

    async def json(self) -> dict:
        import json

        return json.loads(self._body)

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
        self.last_data: Any = None
        self.closed = False

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,  # noqa: A002 - aiohttp signature
        data: Any = None,
        timeout: Any = None,
    ) -> _FakeResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_json = json
        self.last_data = data
        return self.response

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Request shape + auth + streaming
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestPayloadAndAuth:
    async def test_posts_to_rest_endpoint_with_bearer_auth(self) -> None:
        fake = _FakeSession(_FakeResponse(200, [b"hello", b"world"]))
        tts = XaiTTS(api_key="xai-test-key", session=fake)  # type: ignore[arg-type]

        out = b"".join([c async for c in tts.synthesize("ciao")])

        assert out == b"helloworld"
        assert fake.last_url == XAI_TTS_REST_URL
        assert fake.last_headers is not None
        assert fake.last_headers["Authorization"] == "Bearer xai-test-key"
        assert fake.last_headers["Content-Type"] == "application/json"

    async def test_default_payload_is_eve_auto_pcm_16k(self) -> None:
        fake = _FakeSession(_FakeResponse(200, []))
        tts = XaiTTS(api_key="xai-test-key", session=fake)  # type: ignore[arg-type]
        async for _ in tts.synthesize("hi"):
            pass
        body = fake.last_json
        assert body is not None
        assert body["voice_id"] == "eve"
        assert body["language"] == "auto"
        assert body["output_format"] == {"codec": "pcm", "sample_rate": 16000}
        assert "speed" not in body
        assert "optimize_streaming_latency" not in body
        assert "text_normalization" not in body

    async def test_optional_fields_only_added_when_set(self) -> None:
        fake = _FakeSession(_FakeResponse(200, []))
        tts = XaiTTS(
            api_key="xai-test-key",
            session=fake,  # type: ignore[arg-type]
            voice="ara",
            language="en",
            speed=1.2,
            optimize_streaming_latency=1,
            text_normalization=True,
        )
        async for _ in tts.synthesize("ciao"):
            pass
        body = fake.last_json
        assert body["voice_id"] == "ara"
        assert body["speed"] == 1.2
        assert body["optimize_streaming_latency"] == 1
        assert body["text_normalization"] is True

    async def test_bit_rate_nests_under_output_format_when_set(self) -> None:
        fake = _FakeSession(_FakeResponse(200, []))
        tts = XaiTTS(
            api_key="xai-test-key",
            session=fake,  # type: ignore[arg-type]
            codec="mp3",
            sample_rate=44100,
            bit_rate=192000,
        )
        async for _ in tts.synthesize("ciao"):
            pass
        body = fake.last_json
        assert body is not None
        assert body["output_format"] == {
            "codec": "mp3",
            "sample_rate": 44100,
            "bit_rate": 192000,
        }

    async def test_non_200_raises(self) -> None:
        fake = _FakeSession(_FakeResponse(400, [], body="bad request"))
        tts = XaiTTS(api_key="xai-test-key", session=fake)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="400"):
            async for _ in tts.synthesize("x"):
                pass


# ---------------------------------------------------------------------------
# Telephony factories + source format
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTelephonyFactories:
    def test_for_twilio_emits_mulaw_8k_source_format(self) -> None:
        tts = XaiTTS.for_twilio(api_key="xai-test-key")
        assert tts.codec == "mulaw"
        assert tts.sample_rate == 8000
        assert tts.source_audio_format() == AudioFormat(
            encoding="mulaw", sample_rate=8000
        )

    def test_for_telnyx_emits_pcm_16k(self) -> None:
        tts = XaiTTS.for_telnyx(api_key="xai-test-key")
        assert tts.codec == "pcm"
        assert tts.sample_rate == 16000
        assert tts.source_audio_format() == AudioFormat(
            encoding="pcm_s16le", sample_rate=16000
        )

    def test_default_source_format_is_pcm_16k(self) -> None:
        tts = XaiTTS(api_key="xai-test-key")
        assert tts.source_audio_format() == AudioFormat(
            encoding="pcm_s16le", sample_rate=16000
        )


# ---------------------------------------------------------------------------
# Custom voice creation
# ---------------------------------------------------------------------------


class _FakeCustomVoiceSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_data: Any = None

    def post(
        self, url: str, *, headers: dict[str, str], data: Any, timeout: Any = None
    ) -> _FakeResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_data = data
        return self.response

    async def __aenter__(self) -> "_FakeCustomVoiceSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


@pytest.mark.mocked
class TestCustomVoice:
    async def test_create_custom_voice_returns_voice_id(self) -> None:
        import json

        fake = _FakeCustomVoiceSession(
            _FakeResponse(200, [], body=json.dumps({"voice_id": "vc_abc123"}))
        )
        with patch(
            "getpatter.providers.xai_tts.aiohttp.ClientSession", return_value=fake
        ):
            voice_id = await create_custom_voice(
                name="Receptionist",
                language="en",
                audio=b"RIFFwavdata",
                api_key="xai-test-key",
            )
        assert voice_id == "vc_abc123"
        assert fake.last_url == XAI_CUSTOM_VOICES_URL
        assert fake.last_headers == {"Authorization": "Bearer xai-test-key"}
        # ``file`` must be the LAST multipart field (name, language, then file).
        names = [f[0]["name"] for f in fake.last_data._fields]
        assert names == ["name", "language", "file"]
