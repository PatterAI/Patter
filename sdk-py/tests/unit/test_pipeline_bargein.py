"""Unit tests for PipelineStreamHandler barge-in detection (BUG #20).

During TTS playback the STT loop must treat *any* transcript with text —
interim or final — as a barge-in signal. When that happens the handler:

  1. Logs the event.
  2. Flips ``_is_speaking`` to False so the in-flight sentence loop
     breaks out (see ``_synthesize_sentence``).
  3. Calls ``audio_sender.send_clear()`` to drop any frames the
     telephony bridge has already buffered.
  4. Records the interrupted turn on the metrics accumulator.

Without this, the caller has to wait ~1.5 s for a long TTS reply to
finish before the agent even notices the interruption, and earlier
versions of the SDK effectively broke barge-in entirely because the
interim branch was a ``continue`` before the ``is_final`` gate.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator, Iterable
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.handlers.stream_handler import PipelineStreamHandler
from getpatter.providers.base import Transcript

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Shared fixture — stub STT that yields a caller-supplied sequence
# ---------------------------------------------------------------------------


class _StubSTT:
    def __init__(self, transcripts: Iterable[Transcript]) -> None:
        self._transcripts = list(transcripts)

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        for t in self._transcripts:
            yield t
        await asyncio.sleep(0)


def _make_handler(
    stt: _StubSTT,
    audio_sender: AsyncMock,
    metrics: MagicMock | None,
    *,
    speaking: bool,
) -> PipelineStreamHandler:
    handler = PipelineStreamHandler(
        agent=make_agent(),
        audio_sender=audio_sender,
        call_id="call-barge",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=metrics,
        for_twilio=True,
        on_transcript=None,
        conversation_history=deque(maxlen=10),
        transcript_entries=deque(maxlen=10),
    )
    handler._stt = stt  # type: ignore[assignment]
    handler.on_message = None
    handler._llm_loop = None
    handler._is_speaking = speaking
    return handler


# ---------------------------------------------------------------------------
# Interim transcript during TTS → barge-in triggers
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestInterimBargeIn:
    """An interim transcript during TTS must interrupt the agent."""

    async def test_interim_during_tts_calls_send_clear(self) -> None:
        stt = _StubSTT([Transcript(text="wait", is_final=False, confidence=0.4)])
        audio_sender = AsyncMock()
        handler = _make_handler(stt, audio_sender, metrics=None, speaking=True)

        await asyncio.wait_for(handler._stt_loop(), timeout=2.0)

        audio_sender.send_clear.assert_awaited_once()

    async def test_interim_during_tts_flips_is_speaking(self) -> None:
        stt = _StubSTT([Transcript(text="stop", is_final=False, confidence=0.3)])
        audio_sender = AsyncMock()
        handler = _make_handler(stt, audio_sender, metrics=None, speaking=True)

        await asyncio.wait_for(handler._stt_loop(), timeout=2.0)

        assert handler._is_speaking is False

    async def test_interim_during_tts_records_turn_interrupted(self) -> None:
        stt = _StubSTT([Transcript(text="hey", is_final=False, confidence=0.3)])
        audio_sender = AsyncMock()
        metrics = MagicMock()
        handler = _make_handler(
            stt, audio_sender, metrics=metrics, speaking=True
        )

        await asyncio.wait_for(handler._stt_loop(), timeout=2.0)

        metrics.record_turn_interrupted.assert_called_once()

    async def test_interim_barge_in_survives_send_clear_exception(self) -> None:
        """A broken audio_sender must not crash the STT loop."""
        stt = _StubSTT([Transcript(text="hi", is_final=False, confidence=0.3)])
        audio_sender = AsyncMock()
        audio_sender.send_clear.side_effect = RuntimeError("socket closed")
        handler = _make_handler(stt, audio_sender, metrics=None, speaking=True)

        # Must not raise.
        await asyncio.wait_for(handler._stt_loop(), timeout=2.0)

        assert handler._is_speaking is False


# ---------------------------------------------------------------------------
# No barge-in when the agent is NOT speaking
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestNoBargeInWhenIdle:
    """When ``_is_speaking`` is already False the barge-in branch is skipped."""

    async def test_interim_while_idle_does_not_call_send_clear(self) -> None:
        stt = _StubSTT([Transcript(text="hello", is_final=False, confidence=0.3)])
        audio_sender = AsyncMock()
        handler = _make_handler(stt, audio_sender, metrics=None, speaking=False)

        await asyncio.wait_for(handler._stt_loop(), timeout=2.0)

        audio_sender.send_clear.assert_not_awaited()

    async def test_empty_interim_while_speaking_does_not_bargein(self) -> None:
        """Transcript without text must not fire barge-in even during TTS."""
        stt = _StubSTT([Transcript(text="", is_final=False, confidence=0.0)])
        audio_sender = AsyncMock()
        handler = _make_handler(stt, audio_sender, metrics=None, speaking=True)

        await asyncio.wait_for(handler._stt_loop(), timeout=2.0)

        audio_sender.send_clear.assert_not_awaited()
        assert handler._is_speaking is True


# ---------------------------------------------------------------------------
# Final transcript during TTS → both barge-in AND turn processing fire
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestFinalBargeIn:
    """A final transcript during TTS must trigger barge-in before the turn."""

    async def test_final_during_tts_calls_send_clear(self) -> None:
        stt = _StubSTT(
            [Transcript(text="actually wait", is_final=True, confidence=0.9)]
        )
        audio_sender = AsyncMock()
        handler = _make_handler(stt, audio_sender, metrics=None, speaking=True)

        await asyncio.wait_for(handler._stt_loop(), timeout=2.0)

        audio_sender.send_clear.assert_awaited_once()
        assert handler._is_speaking is False
