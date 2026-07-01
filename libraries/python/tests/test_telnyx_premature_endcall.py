"""Premature end_call guard for native-audio realtime engines over Telnyx.

BUG: a realtime engine (GeminiLive / OpenAIRealtime2) can emit ``end_call`` the
instant its greeting finishes — before the caller has spoken. On Telnyx the cold
realtime connect→first-audio window eats the opening call budget, so the model
greets and immediately bails ("no_response") ~1.5 s in, killing a live caller.
Twilio and pipeline mode never hit this. The fix refuses a model-initiated
hang-up that fires before ANY caller speech AND within the opening grace window,
telling the model to keep listening instead.

AUTHENTIC: the real ``OpenAIRealtimeStreamHandler._forward_events`` end_call
branch, the real caller-spoke transcript path (incl. the real
``_is_stt_hallucination`` filter), and the real grace-window arithmetic all run.
The only faked surfaces are the adapter (the provider WebSocket boundary — a real
async generator over a scripted event list) and the hang-up callback (we cannot
place phone calls in CI). We assert on the observable outcomes: whether the
hang-up fired and the ``send_function_result`` payload the model receives back.
"""

from __future__ import annotations

import json
import time
from collections import deque

import pytest

from getpatter.stream_handler import OpenAIRealtimeStreamHandler


class _FakeAdapter:
    """Real async object standing in for the provider WS boundary.

    ``receive_events`` is a real async generator yielding a scripted list of
    ``(type, data)`` events. ``send_function_result`` records the payloads the
    handler hands back to the model.
    """

    def __init__(self, events: list[tuple[str, object]]) -> None:
        self._events = events
        self.function_results: list[tuple[str, str]] = []
        self.closed = False

    async def receive_events(self):
        for ev in self._events:
            yield ev

    async def send_function_result(self, call_id: str, result: str) -> None:
        self.function_results.append((call_id, result))

    async def close(self) -> None:
        self.closed = True


def _make_realtime_handler(adapter: _FakeAdapter) -> OpenAIRealtimeStreamHandler:
    """Construct a real handler with the minimal real state the end_call and
    transcript_input branches touch, bypassing the network-bound constructor
    (mirrors test_telnyx_comfort_noise)."""
    handler = OpenAIRealtimeStreamHandler.__new__(OpenAIRealtimeStreamHandler)
    handler._adapter = adapter
    handler.audio_sender = None
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
    handler._transfer_fn = None
    handler.agent = type("_A", (), {"model": "gpt-realtime", "tools": None})()
    # Guard state under test.
    handler._call_started_at_ms = time.time() * 1000
    handler._user_has_spoken = False
    # Observable hang-up boundary (we cannot place phone calls in CI).
    handler._hangup_calls = 0

    async def _hangup() -> None:
        handler._hangup_calls += 1

    handler._hangup_fn = _hangup
    return handler


_END_CALL_EVENT = (
    "function_call",
    {"call_id": "fc-1", "name": "end_call", "arguments": json.dumps({"reason": "no_response"})},
)


@pytest.mark.mocked
class TestPrematureEndCallGuard:
    async def test_refuses_end_call_before_caller_speaks(self) -> None:
        adapter = _FakeAdapter([_END_CALL_EVENT])
        handler = _make_realtime_handler(adapter)

        await handler._forward_events()

        # Call MUST NOT be torn down.
        assert handler._hangup_calls == 0
        # Model is told to keep listening (rejection payload, session left open).
        assert len(adapter.function_results) == 1
        _, payload = adapter.function_results[0]
        parsed = json.loads(payload)
        assert parsed["status"] == "rejected"
        assert parsed["reason"] == "caller_still_connecting"

    async def test_honors_end_call_after_caller_speaks(self) -> None:
        adapter = _FakeAdapter(
            [
                ("transcript_input", "Hi, I would like to book an appointment."),
                (
                    "function_call",
                    {
                        "call_id": "fc-2",
                        "name": "end_call",
                        "arguments": json.dumps({"reason": "conversation_complete"}),
                    },
                ),
            ]
        )
        handler = _make_realtime_handler(adapter)

        await handler._forward_events()

        assert handler._user_has_spoken is True
        assert handler._hangup_calls == 1

    async def test_honors_end_call_after_grace_window(self) -> None:
        adapter = _FakeAdapter([_END_CALL_EVENT])
        handler = _make_realtime_handler(adapter)
        # Simulate the opening grace window (6 s) having already elapsed.
        handler._call_started_at_ms = time.time() * 1000 - 7000

        await handler._forward_events()

        assert handler._hangup_calls == 1

    async def test_hallucination_does_not_count_as_caller_speech(self) -> None:
        adapter = _FakeAdapter(
            [
                ("transcript_input", "Thank you for watching."),
                _END_CALL_EVENT,
            ]
        )
        handler = _make_realtime_handler(adapter)

        await handler._forward_events()

        assert handler._user_has_spoken is False
        assert handler._hangup_calls == 0
        assert len(adapter.function_results) == 1
