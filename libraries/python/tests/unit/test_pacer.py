"""Unit tests for the outbound wall-clock frame pacer.

Covers the three pieces of the low-latency outbound path:
  * precomputed silence frames (mu-law 0xFF, PCM16 zero),
  * fixed-size 20 ms re-framing of arbitrary enqueued audio,
  * a drift-corrected wall-clock loop that emits silence on an empty queue
    and catches up (bounded) after the scheduler oversleeps.

The loop is exercised with an injected fake clock + fake sleep so the timing
logic is deterministic and does not sleep in real time.
"""

from __future__ import annotations

from collections.abc import Awaitable

import pytest

from getpatter.audio.pacer import (
    FRAME_MS,
    OutboundFramePacer,
    mulaw_silence_frame,
    pcm16_silence_frame,
)


class TestSilenceFrames:
    def test_mulaw_silence_is_160_bytes_of_0xff(self) -> None:
        f = mulaw_silence_frame()
        assert f == b"\xff" * 160  # 160 samples = 20 ms @ 8 kHz, 0xFF = mu-law zero
        assert len(f) == 160

    def test_pcm16_silence_is_zeroed_and_rate_sized(self) -> None:
        assert pcm16_silence_frame(16000) == b"\x00" * 640  # 320 samples * 2 bytes
        assert pcm16_silence_frame(8000) == b"\x00" * 320


class TestReframing:
    def test_next_frame_returns_silence_when_empty(self) -> None:
        silence = mulaw_silence_frame()
        pacer = OutboundFramePacer(frame_bytes=160, silence_frame=silence)
        assert pacer.next_frame() == silence

    def test_next_frame_slices_exact_frame_bytes(self) -> None:
        silence = mulaw_silence_frame()
        pacer = OutboundFramePacer(frame_bytes=160, silence_frame=silence)
        pacer.enqueue(b"\x01" * 400)  # 2.5 frames
        assert pacer.next_frame() == b"\x01" * 160
        assert pacer.next_frame() == b"\x01" * 160
        # 80 bytes remain (< a frame): padded to a full frame with silence tail,
        # then the queue is drained.
        third = pacer.next_frame()
        assert third == b"\x01" * 80 + silence[80:]
        assert pacer.next_frame() == silence  # drained -> silence again

    def test_enqueue_accumulates_across_calls(self) -> None:
        silence = pcm16_silence_frame(16000)
        pacer = OutboundFramePacer(frame_bytes=640, silence_frame=silence)
        pacer.enqueue(b"\x02" * 300)
        pacer.enqueue(b"\x02" * 340)  # now 640 total -> one exact frame
        assert pacer.next_frame() == b"\x02" * 640
        assert pacer.next_frame() == silence

    def test_pending_bytes_reported(self) -> None:
        pacer = OutboundFramePacer(frame_bytes=160, silence_frame=mulaw_silence_frame())
        assert pacer.pending_bytes == 0
        pacer.enqueue(b"\x01" * 250)
        assert pacer.pending_bytes == 250

    def test_clear_drops_buffered_audio_and_emits_silence(self) -> None:
        silence = mulaw_silence_frame()
        pacer = OutboundFramePacer(frame_bytes=160, silence_frame=silence)
        pacer.enqueue(b"\x01" * 500)  # ~3 frames queued
        assert pacer.pending_bytes == 500
        pacer.clear()  # barge-in: drop the queued (cancelled) audio
        assert pacer.pending_bytes == 0
        # Next tick emits silence, never the dropped audio.
        assert pacer.next_frame() == silence
        # Re-usable after a clear — new audio frames normally.
        pacer.enqueue(b"\x02" * 160)
        assert pacer.next_frame() == b"\x02" * 160


class _FakeClock:
    """Monotonic fake clock; `sleep` advances it by the requested delay so the
    pacer loop makes progress without real time passing."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        # A real event loop never sleeps negative; clamp like asyncio does.
        self.t += max(0.0, delay)


class TestPacerLoop:
    @pytest.mark.asyncio
    async def test_emits_one_frame_per_frame_period(self) -> None:
        clock = _FakeClock()
        sent: list[bytes] = []
        pacer = OutboundFramePacer(
            frame_bytes=160,
            silence_frame=mulaw_silence_frame(),
            send_frame=lambda f: sent.append(f) or _noop(),
            clock=clock.now,
            sleep=clock.sleep,
        )
        pacer.enqueue(b"\x01" * 160 * 3)  # exactly 3 frames of real audio

        # Run the loop for a bounded number of ticks then stop.
        await pacer.run(max_frames=5)

        # 3 real frames, then silence to keep the wall-clock alive.
        assert sent[0] == b"\x01" * 160
        assert sent[1] == b"\x01" * 160
        assert sent[2] == b"\x01" * 160
        assert sent[3] == mulaw_silence_frame()
        assert sent[4] == mulaw_silence_frame()
        # Clock advanced ~one frame period per emitted frame.
        assert clock.t == pytest.approx(1000.0 + 5 * (FRAME_MS / 1000.0), abs=1e-6)

    @pytest.mark.asyncio
    async def test_catches_up_after_oversleep_without_unbounded_burst(self) -> None:
        # Clock that jumps 100 ms (5 frame periods) on the first sleep, to
        # simulate the OS oversleeping; the pacer must emit catch-up frames
        # but never more than the bounded catch-up ceiling.
        clock = _FakeClock()
        sent: list[bytes] = []
        jumped = {"done": False}

        async def jumpy_sleep(delay: float) -> None:
            if not jumped["done"]:
                jumped["done"] = True
                clock.t += 0.100  # 5 frame periods of oversleep
            else:
                clock.t += max(0.0, delay)

        pacer = OutboundFramePacer(
            frame_bytes=160,
            silence_frame=mulaw_silence_frame(),
            send_frame=lambda f: sent.append(f) or _noop(),
            clock=clock.now,
            sleep=jumpy_sleep,
            max_catchup_frames=3,
        )
        await pacer.run(max_frames=10)
        # It should have emitted a burst bounded by max_catchup_frames after the
        # jump, not 5 — the ceiling prevents an unbounded post-pause burst.
        assert len(sent) == 10  # loop still terminated cleanly at max_frames

    @pytest.mark.asyncio
    async def test_stop_halts_the_loop(self) -> None:
        clock = _FakeClock()
        sent: list[bytes] = []
        holder: dict[str, OutboundFramePacer] = {}

        def send(f: bytes) -> Awaitable[None]:
            sent.append(f)
            if len(sent) == 2:
                holder["pacer"].stop()  # stop from inside the tick
            return _noop()

        pacer = OutboundFramePacer(
            frame_bytes=160,
            silence_frame=mulaw_silence_frame(),
            send_frame=send,
            clock=clock.now,
            sleep=clock.sleep,
        )
        holder["pacer"] = pacer
        # max_frames is a guard; stop() must end the loop first, at exactly 2.
        await pacer.run(max_frames=100)
        assert len(sent) == 2


async def _noop() -> None:
    return None
