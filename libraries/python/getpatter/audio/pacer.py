"""Wall-clock outbound frame pacer for the carrier media socket.

The outbound path is otherwise event-driven: audio is written to the carrier
WebSocket the instant TTS/realtime produces it, so a stream arrives in bursts
and, after a pause, the buffered backlog is flushed as fast as the socket
accepts it. This module provides the pieces to pace it on a fixed 20 ms grid
instead:

  * :func:`mulaw_silence_frame` / :func:`pcm16_silence_frame` — one precomputed
    silence frame per codec, so an empty queue never stalls the clock and the
    first tick pays no encode cost (no cold-start latency).
  * :class:`OutboundFramePacer` — accumulates arbitrary enqueued audio, re-slices
    it into exact 20 ms frames, and runs a drift-corrected wall-clock loop that
    emits a real frame when one is queued and the silence frame when it is not.

The loop timing is derived from a monotonic clock deadline that advances by a
fixed frame period every tick — never from the duration of the work — so
scheduler jitter cannot accumulate. After an oversleep it emits a *bounded*
number of catch-up frames rather than an unbounded burst.

Frame sizing is codec-relative: mu-law 8 kHz is 8 bytes/ms (160 B/frame), PCM16
16 kHz is 32 bytes/ms (640 B/frame). The caller picks ``frame_bytes`` and the
matching ``silence_frame`` for the format it enqueues.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

# Standard RTP/G.711 telephony framing: 20 ms per packet (RFC 3551) — the
# latency-vs-header-overhead sweet spot every carrier already expects.
FRAME_MS: int = 20

# mu-law encodes digital zero as 0xFF; 8 kHz * 20 ms = 160 samples * 1 byte.
_MULAW_SILENCE_FRAME: bytes = b"\xff" * 160


def mulaw_silence_frame() -> bytes:
    """One 20 ms frame of mu-law (G.711 PCMU) silence — 160 bytes of ``0xFF``."""
    return _MULAW_SILENCE_FRAME


def pcm16_silence_frame(sample_rate: int) -> bytes:
    """One 20 ms frame of PCM16-LE silence (all zero bytes) at ``sample_rate``."""
    return b"\x00" * (sample_rate * 2 * FRAME_MS // 1000)


class OutboundFramePacer:
    """Re-frames enqueued audio into fixed-size frames and paces them onto a
    wall-clock grid, emitting the silence frame whenever the queue runs dry.

    ``send_frame`` is an async (or awaitable-returning) callback invoked with
    each frame; ``clock`` and ``sleep`` are injectable for deterministic tests
    (defaults are :func:`time.monotonic` and :func:`asyncio.sleep`).
    """

    def __init__(
        self,
        *,
        frame_bytes: int,
        silence_frame: bytes,
        send_frame: Callable[[bytes], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_catchup_frames: int = 5,
    ) -> None:
        if frame_bytes <= 0:
            raise ValueError("frame_bytes must be positive")
        self._frame_bytes = frame_bytes
        self._silence = silence_frame
        self._send_frame = send_frame
        self._clock = clock
        self._sleep = sleep
        self._max_catchup_frames = max(1, max_catchup_frames)
        self._frame_period_s = FRAME_MS / 1000.0
        self._buf = bytearray()
        self._running = False

    @property
    def pending_bytes(self) -> int:
        """Bytes buffered but not yet framed out."""
        return len(self._buf)

    def enqueue(self, data: bytes) -> None:
        """Append audio to the outbound buffer (any length; re-framed on tick)."""
        if data:
            self._buf.extend(data)

    def next_frame(self) -> bytes:
        """Return the next frame: a full real frame when one is buffered, the
        remaining partial buffer padded to a full frame with the silence tail
        when only a sub-frame remains, or the silence frame when empty.

        This is the ``AudioSampleProvider.NextSample()`` contract — it always
        returns exactly one frame so the write loop never stalls.
        """
        n = len(self._buf)
        if n == 0:
            return self._silence
        if n >= self._frame_bytes:
            frame = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]
            return frame
        # Sub-frame remainder: pad up to a whole frame with the silence tail so
        # framing stays aligned, and drain the buffer.
        frame = bytes(self._buf) + self._silence[n:]
        self._buf.clear()
        return frame

    def stop(self) -> None:
        """Signal :meth:`run` to exit after the current tick."""
        self._running = False

    async def run(self, *, max_frames: int | None = None) -> None:
        """Drive the pace loop until :meth:`stop` (or ``max_frames`` in tests).

        Deadline-based: ``next_deadline`` advances by exactly one frame period
        per tick, independent of how long the send takes, so drift cannot
        accumulate. If the scheduler overslept past several frame periods, up to
        ``max_catchup_frames`` frames are emitted back-to-back to catch up; being
        further behind than that re-anchors the deadline to now instead of
        bursting the whole backlog.
        """
        self._running = True
        next_deadline = self._clock()
        count = 0
        try:
            while self._running and (max_frames is None or count < max_frames):
                frame = self.next_frame()
                if self._send_frame is not None:
                    await self._send_frame(frame)
                count += 1
                next_deadline += self._frame_period_s
                delay = next_deadline - self._clock()
                if delay > 0:
                    await self._sleep(delay)
                    continue
                # Behind schedule (oversleep): don't sleep; bound the catch-up.
                frames_behind = int(-delay / self._frame_period_s)
                if frames_behind > self._max_catchup_frames:
                    next_deadline = self._clock()
        finally:
            self._running = False
