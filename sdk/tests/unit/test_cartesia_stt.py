"""Unit tests for CartesiaSTT.

MOCK: no real API calls. These tests mock ``aiohttp.ClientSession.ws_connect``
and feed synthetic JSON frames that imitate Cartesia's STT WebSocket protocol
(``transcript`` with ``is_final``, ``flush_done``, ``done``, ``error``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

aiohttp = pytest.importorskip("aiohttp")

from patter.providers.base import STTProvider, Transcript  # noqa: E402
from patter.providers.cartesia_stt import (  # noqa: E402
    CartesiaSTT,
    CartesiaSTTOptions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []
        self.sent_strs: list[str] = []
        self._frames: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.pinged = 0

    def push_text(self, payload: dict) -> None:
        msg = MagicMock()
        msg.type = aiohttp.WSMsgType.TEXT
        msg.data = json.dumps(payload)
        self._frames.put_nowait(msg)

    def push_close(self) -> None:
        msg = MagicMock()
        msg.type = aiohttp.WSMsgType.CLOSED
        msg.data = None
        self._frames.put_nowait(msg)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_str(self, data: str) -> None:
        self.sent_strs.append(data)

    async def ping(self) -> None:
        self.pinged += 1

    async def close(self) -> None:
        self.closed = True
        self.push_close()

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> Any:
        msg = await self._frames.get()
        if msg.type == aiohttp.WSMsgType.CLOSED:
            raise StopAsyncIteration
        return msg


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCartesiaSTTConstruction:
    def test_requires_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            CartesiaSTT(api_key="")

    def test_defaults(self) -> None:
        stt = CartesiaSTT(api_key="key")
        assert stt._opts.model == "ink-whisper"
        assert stt._opts.language == "en"
        assert stt._opts.encoding == "pcm_s16le"
        assert stt._opts.sample_rate == 16000

    def test_repr(self) -> None:
        stt = CartesiaSTT(api_key="key", language="it")
        r = repr(stt)
        assert "CartesiaSTT" in r
        assert "it" in r

    def test_custom_options_override(self) -> None:
        opts = CartesiaSTTOptions(
            model="ink-whisper",
            language="de",
            encoding="pcm_s16le",
            sample_rate=8000,
        )
        stt = CartesiaSTT(api_key="key", options=opts)
        assert stt._opts.language == "de"
        assert stt._opts.sample_rate == 8000

    def test_implements_stt_provider(self) -> None:
        stt = CartesiaSTT(api_key="key")
        assert isinstance(stt, STTProvider)


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCartesiaUrl:
    def test_url_contains_required_params(self) -> None:
        stt = CartesiaSTT(api_key="key")
        url = stt._build_ws_url()
        assert url.startswith("wss://api.cartesia.ai/stt/websocket?")
        assert "model=ink-whisper" in url
        assert "sample_rate=16000" in url
        assert "encoding=pcm_s16le" in url
        assert "cartesia_version=2025-04-16" in url
        assert "api_key=key" in url
        assert "language=en" in url

    def test_url_translates_https_to_wss(self) -> None:
        stt = CartesiaSTT(api_key="key", base_url="https://alt.example.com")
        assert stt._build_ws_url().startswith("wss://alt.example.com/")

    def test_url_translates_http_to_ws(self) -> None:
        stt = CartesiaSTT(api_key="key", base_url="http://localhost:8080")
        assert stt._build_ws_url().startswith("ws://localhost:8080/")

    def test_url_passthrough_ws(self) -> None:
        stt = CartesiaSTT(api_key="key", base_url="ws://custom/")
        assert stt._build_ws_url().startswith("ws://custom//stt/websocket?")


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCartesiaEventHandling:
    def test_transcript_final(self) -> None:
        stt = CartesiaSTT(api_key="key")
        stt._handle_event(
            {
                "type": "transcript",
                "text": "hello",
                "is_final": True,
                "probability": 0.92,
                "request_id": "req-1",
            }
        )
        assert stt.request_id == "req-1"
        assert stt._transcript_queue.qsize() == 1
        t = stt._transcript_queue.get_nowait()
        assert t.text == "hello"
        assert t.is_final is True
        assert t.confidence == pytest.approx(0.92)

    def test_transcript_interim(self) -> None:
        stt = CartesiaSTT(api_key="key")
        stt._handle_event(
            {
                "type": "transcript",
                "text": "he",
                "is_final": False,
                "probability": 0.5,
            }
        )
        t = stt._transcript_queue.get_nowait()
        assert t.text == "he"
        assert t.is_final is False

    def test_transcript_empty_text_and_not_final_ignored(self) -> None:
        stt = CartesiaSTT(api_key="key")
        stt._handle_event({"type": "transcript", "text": "", "is_final": False})
        assert stt._transcript_queue.qsize() == 0

    def test_transcript_empty_text_final_is_not_emitted(self) -> None:
        """A final frame with empty text is valid protocol but we don't surface
        an empty Transcript — callers would dedupe or ignore it anyway."""
        stt = CartesiaSTT(api_key="key")
        stt._handle_event({"type": "transcript", "text": "", "is_final": True})
        assert stt._transcript_queue.qsize() == 0

    def test_done_stops_running(self) -> None:
        stt = CartesiaSTT(api_key="key")
        stt._running = True
        stt._handle_event({"type": "done"})
        assert stt._running is False

    def test_flush_done_does_not_stop(self) -> None:
        stt = CartesiaSTT(api_key="key")
        stt._running = True
        stt._handle_event({"type": "flush_done"})
        assert stt._running is True

    def test_error_logged_but_no_crash(self) -> None:
        stt = CartesiaSTT(api_key="key")
        stt._handle_event({"type": "error", "message": "bad"})
        assert stt._transcript_queue.qsize() == 0


# ---------------------------------------------------------------------------
# Integration with mocked aiohttp
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_full_session_yields_transcripts_via_mocked_ws() -> None:
    fake_ws = _FakeWebSocket()

    fake_session = MagicMock()
    fake_session.ws_connect = AsyncMock(return_value=fake_ws)
    fake_session.close = AsyncMock()

    stt = CartesiaSTT(api_key="key")
    stt._session = fake_session
    await stt.connect()

    # Verify headers sent correctly.
    call_kwargs = fake_session.ws_connect.call_args.kwargs
    assert "User-Agent" in call_kwargs["headers"]

    await stt.send_audio(b"\x00\x00")
    assert fake_ws.sent_bytes == [b"\x00\x00"]

    # Push interim + final.
    fake_ws.push_text(
        {"type": "transcript", "text": "he", "is_final": False, "probability": 0.5}
    )
    fake_ws.push_text(
        {
            "type": "transcript",
            "text": "hello",
            "is_final": True,
            "probability": 0.95,
            "request_id": "req-abc",
        }
    )

    collected: list[Transcript] = []
    agen = stt.receive_transcripts()

    async def drain() -> None:
        async for t in agen:
            collected.append(t)
            if t.is_final:
                break

    await asyncio.wait_for(drain(), timeout=2.0)

    assert len(collected) == 2
    assert collected[0].is_final is False
    assert collected[1].text == "hello"
    assert collected[1].is_final is True
    assert stt.request_id == "req-abc"

    await stt.close()
    assert "finalize" in fake_ws.sent_strs
    assert fake_ws.closed is True


@pytest.mark.unit
async def test_send_audio_raises_when_not_connected() -> None:
    stt = CartesiaSTT(api_key="key")
    with pytest.raises(RuntimeError, match="connect"):
        await stt.send_audio(b"\x00")


@pytest.mark.unit
async def test_close_is_idempotent_when_never_connected() -> None:
    stt = CartesiaSTT(api_key="key")
    await stt.close()
