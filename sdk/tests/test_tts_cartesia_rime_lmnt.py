"""Tests for Cartesia + Rime + LMNT TTS providers.

Most tests use MOCK aiohttp responses so they run offline. Integration
tests that hit the real provider SKIP automatically when the corresponding
API key is not set in the environment.
"""

from __future__ import annotations

import math
import os
import struct
from typing import AsyncIterator

import pytest

aiohttp = pytest.importorskip("aiohttp")

from patter.providers.cartesia_tts import CartesiaTTS
from patter.providers.lmnt_tts import LMNTTTS
from patter.providers.rime_tts import RimeTTS


# ---------------------------------------------------------------------------
# Helpers: MOCK aiohttp stream — emits a synthetic PCM sine wave as "audio".
# ---------------------------------------------------------------------------


def _mock_pcm_sine_chunks(n_chunks: int = 4, samples_per_chunk: int = 800) -> list[bytes]:
    """Return a list of bytes chunks representing a sine wave in PCM_S16LE.

    MOCK data — used purely to prove the HTTP streaming plumbing works and
    yields non-empty bytes.
    """
    chunks: list[bytes] = []
    for c in range(n_chunks):
        samples = [
            int(32767 * 0.25 * math.sin(2 * math.pi * (c * samples_per_chunk + i) / 40))
            for i in range(samples_per_chunk)
        ]
        chunks.append(struct.pack(f"<{samples_per_chunk}h", *samples))
    return chunks


class _MockContent:
    """Mimics ``aiohttp.StreamReader`` for the slice we actually use."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for c in self._chunks:
            yield c


class _MockResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status: int = 200,
        content_type: str = "audio/pcm",
    ):
        self.status = status
        self.content = _MockContent(chunks)
        self.headers = {"Content-Type": content_type}

    async def __aenter__(self) -> "_MockResponse":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None,  # type: ignore
                history=(),
                status=self.status,
                message="MOCK HTTP error",
            )

    async def text(self) -> str:
        return b"".join(self.content._chunks).decode("utf-8", errors="replace")


class _MockSession:
    """Drop-in replacement for ``aiohttp.ClientSession`` in tests."""

    def __init__(self, response: _MockResponse):
        self._response = response
        self.closed = False
        self.last_request: dict | None = None

    def post(self, url, **kwargs):
        self.last_request = {"url": url, **kwargs}
        return self._response

    async def close(self) -> None:
        self.closed = True


async def _drain(it: AsyncIterator[bytes]) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in it:
        out.append(chunk)
    return out


# ---------------------------------------------------------------------------
# CartesiaTTS
# ---------------------------------------------------------------------------


def test_cartesia_requires_api_key(monkeypatch):
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    with pytest.raises(ValueError):
        CartesiaTTS()


def test_cartesia_picks_up_env_key(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "env_key")
    tts = CartesiaTTS()
    assert tts.api_key == "env_key"


def test_cartesia_defaults():
    tts = CartesiaTTS(api_key="key")
    assert tts.model == "sonic-2"
    assert tts.sample_rate == 16000
    assert tts.language == "en"
    # Default voice is Katie — Friendly Fixer from upstream LiveKit.
    assert tts.voice == "f786b574-daa5-4673-aa0c-cbe3e8534c02"


def test_cartesia_repr_does_not_leak_api_key():
    tts = CartesiaTTS(api_key="secret_key_abc")
    assert "secret_key_abc" not in repr(tts)


def test_cartesia_build_payload_shape():
    tts = CartesiaTTS(
        api_key="k",
        model="sonic-3",
        voice="v1",
        language="it",
        sample_rate=24000,
        speed=1.2,
        volume=0.9,
    )
    payload = tts._build_payload("ciao")
    assert payload["model_id"] == "sonic-3"
    assert payload["voice"] == {"mode": "id", "id": "v1"}
    assert payload["transcript"] == "ciao"
    assert payload["language"] == "it"
    assert payload["output_format"] == {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 24000,
    }
    assert payload["generation_config"]["speed"] == 1.2
    assert payload["generation_config"]["volume"] == 0.9


@pytest.mark.asyncio
async def test_cartesia_synthesize_streams_mock_bytes():
    """MOCK: simulate HTTP stream, verify yielded bytes are non-empty."""
    chunks = _mock_pcm_sine_chunks(n_chunks=3, samples_per_chunk=400)
    session = _MockSession(_MockResponse(chunks))

    tts = CartesiaTTS(api_key="k", session=session)  # type: ignore[arg-type]
    out = await _drain(tts.synthesize("hello world"))

    assert len(out) == 3
    assert all(len(c) > 0 for c in out)
    assert out == chunks
    # URL should target /tts/bytes
    assert session.last_request and session.last_request["url"].endswith("/tts/bytes")


@pytest.mark.asyncio
async def test_cartesia_close_is_idempotent():
    tts = CartesiaTTS(api_key="k")
    await tts.close()
    await tts.close()  # should not raise


@pytest.mark.asyncio
async def test_cartesia_close_does_not_close_external_session():
    session = _MockSession(_MockResponse([]))
    tts = CartesiaTTS(api_key="k", session=session)  # type: ignore[arg-type]
    await tts.close()
    assert session.closed is False


# ---------------------------------------------------------------------------
# RimeTTS
# ---------------------------------------------------------------------------


def test_rime_requires_api_key(monkeypatch):
    monkeypatch.delenv("RIME_API_KEY", raising=False)
    with pytest.raises(ValueError):
        RimeTTS()


def test_rime_default_speaker_arcana():
    tts = RimeTTS(api_key="k")
    assert tts.model == "arcana"
    assert tts.speaker == "astra"


def test_rime_default_speaker_mist():
    tts = RimeTTS(api_key="k", model="mistv2")
    assert tts.speaker == "cove"


def test_rime_arcana_payload():
    tts = RimeTTS(
        api_key="k",
        model="arcana",
        temperature=0.5,
        top_p=0.9,
        max_tokens=512,
        sample_rate=16000,
    )
    payload = tts._build_payload("hello")
    assert payload["speaker"] == "astra"
    assert payload["modelId"] == "arcana"
    assert payload["temperature"] == 0.5
    assert payload["top_p"] == 0.9
    assert payload["max_tokens"] == 512
    assert payload["samplingRate"] == 16000
    assert payload["lang"] == "eng"


def test_rime_mist_payload():
    tts = RimeTTS(
        api_key="k",
        model="mistv2",
        speed_alpha=1.1,
        reduce_latency=True,
        pause_between_brackets=True,
    )
    payload = tts._build_payload("hi")
    assert payload["modelId"] == "mistv2"
    assert payload["speaker"] == "cove"
    assert payload["speedAlpha"] == 1.1
    assert payload["reduceLatency"] is True
    assert payload["pauseBetweenBrackets"] is True


def test_rime_timeout_arcana_vs_mist():
    assert RimeTTS(api_key="k", model="arcana")._total_timeout == 60 * 4
    assert RimeTTS(api_key="k", model="mistv2")._total_timeout == 30


@pytest.mark.asyncio
async def test_rime_synthesize_streams_mock_bytes():
    """MOCK: audio/pcm response yields bytes."""
    chunks = _mock_pcm_sine_chunks(n_chunks=2, samples_per_chunk=400)
    session = _MockSession(_MockResponse(chunks, content_type="audio/pcm"))

    tts = RimeTTS(api_key="k", session=session)  # type: ignore[arg-type]
    out = await _drain(tts.synthesize("ciao"))

    assert len(out) == 2
    assert all(len(c) > 0 for c in out)


@pytest.mark.asyncio
async def test_rime_rejects_non_audio_response():
    """MOCK: if Rime responds with JSON/text, we raise."""
    session = _MockSession(
        _MockResponse([b'{"error":"bad"}'], content_type="application/json")
    )
    tts = RimeTTS(api_key="k", session=session)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await _drain(tts.synthesize("hi"))


@pytest.mark.asyncio
async def test_rime_close_is_idempotent():
    tts = RimeTTS(api_key="k")
    await tts.close()
    await tts.close()


# ---------------------------------------------------------------------------
# LMNTTTS
# ---------------------------------------------------------------------------


def test_lmnt_requires_api_key(monkeypatch):
    monkeypatch.delenv("LMNT_API_KEY", raising=False)
    with pytest.raises(ValueError):
        LMNTTTS()


def test_lmnt_defaults():
    tts = LMNTTTS(api_key="k")
    # Patter defaults to raw PCM for telephony output.
    assert tts.format == "raw"
    assert tts.sample_rate == 16000
    assert tts.voice == "leah"
    assert tts.model == "blizzard"
    # blizzard => language defaults to 'auto'
    assert tts.language == "auto"


def test_lmnt_language_default_aurora():
    tts = LMNTTTS(api_key="k", model="aurora")
    assert tts.language == "en"


def test_lmnt_payload_shape():
    tts = LMNTTTS(api_key="k", voice="lily", format="raw", sample_rate=24000)
    payload = tts._build_payload("hola")
    assert payload["text"] == "hola"
    assert payload["voice"] == "lily"
    assert payload["sample_rate"] == 24000
    assert payload["format"] == "raw"
    assert payload["temperature"] == 1.0
    assert payload["top_p"] == 0.8


@pytest.mark.asyncio
async def test_lmnt_synthesize_streams_mock_bytes():
    """MOCK: raw PCM response yields bytes."""
    chunks = _mock_pcm_sine_chunks(n_chunks=4, samples_per_chunk=200)
    session = _MockSession(_MockResponse(chunks, content_type="application/octet-stream"))

    tts = LMNTTTS(api_key="k", session=session)  # type: ignore[arg-type]
    out = await _drain(tts.synthesize("hello"))

    assert len(out) == 4
    assert all(len(c) > 0 for c in out)


@pytest.mark.asyncio
async def test_lmnt_close_is_idempotent():
    tts = LMNTTTS(api_key="k")
    await tts.close()
    await tts.close()


# ---------------------------------------------------------------------------
# Integration tests — SKIP without real API keys
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cartesia_integration():
    if not os.environ.get("CARTESIA_API_KEY"):
        pytest.skip("CARTESIA_API_KEY not set")
    tts = CartesiaTTS(sample_rate=16000)
    try:
        collected = 0
        async for chunk in tts.synthesize("Hello from Patter integration test."):
            collected += len(chunk)
            if collected > 4000:
                break
        assert collected > 0
    finally:
        await tts.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rime_integration():
    if not os.environ.get("RIME_API_KEY"):
        pytest.skip("RIME_API_KEY not set")
    tts = RimeTTS(model="mistv2")
    try:
        collected = 0
        async for chunk in tts.synthesize("Hello from Patter integration test."):
            collected += len(chunk)
            if collected > 4000:
                break
        assert collected > 0
    finally:
        await tts.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lmnt_integration():
    if not os.environ.get("LMNT_API_KEY"):
        pytest.skip("LMNT_API_KEY not set")
    tts = LMNTTTS()
    try:
        collected = 0
        async for chunk in tts.synthesize("Hello from Patter integration test."):
            collected += len(chunk)
            if collected > 4000:
                break
        assert collected > 0
    finally:
        await tts.close()
