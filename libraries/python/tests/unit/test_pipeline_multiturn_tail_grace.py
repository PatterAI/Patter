"""Multi-turn regression tests for the pipeline turn-taking state machine.

Reproduces the live-call failure where the *first* turn works end-to-end but
every *subsequent* turn goes silent, leaving a ghost metrics turn of
``user_text='' agent_text='[interrupted]'``.

Root causes covered here:

1. **Tail-grace misclassification.** After the agent finishes a turn,
   ``_end_speaking_with_grace`` keeps ``_is_speaking=True`` for
   ``PATTER_TTS_TAIL_GRACE_MS`` (default 1500 ms) to swallow the fading TTS
   echo tail. Humans reply in 200-700 ms — well inside that window — so the
   user's next utterance was being mis-detected as a *barge-in*:
   ``record_turn_interrupted`` fired (the ``[interrupted]`` ghost) and the
   leading audio was withheld from STT (only a <=260 ms echo-contaminated
   ring), so no final transcript was produced and the agent never answered.
   The fix treats a VAD ``speech_start`` (or a transcript) during the tail
   grace as the start of a NEW turn, not a barge-in.

2. **Stale ``_llm_cancel_event``.** A real barge-in sets the per-turn cancel
   event; it was only recreated *inside* ``_process_streaming_response`` —
   AFTER ``LLMLoop.run`` had already been handed the (now set) event for the
   next turn. The next turn's LLM stream then bailed immediately. The fix
   recreates the event at the top of ``_dispatch_turn``, before dispatch.

Only the external boundary is mocked (STT/TTS/audio sender). The VAD is a
scripted in-process double so the on_audio_received path runs unmocked.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.providers.base import VADEvent
from getpatter.stream_handler import PipelineStreamHandler

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Scripted in-process VAD — emits a caller-supplied event per frame
# ---------------------------------------------------------------------------


class _ScriptedVAD:
    """Returns the next queued VADEvent (or None) on each ``process_frame``."""

    def __init__(self, events: list[VADEvent | None]) -> None:
        self._events = list(events)
        self.reset_calls = 0

    async def process_frame(self, pcm: bytes, sample_rate: int) -> VADEvent | None:
        if self._events:
            return self._events.pop(0)
        return None

    async def close(self) -> None:  # pragma: no cover - not exercised
        pass

    def reset(self) -> None:
        self.reset_calls += 1


def _make_pipeline_handler(*, metrics: MagicMock | None = None) -> PipelineStreamHandler:
    audio_sender = AsyncMock()
    handler = PipelineStreamHandler(
        agent=make_agent(),
        audio_sender=audio_sender,
        call_id="call-multiturn",
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
    handler._llm_loop = None
    handler._stt = AsyncMock()
    handler._aec = None
    # Treat inbound as already-PCM16 16 kHz so on_audio_received skips the
    # mulaw decode path (the scripted VAD ignores the bytes anyway).
    handler._input_is_mulaw_8k = False
    return handler


def _enter_tail_grace(handler: PipelineStreamHandler) -> None:
    """Put the handler into the post-TTS tail-grace window: the agent has
    finished speaking but ``_is_speaking`` is still held for echo suppression.
    ``_first_audio_sent_at`` is stamped in the past so ``_can_barge_in`` is
    True (the warmup gate elapsed) — exactly the state that produced the bug.
    """
    handler._is_speaking = True
    handler._tail_grace_active = True
    handler._speaking_generation = 1
    handler._speaking_started_at = time.time() - 2.0
    handler._first_audio_sent_at = time.time() - 2.0
    handler._inbound_audio_ring = []


_FRAME = b"\x00\x01" * 160  # arbitrary PCM16 bytes; scripted VAD ignores content


@pytest.mark.unit
@pytest.mark.asyncio
class TestTailGraceNewTurn:
    """Speech during the tail grace is a new turn, not a barge-in."""

    async def test_speech_during_tail_grace_reaches_stt_without_interrupt(self) -> None:
        metrics = MagicMock()
        handler = _make_pipeline_handler(metrics=metrics)
        handler._auto_vad = _ScriptedVAD(
            [None, None, VADEvent(type="speech_start"), None]
        )
        _enter_tail_grace(handler)

        # Two leading frames while still in tail grace → buffered to ring,
        # NOT yet forwarded to STT.
        await handler.on_audio_received(_FRAME)
        await handler.on_audio_received(_FRAME)
        assert handler._stt.send_audio.await_count == 0
        assert len(handler._inbound_audio_ring) == 2

        # VAD speech_start fires → tail grace ends as a NEW TURN.
        await handler.on_audio_received(_FRAME)

        # Not a barge-in: the agent already finished, nothing was interrupted.
        metrics.record_bargein_detected.assert_not_called()
        handler.audio_sender.send_clear.assert_not_awaited()
        assert handler._is_speaking is False
        assert handler._tail_grace_active is False

        # Leading audio recovered (ring flushed) + the trigger frame sent live.
        assert handler._stt.send_audio.await_count >= 3

        # A following frame now streams straight through to STT.
        await handler.on_audio_received(_FRAME)
        assert handler._stt.send_audio.await_count >= 4

    async def test_active_tts_speech_still_barges_in(self) -> None:
        """Regression guard: speech during *active* TTS (not tail grace) must
        still trigger a real barge-in."""
        metrics = MagicMock()
        handler = _make_pipeline_handler(metrics=metrics)
        handler._auto_vad = _ScriptedVAD([VADEvent(type="speech_start")])
        # Active TTS: speaking, but NOT in the post-completion tail grace.
        handler._is_speaking = True
        handler._tail_grace_active = False
        handler._speaking_generation = 1
        handler._speaking_started_at = time.time() - 2.0
        handler._first_audio_sent_at = time.time() - 2.0
        handler._inbound_audio_ring = []

        await handler.on_audio_received(_FRAME)

        metrics.record_bargein_detected.assert_called_once()
        handler.audio_sender.send_clear.assert_awaited_once()
        assert handler._is_speaking is False


@pytest.mark.unit
@pytest.mark.asyncio
class TestTailGraceFlagLifecycle:
    """``_tail_grace_active`` tracks the post-TTS grace window precisely."""

    async def test_begin_speaking_clears_flag(self) -> None:
        handler = _make_pipeline_handler()
        handler._tail_grace_active = True
        await handler._begin_speaking()
        assert handler._is_speaking is True
        assert handler._tail_grace_active is False

    async def test_grace_sets_then_clears_flag(self, monkeypatch) -> None:
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "20")
        handler = _make_pipeline_handler()
        await handler._begin_speaking()
        handler._first_audio_sent_at = time.time() - 1.0

        await handler._end_speaking_with_grace()
        # Grace pending: still "speaking" but flagged as tail grace.
        assert handler._is_speaking is True
        assert handler._tail_grace_active is True

        await asyncio.sleep(0.06)  # > 20 ms grace
        assert handler._is_speaking is False
        assert handler._tail_grace_active is False

    async def test_zero_grace_does_not_enter_tail_grace(self, monkeypatch) -> None:
        monkeypatch.setenv("PATTER_TTS_TAIL_GRACE_MS", "0")
        handler = _make_pipeline_handler()
        await handler._begin_speaking()
        await handler._end_speaking_with_grace()
        assert handler._is_speaking is False
        assert handler._tail_grace_active is False


@pytest.mark.unit
@pytest.mark.asyncio
class TestLlmCancelEventReset:
    """A barge-in's cancel event must not leak into the next turn's dispatch."""

    async def test_dispatch_uses_fresh_cancel_event(self) -> None:
        handler = _make_pipeline_handler()

        captured: dict = {}

        async def _fake_stream():
            yield "hello "

        class _FakeLoop:
            def run(self, text, history, ctx, *, cancel_event=None, **kwargs):
                # Record whether the event handed to the LLM was already set
                # (i.e. leaked from a previous turn's barge-in).
                captured["was_set"] = bool(cancel_event and cancel_event.is_set())
                return _fake_stream()

        handler._llm_loop = _FakeLoop()

        # Avoid the real TTS speak path: stub the response processor.
        async def _fake_process(result, call_id):
            # Drain the generator so the loop's run() actually executed.
            async for _ in result:
                pass
            return "hello"

        handler._process_streaming_response = _fake_process  # type: ignore[assignment]
        handler._emit_assistant_transcript = AsyncMock()

        # Simulate a stale cancel left set by a previous turn's barge-in.
        handler._llm_cancel_event.set()
        assert handler._llm_cancel_event.is_set() is True

        await handler._dispatch_turn("dimmi che ore sono")

        assert captured.get("was_set") is False
