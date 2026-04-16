"""Unit tests for provider adapters — mocked WebSocket and HTTP I/O.

Tests cover the I/O paths of:
- OpenAIRealtimeAdapter (connect, send_audio, receive_events, send_text, etc.)
- ElevenLabsConvAIAdapter (connect, send_audio, receive_events)
- WhisperSTT (connect, send_audio, _transcribe_buffer, receive_transcripts, close)
- DeepgramSTT (connect, send_audio, receive_transcripts, close)
- ElevenLabsTTS (construction, repr)
- OpenAITTS (resample, close)
- TelnyxAdapter (provision_number, configure_number, initiate_call, end_call)
- Transcoding (mulaw/pcm, resample)
- handlers/common (_create_stt_from_config, _create_tts_from_config)
"""

from __future__ import annotations

import base64
import json
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncIterableWS:
    """A minimal async-iterable mock WebSocket for ``async for raw in ws:``."""

    def __init__(self, messages: list):
        self._messages = messages
        self.send = AsyncMock()
        self.recv = AsyncMock()
        self.close = AsyncMock()

    def __aiter__(self):
        return _AsyncIterHelper(list(self._messages))


class _AsyncIterHelper:
    def __init__(self, messages: list):
        self._msgs = messages
        self._idx = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._msgs):
            raise StopAsyncIteration
        msg = self._msgs[self._idx]
        self._idx += 1
        return msg


class _ConnectionClosedWS:
    """A mock WebSocket that raises ConnectionClosed on iteration."""

    def __init__(self):
        self.send = AsyncMock()
        self.recv = AsyncMock()
        self.close = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise ConnectionClosed(None, None)


async def _fake_ws_connect(mock_ws):
    return mock_ws


def _ws_connect_side_effect(mock_ws):
    async def _connect(*a, **kw):
        return mock_ws
    return _connect


# ---------------------------------------------------------------------------
# OpenAIRealtimeAdapter — I/O
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenAIRealtimeAdapterIO:
    """OpenAIRealtimeAdapter with mocked WebSocket for I/O paths."""

    @pytest.mark.asyncio
    async def test_connect_sends_session_update(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test", instructions="Be helpful.")
        mock_ws = AsyncMock()
        mock_ws.recv.return_value = json.dumps({"type": "session.created"})

        with patch("patter.providers.openai_realtime.websockets.connect", side_effect=_ws_connect_side_effect(mock_ws)):
            await adapter.connect()

        assert adapter._running is True
        assert adapter._ws is mock_ws
        mock_ws.send.assert_called_once()
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["type"] == "session.update"
        assert sent["session"]["voice"] == "alloy"
        assert sent["session"]["instructions"] == "Be helpful."

    @pytest.mark.asyncio
    async def test_connect_with_tools(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        tools = [{"name": "search", "description": "Search", "parameters": {"type": "object"}}]
        adapter = OpenAIRealtimeAdapter(api_key="sk-test", tools=tools)
        mock_ws = AsyncMock()
        mock_ws.recv.return_value = json.dumps({"type": "session.created"})

        with patch("patter.providers.openai_realtime.websockets.connect", side_effect=_ws_connect_side_effect(mock_ws)):
            await adapter.connect()

        sent = json.loads(mock_ws.send.call_args[0][0])
        assert "tools" in sent["session"]
        assert sent["session"]["tools"][0]["name"] == "search"

    @pytest.mark.asyncio
    async def test_connect_default_instructions(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test", instructions="", language="fr")
        mock_ws = AsyncMock()
        mock_ws.recv.return_value = json.dumps({"type": "session.created"})

        with patch("patter.providers.openai_realtime.websockets.connect", side_effect=_ws_connect_side_effect(mock_ws)):
            await adapter.connect()

        sent = json.loads(mock_ws.send.call_args[0][0])
        assert "fr" in sent["session"]["instructions"]

    @pytest.mark.asyncio
    async def test_connect_raises_on_unexpected_first_message(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        mock_ws = AsyncMock()
        mock_ws.recv.return_value = json.dumps({"type": "error"})

        with patch("patter.providers.openai_realtime.websockets.connect", side_effect=_ws_connect_side_effect(mock_ws)):
            with pytest.raises(RuntimeError, match="Expected session.created"):
                await adapter.connect()

    @pytest.mark.asyncio
    async def test_send_audio_encodes_base64(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        adapter._ws = AsyncMock()

        audio = b"\x00\x01\x02\x03"
        await adapter.send_audio(audio)

        adapter._ws.send.assert_called_once()
        sent = json.loads(adapter._ws.send.call_args[0][0])
        assert sent["type"] == "input_audio_buffer.append"
        assert base64.b64decode(sent["audio"]) == audio

    @pytest.mark.asyncio
    async def test_cancel_response_sends_cancel(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        adapter._ws = AsyncMock()
        await adapter.cancel_response()
        sent = json.loads(adapter._ws.send.call_args[0][0])
        assert sent["type"] == "response.cancel"

    @pytest.mark.asyncio
    async def test_send_text_creates_item_and_triggers_response(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        adapter._ws = AsyncMock()
        await adapter.send_text("hello")

        assert adapter._ws.send.call_count == 2
        first = json.loads(adapter._ws.send.call_args_list[0][0][0])
        assert first["type"] == "conversation.item.create"
        assert first["item"]["content"][0]["text"] == "hello"
        second = json.loads(adapter._ws.send.call_args_list[1][0][0])
        assert second["type"] == "response.create"

    @pytest.mark.asyncio
    async def test_send_function_result(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        adapter._ws = AsyncMock()
        await adapter.send_function_result("call_123", '{"result": "ok"}')

        assert adapter._ws.send.call_count == 2
        first = json.loads(adapter._ws.send.call_args_list[0][0][0])
        assert first["type"] == "conversation.item.create"
        assert first["item"]["type"] == "function_call_output"
        assert first["item"]["call_id"] == "call_123"

    @pytest.mark.asyncio
    async def test_receive_events_yields_audio(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        audio_bytes = b"\xaa\xbb\xcc"
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        messages = [json.dumps({"type": "response.audio.delta", "delta": encoded})]
        adapter._ws = _AsyncIterableWS(messages)

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert events[0] == ("audio", audio_bytes)

    @pytest.mark.asyncio
    async def test_receive_events_yields_transcript_output(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        messages = [json.dumps({"type": "response.audio_transcript.delta", "delta": "Hello"})]
        adapter._ws = _AsyncIterableWS(messages)

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert events[0] == ("transcript_output", "Hello")

    @pytest.mark.asyncio
    async def test_receive_events_yields_transcript_input(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        messages = [json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Hi"})]
        adapter._ws = _AsyncIterableWS(messages)

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert events[0] == ("transcript_input", "Hi")

    @pytest.mark.asyncio
    async def test_receive_events_yields_speech_events(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        messages = [
            json.dumps({"type": "input_audio_buffer.speech_started"}),
            json.dumps({"type": "input_audio_buffer.speech_stopped"}),
        ]
        adapter._ws = _AsyncIterableWS(messages)

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert events[0] == ("speech_started", None)
        assert events[1] == ("speech_stopped", None)

    @pytest.mark.asyncio
    async def test_receive_events_yields_function_call(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        messages = [json.dumps({
            "type": "response.function_call_arguments.done",
            "call_id": "fc1", "name": "search", "arguments": '{"q":"test"}',
        })]
        adapter._ws = _AsyncIterableWS(messages)

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert events[0][0] == "function_call"
        assert events[0][1]["call_id"] == "fc1"

    @pytest.mark.asyncio
    async def test_receive_events_yields_response_done(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        messages = [json.dumps({"type": "response.done", "response": {"id": "r1"}})]
        adapter._ws = _AsyncIterableWS(messages)

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert events[0][0] == "response_done"

    @pytest.mark.asyncio
    async def test_receive_events_handles_error_event(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        messages = [json.dumps({"type": "error", "error": {"message": "bad"}})]
        adapter._ws = _AsyncIterableWS(messages)

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_receive_events_handles_connection_closed(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        adapter._running = True
        adapter._ws = _ConnectionClosedWS()

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert adapter._running is False

    @pytest.mark.asyncio
    async def test_receive_events_noop_when_no_ws(self) -> None:
        from patter.providers.openai_realtime import OpenAIRealtimeAdapter

        adapter = OpenAIRealtimeAdapter(api_key="sk-test")
        adapter._ws = None
        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert len(events) == 0


# ---------------------------------------------------------------------------
# ElevenLabsConvAIAdapter — I/O
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestElevenLabsConvAIAdapterIO:
    """ElevenLabsConvAIAdapter with mocked WebSocket for I/O paths."""

    @pytest.mark.asyncio
    async def test_connect_without_agent_id(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test")
        mock_ws = AsyncMock()

        with patch("patter.providers.elevenlabs_convai.websockets.connect", side_effect=_ws_connect_side_effect(mock_ws)) as mc:
            await adapter.connect()

        assert adapter._running is True
        call_url = mc.call_args[0][0]
        assert "agent_id=" not in call_url
        mock_ws.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_with_agent_id(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test", agent_id="agent_xyz")
        mock_ws = AsyncMock()

        with patch("patter.providers.elevenlabs_convai.websockets.connect", side_effect=_ws_connect_side_effect(mock_ws)) as mc:
            await adapter.connect()

        call_url = mc.call_args[0][0]
        assert "agent_id=agent_xyz" in call_url

    @pytest.mark.asyncio
    async def test_connect_with_first_message(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test", first_message="Hi there!")
        mock_ws = AsyncMock()

        with patch("patter.providers.elevenlabs_convai.websockets.connect", side_effect=_ws_connect_side_effect(mock_ws)):
            await adapter.connect()

        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["conversation_config_override"]["agent"]["first_message"] == "Hi there!"

    @pytest.mark.asyncio
    async def test_connect_without_first_message(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test", first_message="")
        mock_ws = AsyncMock()

        with patch("patter.providers.elevenlabs_convai.websockets.connect", side_effect=_ws_connect_side_effect(mock_ws)):
            await adapter.connect()

        sent = json.loads(mock_ws.send.call_args[0][0])
        assert "agent" not in sent.get("conversation_config_override", {})

    @pytest.mark.asyncio
    async def test_send_audio_encodes_base64(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test")
        adapter._ws = AsyncMock()

        audio = b"\xaa\xbb\xcc"
        await adapter.send_audio(audio)
        sent = json.loads(adapter._ws.send.call_args[0][0])
        assert sent["type"] == "audio"
        assert base64.b64decode(sent["audio"]) == audio

    @pytest.mark.asyncio
    async def test_receive_events_yields_audio(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test")
        audio_bytes = b"\xdd\xee"
        encoded = base64.b64encode(audio_bytes).decode("ascii")
        adapter._ws = _AsyncIterableWS([json.dumps({"type": "audio", "audio": encoded})])

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert events[0] == ("audio", audio_bytes)

    @pytest.mark.asyncio
    async def test_receive_events_yields_transcripts(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test")
        adapter._ws = _AsyncIterableWS([
            json.dumps({"type": "user_transcript", "text": "Hi"}),
            json.dumps({"type": "agent_response", "text": "Hello"}),
            json.dumps({"type": "interruption"}),
        ])

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert events[0] == ("transcript_input", "Hi")
        assert events[1] == ("transcript_output", "Hello")
        assert events[2] == ("response_done", {})
        assert events[3] == ("interruption", None)

    @pytest.mark.asyncio
    async def test_receive_events_empty_audio_skipped(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test")
        adapter._ws = _AsyncIterableWS([json.dumps({"type": "audio", "audio": ""})])

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_receive_events_error_event_logged(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test")
        adapter._ws = _AsyncIterableWS([json.dumps({"type": "error", "message": "bad"})])

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_receive_events_handles_connection_closed(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test")
        adapter._running = True
        adapter._ws = _ConnectionClosedWS()

        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert adapter._running is False

    @pytest.mark.asyncio
    async def test_receive_events_noop_when_no_ws(self) -> None:
        from patter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

        adapter = ElevenLabsConvAIAdapter(api_key="el-test")
        adapter._ws = None
        events = []
        async for event in adapter.receive_events():
            events.append(event)
        assert len(events) == 0


# ---------------------------------------------------------------------------
# WhisperSTT — full coverage
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWhisperSTT:
    """WhisperSTT construction, buffering, transcription, and lifecycle."""

    def test_construction(self) -> None:
        from patter.providers.whisper_stt import WhisperSTT

        stt = WhisperSTT(api_key="sk-test", language="en", model="whisper-1")
        assert stt.api_key == "sk-test"
        assert stt.language == "en"
        assert stt.model == "whisper-1"
        assert stt._running is False

    @pytest.mark.asyncio
    async def test_connect_resets_state(self) -> None:
        from patter.providers.whisper_stt import WhisperSTT

        stt = WhisperSTT(api_key="sk-test")
        stt._buffer = bytearray(b"\x01\x02\x03")
        await stt.connect()
        assert stt._running is True
        assert len(stt._buffer) == 0

    @pytest.mark.asyncio
    async def test_send_audio_buffers_small_chunks(self) -> None:
        from patter.providers.whisper_stt import WhisperSTT

        stt = WhisperSTT(api_key="sk-test")
        stt._running = True
        await stt.send_audio(b"\x00" * 100)
        assert len(stt._buffer) == 100

    @pytest.mark.asyncio
    async def test_send_audio_transcribes_when_buffer_full(self) -> None:
        from patter.providers.whisper_stt import BUFFER_SIZE_BYTES, WhisperSTT

        stt = WhisperSTT(api_key="sk-test")
        stt._running = True

        mock_response = MagicMock()
        mock_response.json.return_value = {"text": "Hello world"}
        mock_response.raise_for_status = MagicMock()
        stt._client = AsyncMock()
        stt._client.post.return_value = mock_response

        chunk = b"\x00\x00" * BUFFER_SIZE_BYTES
        await stt.send_audio(chunk)
        stt._client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_transcribe_buffer_returns_transcript(self) -> None:
        from patter.providers.whisper_stt import WhisperSTT

        stt = WhisperSTT(api_key="sk-test")
        mock_response = MagicMock()
        mock_response.json.return_value = {"text": "Test transcript"}
        mock_response.raise_for_status = MagicMock()
        stt._client = AsyncMock()
        stt._client.post.return_value = mock_response

        result = await stt._transcribe_buffer(b"\x00\x00" * 8000)
        assert result is not None
        assert result.text == "Test transcript"
        assert result.is_final is True
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_transcribe_buffer_returns_none_for_empty_text(self) -> None:
        from patter.providers.whisper_stt import WhisperSTT

        stt = WhisperSTT(api_key="sk-test")
        mock_response = MagicMock()
        mock_response.json.return_value = {"text": "   "}
        mock_response.raise_for_status = MagicMock()
        stt._client = AsyncMock()
        stt._client.post.return_value = mock_response

        result = await stt._transcribe_buffer(b"\x00\x00" * 8000)
        assert result is None

    @pytest.mark.asyncio
    async def test_transcribe_buffer_returns_none_on_error(self) -> None:
        from patter.providers.whisper_stt import WhisperSTT

        stt = WhisperSTT(api_key="sk-test")
        stt._client = AsyncMock()
        stt._client.post.side_effect = Exception("API error")

        result = await stt._transcribe_buffer(b"\x00\x00" * 8000)
        assert result is None

    @pytest.mark.asyncio
    async def test_close_flushes_large_buffer(self) -> None:
        from patter.providers.whisper_stt import BUFFER_SIZE_BYTES, WhisperSTT

        stt = WhisperSTT(api_key="sk-test")
        stt._running = True
        mock_response = MagicMock()
        mock_response.json.return_value = {"text": "Final words"}
        mock_response.raise_for_status = MagicMock()
        stt._client = AsyncMock()
        stt._client.post.return_value = mock_response

        stt._buffer = bytearray(b"\x00\x00" * (BUFFER_SIZE_BYTES // 4 + 100))
        await stt.close()

        assert stt._running is False
        assert len(stt._buffer) == 0
        stt._client.post.assert_called_once()
        stt._client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_skips_small_buffer(self) -> None:
        from patter.providers.whisper_stt import WhisperSTT

        stt = WhisperSTT(api_key="sk-test")
        stt._running = True
        stt._client = AsyncMock()
        stt._buffer = bytearray(b"\x00" * 10)
        await stt.close()

        assert stt._running is False
        stt._client.post.assert_not_called()
        stt._client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_receive_transcripts_yields_from_queue(self) -> None:
        from patter.providers.whisper_stt import WhisperSTT, _Transcript

        stt = WhisperSTT(api_key="sk-test")
        stt._running = True

        transcript = _Transcript(text="Hello")
        await stt._transcript_queue.put(transcript)

        results = []
        async for t in stt.receive_transcripts():
            results.append(t)
            stt._running = False

        assert len(results) == 1
        assert results[0].text == "Hello"

    def test_transcript_dataclass(self) -> None:
        from patter.providers.whisper_stt import _Transcript

        t = _Transcript(text="hello")
        assert t.text == "hello"
        assert t.is_final is True
        assert t.confidence == 1.0


# ---------------------------------------------------------------------------
# DeepgramSTT — I/O
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeepgramSTTIO:
    """DeepgramSTT connect, send_audio, receive_transcripts, close."""

    @pytest.mark.asyncio
    async def test_connect(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        mock_ws = AsyncMock()

        with patch("patter.providers.deepgram_stt.websockets.connect", side_effect=_ws_connect_side_effect(mock_ws)) as mc:
            await stt.connect()

        assert stt._ws is mock_ws
        call_url = mc.call_args[0][0]
        assert "model=nova-3" in call_url

    @pytest.mark.asyncio
    async def test_send_audio(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        mock_ws = AsyncMock()
        stt._ws = mock_ws

        audio = b"\x00\x01\x02\x03"
        await stt.send_audio(audio)
        mock_ws.send.assert_called_once_with(audio)

    @pytest.mark.asyncio
    async def test_send_audio_raises_when_not_connected(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        with pytest.raises(RuntimeError, match="Not connected"):
            await stt.send_audio(b"\x00")

    @pytest.mark.asyncio
    async def test_receive_transcripts_yields_results(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        messages = [json.dumps({
            "type": "Results",
            "is_final": True,
            "speech_final": True,
            "channel": {"alternatives": [{"transcript": "Hello", "confidence": 0.9}]},
        })]
        stt._ws = _AsyncIterableWS(messages)

        transcripts = []
        async for t in stt.receive_transcripts():
            transcripts.append(t)
        assert len(transcripts) == 1
        assert transcripts[0].text == "Hello"

    @pytest.mark.asyncio
    async def test_receive_transcripts_skips_binary_frames(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        stt._ws = _AsyncIterableWS([b"\x00\x01"])

        transcripts = []
        async for t in stt.receive_transcripts():
            transcripts.append(t)
        assert len(transcripts) == 0

    @pytest.mark.asyncio
    async def test_receive_transcripts_raises_when_not_connected(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        with pytest.raises(RuntimeError, match="Not connected"):
            async for _ in stt.receive_transcripts():
                pass

    def test_parse_message_metadata(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        raw = json.dumps({"type": "Metadata", "request_id": "req-123"})
        result = stt._parse_message(raw)
        assert result is None
        assert stt.request_id == "req-123"

    @pytest.mark.asyncio
    async def test_close_sends_close_stream(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        mock_ws = AsyncMock()
        stt._ws = mock_ws

        await stt.close()

        mock_ws.send.assert_called_once()
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["type"] == "CloseStream"
        mock_ws.close.assert_called_once()
        assert stt._ws is None

    @pytest.mark.asyncio
    async def test_close_handles_send_error(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        mock_ws = AsyncMock()
        mock_ws.send.side_effect = Exception("ws closed")
        stt._ws = mock_ws

        await stt.close()
        mock_ws.close.assert_called_once()
        assert stt._ws is None

    @pytest.mark.asyncio
    async def test_close_noop_when_no_ws(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test")
        stt._ws = None
        await stt.close()

    def test_for_twilio(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT.for_twilio(api_key="dg-test", language="es")
        assert stt.encoding == "mulaw"
        assert stt.sample_rate == 8000

    def test_repr(self) -> None:
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="dg-test", model="nova-2", language="fr")
        r = repr(stt)
        assert "nova-2" in r
        assert "fr" in r


# ---------------------------------------------------------------------------
# OpenAITTS — resample
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenAITTSResample:
    """OpenAITTS resample logic."""

    def test_resample_24k_to_16k_basic(self) -> None:
        from patter.providers.openai_tts import OpenAITTS

        samples = [100, 200, 300, 400, 500, 600]
        audio = struct.pack(f"<{len(samples)}h", *samples)
        result = OpenAITTS._resample_24k_to_16k(audio)
        out_samples = struct.unpack(f"<{len(result)//2}h", result)
        assert len(out_samples) == 4

    def test_resample_24k_to_16k_empty(self) -> None:
        from patter.providers.openai_tts import OpenAITTS

        assert OpenAITTS._resample_24k_to_16k(b"") == b""

    def test_resample_24k_to_16k_single_byte(self) -> None:
        from patter.providers.openai_tts import OpenAITTS

        assert OpenAITTS._resample_24k_to_16k(b"\x00") == b"\x00"

    def test_resample_24k_to_16k_partial_group(self) -> None:
        from patter.providers.openai_tts import OpenAITTS

        samples = [100, 200, 300, 400, 500]
        audio = struct.pack(f"<{len(samples)}h", *samples)
        result = OpenAITTS._resample_24k_to_16k(audio)
        assert len(result) > 0

    def test_repr(self) -> None:
        from patter.providers.openai_tts import OpenAITTS

        tts = OpenAITTS(api_key="x", voice="shimmer", model="tts-1-hd")
        r = repr(tts)
        assert "shimmer" in r
        assert "tts-1-hd" in r

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        from patter.providers.openai_tts import OpenAITTS

        tts = OpenAITTS(api_key="sk-test")
        tts._client = AsyncMock()
        await tts.close()
        tts._client.aclose.assert_called_once()


# ---------------------------------------------------------------------------
# ElevenLabsTTS
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestElevenLabsTTSIO:
    """ElevenLabsTTS construction and repr."""

    def test_repr(self) -> None:
        from patter.providers.elevenlabs_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="x", voice_id="v1", model_id="m1")
        r = repr(tts)
        assert "m1" in r
        assert "v1" in r

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        from patter.providers.elevenlabs_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="el-test")
        tts._client = AsyncMock()
        await tts.close()
        tts._client.aclose.assert_called_once()


# ---------------------------------------------------------------------------
# TelnyxAdapter — I/O
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTelnyxAdapterIO:
    """TelnyxAdapter mocked HTTP calls."""

    @pytest.mark.asyncio
    async def test_configure_number(self) -> None:
        from patter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key_test", connection_id="conn_123")
        adapter._client = AsyncMock()
        await adapter.configure_number("+15551234567", "https://example.com/webhook")
        adapter._client.patch.assert_called_once()

    @pytest.mark.asyncio
    async def test_end_call(self) -> None:
        from patter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key_test")
        adapter._client = AsyncMock()
        await adapter.end_call("v3:test-id")
        adapter._client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_initiate_call(self) -> None:
        from patter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key_test", connection_id="conn_123")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"call_control_id": "v3:new-id"}}
        mock_resp.raise_for_status = MagicMock()
        adapter._client = AsyncMock()
        adapter._client.post.return_value = mock_resp

        call_id = await adapter.initiate_call("+15551111111", "+15552222222", "wss://stream.example.com")
        assert call_id == "v3:new-id"

    @pytest.mark.asyncio
    async def test_provision_number(self) -> None:
        from patter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key_test")
        list_resp = MagicMock()
        list_resp.json.return_value = {"data": [{"phone_number": "+15559999999"}]}
        list_resp.raise_for_status = MagicMock()
        buy_resp = MagicMock()
        buy_resp.raise_for_status = MagicMock()
        adapter._client = AsyncMock()
        adapter._client.get.return_value = list_resp
        adapter._client.post.return_value = buy_resp

        number = await adapter.provision_number("US")
        assert number == "+15559999999"

    @pytest.mark.asyncio
    async def test_provision_number_no_numbers(self) -> None:
        from patter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key_test")
        list_resp = MagicMock()
        list_resp.json.return_value = {"data": []}
        list_resp.raise_for_status = MagicMock()
        adapter._client = AsyncMock()
        adapter._client.get.return_value = list_resp

        with pytest.raises(ValueError, match="No numbers"):
            await adapter.provision_number("US")

    @pytest.mark.asyncio
    async def test_close(self) -> None:
        from patter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="key_test")
        adapter._client = AsyncMock()
        await adapter.close()
        adapter._client.aclose.assert_called_once()


# ---------------------------------------------------------------------------
# Transcoding — full coverage
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTranscoding:
    """Test transcoding functions."""

    def test_mulaw_to_pcm16(self) -> None:
        from patter.services.transcoding import mulaw_to_pcm16

        pcm = mulaw_to_pcm16(b"\xff" * 160)
        assert len(pcm) > 0

    def test_pcm16_to_mulaw(self) -> None:
        from patter.services.transcoding import pcm16_to_mulaw

        pcm = struct.pack("<4h", 0, 1000, -1000, 500)
        mulaw = pcm16_to_mulaw(pcm)
        assert len(mulaw) > 0

    def test_mulaw_pcm_roundtrip(self) -> None:
        from patter.services.transcoding import mulaw_to_pcm16, pcm16_to_mulaw

        pcm = struct.pack("<4h", 0, 1000, -1000, 500)
        pcm_back = mulaw_to_pcm16(pcm16_to_mulaw(pcm))
        assert len(pcm_back) > 0

    def test_resample_8k_to_16k(self) -> None:
        from patter.services.transcoding import resample_8k_to_16k

        pcm = struct.pack("<4h", 100, 200, 300, 400)
        result = resample_8k_to_16k(pcm)
        assert len(result) >= len(pcm)

    def test_resample_8k_to_16k_empty(self) -> None:
        from patter.services.transcoding import resample_8k_to_16k

        assert resample_8k_to_16k(b"") == b""

    def test_resample_16k_to_8k(self) -> None:
        from patter.services.transcoding import resample_16k_to_8k

        pcm = struct.pack("<8h", 100, 200, 300, 400, 500, 600, 700, 800)
        result = resample_16k_to_8k(pcm)
        assert len(result) <= len(pcm)

    def test_resample_16k_to_8k_empty(self) -> None:
        from patter.services.transcoding import resample_16k_to_8k

        assert resample_16k_to_8k(b"") == b""


# ---------------------------------------------------------------------------
# handlers/common — _create_stt/tts_from_config
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateSTTFromConfig:
    """_create_stt_from_config creates the right adapter."""

    def test_deepgram_default(self) -> None:
        from patter.handlers.common import _create_stt_from_config
        from patter.models import STTConfig

        config = STTConfig(provider="deepgram", api_key="dg-key", language="en")
        stt = _create_stt_from_config(config)
        assert stt is not None
        assert stt.__class__.__name__ == "DeepgramSTT"
        assert stt.encoding == "linear16"

    def test_deepgram_for_twilio(self) -> None:
        from patter.handlers.common import _create_stt_from_config
        from patter.models import STTConfig

        config = STTConfig(provider="deepgram", api_key="dg-key", language="es")
        stt = _create_stt_from_config(config, for_twilio=True)
        assert stt is not None
        assert stt.encoding == "mulaw"
        assert stt.sample_rate == 8000

    def test_whisper(self) -> None:
        from patter.handlers.common import _create_stt_from_config
        from patter.models import STTConfig

        config = STTConfig(provider="whisper", api_key="sk-key", language="fr")
        stt = _create_stt_from_config(config)
        assert stt is not None
        assert stt.__class__.__name__ == "WhisperSTT"

    def test_unknown_provider(self) -> None:
        from patter.handlers.common import _create_stt_from_config
        from patter.models import STTConfig

        config = STTConfig(provider="unknown", api_key="x")
        assert _create_stt_from_config(config) is None


@pytest.mark.unit
class TestCreateTTSFromConfig:
    """_create_tts_from_config creates the right adapter."""

    def test_elevenlabs(self) -> None:
        from patter.handlers.common import _create_tts_from_config
        from patter.models import TTSConfig

        config = TTSConfig(provider="elevenlabs", api_key="el-key", voice="v1")
        tts = _create_tts_from_config(config)
        assert tts is not None
        assert tts.__class__.__name__ == "ElevenLabsTTS"

    def test_openai(self) -> None:
        from patter.handlers.common import _create_tts_from_config
        from patter.models import TTSConfig

        config = TTSConfig(provider="openai", api_key="sk-key", voice="alloy")
        tts = _create_tts_from_config(config)
        assert tts is not None
        assert tts.__class__.__name__ == "OpenAITTS"

    def test_unknown_provider(self) -> None:
        from patter.handlers.common import _create_tts_from_config
        from patter.models import TTSConfig

        config = TTSConfig(provider="unknown", api_key="x")
        assert _create_tts_from_config(config) is None


# ---------------------------------------------------------------------------
# OpenAITTS — synthesize (streaming)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenAITTSSynthesize:
    """OpenAITTS.synthesize streams resampled chunks."""

    @pytest.mark.asyncio
    async def test_synthesize_streams_resampled_audio(self) -> None:
        from patter.providers.openai_tts import OpenAITTS

        tts = OpenAITTS(api_key="sk-test", voice="alloy", model="tts-1")

        # Build a small 24kHz PCM16 chunk (6 samples -> 12 bytes)
        samples = [100, 200, 300, 400, 500, 600]
        audio_chunk = struct.pack(f"<{len(samples)}h", *samples)

        # Mock the streaming response
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()

        async def _aiter_bytes(chunk_size: int = 4096):
            yield audio_chunk

        mock_resp.aiter_bytes = _aiter_bytes
        mock_resp.aclose = AsyncMock()

        tts._client = AsyncMock()
        tts._client.build_request.return_value = MagicMock()
        tts._client.send.return_value = mock_resp

        chunks = []
        async for chunk in tts.synthesize("hello"):
            chunks.append(chunk)

        assert len(chunks) == 1
        assert len(chunks[0]) > 0
        mock_resp.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_synthesize_raises_on_http_error(self) -> None:
        import httpx
        from patter.providers.openai_tts import OpenAITTS

        tts = OpenAITTS(api_key="sk-test")

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error", request=MagicMock(), response=MagicMock()
        )

        tts._client = AsyncMock()
        tts._client.build_request.return_value = MagicMock()
        tts._client.send.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            async for _ in tts.synthesize("hello"):
                pass


# ---------------------------------------------------------------------------
# ElevenLabsTTS — synthesize (streaming)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestElevenLabsTTSSynthesize:
    """ElevenLabsTTS.synthesize streams audio chunks."""

    @pytest.mark.asyncio
    async def test_synthesize_streams_audio(self) -> None:
        from patter.providers.elevenlabs_tts import ElevenLabsTTS

        tts = ElevenLabsTTS(api_key="el-test", voice_id="v1")

        audio_chunk = b"\x00\x01\x02\x03" * 100

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()

        async def _aiter_bytes(chunk_size: int = 4096):
            yield audio_chunk

        mock_resp.aiter_bytes = _aiter_bytes
        mock_resp.aclose = AsyncMock()

        tts._client = AsyncMock()
        tts._client.build_request.return_value = MagicMock()
        tts._client.send.return_value = mock_resp

        chunks = []
        async for chunk in tts.synthesize("test"):
            chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0] == audio_chunk
        mock_resp.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# TwilioAdapter — async I/O
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTwilioAdapterIO:
    """TwilioAdapter async methods with mocked Twilio client."""

    @pytest.mark.asyncio
    async def test_provision_number(self) -> None:
        from patter.providers.twilio_adapter import TwilioAdapter

        adapter = TwilioAdapter(account_sid="AC_test_sid_12345", auth_token="tok")
        mock_number = MagicMock()
        mock_number.phone_number = "+15559999999"
        adapter._twilio_client = MagicMock()
        adapter._twilio_client.available_phone_numbers.return_value.local.list.return_value = [mock_number]

        mock_purchased = MagicMock()
        mock_purchased.phone_number = "+15559999999"
        adapter._twilio_client.incoming_phone_numbers.create.return_value = mock_purchased

        number = await adapter.provision_number("US")
        assert number == "+15559999999"

    @pytest.mark.asyncio
    async def test_provision_number_no_numbers(self) -> None:
        from patter.providers.twilio_adapter import TwilioAdapter

        adapter = TwilioAdapter(account_sid="AC_test_sid_12345", auth_token="tok")
        adapter._twilio_client = MagicMock()
        adapter._twilio_client.available_phone_numbers.return_value.local.list.return_value = []

        with pytest.raises(ValueError, match="No numbers"):
            await adapter.provision_number("US")

    @pytest.mark.asyncio
    async def test_configure_number(self) -> None:
        from patter.providers.twilio_adapter import TwilioAdapter

        adapter = TwilioAdapter(account_sid="AC_test_sid_12345", auth_token="tok")
        mock_num = MagicMock()
        adapter._twilio_client = MagicMock()
        adapter._twilio_client.incoming_phone_numbers.list.return_value = [mock_num]

        await adapter.configure_number("+15551111111", "https://example.com/webhook")
        mock_num.update.assert_called_once_with(voice_url="https://example.com/webhook", voice_method="POST")

    @pytest.mark.asyncio
    async def test_configure_number_not_found(self) -> None:
        from patter.providers.twilio_adapter import TwilioAdapter

        adapter = TwilioAdapter(account_sid="AC_test_sid_12345", auth_token="tok")
        adapter._twilio_client = MagicMock()
        adapter._twilio_client.incoming_phone_numbers.list.return_value = []

        with pytest.raises(ValueError, match="not found"):
            await adapter.configure_number("+15551111111", "https://example.com/webhook")

    @pytest.mark.asyncio
    async def test_initiate_call(self) -> None:
        from patter.providers.twilio_adapter import TwilioAdapter

        adapter = TwilioAdapter(account_sid="AC_test_sid_12345", auth_token="tok")
        mock_call = MagicMock()
        mock_call.sid = "CA_test_call_sid"
        adapter._twilio_client = MagicMock()
        adapter._twilio_client.calls.create.return_value = mock_call

        sid = await adapter.initiate_call("+15551111111", "+15552222222", "wss://stream.example.com")
        assert sid == "CA_test_call_sid"

    @pytest.mark.asyncio
    async def test_initiate_call_with_extra_params(self) -> None:
        from patter.providers.twilio_adapter import TwilioAdapter

        adapter = TwilioAdapter(account_sid="AC_test_sid_12345", auth_token="tok")
        mock_call = MagicMock()
        mock_call.sid = "CA_test_call_sid"
        adapter._twilio_client = MagicMock()
        adapter._twilio_client.calls.create.return_value = mock_call

        sid = await adapter.initiate_call(
            "+15551111111", "+15552222222", "wss://stream.example.com",
            extra_params={"machine_detection": "Enable"},
        )
        assert sid == "CA_test_call_sid"
        call_kwargs = adapter._twilio_client.calls.create.call_args[1]
        assert call_kwargs["machine_detection"] == "Enable"

    @pytest.mark.asyncio
    async def test_end_call(self) -> None:
        from patter.providers.twilio_adapter import TwilioAdapter

        adapter = TwilioAdapter(account_sid="AC_test_sid_12345", auth_token="tok")
        adapter._twilio_client = MagicMock()

        await adapter.end_call("CA_test_call_sid")
        adapter._twilio_client.calls.return_value.update.assert_called_once_with(status="completed")

    def test_generate_stream_twiml(self) -> None:
        from patter.providers.twilio_adapter import TwilioAdapter

        twiml = TwilioAdapter.generate_stream_twiml("wss://stream.example.com/ws")
        assert "wss://stream.example.com/ws" in twiml
        assert "<Stream" in twiml
