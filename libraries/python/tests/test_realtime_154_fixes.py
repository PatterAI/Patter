"""Issue #154 Realtime fixes — Python core.

Covers the five behavioural fixes landed for the Realtime handler:

* FIX-1 flush-on-drop: when a Whisper hallucination is dropped from the
  displayed transcript, any assistant turn buffered by ``response_done`` must
  flush IMMEDIATELY rather than stalling on the ~3 s fallback timer.
* FIX-2 narrowed blocklist: ``_is_stt_hallucination`` must keep real
  conversational words ('yes' / 'no' / 'okay' / 'right') while still dropping
  caption credits + sign-offs.
* Barge-in (issue #154 server-managed turn-taking): on ``speech_started`` the
  DEFAULT (gate=False) server-managed path does ``send_clear`` +
  ``truncate_playback`` only (the server owns the cancel via
  ``interrupt_response=true``); the LEGACY (gate=True) client-managed opt-out
  runs the anti-flicker gate, ``cancel_response``, and the barge-in metrics
  (``record_bargein_detected`` + ``anchor_user_speech_start``) that arm the
  post-barge-in hygiene gate (endpoint_ms None / stt_ms 0).
* FIX-4 error events: an injected ``error`` frame must log a warning and NOT
  crash the event loop (both the GA and ConvAI loops).
* FIX-5 reserved turn index + live lines: ``reserve_turn_index`` hands out a
  monotonic counter at turn-open and the same index is stamped onto the user
  and assistant transcript lines and the recorded turn.

The pure-function ``_is_stt_hallucination`` tests are ``unit``. The handler
event-loop tests mock ONLY the adapter (the OpenAI/ElevenLabs WS boundary) and
drive the REAL ``_forward_events`` loop with a REAL ``CallMetricsAccumulator``
where metrics are asserted — so they are tagged ``mocked``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

import pytest

from getpatter.services.metrics import CallMetricsAccumulator
from getpatter.stream_handler import (
    OpenAIRealtimeStreamHandler,
    _is_stt_hallucination,
)


# ---------------------------------------------------------------------------
# FIX-2 — narrowed hallucination blocklist (pure function, unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNarrowedHallucinationBlocklist:
    @pytest.mark.parametrize(
        "text",
        ["yes", "no", "okay", "ok", "right", "yeah", "you", "the", "bye", "cool"],
    )
    def test_real_conversational_words_are_kept(self, text: str) -> None:
        """Now that the filter is DISPLAY-ONLY, real words must NOT be dropped —
        otherwise the user's transcript line silently disappears."""
        assert _is_stt_hallucination(text) is False
        # Case / trailing-punctuation variants must also survive.
        assert _is_stt_hallucination(text.upper()) is False
        assert _is_stt_hallucination(f"{text}.") is False

    @pytest.mark.parametrize(
        "text",
        [
            "thank you for watching",
            "Thank you for watching.",
            "thanks for watching!",
            "please subscribe",
            "subtitles by the amara.org community",
            "[music]",
            "[silence]",
            "[blank_audio]",
            "♪",
        ],
    )
    def test_caption_credits_and_artifacts_still_dropped(self, text: str) -> None:
        assert _is_stt_hallucination(text) is True

    def test_multi_sentence_signoff_still_dropped(self) -> None:
        """The multi-sentence split must still drop a phrase composed entirely
        of known closers."""
        assert _is_stt_hallucination("We'll see you next time. Bye bye.") is True

    def test_empty_after_strip_is_dropped(self) -> None:
        assert _is_stt_hallucination("") is True
        assert _is_stt_hallucination("   ...  ") is True

    def test_real_sentence_with_filler_word_not_dropped(self) -> None:
        """A real multi-word sentence that merely contains a kept word must
        never be dropped."""
        assert _is_stt_hallucination("yes please book me a table") is False
        assert _is_stt_hallucination("no thank you that's all") is False


# ---------------------------------------------------------------------------
# Shared real-adapter test double (mocks ONLY the WS boundary)
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """Real async object standing in for the provider WS boundary.

    Yields a scripted event list through ``receive_events`` (a real async
    generator). Records cancel calls and any audio-sender clears so tests can
    assert the barge-in sequence without a live WebSocket.
    """

    def __init__(self, events: list[tuple[str, Any]], *, gate: bool = False) -> None:
        self._events = events
        self.gate_response_on_transcript = gate
        self.cancelled = 0
        # Server-managed barge-in calls truncate_playback() (truncate only);
        # legacy opt-out calls cancel_response() (truncate + response.cancel).
        self.truncated = 0
        # Set non-None so the speech_started branch passes the anti-flicker
        # gate (response already speaking long enough to allow barge-in).
        self._current_response_first_audio_at = None

    async def receive_events(self):
        for name, data in self._events:
            yield (name, data)
            await asyncio.sleep(0)

    async def request_response(self) -> None:  # pragma: no cover - default path
        pass

    async def send_text(self, text: str) -> None:  # pragma: no cover
        pass

    async def cancel_response(self) -> None:
        self.cancelled += 1

    async def truncate_playback(self) -> None:
        self.truncated += 1


class _FakeAudioSender:
    """Real async audio sink — records clears so barge-in tests can assert the
    cancel sequence ran."""

    def __init__(self) -> None:
        self.clears = 0
        self.audio_chunks = 0
        self.marks = 0

    async def send_clear(self) -> None:
        self.clears += 1

    async def send_audio(self, data: bytes) -> None:  # pragma: no cover
        self.audio_chunks += 1

    async def send_mark(self, mark: str) -> None:  # pragma: no cover
        self.marks += 1


def _make_handler(
    adapter: _FakeAdapter,
    *,
    metrics: CallMetricsAccumulator | None = None,
    on_transcript=None,
    on_metrics=None,
    on_transcript_line=None,
    audio_sender: _FakeAudioSender | None = None,
):
    """Construct a real handler with the minimal real state ``_forward_events``
    touches, bypassing the network-bound constructor (mirrors
    ``test_realtime_response_decoupling``)."""
    handler = OpenAIRealtimeStreamHandler.__new__(OpenAIRealtimeStreamHandler)
    handler._adapter = adapter
    handler.metrics = metrics
    handler.on_transcript = on_transcript
    handler.on_transcript_line = on_transcript_line
    handler.on_metrics = on_metrics
    handler.call_id = "CA0000000000000000000000000000a001"
    handler.caller = "+15551230000"
    handler.callee = "+15551239999"
    handler.conversation_history = deque(maxlen=200)
    handler.transcript_entries = deque(maxlen=200)
    handler.speech_events = None  # _emit_* helpers no-op when this is None
    handler._user_speech_start_ms = None
    handler._agent_turn_start_ms = None
    handler._user_transcript_pending = False
    handler._pending_assistant_turn = None
    handler._pending_assistant_timer = None
    handler._current_turn_index = None
    handler.audio_sender = audio_sender
    handler.agent = type("_A", (), {"model": "gpt-realtime", "tools": None})()
    return handler


def _make_metrics() -> CallMetricsAccumulator:
    return CallMetricsAccumulator(
        call_id="CA0000000000000000000000000000a001",
        provider_mode="openai_realtime",
        telephony_provider="twilio",
        realtime_model="gpt-realtime",
    )


# ---------------------------------------------------------------------------
# FIX-1 — flush buffered assistant turn on hallucination-drop
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestFlushOnHallucinationDrop:
    async def test_buffered_assistant_flushes_immediately_when_transcript_dropped(
        self,
    ) -> None:
        """``response_done`` buffers the assistant reply waiting for the user
        transcript; when that transcript is a dropped hallucination the reply
        must flush right away (not wait for the ~3 s fallback timer)."""
        seen: list[dict] = []

        async def on_transcript(payload: dict) -> None:
            seen.append(payload)

        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_output", "Sure, I can help with that."),
                ("response_done", {}),
                # Whisper hallucination on the trailing silence/echo.
                ("transcript_input", "Thank you for watching."),
            ]
        )
        metrics = _make_metrics()
        handler = _make_handler(adapter, metrics=metrics, on_transcript=on_transcript)

        await handler._forward_events()

        # The assistant reply was surfaced (flushed) ...
        assistant_lines = [p for p in seen if p["role"] == "assistant"]
        assert len(assistant_lines) == 1
        assert assistant_lines[0]["text"] == "Sure, I can help with that."
        # ... and no buffered turn / timer is left dangling.
        assert handler._pending_assistant_turn is None
        assert handler._pending_assistant_timer is None
        # The dropped hallucination never reached the displayed transcript.
        assert all(p["role"] != "user" for p in seen)
        # The assistant turn was recorded exactly once.
        assert len(metrics._turns) == 1

    async def test_no_buffered_turn_drop_is_safe_noop(self) -> None:
        """A dropped hallucination with no buffered assistant turn must be a
        clean no-op (no crash, nothing flushed)."""
        seen: list[dict] = []

        async def on_transcript(payload: dict) -> None:
            seen.append(payload)

        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_input", "Thanks for watching!"),
            ]
        )
        handler = _make_handler(
            adapter, metrics=_make_metrics(), on_transcript=on_transcript
        )

        await handler._forward_events()

        assert seen == []
        assert handler._pending_assistant_turn is None


# ---------------------------------------------------------------------------
# Issue #154 — barge-in on the Realtime interrupt path
#
# DEFAULT (server-managed, gate=False): the server owns the cancel via
# interrupt_response=true. The WebSocket client does send_clear + truncate_playback
# only — NO response.cancel, NO MIN_AGENT_SPEAKING gate, NO record_bargein /
# anchor (the engine turn stays anchored at speech_stopped).
#
# LEGACY (client-managed, gate=True): the full client-side barge-in — anti-flicker
# gate, cancel_response (truncate + response.cancel), and FIX-3 barge-in metrics.
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestRealtimeBargeInServerManaged:
    async def test_default_barge_in_truncates_without_cancel_or_metrics(self) -> None:
        """DEFAULT (gate=False, server-managed): on ``speech_started`` the
        WebSocket client does ``send_clear`` + ``truncate_playback`` ONLY.
        It must NOT call ``cancel_response`` (the server cancels via
        interrupt_response=true), must NOT stamp ``record_bargein_detected``,
        and must NOT re-anchor the turn to user-speech-start."""
        import time as _time

        adapter = _FakeAdapter(events=[("speech_started", None)], gate=False)
        # A response is in flight (used only as a proxy; the server-managed
        # path does NOT consult the anti-flicker gate, so this never suppresses).
        adapter._current_response_first_audio_at = _time.monotonic() - 10.0
        metrics = _make_metrics()
        metrics.start_turn()
        audio_sender = _FakeAudioSender()
        handler = _make_handler(adapter, metrics=metrics, audio_sender=audio_sender)

        await handler._forward_events()

        # WebSocket-only bookkeeping ran: buffer clear + truncate.
        assert audio_sender.clears == 1
        assert adapter.truncated == 1
        # The server owns the cancel — Patter must not send response.cancel.
        assert adapter.cancelled == 0
        # No barge-in detection stamped on the server-managed path: the turn
        # stays anchored at speech_stopped (re-anchoring inflated total_ms).
        assert metrics._last_bargein_at is None

    async def test_default_barge_in_not_suppressed_by_early_response(self) -> None:
        """The MIN_AGENT_SPEAKING anti-flicker gate is LEGACY-only. On the
        server-managed default path an early barge-in (response speaking
        < 500 ms) must STILL clear + truncate — it is not suppressed."""
        import time as _time

        adapter = _FakeAdapter(events=[("speech_started", None)], gate=False)
        # Response only just started (< MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_NO_AEC).
        adapter._current_response_first_audio_at = _time.monotonic()
        metrics = _make_metrics()
        metrics.start_turn()
        audio_sender = _FakeAudioSender()
        handler = _make_handler(adapter, metrics=metrics, audio_sender=audio_sender)

        await handler._forward_events()

        # Not suppressed: the WebSocket-only bookkeeping ran.
        assert audio_sender.clears == 1
        assert adapter.truncated == 1
        assert adapter.cancelled == 0


@pytest.mark.mocked
class TestRealtimeBargeInLegacyOptOut:
    async def test_speech_started_stamps_bargein_and_anchors(self) -> None:
        """LEGACY opt-out (gate=True): a barge-in (``speech_started`` while the
        agent is past the anti-flicker gate) must record a barge-in detection
        and re-anchor the turn, and the cancel sequence (audio clear + adapter
        ``cancel_response``) must run. ``truncate_playback`` is NOT called
        directly — ``cancel_response`` does the truncate itself."""
        import time as _time

        adapter = _FakeAdapter(events=[("speech_started", None)], gate=True)
        # Mark a response as having been speaking long enough to clear the
        # anti-flicker gate so the barge-in is NOT suppressed. Same monotonic
        # clock the handler compares against.
        adapter._current_response_first_audio_at = _time.monotonic() - 10.0
        metrics = _make_metrics()
        metrics.start_turn()
        audio_sender = _FakeAudioSender()
        handler = _make_handler(adapter, metrics=metrics, audio_sender=audio_sender)

        await handler._forward_events()

        # Cancel sequence ran (full cancel, not the truncate-only path).
        assert audio_sender.clears == 1
        assert adapter.cancelled == 1
        assert adapter.truncated == 0
        # Barge-in detection was stamped — ``_last_bargein_at`` is the durable
        # signal that arms the post-barge-in hygiene gate. (``record_turn_
        # interrupted`` runs immediately after and resets the per-turn
        # ``_bargein_detected_at``, but ``_last_bargein_at`` deliberately
        # survives the reset.)
        assert metrics._last_bargein_at is not None

    async def test_early_response_suppresses_legacy_barge_in(self) -> None:
        """LEGACY opt-out (gate=True): an early barge-in (response speaking
        < MIN_AGENT_SPEAKING) is suppressed by the anti-flicker gate — no
        clear, no cancel, no metrics."""
        import time as _time

        adapter = _FakeAdapter(events=[("speech_started", None)], gate=True)
        # Response just started — inside the anti-flicker window.
        adapter._current_response_first_audio_at = _time.monotonic()
        metrics = _make_metrics()
        metrics.start_turn()
        audio_sender = _FakeAudioSender()
        handler = _make_handler(adapter, metrics=metrics, audio_sender=audio_sender)

        await handler._forward_events()

        # Barge-in suppressed: nothing fired.
        assert audio_sender.clears == 0
        assert adapter.cancelled == 0
        assert adapter.truncated == 0
        assert metrics._last_bargein_at is None

    async def test_post_bargein_turn_within_100ms_drops_endpoint_and_stt(
        self,
    ) -> None:
        """After a Realtime barge-in, a turn that starts within 100 ms must
        report endpoint_ms None and stt_ms 0 (post-barge-in hygiene gate)."""
        metrics = _make_metrics()
        # Simulate the barge-in stamping then a fresh turn anchored immediately.
        metrics.record_bargein_detected()
        metrics.anchor_user_speech_start()  # opens a new turn at ~now
        # STT completes on the polluted turn.
        metrics.record_stt_complete("hello", audio_seconds=1.0)
        turn = metrics.record_turn_complete("Hi there!")

        assert turn is not None
        assert turn.latency.endpoint_ms is None
        assert turn.latency.stt_ms == 0


# ---------------------------------------------------------------------------
# FIX-4 — surface Realtime / ConvAI error events
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestRealtimeErrorEvent:
    async def test_error_frame_logs_warning_and_does_not_crash(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _FakeAdapter(
            events=[
                (
                    "error",
                    {
                        "type": "invalid_request_error",
                        "code": "session_expired",
                        "message": "Your session has expired.",
                    },
                ),
                # A normal event after the error proves the loop kept running.
                ("speech_stopped", None),
            ]
        )
        metrics = _make_metrics()
        handler = _make_handler(adapter, metrics=metrics)

        with caplog.at_level(logging.WARNING, logger="getpatter"):
            await handler._forward_events()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("error event" in r.getMessage() for r in warnings)
        msg = " ".join(r.getMessage() for r in warnings)
        assert "session_expired" in msg
        assert "Your session has expired." in msg
        # The loop continued past the error (speech_stopped reserved an index).
        assert handler._current_turn_index == 0

    async def test_error_frame_with_nested_error_wrapper(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An ``{"error": {...}}`` wrapper shape is unwrapped for logging."""
        adapter = _FakeAdapter(
            events=[
                (
                    "error",
                    {"error": {"type": "server_error", "code": "rate_limited"}},
                ),
            ]
        )
        handler = _make_handler(adapter, metrics=_make_metrics())

        with caplog.at_level(logging.WARNING, logger="getpatter"):
            await handler._forward_events()

        msg = " ".join(
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        )
        assert "rate_limited" in msg


# ---------------------------------------------------------------------------
# FIX-5 — reserved turn index + live transcript lines
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestReservedTurnIndexAndLiveLines:
    async def test_reserve_turn_index_is_monotonic(self) -> None:
        metrics = _make_metrics()
        assert metrics.reserve_turn_index() == 0
        assert metrics.reserve_turn_index() == 1
        assert metrics.reserve_turn_index() == 2

    async def test_record_turn_complete_uses_pre_reserved_index(self) -> None:
        metrics = _make_metrics()
        # First completed turn but reserved index 5 (e.g. earlier turns were
        # dropped). turn_index must follow the reserved value, not len(turns).
        metrics.start_turn()
        turn = metrics.record_turn_complete("Reply", pre_reserved_index=5)
        assert turn is not None
        assert turn.turn_index == 5

    async def test_record_turn_complete_defaults_to_len_turns(self) -> None:
        """Back-compat: callers that never reserve get the legacy
        len(self._turns) index."""
        metrics = _make_metrics()
        metrics.start_turn()
        first = metrics.record_turn_complete("A")
        metrics.start_turn()
        second = metrics.record_turn_complete("B")
        assert first is not None and second is not None
        assert first.turn_index == 0
        assert second.turn_index == 1

    async def test_live_lines_carry_reserved_turn_index(self) -> None:
        """The live user line (transcript_input) and the live assistant line
        (flush) must both fire ``on_transcript_line`` with the SAME reserved
        turn index so the dashboard can order them by (turn_index, role)."""
        lines: list[dict] = []

        async def on_transcript_line(payload: dict) -> None:
            lines.append(payload)

        # Realistic Realtime ordering: the model response completes (and is
        # buffered while the user transcript is pending) BEFORE the slower
        # Whisper ``transcript_input`` arrives and triggers the flush.
        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_output", "Hello, how can I help?"),
                ("response_done", {}),
                ("transcript_input", "I'd like to book a table"),
            ]
        )
        metrics = _make_metrics()
        handler = _make_handler(
            adapter, metrics=metrics, on_transcript_line=on_transcript_line
        )

        await handler._forward_events()

        user_lines = [p for p in lines if p["role"] == "user"]
        assistant_lines = [p for p in lines if p["role"] == "assistant"]
        assert len(user_lines) == 1
        assert len(assistant_lines) == 1
        # Live-line payload shape: {call_id, turnIndex, role, text}.
        assert user_lines[0]["text"] == "I'd like to book a table"
        assert assistant_lines[0]["text"] == "Hello, how can I help?"
        assert user_lines[0]["call_id"] == handler.call_id
        # Both lines carry the reserved index 0 from the single turn.
        assert user_lines[0]["turnIndex"] == 0
        assert assistant_lines[0]["turnIndex"] == 0
        # The recorded turn carries the same stable index.
        assert metrics._turns[0].turn_index == 0

    async def test_reserved_index_advances_across_turns(self) -> None:
        lines: list[dict] = []

        async def on_transcript_line(payload: dict) -> None:
            lines.append(payload)

        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_output", "First reply"),
                ("response_done", {}),
                ("transcript_input", "first user line"),
                ("speech_stopped", None),
                ("transcript_output", "Second reply"),
                ("response_done", {}),
                ("transcript_input", "second user line"),
            ]
        )
        metrics = _make_metrics()
        handler = _make_handler(
            adapter, metrics=metrics, on_transcript_line=on_transcript_line
        )

        await handler._forward_events()

        user_lines = [p for p in lines if p["role"] == "user"]
        assistant_lines = [p for p in lines if p["role"] == "assistant"]
        assert [p["turnIndex"] for p in user_lines] == [0, 1]
        assert [p["turnIndex"] for p in assistant_lines] == [0, 1]
        assert [t.turn_index for t in metrics._turns] == [0, 1]

    async def test_live_lines_no_callback_is_safe(self) -> None:
        """With no ``on_transcript_line`` wired the handler must still record
        turns — the live-line emission simply no-ops (back-compat)."""
        adapter = _FakeAdapter(
            events=[
                ("speech_stopped", None),
                ("transcript_output", "Reply"),
                ("response_done", {}),
                ("transcript_input", "a user line"),
            ]
        )
        metrics = _make_metrics()
        handler = _make_handler(adapter, metrics=metrics)  # on_transcript_line=None

        await handler._forward_events()

        assert [t.turn_index for t in metrics._turns] == [0]
