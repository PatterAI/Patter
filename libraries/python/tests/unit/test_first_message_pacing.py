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
