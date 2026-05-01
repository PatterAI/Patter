"""Unit tests for PipelineStreamHandler STT dedup/throttle/hallucination filter.

Regression tests for BUG #22 — Whisper on mulaw 8 kHz repeatedly hallucinated
"you" / "." and the pipeline kicked off a new LLM+TTS turn for each, producing
overlapping audio on the caller's line.

The filter lives in :func:`PipelineStreamHandler._stt_loop` and enforces three
rules on final transcripts:

  1. Hallucination blacklist — drop common one-word fillers ("you",
     "thank you", ".", etc.).
  2. 2-second duplicate window — drop if the normalised text matches the
     previous committed text and arrived within 2.0 s.
  3. 500 ms back-to-back throttle — drop any final that lands within
     500 ms of the previous committed turn.

Tests drive the loop via a mocked STT whose ``receive_transcripts`` yields
a caller-controlled async iterator, and assert against the callbacks that
``_stt_loop`` invokes only for transcripts that pass the filter.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator, Iterable
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.stream_handler import PipelineStreamHandler
from getpatter.providers.base import Transcript

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubSTT:
    """Minimal STT double that yields a pre-supplied sequence of Transcripts.

    Supports gap injection between yields via ``delays`` — a parallel list of
    monotonic time offsets applied through the loop's ``time.time`` lookup.
    """

    def __init__(self, transcripts: Iterable[Transcript]) -> None:
        self._transcripts = list(transcripts)

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        for t in self._transcripts:
            yield t
        # Hold the generator open briefly so the loop can complete processing.
        await asyncio.sleep(0)


def _make_handler(stt: _StubSTT, on_transcript: AsyncMock) -> PipelineStreamHandler:
    """Build a PipelineStreamHandler with the stub STT pre-installed.

    The handler is constructed without calling ``start`` — we only exercise
    ``_stt_loop``. The internal ``_llm_loop`` is left ``None`` so the loop
    records ``turn_interrupted`` on the metrics double instead of invoking
    an LLM, which keeps the test focused on the filter behaviour.
    """
    agent = make_agent()
    audio_sender = MagicMock()
    audio_sender.send_clear = AsyncMock()
    audio_sender.send_audio = AsyncMock()
    audio_sender.send_mark = AsyncMock()

    handler = PipelineStreamHandler(
        agent=agent,
        audio_sender=audio_sender,
        call_id="call-test",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="test prompt",
        metrics=None,  # skip metrics bookkeeping for this test
        for_twilio=True,
        on_transcript=on_transcript,
        conversation_history=deque(maxlen=50),
        transcript_entries=deque(maxlen=50),
    )
    handler._stt = stt  # type: ignore[assignment]
    # No LLM / message handler — the loop will call record_turn_interrupted on
    # a None metrics object, which we guard via the `if self.metrics` checks.
    handler.on_message = None
    handler._llm_loop = None
    return handler


async def _run_loop(handler: PipelineStreamHandler) -> None:
    """Drive ``_stt_loop`` until the stub iterator is exhausted."""
    await asyncio.wait_for(handler._stt_loop(), timeout=2.0)


# ---------------------------------------------------------------------------
# Hallucination blacklist
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestHallucinationFilter:
    """Rule 1 — drop common Whisper / Deepgram filler words."""

    async def test_drops_single_word_you(self) -> None:
        stt = _StubSTT([Transcript(text="you", is_final=True, confidence=0.9)])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        on_transcript.assert_not_called()

    async def test_drops_period_only(self) -> None:
        stt = _StubSTT([Transcript(text=".", is_final=True, confidence=0.8)])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        on_transcript.assert_not_called()

    async def test_drops_thank_you_with_punctuation(self) -> None:
        """Trailing punctuation/whitespace is stripped before the blacklist check."""
        stt = _StubSTT(
            [Transcript(text="Thank you.", is_final=True, confidence=0.8)]
        )
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        on_transcript.assert_not_called()

    async def test_drops_case_variants(self) -> None:
        """Normalisation is case-insensitive."""
        stt = _StubSTT([Transcript(text="YOU", is_final=True, confidence=0.9)])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        on_transcript.assert_not_called()

    async def test_drops_empty_after_strip(self) -> None:
        """Transcript of only punctuation/whitespace is treated as empty."""
        stt = _StubSTT([Transcript(text="  ...  ", is_final=True, confidence=0.5)])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        on_transcript.assert_not_called()

    async def test_passes_legitimate_text(self) -> None:
        """A real utterance that is NOT on the blacklist must pass through."""
        stt = _StubSTT(
            [Transcript(text="What's the weather today?", is_final=True, confidence=0.95)]
        )
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        on_transcript.assert_awaited_once()
        args = on_transcript.await_args.args[0]
        assert args["text"] == "What's the weather today?"


# ---------------------------------------------------------------------------
# Duplicate window (2.0 s)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestDuplicateFilter:
    """Rule 2 — drop duplicates within 2.0 s of the last committed turn."""

    async def test_drops_duplicate_within_2s(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two identical legitimate finals separated by 1.0 s (inside the window).
        times = iter([100.0, 101.0])
        monkeypatch.setattr(
            "getpatter.stream_handler.time.time",
            lambda: next(times),
        )
        stt = _StubSTT([
            Transcript(text="Hello there", is_final=True, confidence=0.9),
            Transcript(text="Hello there", is_final=True, confidence=0.9),
        ])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        # Only the first transcript should have been forwarded.
        assert on_transcript.await_count == 1

    async def test_passes_duplicate_after_2s(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same text, but 2.5 s apart — caller legitimately repeating themselves.
        times = iter([100.0, 102.5])
        monkeypatch.setattr(
            "getpatter.stream_handler.time.time",
            lambda: next(times),
        )
        stt = _StubSTT([
            Transcript(text="Hello there", is_final=True, confidence=0.9),
            Transcript(text="Hello there", is_final=True, confidence=0.9),
        ])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        assert on_transcript.await_count == 2

    async def test_duplicate_normalises_whitespace_and_case(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        times = iter([100.0, 100.8])
        monkeypatch.setattr(
            "getpatter.stream_handler.time.time",
            lambda: next(times),
        )
        stt = _StubSTT([
            Transcript(text="Book a table", is_final=True, confidence=0.9),
            Transcript(text="  book a table  ", is_final=True, confidence=0.9),
        ])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        # Second is a duplicate modulo case/whitespace — dropped.
        assert on_transcript.await_count == 1


# ---------------------------------------------------------------------------
# Back-to-back throttle (500 ms)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestThrottleFilter:
    """Rule 3 — drop ANY final that lands within 500 ms of the last turn."""

    async def test_drops_back_to_back_under_500ms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Different text but only 0.2 s apart — treated as STT over-firing.
        times = iter([100.0, 100.2])
        monkeypatch.setattr(
            "getpatter.stream_handler.time.time",
            lambda: next(times),
        )
        stt = _StubSTT([
            Transcript(text="What time is it", is_final=True, confidence=0.9),
            Transcript(text="Tell me the weather", is_final=True, confidence=0.9),
        ])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        # Only the first should have been forwarded.
        assert on_transcript.await_count == 1

    async def test_passes_after_500ms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Different text, 700 ms apart — legitimate second turn.
        times = iter([100.0, 100.7])
        monkeypatch.setattr(
            "getpatter.stream_handler.time.time",
            lambda: next(times),
        )
        stt = _StubSTT([
            Transcript(text="What time is it", is_final=True, confidence=0.9),
            Transcript(text="Tell me the weather", is_final=True, confidence=0.9),
        ])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        assert on_transcript.await_count == 2


# ---------------------------------------------------------------------------
# Interim / non-final transcripts
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestInterimTranscripts:
    """Non-final (interim) transcripts must never fire the turn pipeline."""

    async def test_interim_does_not_fire_on_transcript(self) -> None:
        stt = _StubSTT([
            Transcript(text="Hello", is_final=False, confidence=0.5),
            Transcript(text="Hello world", is_final=False, confidence=0.6),
        ])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        on_transcript.assert_not_called()

    async def test_empty_final_is_ignored(self) -> None:
        """A final transcript with empty text must not fire the pipeline."""
        stt = _StubSTT([Transcript(text="", is_final=True, confidence=0.0)])
        on_transcript = AsyncMock()
        handler = _make_handler(stt, on_transcript)

        await _run_loop(handler)

        on_transcript.assert_not_called()
