"""Unit tests for the firstMessage mark-gated paced sender (BUG #128).

Pre-fix the firstMessage TTS chunks were pushed into the carrier WebSocket
as fast as the TTS provider yielded them. A barge-in mid-buffer issued
``send_clear``, but the WebSocket queue between the SDK and the carrier
held several seconds of media frames already, and the agent kept talking
on the user's earpiece until that drained.

Post-fix the loop sends a mark after every chunk and awaits the oldest
mark once ``_FIRST_MESSAGE_MARK_WINDOW`` chunks are unconfirmed;
``_drain_pending_marks`` (called from the cancel path) resolves every
pending future so the waiting loop exits on the next tick. On Telnyx
(no mark concept) the loop falls back to a playout-time-based sleep so
the carrier buffer never grows beyond one chunk.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from getpatter.stream_handler import AudioSender, PipelineStreamHandler


CHUNK_BYTES = 1280  # mirrors PipelineStreamHandler._PREWARM_CHUNK_BYTES


class _RecordingAudioSender(AudioSender):
    """In-memory AudioSender that records every call for inspection."""

    def __init__(self) -> None:
        self.audio_chunks: list[bytes] = []
        self.marks: list[str] = []
        self.clears: int = 0

    async def send_audio(self, pcm_audio: bytes) -> None:
        self.audio_chunks.append(pcm_audio)

    async def send_clear(self) -> None:
        self.clears += 1

    async def send_mark(self, mark_name: str) -> None:
        self.marks.append(mark_name)


def _make_handler(
    *, for_twilio: bool = True
) -> tuple[PipelineStreamHandler, _RecordingAudioSender]:
    """Build a PipelineStreamHandler shell without exercising __init__.

    Tests need only the paced-sender / on_mark / cancel surface — we don't
    want to mock 30 unrelated dependencies (STT/TTS/metrics/etc.).
    """
    handler = PipelineStreamHandler.__new__(PipelineStreamHandler)
    sender = _RecordingAudioSender()
    handler.audio_sender = sender
    handler._is_speaking = True
    handler._speaking_started_at = time.time()
    handler._first_audio_sent_at = time.time()
    handler._aec = None
    handler._for_twilio = for_twilio
    handler._pending_marks = []
    handler._first_message_mark_counter = 0
    handler.call_id = "call-test"
    handler.metrics = None
    return handler, sender


def _mark_first_audio_sent_noop(self: PipelineStreamHandler) -> None:
    """No-op replacement for the real ``_mark_first_audio_sent`` so we don't
    need to wire the per-turn metrics accumulator into the test fixture.
    """
    return None


@pytest.fixture(autouse=True)
def _patch_mark_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        PipelineStreamHandler,
        "_mark_first_audio_sent",
        _mark_first_audio_sent_noop,
    )


@pytest.fixture(autouse=True)
def _instant_playout_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace asyncio.sleep with sleep(0) so the playout pacing in
    ``_send_paced_first_message_bytes`` yields once instead of waiting
    40 ms per chunk. This lets mark-gating tests advance through multiple
    chunks in a handful of ``asyncio.sleep(0)`` iterations without
    waiting real time, while leaving ``asyncio.wait_for`` timeouts in
    ``_wait_for_mark_window`` unaffected (they use the event-loop clock,
    not asyncio.sleep).
    """
    _real_sleep = asyncio.sleep

    async def _zero(secs: float) -> None:
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _zero)


@pytest.mark.unit
class TestFirstMessageMarkGatedPacing:
    """BUG #128 regression coverage: firstMessage must be cancellable."""

    async def test_caps_in_flight_at_window_and_bails_on_barge_in(self) -> None:
        handler, sender = _make_handler(for_twilio=True)
        # 4 chunks. Window=3, so chunks 1–3 send back-to-back and chunk 4
        # blocks on _wait_for_mark_window until either a mark echoes OR
        # _drain_pending_marks (called from cancel) resolves the futures.
        bytes_ = b"\x00" * (CHUNK_BYTES * 4)

        task = asyncio.create_task(handler._send_paced_first_message_bytes(bytes_))

        # Yield enough so the loop sends the first three chunks and enters
        # the window wait.
        for _ in range(20):
            await asyncio.sleep(0)

        assert len(sender.audio_chunks) == 3
        assert sender.marks == ["fm_1", "fm_2", "fm_3"]
        assert len(handler._pending_marks) == 3

        # Simulate the cancel side of a confirmed barge-in. ``send_clear`` is
        # the canonical signal; ``_drain_pending_marks`` unblocks the
        # waiting loop so it sees ``_is_speaking=False`` on the next tick.
        handler._is_speaking = False
        handler._drain_pending_marks()
        await sender.send_clear()

        sent = await task

        assert sent is True
        assert sender.clears == 1
        # Chunk 4 must NOT have hit the wire.
        assert len(sender.audio_chunks) == 3

    async def test_echoed_mark_slides_window_and_next_chunk_goes_out(self) -> None:
        handler, sender = _make_handler(for_twilio=True)
        bytes_ = b"\x00" * (CHUNK_BYTES * 4)

        task = asyncio.create_task(handler._send_paced_first_message_bytes(bytes_))

        for _ in range(20):
            await asyncio.sleep(0)
        assert len(sender.audio_chunks) == 3
        assert sender.marks == ["fm_1", "fm_2", "fm_3"]

        # Twilio echoes chunk 1 → loop should advance to chunk 4.
        await handler.on_mark("fm_1")
        for _ in range(20):
            await asyncio.sleep(0)

        assert len(sender.audio_chunks) == 4
        assert sender.marks == ["fm_1", "fm_2", "fm_3", "fm_4"]

        # Drain the rest so the loop completes naturally.
        await handler.on_mark("fm_2")
        await handler.on_mark("fm_3")
        await handler.on_mark("fm_4")
        await task
        assert handler._pending_marks == []

    async def test_telnyx_paces_via_playout_time_and_bails_on_cancel(self) -> None:
        handler, sender = _make_handler(for_twilio=False)
        # 4 chunks. Telnyx never sends marks — every iteration awaits a
        # real ``asyncio.sleep`` keyed to chunk playout duration.
        bytes_ = b"\x00" * (CHUNK_BYTES * 4)

        task = asyncio.create_task(handler._send_paced_first_message_bytes(bytes_))

        # Yield enough so at least the first chunk hits the wire.
        for _ in range(5):
            await asyncio.sleep(0)
        sent_before_cancel = len(sender.audio_chunks)
        assert sent_before_cancel >= 1
        # Telnyx must never accumulate marks.
        assert sender.marks == []
        assert handler._pending_marks == []

        # Cancel mid-loop.
        handler._is_speaking = False
        handler._drain_pending_marks()
        await sender.send_clear()
        await task

        assert sender.clears == 1
        # No further chunks may go out after cancel.
        assert len(sender.audio_chunks) == sent_before_cancel


@pytest.mark.unit
class TestOnMarkResolvesWaiters:
    """``on_mark`` matches the FIFO entry and resolves all earlier ones too."""

    async def test_echo_for_later_mark_resolves_earlier_waiters(self) -> None:
        handler, _sender = _make_handler(for_twilio=True)

        # Manually queue three marks (skipping send_audio so we test the
        # matching logic in isolation).
        await handler._send_mark_awaitable()
        await handler._send_mark_awaitable()
        await handler._send_mark_awaitable()
        assert [name for name, _ in handler._pending_marks] == ["fm_1", "fm_2", "fm_3"]

        await handler.on_mark("fm_2")
        # fm_1 and fm_2 are drained; fm_3 stays pending.
        assert [name for name, _ in handler._pending_marks] == ["fm_3"]


@pytest.mark.unit
class TestCleanupDrainsPendingMarks:
    """Cleanup on abnormal call end (carrier WS drop / hangup mid
    firstMessage) must resolve every pending mark future so the paced
    send loop never leaves orphan ``asyncio.Future`` instances.
    """

    async def test_cleanup_drains_pending_marks(self) -> None:
        handler, _sender = _make_handler(for_twilio=True)
        # Wire enough stubs so PipelineStreamHandler.cleanup() does not
        # crash. The actual stt/tts/remote_handler tear-down branches
        # short-circuit on ``None``.
        handler._barge_in_pending_task = None
        handler._barge_in_pending_since = None
        handler._stt_task = None
        handler._stt = None
        handler._tts = None
        handler._remote_handler = None
        handler._resampler_8k_to_16k = None

        # Queue three marks via the public send path then trigger
        # cleanup to mimic an abnormal end mid-send.
        await handler._send_mark_awaitable()
        await handler._send_mark_awaitable()
        await handler._send_mark_awaitable()
        pending_futures = [fut for _name, fut in handler._pending_marks]
        assert len(pending_futures) == 3
        assert all(not fut.done() for fut in pending_futures)

        await handler.cleanup()

        # Every queued future is resolved and the queue is empty.
        assert handler._pending_marks == []
        assert all(fut.done() for fut in pending_futures)


@pytest.mark.unit
class TestFirstMessageMarkCounterReset:
    """The ``_first_message_mark_counter`` must reset at the top of each
    paced send AND on cleanup so a re-used handler instance never reuses
    a stale ``fm_<n>`` name across turns.
    """

    async def test_send_paced_resets_counter_between_consecutive_sends(self) -> None:
        """Each ``_send_paced_first_message_bytes`` invocation re-starts
        the ``fm_<n>`` numbering at 1 — without the reset, the counter
        would grow monotonically across turns and a stale echo for an
        earlier turn's ``fm_N`` could match a mark name issued later.
        """
        handler, sender = _make_handler(for_twilio=True)
        bytes_ = b"\x00" * (CHUNK_BYTES * 2)

        # First send: two chunks ≤ window (3) so the loop yields after
        # the first ``_wait_for_mark_window`` pre-check on chunk 3.
        task1 = asyncio.create_task(handler._send_paced_first_message_bytes(bytes_))
        for _ in range(20):
            await asyncio.sleep(0)
        await handler.on_mark("fm_1")
        await handler.on_mark("fm_2")
        await task1
        assert handler._first_message_mark_counter == 2
        assert handler._pending_marks == []
        assert sender.marks == ["fm_1", "fm_2"]

        # Second send: counter must reset to 0 before iterating so the
        # new sequence is fm_1, fm_2 — NOT fm_3, fm_4.
        task2 = asyncio.create_task(handler._send_paced_first_message_bytes(bytes_))
        for _ in range(20):
            await asyncio.sleep(0)
        # New marks recorded by the sender are appended after the prior
        # turn's two marks.
        new_marks = sender.marks[2:]
        assert new_marks == ["fm_1", "fm_2"]
        assert handler._first_message_mark_counter == 2

        await handler.on_mark("fm_1")
        await handler.on_mark("fm_2")
        await task2

    async def test_cleanup_resets_counter(self) -> None:
        """Cleanup must reset ``_first_message_mark_counter`` to 0 so a
        re-used handler starts fresh on the next call. Defensive: the
        per-send reset is the canonical path, but cleanup belt-and-braces
        the cross-call boundary.
        """
        handler, _sender = _make_handler(for_twilio=True)
        handler._barge_in_pending_task = None
        handler._barge_in_pending_since = None
        handler._stt_task = None
        handler._stt = None
        handler._tts = None
        handler._remote_handler = None
        handler._resampler_8k_to_16k = None

        # Pretend a prior call left the counter at 7.
        handler._first_message_mark_counter = 7

        await handler.cleanup()

        assert handler._first_message_mark_counter == 0
