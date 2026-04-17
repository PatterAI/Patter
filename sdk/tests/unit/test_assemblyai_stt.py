"""Unit tests for AssemblyAISTT.

MOCK: no real API calls. These tests mock ``aiohttp.ClientSession.ws_connect``
and feed synthetic JSON frames that imitate AssemblyAI's v3 Universal Streaming
protocol (``Begin``, ``Turn``, ``Termination``).

Integration tests that hit the live API live in ``tests/integration`` and are
skipped unless ``ASSEMBLYAI_API_KEY`` is set.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

aiohttp = pytest.importorskip("aiohttp")

from patter.providers.assemblyai_stt import (  # noqa: E402
    AssemblyAISTT,
    AssemblyAISTTOptions,
)
from patter.providers.base import STTProvider, Transcript  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal aiohttp.ClientWebSocketResponse stand-in.

    Frames are pushed via ``push_text`` and yielded to the consumer via
    ``__aiter__`` — this replicates the ``async for msg in ws`` loop in
    the AssemblyAISTT recv task.
    """

    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []
        self.sent_strs: list[str] = []
        self._frames: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

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

    async def close(self) -> None:
        self.closed = True
        # Unblock any pending receivers.
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
class TestAssemblyAISTTConstruction:
    def test_requires_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            AssemblyAISTT(api_key="")

    def test_defaults(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        assert stt._opts.sample_rate == 16000
        assert stt._opts.encoding == "pcm_s16le"
        assert stt._opts.model == "universal-streaming-english"
        # Default min_turn_silence is 100 ms for u3-rt-pro parity.
        assert stt._opts.min_turn_silence == 100

    def test_repr(self) -> None:
        stt = AssemblyAISTT(api_key="key", model="u3-rt-pro")
        r = repr(stt)
        assert "AssemblyAISTT" in r
        assert "u3-rt-pro" in r

    def test_for_twilio_factory(self) -> None:
        stt = AssemblyAISTT.for_twilio(api_key="key")
        assert stt._opts.sample_rate == 8000
        assert stt._opts.encoding == "pcm_mulaw"

    def test_custom_options_override(self) -> None:
        opts = AssemblyAISTTOptions(
            sample_rate=24000,
            encoding="pcm_s16le",
            model="universal-streaming-multilingual",
            vad_threshold=0.5,
        )
        stt = AssemblyAISTT(api_key="key", options=opts)
        assert stt._opts.sample_rate == 24000
        assert stt._opts.vad_threshold == 0.5

    def test_implements_stt_provider(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        assert isinstance(stt, STTProvider)


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAssemblyAIUrl:
    def test_url_contains_required_params(self) -> None:
        stt = AssemblyAISTT(api_key="key", sample_rate=16000)
        url = stt._build_url()
        assert "sample_rate=16000" in url
        assert "encoding=pcm_s16le" in url
        assert "speech_model=universal-streaming-english" in url
        # English model: language_detection defaults to false.
        assert "language_detection=false" in url

    def test_url_multilingual_defaults_detection_true(self) -> None:
        stt = AssemblyAISTT(api_key="key", model="universal-streaming-multilingual")
        assert "language_detection=true" in stt._build_url()

    def test_url_u3_rt_pro_min_max_silence_defaults(self) -> None:
        stt = AssemblyAISTT(api_key="key", model="u3-rt-pro")
        url = stt._build_url()
        assert "min_turn_silence=100" in url
        assert "max_turn_silence=100" in url

    def test_url_omits_unset_options(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        url = stt._build_url()
        assert "format_turns" not in url
        assert "prompt" not in url
        assert "speaker_labels" not in url

    def test_url_serialises_keyterms_prompt_as_json_string(self) -> None:
        opts = AssemblyAISTTOptions(keyterms_prompt=["acme", "delta"])
        stt = AssemblyAISTT(api_key="key", options=opts)
        url = stt._build_url()
        assert "keyterms_prompt=" in url
        assert "acme" in url


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAssemblyAIEventHandling:
    def test_begin_sets_session_id(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        stt._handle_event({"type": "Begin", "id": "sess-1", "expires_at": 1234})
        assert stt.session_id == "sess-1"
        assert stt.expires_at == 1234

    def test_turn_final_enqueues_transcript(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        stt._handle_event(
            {
                "type": "Turn",
                "end_of_turn": True,
                "turn_is_formatted": False,
                "transcript": "hello world",
                "words": [
                    {"text": "hello", "confidence": 0.9},
                    {"text": "world", "confidence": 0.8},
                ],
            }
        )
        assert stt._transcript_queue.qsize() == 1
        t = stt._transcript_queue.get_nowait()
        assert t.text == "hello world"
        assert t.is_final is True
        # Average of 0.9 + 0.8 = 0.85
        assert abs(t.confidence - 0.85) < 1e-6

    def test_turn_final_when_format_turns_waits_for_formatted(self) -> None:
        opts = AssemblyAISTTOptions(format_turns=True)
        stt = AssemblyAISTT(api_key="key", options=opts)
        # Unformatted final — should NOT surface.
        stt._handle_event(
            {
                "type": "Turn",
                "end_of_turn": True,
                "turn_is_formatted": False,
                "transcript": "hi",
                "words": [{"text": "hi", "confidence": 1.0}],
            }
        )
        assert stt._transcript_queue.qsize() == 0
        # Formatted final — should surface.
        stt._handle_event(
            {
                "type": "Turn",
                "end_of_turn": True,
                "turn_is_formatted": True,
                "transcript": "Hi.",
                "words": [{"text": "Hi.", "confidence": 1.0}],
            }
        )
        assert stt._transcript_queue.qsize() == 1

    def test_turn_interim_enqueues_non_final(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        stt._handle_event(
            {
                "type": "Turn",
                "end_of_turn": False,
                "transcript": "",
                "words": [
                    {"text": "hello", "confidence": 0.7},
                ],
            }
        )
        t = stt._transcript_queue.get_nowait()
        assert t.text == "hello"
        assert t.is_final is False
        assert t.confidence == pytest.approx(0.7)

    def test_turn_interim_empty_words_no_output(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        stt._handle_event({"type": "Turn", "end_of_turn": False, "words": []})
        assert stt._transcript_queue.qsize() == 0

    def test_turn_final_empty_transcript_ignored(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        stt._handle_event(
            {
                "type": "Turn",
                "end_of_turn": True,
                "transcript": "   ",
                "words": [],
            }
        )
        assert stt._transcript_queue.qsize() == 0

    def test_termination_stops_running(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        stt._running = True
        stt._handle_event({"type": "Termination"})
        assert stt._running is False

    def test_unknown_event_ignored(self) -> None:
        stt = AssemblyAISTT(api_key="key")
        stt._handle_event({"type": "SomethingElse"})
        assert stt._transcript_queue.qsize() == 0


# ---------------------------------------------------------------------------
# Integration with mocked aiohttp
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_full_session_yields_transcripts_via_mocked_ws() -> None:
    """End-to-end test against a fake WebSocket:
    connect -> send_audio -> receive Begin + interim + final -> close.
    """
    fake_ws = _FakeWebSocket()

    fake_session = MagicMock()
    fake_session.ws_connect = AsyncMock(return_value=fake_ws)
    fake_session.close = AsyncMock()

    stt = AssemblyAISTT(api_key="key")
    stt._session = fake_session  # inject mock
    # Must not create another session in connect().
    await stt.connect()

    # Verify ws_connect was called with the Authorization header.
    call_kwargs = fake_session.ws_connect.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "key"

    # Send audio -> forwarded as bytes.
    await stt.send_audio(b"\x00\x01\x02\x03")
    assert fake_ws.sent_bytes == [b"\x00\x01\x02\x03"]

    # Push Begin + interim + final + termination.
    fake_ws.push_text({"type": "Begin", "id": "s", "expires_at": 42})
    fake_ws.push_text(
        {
            "type": "Turn",
            "end_of_turn": False,
            "words": [{"text": "hello", "confidence": 0.9}],
        }
    )
    fake_ws.push_text(
        {
            "type": "Turn",
            "end_of_turn": True,
            "transcript": "hello world",
            "words": [
                {"text": "hello", "confidence": 0.9},
                {"text": "world", "confidence": 0.95},
            ],
        }
    )

    # Drain transcripts with a timeout to avoid hanging the test.
    collected: list[Transcript] = []
    agen = stt.receive_transcripts()

    async def drain() -> None:
        async for t in agen:
            collected.append(t)
            if t.is_final:
                break

    await asyncio.wait_for(drain(), timeout=2.0)

    assert len(collected) == 2
    assert collected[0].text == "hello"
    assert collected[0].is_final is False
    assert collected[1].text == "hello world"
    assert collected[1].is_final is True

    assert stt.session_id == "s"

    await stt.close()
    # Terminate message should have been sent.
    assert any('"Terminate"' in s for s in fake_ws.sent_strs)
    assert fake_ws.closed is True


@pytest.mark.unit
async def test_send_audio_raises_when_not_connected() -> None:
    stt = AssemblyAISTT(api_key="key")
    with pytest.raises(RuntimeError, match="connect"):
        await stt.send_audio(b"\x00")


@pytest.mark.unit
async def test_close_is_idempotent_when_never_connected() -> None:
    stt = AssemblyAISTT(api_key="key")
    # Should not raise.
    await stt.close()
