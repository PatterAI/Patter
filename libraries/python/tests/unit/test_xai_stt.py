"""Unit tests for the xAI STT provider (streaming adapter + batch helper).

Only the xAI network boundary is mocked; URL/query construction, the
``transcript.created`` handshake, event mapping, ``finalize()``, and multipart
field ordering all run against real code. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import aiohttp
import pytest

from getpatter.providers.xai_stt import (
    XAI_STT_REST_URL,
    XaiSTT,
    XaiTranscription,
    _build_transcribe_fields,
    transcribe,
)

# ---------------------------------------------------------------------------
# aiohttp WebSocket fakes — only the network boundary is mocked.
# ---------------------------------------------------------------------------


class _FakeMsg:
    """Minimal stand-in for an ``aiohttp.WSMessage`` (type + data)."""

    def __init__(self, type_: Any, data: str) -> None:
        self.type = type_
        self.data = data


def _text(payload: dict) -> _FakeMsg:
    return _FakeMsg(aiohttp.WSMsgType.TEXT, json.dumps(payload))


class _FakeWebSocket:
    """Fake aiohttp ws: records sends, replays a scripted incoming stream."""

    def __init__(self, incoming: list[_FakeMsg]) -> None:
        self.closed = False
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self._incoming = list(incoming)

    async def send_str(self, payload: str) -> None:
        self.sent_text.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.sent_bytes.append(payload)

    async def close(self) -> None:
        self.closed = True

    def exception(self) -> None:  # pragma: no cover - only on ERROR frames
        return None

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> _FakeMsg:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


class _FakeSession:
    """Stand-in for ``aiohttp.ClientSession`` returning a fixed fake ws."""

    def __init__(self, ws: _FakeWebSocket) -> None:
        self._ws = ws
        self.closed = False
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None

    async def ws_connect(
        self, url: str, *, headers: dict[str, str] | None = None, **_kw: Any
    ) -> _FakeWebSocket:
        self.last_url = url
        self.last_headers = headers
        return self._ws

    async def close(self) -> None:
        self.closed = True


async def _connect(stt: XaiSTT, ws: _FakeWebSocket) -> _FakeSession:
    """Connect ``stt`` against a mocked ClientSession and return the session."""
    fake_session = _FakeSession(ws)
    with patch(
        "getpatter.providers.xai_stt.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        await stt.connect()
    return fake_session


# ---------------------------------------------------------------------------
# URL / query construction
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestQueryConstruction:
    async def test_default_query_is_pcm_16k_with_interim(self) -> None:
        ws = _FakeWebSocket([_text({"type": "transcript.created"})])
        session = await _connect(XaiSTT(api_key="xai-test-key"), ws)
        assert session.last_url is not None
        assert session.last_headers == {"Authorization": "Bearer xai-test-key"}
        params = dict(parse_qsl(urlsplit(session.last_url).query))
        assert params["encoding"] == "pcm"
        assert params["sample_rate"] == "16000"
        assert params["interim_results"] == "true"

    async def test_keyterms_are_repeated_query_params(self) -> None:
        ws = _FakeWebSocket([_text({"type": "transcript.created"})])
        stt = XaiSTT(
            api_key="xai-test-key",
            language="en",
            keyterms=("Grok", "xAI"),
            smart_turn=0.7,
            smart_turn_timeout_ms=2000,
            diarize=True,
        )
        session = await _connect(stt, ws)
        pairs = parse_qsl(urlsplit(session.last_url).query)
        keyterms = [v for k, v in pairs if k == "keyterm"]
        assert keyterms == ["Grok", "xAI"]
        flat = dict(pairs)
        assert flat["language"] == "en"
        assert flat["smart_turn"] == "0.7"
        assert flat["smart_turn_timeout"] == "2000"
        assert flat["diarize"] == "true"


# ---------------------------------------------------------------------------
# Handshake ordering
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestHandshake:
    async def test_connect_waits_for_transcript_created(self) -> None:
        ws = _FakeWebSocket([_text({"type": "transcript.created"})])
        stt = XaiSTT(api_key="xai-test-key")
        await _connect(stt, ws)
        # No setup message is sent — config is URL-only.
        assert ws.sent_text == []

    async def test_send_audio_before_connect_raises(self) -> None:
        stt = XaiSTT(api_key="xai-test-key")
        with pytest.raises(RuntimeError):
            await stt.send_audio(b"\x00\x00")

    async def test_close_before_created_raises(self) -> None:
        ws = _FakeWebSocket([])  # stream ends before the handshake
        stt = XaiSTT(api_key="xai-test-key")
        with pytest.raises(RuntimeError):
            await _connect(stt, ws)

    async def test_error_during_handshake_raises(self) -> None:
        ws = _FakeWebSocket([_text({"type": "error", "message": "bad key"})])
        stt = XaiSTT(api_key="xai-test-key")
        with pytest.raises(RuntimeError, match="bad key"):
            await _connect(stt, ws)


# ---------------------------------------------------------------------------
# Event mapping (is_final / speech_final) + finalize
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestTranscriptMapping:
    async def test_maps_interim_chunk_final_and_utterance_end(self) -> None:
        ws = _FakeWebSocket(
            [
                _text({"type": "transcript.created"}),
                _text({"type": "transcript.partial", "text": "hel", "is_final": False}),
                _text(
                    {
                        "type": "transcript.partial",
                        "text": "hello",
                        "is_final": True,
                        "speech_final": False,
                    }
                ),
                _text(
                    {
                        "type": "transcript.partial",
                        "text": "hello world",
                        "is_final": True,
                        "speech_final": True,
                    }
                ),
            ]
        )
        stt = XaiSTT(api_key="xai-test-key")
        await _connect(stt, ws)
        results = [t async for t in stt.receive_transcripts()]
        assert [(t.text, t.is_final, t.speech_final) for t in results] == [
            ("hel", False, False),
            ("hello", True, False),
            ("hello world", True, True),
        ]

    async def test_transcript_done_flushes_a_final(self) -> None:
        ws = _FakeWebSocket(
            [
                _text({"type": "transcript.created"}),
                _text({"type": "transcript.done", "text": "final text"}),
            ]
        )
        stt = XaiSTT(api_key="xai-test-key")
        await _connect(stt, ws)
        results = [t async for t in stt.receive_transcripts()]
        assert len(results) == 1
        assert results[0].text == "final text"
        assert results[0].is_final is True
        assert results[0].speech_final is True

    async def test_error_event_does_not_close_stream(self) -> None:
        ws = _FakeWebSocket(
            [
                _text({"type": "transcript.created"}),
                _text({"type": "error", "message": "transient"}),
                _text(
                    {
                        "type": "transcript.partial",
                        "text": "after error",
                        "is_final": True,
                        "speech_final": True,
                    }
                ),
            ]
        )
        stt = XaiSTT(api_key="xai-test-key")
        await _connect(stt, ws)
        results = [t async for t in stt.receive_transcripts()]
        assert [t.text for t in results] == ["after error"]

    async def test_finalize_sends_finalize_frame(self) -> None:
        ws = _FakeWebSocket([_text({"type": "transcript.created"})])
        stt = XaiSTT(api_key="xai-test-key")
        await _connect(stt, ws)
        await stt.finalize()
        assert json.loads(ws.sent_text[-1]) == {"type": "finalize"}

    async def test_close_sends_audio_done(self) -> None:
        ws = _FakeWebSocket([_text({"type": "transcript.created"})])
        stt = XaiSTT(api_key="xai-test-key")
        await _connect(stt, ws)
        await stt.close()
        assert json.loads(ws.sent_text[-1]) == {"type": "audio.done"}
        assert ws.closed is True


# ---------------------------------------------------------------------------
# Batch transcription helper
# ---------------------------------------------------------------------------


class TestBatchFieldOrdering:
    @pytest.mark.unit
    def test_file_is_the_last_multipart_field(self) -> None:
        fields = _build_transcribe_fields(
            audio=b"RIFFDATA",
            url=None,
            language="en",
            format=True,
            diarize=True,
            keyterms=("Grok", "xAI"),
            filler_words=True,
            audio_format="pcm",
            sample_rate=16000,
            filename="clip.wav",
        )
        names = [name for name, _v, _extra in fields]
        assert names[-1] == "file"
        assert names.index("file") == len(names) - 1
        # All scalar fields precede the file; keyterm repeats once per term.
        assert names[:-1] == [
            "language",
            "format",
            "diarize",
            "filler_words",
            "audio_format",
            "sample_rate",
            "keyterm",
            "keyterm",
        ]

    @pytest.mark.unit
    def test_url_used_instead_of_file_when_given(self) -> None:
        fields = _build_transcribe_fields(
            audio=None,
            url="https://example.com/audio.wav",
            language=None,
            format=False,
            diarize=False,
            keyterms=(),
            filler_words=False,
            audio_format=None,
            sample_rate=None,
            filename="audio.wav",
        )
        assert [name for name, _v, _e in fields] == ["url"]


# --- REST response parsing (network boundary mocked) ---


class _FakeRestResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def json(self) -> dict:
        return self._payload

    async def text(self) -> str:  # pragma: no cover - only on non-200
        return json.dumps(self._payload)

    async def __aenter__(self) -> "_FakeRestResponse":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeRestSession:
    def __init__(self, response: _FakeRestResponse) -> None:
        self.response = response
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_data: Any = None

    def post(
        self, url: str, *, headers: dict[str, str], data: Any, timeout: Any = None
    ) -> _FakeRestResponse:
        self.last_url = url
        self.last_headers = headers
        self.last_data = data
        return self.response

    async def __aenter__(self) -> "_FakeRestSession":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


@pytest.mark.mocked
class TestBatchTranscribe:
    async def test_parses_response_into_dataclass(self) -> None:
        payload = {
            "text": "The balance is $167,983.15.",
            "language": "English",
            "duration": 3.45,
            "words": [
                {"text": "The", "start": 0.24, "end": 0.48},
                {"text": "balance", "start": 0.48, "end": 0.9, "speaker": 1},
            ],
        }
        fake = _FakeRestSession(_FakeRestResponse(200, payload))
        with patch(
            "getpatter.providers.xai_stt.aiohttp.ClientSession", return_value=fake
        ):
            result = await transcribe(b"RIFFDATA", api_key="xai-test-key", diarize=True)
        assert isinstance(result, XaiTranscription)
        assert result.text == "The balance is $167,983.15."
        assert result.language == "English"
        assert result.duration == 3.45
        assert len(result.words) == 2
        assert result.words[0].text == "The"
        assert result.words[1].speaker == 1
        assert fake.last_url == XAI_STT_REST_URL
        assert fake.last_headers == {"Authorization": "Bearer xai-test-key"}

    async def test_requires_exactly_one_of_audio_or_url(self) -> None:
        with pytest.raises(ValueError):
            await transcribe(api_key="xai-test-key")
        with pytest.raises(ValueError):
            await transcribe(b"x", url="https://x/y.wav", api_key="xai-test-key")
