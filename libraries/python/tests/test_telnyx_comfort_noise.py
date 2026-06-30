"""Realtime-engine outbound keepalive over Telnyx (comfort-noise pump).

BUG: native-audio realtime engines (GeminiLive / OpenAIRealtime2 / ConvAI) put
NO bytes on the carrier between the carrier ``start`` and the model's first
audio delta (cold ``connect()`` + TTFT + resampler warmup, often >1.5 s).
Twilio tolerates the gap; Telnyx clears the idle bidirectional RTP leg
(~1.6 s). The fix pumps paced μ-law-8k silence from stream-start until the
first real model frame.

AUTHENTIC: the real ``OpenAIRealtimeStreamHandler`` (base-class pump helpers
and the real ``_forward_events`` audio guard) is exercised. The only faked
surface is the adapter (the provider WebSocket boundary — a real async
generator over a scripted event list) and the ``AudioSender`` (we cannot place
phone calls in CI — a real recording test double). We assert on the observable
outcome: the bytes handed to ``audio_sender.send_audio``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import pytest

from getpatter.stream_handler import OpenAIRealtimeStreamHandler, StreamHandler

# The exact frame the pump emits (private class const — re-derived here).
SILENCE_FRAME = b"\xff" * 160


class _RecordingAudioSender:
    """Real recording test double for the carrier AudioSender boundary.

    On the realtime path the sender runs in ``_input_is_mulaw_8k`` pass-through
    mode, so whatever bytes the handler sends are exactly what reaches the
    carrier — we record them verbatim.
    """

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_audio(self, audio: bytes) -> None:
        self.sent.append(audio)

    async def send_clear(self) -> None:
        pass

    async def send_mark(self, mark_name: str) -> None:
        pass

    def reset_pcm_carry(self) -> None:
        pass

    def silence_frame_count(self) -> int:
        return sum(1 for a in self.sent if a == SILENCE_FRAME)


class _FakeAdapter:
    """Real async object standing in for the provider WS boundary.

    ``receive_events`` is a real async generator that first idles (so the pump
    has a window to emit silence) and then yields one real ``audio`` frame.
    """

    def __init__(self, idle_before_audio_s: float = 0.06) -> None:
        self._idle = idle_before_audio_s
        self.closed = False

    async def receive_events(self):
        # Simulate the connect→first-delta gap: no events yet. The pump runs
        # concurrently during this await.
        await asyncio.sleep(self._idle)
        yield ("audio", b"\x00\x00" * 240)  # one real model PCM frame

    async def close(self) -> None:
        self.closed = True


def _make_realtime_handler(adapter: _FakeAdapter, sender: _RecordingAudioSender):
    """Construct a real handler with the minimal real state the pump and the
    ``_forward_events`` audio guard touch, bypassing the network-bound
    constructor (mirrors test_realtime_response_decoupling)."""
    handler = OpenAIRealtimeStreamHandler.__new__(OpenAIRealtimeStreamHandler)
    handler._adapter = adapter
    handler.audio_sender = sender
    handler.metrics = None
    handler.on_transcript = None
    handler.on_transcript_line = None
    handler.call_id = "CA0000000000000000000000000000a001"
    handler.conversation_history = deque(maxlen=200)
    handler.transcript_entries = deque(maxlen=200)
    handler.speech_events = None
    handler._user_speech_start_ms = None
    handler._agent_turn_start_ms = None
    handler._user_transcript_pending = False
    handler._pending_assistant_turn = None
    handler._pending_assistant_timer = None
    handler._current_turn_index = None
    handler.local_recorder = None
    handler._comfort_noise_task = None
    handler.agent = type("_A", (), {"model": "gpt-realtime", "tools": None})()
    return handler


@pytest.mark.unit
class TestComfortNoiseFrame:
    def test_silence_frame_is_160_bytes_of_mulaw_digital_zero(self) -> None:
        # 20 ms @ 8 kHz μ-law = 160 samples × 1 byte. 0xFF == μ-law digital
        # zero (decodes to linear PCM 0).
        from getpatter.audio.transcoding import mulaw_to_pcm16

        frame = StreamHandler._MULAW_SILENCE_FRAME
        assert frame == SILENCE_FRAME
        assert len(frame) == 160

        pcm = mulaw_to_pcm16(frame)
        # 160 μ-law bytes -> 160 PCM16 samples (2 bytes each) = 320 bytes,
        # every sample exactly 0 (true silence).
        assert len(pcm) == 320
        for i in range(0, len(pcm), 2):
            assert int.from_bytes(pcm[i : i + 2], "little", signed=True) == 0


@pytest.mark.mocked
class TestComfortNoisePump:
    async def test_pump_emits_silence_then_stops_on_first_frame(self) -> None:
        sender = _RecordingAudioSender()
        adapter = _FakeAdapter(idle_before_audio_s=0.06)
        handler = _make_realtime_handler(adapter, sender)

        # Arm the pump (as start() does after connect), then run the real
        # event loop concurrently. The loop idles ~60 ms (≥3 pump intervals)
        # before the first real audio frame, which must stop the pump.
        handler._start_comfort_noise()
        await handler._forward_events()

        # At least one silence frame reached the carrier before the model
        # produced audio.
        assert sender.silence_frame_count() >= 1
        # The pump self-cancelled on the first real frame.
        assert handler._comfort_noise_task is None

        frames_after = sender.silence_frame_count()
        # No further silence frames are produced after the first real frame.
        await asyncio.sleep(0.06)
        assert sender.silence_frame_count() == frames_after

        # The real model frame was forwarded to the carrier.
        assert any(a == b"\x00\x00" * 240 for a in sender.sent)

    async def test_start_is_idempotent(self) -> None:
        sender = _RecordingAudioSender()
        adapter = _FakeAdapter()
        handler = _make_realtime_handler(adapter, sender)

        handler._start_comfort_noise()
        first = handler._comfort_noise_task
        handler._start_comfort_noise()  # no-op while running
        assert handler._comfort_noise_task is first

        handler._stop_comfort_noise()
        assert handler._comfort_noise_task is None

    async def test_stop_before_any_audio_clears_the_task_no_leak(self) -> None:
        sender = _RecordingAudioSender()
        adapter = _FakeAdapter()
        handler = _make_realtime_handler(adapter, sender)

        handler._start_comfort_noise()
        await asyncio.sleep(0.03)  # let it emit a couple frames
        handler._stop_comfort_noise()
        assert handler._comfort_noise_task is None

        frames = sender.silence_frame_count()
        await asyncio.sleep(0.06)
        # No frames after teardown — the task is truly cancelled, not leaked.
        assert sender.silence_frame_count() == frames

    async def test_cleanup_before_any_audio_stops_the_pump(self) -> None:
        sender = _RecordingAudioSender()
        adapter = _FakeAdapter()
        handler = _make_realtime_handler(adapter, sender)
        # cleanup() touches several teardown fields — supply the real ones it
        # reads so the real cleanup() path runs end-to-end.
        handler._max_call_watchdog = None
        handler._background_task = None
        handler._adapter = adapter  # has a real async close()
        handler._resampler_16k_to_8k = None

        async def _noop_close_mcp() -> None:
            return None

        handler._close_mcp = _noop_close_mcp  # external MCP boundary
        handler._close_local_recorder = lambda: None  # no recording in CI

        handler._start_comfort_noise()
        await asyncio.sleep(0.03)
        await handler.cleanup()
        assert handler._comfort_noise_task is None
