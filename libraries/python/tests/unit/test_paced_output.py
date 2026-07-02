"""Unit tests for opt-in wall-clock outbound pacing in the pipeline handler.

``agent.paced_output`` routes every outbound pipeline send through an
:class:`OutboundFramePacer` that emits fixed 20 ms frames on a monotonic
grid (silence fills the gaps). Default OFF must stay byte-identical to the
event-driven direct send.

The pacer loop is exercised with an injected fake clock/sleep (same pattern
as ``test_pacer.py``) so timing is deterministic; the handler's REAL paced
helpers (``_send_pipeline_chunk`` / ``_enqueue_paced`` / ``_paced_send_frame``
/ ``_refresh_paced_backlog``) drive it.

Parity with TS tests/unit/paced-output.test.ts.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock

import pytest

from getpatter.audio.pacer import (
    OutboundFramePacer,
    mulaw_silence_frame,
    pcm16_silence_frame,
)
from getpatter.stream_handler import PipelineStreamHandler

from tests.conftest import make_agent


class _FakeClock:
    """Monotonic fake clock; ``sleep`` advances it by the requested delay so
    the pacer loop makes progress without real time passing."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        self.t += max(0.0, delay)


def _make_audio_sender(*, mulaw_native: bool = False) -> AsyncMock:
    sender = AsyncMock()
    sender._input_is_mulaw_8k = mulaw_native
    return sender


def _make_handler(sender: AsyncMock, *, paced: bool) -> PipelineStreamHandler:
    handler = PipelineStreamHandler(
        agent=make_agent(paced_output=paced),
        audio_sender=sender,
        call_id="call-paced",
        caller="+15551110000",
        callee="+15552220000",
        resolved_prompt="p",
        metrics=None,
        for_twilio=True,
        on_transcript=None,
        conversation_history=deque(maxlen=10),
        transcript_entries=deque(maxlen=10),
    )
    handler.on_message = None
    return handler


def _attach_test_pacer(
    handler: PipelineStreamHandler, clock: _FakeClock
) -> OutboundFramePacer:
    """Mirror ``_start_pacer`` but inject the fake clock/sleep for determinism."""
    native = bool(getattr(handler.audio_sender, "_input_is_mulaw_8k", False))
    if native:
        frame_bytes = 160
        handler._pacer_bytes_per_s = 8_000.0
        handler._pacer_silence = mulaw_silence_frame()
    else:
        frame_bytes = 640
        handler._pacer_bytes_per_s = 32_000.0
        handler._pacer_silence = pcm16_silence_frame(16000)
    handler._pacer = OutboundFramePacer(
        frame_bytes=frame_bytes,
        silence_frame=handler._pacer_silence,
        send_frame=handler._paced_send_frame,
        clock=clock.now,
        sleep=clock.sleep,
    )
    return handler._pacer


def _sent_frames(sender: AsyncMock) -> list[bytes]:
    return [call.args[0] for call in sender.send_audio.await_args_list]


class TestPacedFraming:
    @pytest.mark.asyncio
    async def test_emits_fixed_frames_on_20ms_grid_then_silence(self) -> None:
        # (a) fixed-size frames on a 20 ms grid + (b) silence fills the gap.
        sender = _make_audio_sender(mulaw_native=True)  # 160 B μ-law frames
        handler = _make_handler(sender, paced=True)
        clock = _FakeClock()
        pacer = _attach_test_pacer(handler, clock)

        # Real send-site: enqueue exactly 3 frames of real audio.
        await handler._send_pipeline_chunk(b"\x01" * 160 * 3)
        # Nothing hit the wire yet — the pacer owns emission.
        assert sender.send_audio.await_count == 0

        await pacer.run(max_frames=5)

        sent = _sent_frames(sender)
        assert sent[0] == b"\x01" * 160
        assert sent[1] == b"\x01" * 160
        assert sent[2] == b"\x01" * 160
        assert sent[3] == mulaw_silence_frame()  # queue drained -> silence
        assert sent[4] == mulaw_silence_frame()
        # ~one frame period per emitted frame.
        assert clock.t == pytest.approx(1000.0 + 5 * 0.020, abs=1e-6)

    @pytest.mark.asyncio
    async def test_reframes_arbitrary_chunk_lengths_into_fixed_frames(self) -> None:
        sender = _make_audio_sender()  # PCM16 16 kHz -> 640 B frames
        handler = _make_handler(sender, paced=True)
        clock = _FakeClock()
        pacer = _attach_test_pacer(handler, clock)
        # 1.5 frames enqueued as one odd chunk; the pacer re-slices to 640.
        await handler._send_pipeline_chunk(b"\x02" * 960)
        await pacer.run(max_frames=2)
        sent = _sent_frames(sender)
        assert sent[0] == b"\x02" * 640
        # remainder padded to a full frame with the silence tail.
        assert sent[1] == b"\x02" * 320 + pcm16_silence_frame(16000)[320:]


class TestBargeInClear:
    @pytest.mark.asyncio
    async def test_clear_drops_queued_frames_and_resumes_silence(self) -> None:
        # (c) barge-in drops the pacer's queued frames promptly.
        sender = _make_audio_sender()
        handler = _make_handler(sender, paced=True)
        clock = _FakeClock()
        pacer = _attach_test_pacer(handler, clock)
        await handler._send_pipeline_chunk(b"\x03" * 640 * 3)  # 3 frames queued
        handler._paced_emitted_s = 0.123  # pretend some emission happened
        assert handler._pacer.pending_bytes == 640 * 3

        # The barge-in cancel wiring drops the pacer queue + resets the cursor.
        handler._pacer.clear()
        handler._paced_emitted_s = 0.0

        assert handler._pacer.pending_bytes == 0
        await pacer.run(max_frames=1)
        # The cancelled audio never reached the wire — only silence did.
        assert _sent_frames(sender)[-1] == pcm16_silence_frame(16000)


class TestPacedPlaybackTracking:
    @pytest.mark.asyncio
    async def test_heard_prefix_tracks_real_emitted_frames(self) -> None:
        # (e) playback-position tracking stays correct under pacing: the heard
        # prefix follows REAL emitted frames, not the (faster) enqueue burst.
        sender = _make_audio_sender()  # 640 B / 20 ms frames
        handler = _make_handler(sender, paced=True)
        clock = _FakeClock()
        pacer = _attach_test_pacer(handler, clock)

        # Sentence "one": stamped at the current cursor, then 2 frames enqueued.
        handler._turn_spoken_segments.append(
            ("one", handler._turn_playback_total_s, 0.0)
        )
        await handler._send_pipeline_chunk(b"\x01" * 640 * 2)  # +40 ms
        # Sentence "two": stamped at 40 ms, then 1 frame enqueued.
        handler._turn_spoken_segments.append(
            ("two", handler._turn_playback_total_s, 0.0)
        )
        await handler._send_pipeline_chunk(b"\x02" * 640)  # +20 ms  (total 60 ms)

        # Only ONE 20 ms frame actually leaves the SDK.
        await pacer.run(max_frames=1)

        prefix = handler._heard_response_prefix()
        assert prefix is not None
        text, heard_everything = prefix
        # (d) The paced emitted-frame cursor (NOT the faster enqueue burst)
        # drives the cutoff: heard ~= 20 ms lands mid-"one" (0..40 ms). The
        # single word rounds to the nearest boundary (heard) while "two"
        # (start 40 ms) has not begun.
        assert text == "one"
        assert heard_everything is False


class TestDefaultOffByteIdentical:
    @pytest.mark.asyncio
    async def test_pacer_not_created_when_disabled(self) -> None:
        handler = _make_handler(_make_audio_sender(), paced=False)
        assert handler._paced_output is False
        assert handler._pacer is None
        handler._start_pacer()  # explicit no-op when disabled
        assert handler._pacer is None

    @pytest.mark.asyncio
    async def test_direct_send_is_byte_identical(self) -> None:
        # (d) default OFF calls send_audio directly with UNCHANGED bytes and
        # runs the byte-estimate playback tracking — exactly as before.
        sender = _make_audio_sender()
        handler = _make_handler(sender, paced=False)
        chunk = b"\x07" * 321  # arbitrary odd length
        before = handler._playback_buffered_until
        await handler._send_pipeline_chunk(chunk)
        sender.send_audio.assert_awaited_once_with(chunk)  # exact bytes, no reframe
        assert handler._first_audio_sent_at is not None
        assert handler._playback_buffered_until > before  # _track_outbound_playback ran
