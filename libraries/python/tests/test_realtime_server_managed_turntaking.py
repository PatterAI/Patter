"""Server-managed turn-taking + barge-in dispatch (issue #154 unification).

For END-TO-END OpenAI Realtime engines (v1 + GA) turn-taking is handed to the
server: ``turn_detection.create_response`` and ``interrupt_response`` default
to ``True``. On the WebSocket transport the client still does the playout-buffer
bookkeeping the server cannot do for it — ``send_clear`` + ``conversation.item.truncate``
on ``speech_started`` — but it does NOT send ``response.cancel`` and does NOT
drive ``response.create``.

This file pins the three DISPATCH paths so they never drift:

- OpenAI engine, DEFAULT (gate=False): send_clear + truncate_playback only.
- OpenAI engine, LEGACY opt-out (gate=True): send_clear + cancel_response + metrics.
- ElevenLabs ConvAI ``interruption``: send_clear ONLY (no truncate, no cancel,
  no gate, no barge-in metrics) — server-managed agent, unchanged.

Every test drives the REAL ``_forward_events`` event loop. The only faked
surface is the adapter (the provider WebSocket boundary) and the audio sink.
Mirrors the parity TypeScript ``stream-handler.mocked.test.ts``.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

import pytest

from getpatter.services.metrics import CallMetricsAccumulator
from getpatter.stream_handler import (
    ElevenLabsConvAIStreamHandler,
    OpenAIRealtimeStreamHandler,
)


class _FakeOpenAIAdapter:
    """Real async stand-in for the OpenAI Realtime WS boundary.

    Records which barge-in method the handler chose so a test can assert the
    server-managed (truncate-only) vs legacy (cancel) dispatch.
    """

    def __init__(self, events: list[tuple[str, Any]], *, gate: bool) -> None:
        self._events = events
        self.gate_response_on_transcript = gate
        self.cancelled = 0
        self.truncated = 0
        # Past the anti-flicker window so the legacy path is never suppressed.
        self._current_response_first_audio_at = time.monotonic() - 10.0

    async def receive_events(self):
        for name, data in self._events:
            yield (name, data)
            await asyncio.sleep(0)

    async def cancel_response(self) -> None:
        self.cancelled += 1

    async def truncate_playback(self) -> None:
        self.truncated += 1

    async def request_response(self) -> None:  # pragma: no cover - unused here
        pass


class _FakeConvAIAdapter:
    """Real async stand-in for the ElevenLabs ConvAI WS boundary.

    Deliberately exposes NO truncate/cancel methods — if the ConvAI loop ever
    tried to call one, the test would raise ``AttributeError`` and fail.
    """

    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self._events = events

    async def receive_events(self):
        for name, data in self._events:
            yield (name, data)
            await asyncio.sleep(0)


class _FakeAudioSender:
    def __init__(self) -> None:
        self.clears = 0

    async def send_clear(self) -> None:
        self.clears += 1

    async def send_audio(self, data: bytes) -> None:  # pragma: no cover
        pass

    async def send_mark(self, mark: str) -> None:  # pragma: no cover
        pass


def _metrics() -> CallMetricsAccumulator:
    return CallMetricsAccumulator(
        call_id="CA0000000000000000000000000000a001",
        provider_mode="openai_realtime",
        telephony_provider="twilio",
        realtime_model="gpt-realtime",
    )


def _openai_handler(adapter, *, metrics, audio_sender):
    handler = OpenAIRealtimeStreamHandler.__new__(OpenAIRealtimeStreamHandler)
    handler._adapter = adapter
    handler.metrics = metrics
    handler.on_transcript = None
    handler.on_transcript_line = None
    handler.on_metrics = None
    handler.call_id = "CA0000000000000000000000000000a001"
    handler.caller = "+15551230000"
    handler.callee = "+15551239999"
    handler.conversation_history = deque(maxlen=200)
    handler.transcript_entries = deque(maxlen=200)
    handler.speech_events = None
    handler._user_speech_start_ms = None
    handler._agent_turn_start_ms = None
    handler._user_transcript_pending = False
    handler._pending_assistant_turn = None
    handler._pending_assistant_timer = None
    handler._current_turn_index = None
    handler.audio_sender = audio_sender
    handler.agent = type("_A", (), {"model": "gpt-realtime", "tools": None})()
    return handler


def _convai_handler(adapter, *, metrics, audio_sender):
    handler = ElevenLabsConvAIStreamHandler.__new__(ElevenLabsConvAIStreamHandler)
    handler._adapter = adapter
    handler.metrics = metrics
    handler.on_transcript = None
    handler.on_transcript_line = None
    handler.on_metrics = None
    handler.call_id = "CA0000000000000000000000000000a001"
    handler.conversation_history = deque(maxlen=200)
    handler.transcript_entries = deque(maxlen=200)
    handler.speech_events = None
    handler._current_turn_index = None
    handler.audio_sender = audio_sender
    handler.agent = type("_A", (), {"model": "convai", "tools": None})()
    return handler


@pytest.mark.mocked
class TestEngineServerManagedDispatch:
    async def test_default_path_truncates_only(self) -> None:
        adapter = _FakeOpenAIAdapter([("speech_started", None)], gate=False)
        metrics = _metrics()
        metrics.start_turn()
        sender = _FakeAudioSender()
        handler = _openai_handler(adapter, metrics=metrics, audio_sender=sender)

        await handler._forward_events()

        assert sender.clears == 1
        assert adapter.truncated == 1
        assert adapter.cancelled == 0
        # Engine turn stays anchored at speech_stopped — no re-anchor / bargein.
        assert metrics._last_bargein_at is None

    async def test_legacy_path_cancels_with_metrics(self) -> None:
        adapter = _FakeOpenAIAdapter([("speech_started", None)], gate=True)
        metrics = _metrics()
        metrics.start_turn()
        sender = _FakeAudioSender()
        handler = _openai_handler(adapter, metrics=metrics, audio_sender=sender)

        await handler._forward_events()

        assert sender.clears == 1
        assert adapter.cancelled == 1
        assert adapter.truncated == 0
        assert metrics._last_bargein_at is not None


@pytest.mark.mocked
class TestConvAIInterruptionSendClearOnly:
    async def test_interruption_is_send_clear_only(self) -> None:
        """ConvAI is server-managed: ``interruption`` clears the playout buffer
        and records the interrupt — nothing else. The adapter exposes no
        truncate/cancel; calling one would raise and fail this test."""
        adapter = _FakeConvAIAdapter([("interruption", None)])
        metrics = _metrics()
        metrics.start_turn()
        sender = _FakeAudioSender()
        handler = _convai_handler(adapter, metrics=metrics, audio_sender=sender)

        await handler._forward_events()

        # send_clear fired once ...
        assert sender.clears == 1
        # ... the active turn was closed as interrupted ...
        assert any(t.agent_text == "[interrupted]" for t in metrics._turns)
        # ... and NO barge-in detection / re-anchor was stamped (no client gate).
        assert metrics._last_bargein_at is None
