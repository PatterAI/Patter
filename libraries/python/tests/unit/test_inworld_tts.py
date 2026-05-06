"""Unit tests for the Inworld TTS provider.

Mock the aiohttp boundary; everything else (payload assembly, NDJSON
parsing, base64 decoding, env-var fallback) runs against real code.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import patch

import pytest

from getpatter.providers.inworld_tts import (
    INWORLD_BASE_URL,
    InworldModel,
    InworldTTS,
)
from getpatter.tts import inworld as inworld_ns


class _FakeContent:
    """Async-iterable line iterator emulating ``aiohttp.StreamReader``."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> "_FakeContent":
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeResponse:
    def __init__(self, status: int, lines: list[bytes], body: str = "") -> None:
        self.status = status
        self.content = _FakeContent(lines)
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


def _ndjson_lines(*chunks_b64: str, timestamp_after: int | None = 1) -> list[bytes]:
    """Build an NDJSON byte stream like ``aiohttp.StreamReader`` exposes it.

    Each entry is one ``readline()``-style chunk with the trailing \n. When
    ``timestamp_after`` is set, a timestamp-only line is inserted after the
    audio chunk at that 1-based position so the parser is exercised on a
    realistic ``[audio, timestamp, audio]`` interleave (not just a trailing
    timestamp).
    """
    lines: list[bytes] = []
    for idx, b64 in enumerate(chunks_b64, start=1):
        lines.append(json.dumps({"result": {"audioContent": b64}}).encode() + b"\n")
        if timestamp_after is not None and idx == timestamp_after:
            lines.append(
                json.dumps(
                    {"result": {"timestampInfo": {"wordAlignment": []}}}
                ).encode()
                + b"\n"
            )
    return lines


@pytest.mark.unit
class TestPayloadAndAuth:
    async def test_posts_to_streaming_endpoint_with_basic_auth(self) -> None:
        chunk_a = base64.b64encode(b"hello").decode()
        chunk_b = base64.b64encode(b"world").decode()
        fake = _FakeSession(_FakeResponse(200, _ndjson_lines(chunk_a, chunk_b)))

        tts = InworldTTS(auth_token="tok", session=fake)  # type: ignore[arg-type]
        out = b"".join([c async for c in tts.synthesize("ciao")])

        assert out == b"helloworld"
        assert fake.last_url == INWORLD_BASE_URL
        assert fake.last_headers is not None
        assert fake.last_headers["Authorization"] == "Basic tok"
        assert fake.last_headers["Content-Type"] == "application/json"

    async def test_default_payload_uses_tts2_pcm_16k(self) -> None:
        fake = _FakeSession(_FakeResponse(200, []))
        tts = InworldTTS(auth_token="tok", session=fake)  # type: ignore[arg-type]
        async for _ in tts.synthesize("hi"):
            pass
        body = fake.last_json
        assert body is not None
        assert body["modelId"] == InworldModel.TTS_2.value
        assert body["voiceId"] == "Ashley"
        assert body["speakingRate"] == 1.0
        assert body["audioConfig"] == {
            "audioEncoding": "PCM",
            "bitrate": 64000,
            "sampleRateHertz": 16000,
        }
        assert "language" not in body
        assert "temperature" not in body
        assert "deliveryMode" not in body

    async def test_optional_fields_only_added_when_set(self) -> None:
        fake = _FakeSession(_FakeResponse(200, []))
        tts = InworldTTS(
            auth_token="tok",
            session=fake,  # type: ignore[arg-type]
            language="it",
            temperature=0.7,
            delivery_mode="BALANCED",
        )
        async for _ in tts.synthesize("ciao"):
            pass
        body = fake.last_json
        assert body is not None
        assert body["language"] == "it"
        assert body["temperature"] == 0.7
        assert body["deliveryMode"] == "BALANCED"

    async def test_non_200_raises_with_body_excerpt(self) -> None:
        fake = _FakeSession(_FakeResponse(429, [], body="rate limited"))
        tts = InworldTTS(auth_token="tok", session=fake)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match=r"Inworld TTS error 429"):
            async for _ in tts.synthesize("hi"):
                pass


@pytest.mark.unit
class TestNamespacePublicTTS:
    def test_requires_api_key_or_env_var(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os as _os

            _os.environ.pop("INWORLD_API_KEY", None)
            with pytest.raises(ValueError, match=r"INWORLD_API_KEY"):
                inworld_ns.TTS()

    def test_env_var_fallback(self) -> None:
        with patch.dict("os.environ", {"INWORLD_API_KEY": "env-tok"}, clear=False):
            tts = inworld_ns.TTS()
            assert tts.auth_token == "env-tok"

    def test_explicit_api_key_wins(self) -> None:
        with patch.dict("os.environ", {"INWORLD_API_KEY": "env-tok"}, clear=False):
            tts = inworld_ns.TTS(api_key="explicit")
            assert tts.auth_token == "explicit"
