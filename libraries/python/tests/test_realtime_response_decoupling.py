"""Decouple the OpenAI Realtime model response from the Whisper transcript.

Issue #154 (Option A): in OpenAI Realtime mode the end-to-end speech-to-speech
model must respond INDEPENDENTLY of the Whisper input transcription. By default
Patter fires ``response.create`` as soon as the user stops speaking
(``speech_stopped``) — it no longer waits ~500 ms for the
``input_audio_transcription.completed`` event. The Whisper transcript becomes
pure observability (dashboard / history / ``on_transcript``); the hallucination
filter applies to the DISPLAYED transcript only and never gates or cancels the
response.

These tests drive the REAL ``OpenAIRealtimeStreamHandler._forward_events`` event
loop. The only faked surface is the adapter (the OpenAI WebSocket boundary) —
its ``receive_events`` is a real async generator over a scripted event list and
its ``request_response`` is a real coroutine that records WHICH event it was
fired after. Everything inward (the decoupling branch, the hallucination filter,
the history push, the transcript callback) is real code.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import pytest

from getpatter.stream_handler import OpenAIRealtimeStreamHandler


class _FakeAdapter:
    """Real async object standing in for the OpenAI Realtime WS boundary.

    Yields a scripted event list through ``receive_events`` (a real async
    generator) and records every ``request_response`` call together with the
    event that was being processed when it fired, so a test can assert the
    response was requested on ``speech_stopped`` (decoupled) vs.
    ``transcript_input`` (legacy gated).
    """

    def __init__(self, events: list[tuple[str, Any]], *, gate: bool) -> None:
        self._events = events
        self.gate_response_on_transcript = gate
        # (event_name_being_processed, ) recorded at each request_response call.
        self.request_response_after: list[str] = []
        self._current_event: str | None = None
        # Captured everything sent via send_text / send_function_result so a
        # test can prove no transcript text was ever re-injected into the model.
        self.sent_items: list[dict] = []
        self.cancelled = 0

    async def receive_events(self):
        for name, data in self._events:
            self._current_event = name
            yield (name, data)
            # Let any awaited request_response() inside the handler settle so
            # the attribution (request_response_after) is recorded against the
            # event it was triggered from.
            await asyncio.sleep(0)

    async def request_response(self) -> None:
        # Attribute the call to the event currently being processed.
        self.request_response_after.append(self._current_event or "")

    async def send_text(self, text: str) -> None:
        self.sent_items.append({"kind": "text", "text": text})

    async def send_function_result(self, call_id: str, result: str) -> None:
        self.sent_items.append({"kind": "function_result", "result": result})

    async def cancel_response(self) -> None:
        self.cancelled += 1


def _make_handler(adapter: _FakeAdapter, *, on_transcript=None):
    """Construct a real handler with the minimal real state ``_forward_events``
    touches, bypassing the network-bound constructor (mirrors the existing
    test_speech_events stream-handler tests)."""
    handler = OpenAIRealtimeStreamHandler.__new__(OpenAIRealtimeStreamHandler)
    handler._adapter = adapter
    handler.metrics = None  # exercise the no-metrics path; metrics covered elsewhere
    handler.on_transcript = on_transcript
    # FIX-5 (issue #154): live transcript-line callback, normally set in
    # __init__ (bypassed here by __new__). None == no live-line emission.
    handler.on_transcript_line = None
    handler.call_id = "CA0000000000000000000000000000a001"
    handler.conversation_history = deque(maxlen=200)
    handler.transcript_entries = deque(maxlen=200)
    handler.speech_events = None  # _emit_* helpers no-op when this is None
    handler._user_speech_start_ms = None
    handler._agent_turn_start_ms = None
    handler._user_transcript_pending = False
    handler._pending_assistant_turn = None
    handler._pending_assistant_timer = None
    # Issue #154 FIX-5: reserved per-turn index, normally set in __init__
    # (bypassed here by __new__) and stamped onto each transcript line.
    handler._current_turn_index = None
    # audio_sender is only touched on "audio" events, which these scripts omit.
    handler.audio_sender = None
    handler.agent = type("_A", (), {"model": "gpt-realtime", "tools": None})()
    return handler


@pytest.mark.unit
class TestResponseDecoupledFromTranscript:
    async def test_patter_does_not_drive_response_by_default(self) -> None:
        """Default (gate=False): the SERVER auto-creates the response on commit
        (``create_response=True`` in the GA session). Patter must NOT drive
        ``response.create`` itself — not on ``speech_stopped`` (would race the
        server-side commit) nor on ``transcript_input`` (would double up)."""
        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_input", "I'd like to book a table"),
            ],
            gate=False,
        )
        handler = _make_handler(adapter)

        await handler._forward_events()

        # The server creates the response; Patter never calls request_response.
        assert adapter.request_response_after == []

    async def test_transcript_still_lands_in_history_when_decoupled(self) -> None:
        """The transcript is still observability: it populates history and the
        on_transcript callback even though it did not drive the response."""
        seen: list[dict] = []

        async def on_transcript(payload: dict) -> None:
            seen.append(payload)

        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_input", "what are your hours"),
            ],
            gate=False,
        )
        handler = _make_handler(adapter, on_transcript=on_transcript)

        await handler._forward_events()

        # The server creates the response; Patter drives nothing ...
        assert adapter.request_response_after == []
        # ... and the transcript still reached history + the callback.
        assert any(
            e["role"] == "user" and e["text"] == "what are your hours"
            for e in handler.conversation_history
        )
        assert any(
            p["role"] == "user" and p["text"] == "what are your hours" for p in seen
        )

    async def test_hallucination_dropped_from_display_but_response_still_fired(
        self,
    ) -> None:
        """A Whisper hallucination ('Thank you for watching.') must NOT appear
        in the displayed transcript, yet the response — created server-side on
        the audio commit — is unaffected. The filter gates DISPLAY only."""
        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_input", "Thank you for watching."),
            ],
            gate=False,
        )
        handler = _make_handler(adapter)

        await handler._forward_events()

        # The server creates the response; Patter never drives it. The phantom
        # transcript is filtered from DISPLAY but never gates/cancels the reply.
        assert adapter.request_response_after == []
        # The phantom transcript was dropped from history (display) entirely.
        assert all(
            e.get("text") != "Thank you for watching."
            for e in handler.conversation_history
        )
        assert len(handler.transcript_entries) == 0

    async def test_no_transcript_text_reinjected_into_model(self) -> None:
        """The model already has the audio — Patter must never re-inject the
        Whisper transcript text via send_text / conversation.item.create."""
        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_input", "book me a haircut tomorrow"),
            ],
            gate=False,
        )
        handler = _make_handler(adapter)

        await handler._forward_events()

        # request_response carries no payload; nothing was sent as a user item.
        assert adapter.sent_items == []


@pytest.mark.unit
class TestLegacyTranscriptGatedOptOut:
    async def test_response_gated_on_transcript_when_flag_true(self) -> None:
        """Opt-out (gate=True): restores the legacy path — the response is
        requested on ``transcript_input``, NOT on ``speech_stopped``."""
        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_input", "I'd like to book a table"),
            ],
            gate=True,
        )
        handler = _make_handler(adapter)

        await handler._forward_events()

        assert adapter.request_response_after == ["transcript_input"]

    async def test_legacy_hallucination_suppresses_response(self) -> None:
        """In the legacy gated path, a dropped hallucination means NO response
        is requested at all (the filter gates the response there)."""
        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_input", "Thanks for watching!"),
            ],
            gate=True,
        )
        handler = _make_handler(adapter)

        await handler._forward_events()

        assert adapter.request_response_after == []


@pytest.mark.unit
class TestAdapterDefaultMissing:
    async def test_missing_flag_attr_defaults_to_decoupled(self) -> None:
        """An adapter that predates the flag (no ``gate_response_on_transcript``
        attribute) must default to the decoupled behavior — Patter drives no
        ``response.create`` (the server auto-creates it) — via the
        getattr(..., False) fallback."""

        class _OldAdapter(_FakeAdapter):
            def __init__(self, events: list[tuple[str, Any]]) -> None:
                super().__init__(events, gate=False)
                # Simulate an older adapter without the attribute.
                del self.gate_response_on_transcript

        adapter = _OldAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_input", "hello"),
            ]
        )
        handler = _make_handler(adapter)

        await handler._forward_events()

        assert adapter.request_response_after == []
