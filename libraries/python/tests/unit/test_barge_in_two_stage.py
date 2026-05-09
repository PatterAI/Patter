"""Two-stage barge-in regression tests.

Cover the wiring between :class:`PipelineStreamHandler` and the opt-in
``barge_in_strategies`` confirmation pipeline:

* a sub-threshold transcript while the agent is speaking does NOT cancel
  the agent (legacy behaviour cancelled unconditionally on any
  transcript while ``_is_speaking``);
* a transcript that meets the strategy's threshold confirms the
  barge-in and runs the cancel path (LLM cancel event set, sendClear
  fired, ``_is_speaking`` flipped to False);
* the pending timeout drops the pending state and emits an
  ``overlap_end(was_interruption=False)`` metric.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.providers.base import Transcript
from getpatter.services.barge_in_strategies import MinWordsStrategy
from getpatter.stream_handler import PipelineStreamHandler


def _make_handler(*, strategies, confirm_s: float = 0.05) -> PipelineStreamHandler:
    """Build a minimally-wired handler suitable for unit-testing the
    barge-in decision path. Uses ``object.__new__`` to skip the heavy
    __init__ and only sets the fields the barge-in path actually
    touches."""
    h = object.__new__(PipelineStreamHandler)
    h._is_speaking = True
    h._aec = None  # PSTN default — anti-flicker gate
    h._speaking_started_at = time.time() - 1.0  # past the 0.25 s gate
    h._first_audio_sent_at = time.time() - 1.0  # post first byte
    h._speaking_generation = 0
    h._last_cancel_at = None
    h._llm_cancel_event = asyncio.Event()
    h.metrics = MagicMock()
    h.metrics.record_overlap_start = MagicMock()
    h.metrics.record_overlap_end = MagicMock()
    h.metrics.record_bargein_detected = MagicMock()
    h.metrics.record_tts_stopped = MagicMock()
    h.metrics.record_turn_interrupted = MagicMock()
    h.call_id = "test-call"
    h.audio_sender = MagicMock()
    h.audio_sender.send_clear = AsyncMock()
    h._inbound_audio_ring = []
    h._stt = None
    h._barge_in_strategies = tuple(strategies)
    h._barge_in_confirm_s = confirm_s
    h._barge_in_pending_since = None
    h._barge_in_pending_task = None
    return h


class TestBargeInTwoStageConfirmation:
    async def test_sub_threshold_transcript_does_not_cancel(self) -> None:
        h = _make_handler(strategies=[MinWordsStrategy(min_words=3)])

        await h._handle_barge_in(
            Transcript(text="okay", is_final=True, speech_final=True)
        )

        assert h._is_speaking is True, (
            "single-word backchannel must NOT cancel the agent when MinWords=3"
        )
        assert not h._llm_cancel_event.is_set()
        h.audio_sender.send_clear.assert_not_called()
        h.metrics.record_turn_interrupted.assert_not_called()

    async def test_meets_threshold_confirms_and_cancels(self) -> None:
        h = _make_handler(strategies=[MinWordsStrategy(min_words=3)])

        await h._handle_barge_in(
            Transcript(
                text="please stop talking now",
                is_final=True,
                speech_final=True,
            )
        )

        assert h._is_speaking is False
        assert h._llm_cancel_event.is_set()
        h.audio_sender.send_clear.assert_awaited_once()
        h.metrics.record_turn_interrupted.assert_called_once()
        h.metrics.record_overlap_end.assert_called_once()

    async def test_legacy_path_cancels_immediately_with_no_strategies(self) -> None:
        h = _make_handler(strategies=[])

        await h._handle_barge_in(
            Transcript(text="okay", is_final=True, speech_final=True)
        )

        # Without any strategies the legacy contract holds: the very first
        # transcript while ``_is_speaking`` cancels the agent.
        assert h._is_speaking is False
        assert h._llm_cancel_event.is_set()


class TestPendingBargeInLifecycle:
    async def test_pending_timeout_clears_state_and_records_overlap_end(self) -> None:
        h = _make_handler(strategies=[MinWordsStrategy(min_words=3)], confirm_s=0.03)

        await h._start_pending_barge_in()
        assert h._barge_in_pending_since is not None
        assert h._barge_in_pending_task is not None
        h.metrics.record_overlap_start.assert_called_once()

        # Wait past the timeout.
        await asyncio.sleep(0.08)

        assert h._barge_in_pending_since is None
        assert h._barge_in_pending_task is None
        # Timeout emits overlap_end(was_interruption=False) — distinguishing
        # genuine cancels from "agent kept talking, false positive".
        h.metrics.record_overlap_end.assert_called_once()
        called_with = h.metrics.record_overlap_end.call_args
        # The timeout path passes was_interruption=False (positional or
        # keyword); accept either.
        if called_with.kwargs:
            assert called_with.kwargs.get("was_interruption") is False
        else:
            assert called_with.args == (False,) or called_with.args[0] is False

    async def test_clear_pending_cancels_timeout_task(self) -> None:
        h = _make_handler(strategies=[MinWordsStrategy(min_words=3)], confirm_s=10)

        await h._start_pending_barge_in()
        task = h._barge_in_pending_task
        assert task is not None and not task.done()

        h._clear_pending_barge_in()
        # Yield once so the cancellation propagates.
        await asyncio.sleep(0)

        assert h._barge_in_pending_since is None
        assert h._barge_in_pending_task is None
        assert task.cancelled() or task.done()

    async def test_confirmation_clears_pending(self) -> None:
        h = _make_handler(strategies=[MinWordsStrategy(min_words=2)], confirm_s=10)
        await h._start_pending_barge_in()
        assert h._barge_in_pending_since is not None

        await h._handle_barge_in(
            Transcript(text="please stop", is_final=True, speech_final=True)
        )

        # The cancel path must also drop pending state.
        assert h._barge_in_pending_since is None
        assert h._barge_in_pending_task is None
        assert h._is_speaking is False


class TestBargeInIdempotency:
    async def test_double_start_pending_is_noop(self) -> None:
        h = _make_handler(strategies=[MinWordsStrategy(min_words=3)], confirm_s=10)

        await h._start_pending_barge_in()
        first_since = h._barge_in_pending_since
        first_task = h._barge_in_pending_task

        # Second call must not overwrite or restart the timer.
        await h._start_pending_barge_in()

        assert h._barge_in_pending_since == first_since
        assert h._barge_in_pending_task is first_task
        # overlap_start was called once — strategy is idempotent.
        h.metrics.record_overlap_start.assert_called_once()


@pytest.mark.parametrize("min_words", [2, 3, 5])
async def test_min_words_threshold_is_honoured_end_to_end(min_words: int) -> None:
    h = _make_handler(strategies=[MinWordsStrategy(min_words=min_words)])

    # Below threshold: keep agent talking
    below = "word " * (min_words - 1)
    await h._handle_barge_in(
        Transcript(text=below.strip(), is_final=True, speech_final=True)
    )
    assert h._is_speaking is True

    # At threshold: confirm
    at = "word " * min_words
    await h._handle_barge_in(
        Transcript(text=at.strip(), is_final=True, speech_final=True)
    )
    assert h._is_speaking is False
