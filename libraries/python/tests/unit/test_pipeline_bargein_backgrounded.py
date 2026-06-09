"""Backgrounded-dispatch barge-in: the STT receive loop keeps draining
transcripts during a long agent-runtime turn, so a barge-in (transcript OR
VAD) can interrupt the LIVE turn instead of being processed only after it ends.

Reproduces the live Hermes failure: while the assistant was speaking, the
caller said "ferma" but Patter only reacted after the turn finished — because
``_stt_loop`` awaited ``_dispatch_turn`` inline and stopped reading transcripts
for the whole (30-90 s) turn. Covers:

* the decoupled dispatch + transcript barge-in cancelling the in-flight turn;
* the VAD legacy branch now setting ``_llm_cancel_event`` (pre-first-token
  teardown parity with TS);
* the opt-in ``PATTER_FORWARD_STT_WHILE_SPEAKING`` guard.

Only the external boundary (LLM stream timing, TTS bytes, STT) is faked.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.providers.base import Transcript, VADEvent
from getpatter.stream_handler import PipelineStreamHandler

from tests.conftest import make_agent


class _FakeTTS:
    output_format = "pcm_16000"

    def __init__(self) -> None:
        self.synthesized: list[str] = []

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        yield b"\x00\x00" * 80


class _ScriptedVAD:
    def __init__(self, events: list[VADEvent | None]) -> None:
        self._events = list(events)

    async def process_frame(self, pcm: bytes, sample_rate: int) -> VADEvent | None:
        return self._events.pop(0) if self._events else None

    async def close(self) -> None:  # pragma: no cover
        pass

    def reset(self) -> None:
        pass


def _make_handler(*, metrics: MagicMock | None = None) -> PipelineStreamHandler:
    handler = PipelineStreamHandler(
        agent=make_agent(),
        audio_sender=AsyncMock(),
        call_id="call-bg",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=metrics,
        for_twilio=True,
        on_transcript=None,
        conversation_history=deque(maxlen=20),
        transcript_entries=deque(maxlen=20),
    )
    handler.on_message = None
    handler._tts = _FakeTTS()  # type: ignore[assignment]
    handler._stt = AsyncMock()
    handler._aec = None
    handler._input_is_mulaw_8k = False
    return handler


_FRAME = b"\x00\x01" * 160


@pytest.mark.unit
@pytest.mark.asyncio
class TestTranscriptBargeInDuringInFlightTurn:
    """A barge-in transcript cancels the LIVE turn — proving the receive loop
    is no longer blocked on dispatch."""

    async def test_bargein_transcript_cancels_inflight_long_turn(self) -> None:
        metrics = MagicMock()
        handler = _make_handler(metrics=metrics)
        # Past the warmup gate so barge-in is allowed.
        handler._can_barge_in = lambda: True  # type: ignore[assignment]

        cancel_seen = asyncio.Event()

        class _ParkUntilCancelLoop:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, text, history, ctx, *, cancel_event=None, **kw):
                self.calls += 1
                first = self.calls == 1

                async def _gen():
                    if first:
                        # Long turn: only ends when the barge-in sets cancel.
                        while cancel_event is None or not cancel_event.is_set():
                            await asyncio.sleep(0.005)
                        cancel_seen.set()
                        return
                    yield "ok "  # any later turn replies quickly

                return _gen()

        handler._llm_loop = _ParkUntilCancelLoop()

        class _STT:
            async def receive_transcripts(self) -> AsyncIterator[Transcript]:
                yield Transcript(text="dimmi una storia", is_final=True, confidence=0.9)
                # Let turn 1 begin_speaking + park on its (long) LLM stream.
                await asyncio.sleep(0.08)
                # Caller barges in WHILE the agent turn is in flight.
                yield Transcript(text="ferma per favore", is_final=True, confidence=0.9)
                await asyncio.sleep(0.08)

        handler._stt = _STT()  # type: ignore[assignment]

        await asyncio.wait_for(handler._stt_loop(), timeout=3.0)

        # The barge-in fired DURING turn 1 (the loop kept reading transcripts):
        handler.audio_sender.send_clear.assert_awaited()
        metrics.record_bargein_detected.assert_called()
        # Turn 1's LLM stream observed the cancel (it was torn down, not left
        # running until the next turn).
        assert cancel_seen.is_set()
        # The REAL follow-up "ferma per favore" (different text, arriving <0.5s
        # after turn 1) is NOT swallowed by the back-to-back dedup — it
        # dispatches as a fresh turn (calls==2). Before the dedup fix it was
        # dropped, leaving an empty [interrupted] turn and no reply.
        assert handler._llm_loop.calls >= 2


@pytest.mark.unit
@pytest.mark.asyncio
class TestVadLegacyBranchSetsCancelEvent:
    """A VAD-only barge-in during TTS tears down the LLM stream (layer 2a)."""

    async def test_vad_speech_start_sets_llm_cancel_event(self) -> None:
        metrics = MagicMock()
        handler = _make_handler(metrics=metrics)
        handler._auto_vad = _ScriptedVAD([VADEvent(type="speech_start")])
        handler._is_speaking = True
        handler._tail_grace_active = False
        handler._speaking_generation = 1
        handler._speaking_started_at = time.time() - 2.0
        handler._first_audio_sent_at = time.time() - 2.0
        handler._inbound_audio_ring = []
        assert handler._llm_cancel_event.is_set() is False

        await handler.on_audio_received(_FRAME)

        # Real barge-in cancel ran AND the LLM stream cancel was signalled
        # (previously only `_is_speaking` flipped, which Hermes never observed
        # pre-first-token).
        metrics.record_bargein_detected.assert_called_once()
        assert handler._is_speaking is False
        assert handler._llm_cancel_event.is_set() is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestForwardSttWhileSpeakingFlag:
    """``PATTER_FORWARD_STT_WHILE_SPEAKING`` gates audio-to-STT during TTS."""

    async def test_flag_off_buffers_and_returns(self, monkeypatch) -> None:
        monkeypatch.delenv("PATTER_FORWARD_STT_WHILE_SPEAKING", raising=False)
        handler = _make_handler()
        handler._auto_vad = _ScriptedVAD([None, None])
        handler._stt = AsyncMock()
        handler._is_speaking = True
        handler._tail_grace_active = False
        handler._inbound_audio_ring = []

        await handler.on_audio_received(_FRAME)
        await handler.on_audio_received(_FRAME)

        # Default: audio withheld from STT during TTS, only ring-buffered.
        assert handler._stt.send_audio.await_count == 0
        assert len(handler._inbound_audio_ring) == 2

    async def test_flag_on_forwards_to_stt_during_tts(self, monkeypatch) -> None:
        monkeypatch.setenv("PATTER_FORWARD_STT_WHILE_SPEAKING", "1")
        handler = _make_handler()
        assert handler._forward_stt_while_speaking is True
        handler._auto_vad = _ScriptedVAD([None, None])
        handler._stt = AsyncMock()
        handler._is_speaking = True
        handler._tail_grace_active = False
        handler._inbound_audio_ring = []

        await handler.on_audio_received(_FRAME)
        await handler.on_audio_received(_FRAME)

        # Flag on: audio ALSO reaches STT during TTS (so the transcript barge-in
        # path can fire on echo-masked links) AND the ring still captures the
        # leading edge for flush-on-barge-in.
        assert handler._stt.send_audio.await_count == 2
        assert len(handler._inbound_audio_ring) == 2


class _PassthroughAEC:
    """Minimal AEC stand-in: marks the link as AEC-protected without altering audio."""

    def process_near_end(self, pcm: bytes) -> bytes:
        return pcm


@pytest.mark.unit
@pytest.mark.asyncio
class TestForwardSttDeferredBargeIn:
    """On a forward-STT link WITHOUT AEC, a VAD ``speech_start`` during TTS is
    very often the agent's own echo. Cancelling on raw VAD energy self-interrupts
    almost every turn (live Hermes "bene bene" → [interrupted] cascade). The fix
    defers the cancel to a transcript that survives the echo guard.
    """

    def _speaking_handler(self, *, forward: bool, aec) -> PipelineStreamHandler:
        handler = _make_handler()
        handler._forward_stt_while_speaking = forward
        handler._aec = aec
        handler._barge_in_strategies = ()
        handler._auto_vad = _ScriptedVAD([VADEvent(type="speech_start")])
        handler._stt = AsyncMock()
        handler._is_speaking = True
        handler._tail_grace_active = False
        handler._speaking_generation = 1
        handler._speaking_started_at = time.time() - 2.0
        handler._first_audio_sent_at = time.time() - 2.0
        handler._inbound_audio_ring = []
        handler._can_barge_in = lambda: True  # type: ignore[assignment]
        return handler

    async def test_no_aec_no_strategies_defers_to_pending(self) -> None:
        """forward-STT + no AEC + no strategies → VAD speech_start goes PENDING,
        does NOT cancel: the agent keeps talking until a real transcript confirms."""
        handler = self._speaking_handler(forward=True, aec=None)

        await handler.on_audio_received(_FRAME)

        # Deferred: no cancel, agent still owns the floor, LLM stream untouched.
        assert handler._is_speaking is True
        assert handler._barge_in_pending_since is not None
        handler.audio_sender.send_clear.assert_not_called()
        assert handler._llm_cancel_event.is_set() is False
        # Clean up the pending-timeout task so it doesn't outlive the test.
        handler._clear_pending_barge_in()

    async def test_with_aec_still_immediate_cancel(self) -> None:
        """forward-STT + AEC ON → the canceller makes VAD trustworthy, so the
        legacy immediate cancel is preserved (responsive barge-in)."""
        handler = self._speaking_handler(forward=True, aec=_PassthroughAEC())

        await handler.on_audio_received(_FRAME)

        assert handler._is_speaking is False
        assert handler._barge_in_pending_since is None
        handler.audio_sender.send_clear.assert_awaited()
        assert handler._llm_cancel_event.is_set() is True

    async def test_pending_then_real_transcript_cancels(self) -> None:
        """After a deferred (pending) VAD barge-in, a real (non-echo) transcript
        confirms the cancel via the echo-guarded transcript path."""
        handler = self._speaking_handler(forward=True, aec=None)
        handler._current_agent_spoken_text = "sto bene grazie e tu come stai oggi"

        await handler.on_audio_received(_FRAME)
        assert handler._barge_in_pending_since is not None  # pending

        # A genuinely different caller utterance (not the agent's own words).
        await handler._handle_barge_in(
            Transcript(
                text="fermati e dimmi solo questo", is_final=True, confidence=0.9
            )
        )

        assert handler._is_speaking is False
        assert handler._barge_in_pending_since is None  # pending cleared on confirm
        handler.audio_sender.send_clear.assert_awaited()
        assert handler._llm_cancel_event.is_set() is True

    async def test_pending_then_echo_transcript_does_not_cancel(self) -> None:
        """An echo transcript (the agent's own forwarded TTS) must NOT confirm
        the pending barge-in — the agent keeps talking."""
        handler = self._speaking_handler(forward=True, aec=None)
        handler._current_agent_spoken_text = "sto bene grazie e tu come stai oggi"

        await handler.on_audio_received(_FRAME)
        assert handler._barge_in_pending_since is not None  # pending

        # Transcript is a fragment of what the agent is currently saying.
        await handler._handle_barge_in(
            Transcript(
                text="sto bene grazie e tu come stai", is_final=True, confidence=0.9
            )
        )

        # Echo guard dropped it — no cancel, still pending, agent still speaking.
        assert handler._is_speaking is True
        handler.audio_sender.send_clear.assert_not_called()
        assert handler._llm_cancel_event.is_set() is False
        handler._clear_pending_barge_in()
