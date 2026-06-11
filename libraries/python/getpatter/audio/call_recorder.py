"""Carrier-neutral local call recording — interleaved stereo WAV on disk.

``LocalCallRecorder`` taps the audio that already flows through the SDK's
per-call stream handlers and writes a synchronized two-channel recording:

* **left channel  = caller**  (inbound, post mulaw→PCM16 decode)
* **right channel = agent**   (outbound TTS / Realtime / ConvAI audio)

Unlike the carrier-side ``recording=True`` option (Twilio Recordings API,
Telnyx ``record_start``, Plivo Record API) this works on ANY carrier because
nothing leaves the process: the recorder is fed at the transport tap points
and appends PCM to ``recording.wav`` incrementally. Enable via
``Patter.serve(local_recording=True)`` (or a directory string). Parity with
TypeScript ``src/audio/call-recorder.ts``.

Format
------
16-bit little-endian PCM, 16 kHz, 2 channels. A placeholder RIFF header is
written at open with zero sizes; :meth:`close` patches the ``RIFF`` and
``data`` chunk sizes so the file is parseable by any WAV reader. The handler
cleanup path calls :meth:`close` on every teardown (including abnormal carrier
WebSocket drops), so a truncated call still yields a valid file.

Time alignment (documented approach)
------------------------------------
The recorder is **caller-clocked**. On PSTN the inbound media stream is a
continuous realtime sequence of ~20 ms frames (silence included), so the
caller channel is used as the wall clock:

* Each caller chunk advances the file by exactly its own duration. The right
  channel for that span is popped from a bounded agent FIFO and zero-padded
  (silence) when the agent has nothing buffered — i.e. the lagging channel is
  padded with silence whenever the other channel writes.
* Agent audio is *appended to the FIFO* on arrival and drained at the caller
  clock rate. TTS providers push audio faster than realtime (multi-second
  bursts); draining at the caller rate mirrors the carrier's own playout
  buffer, so agent audio lands on the timeline roughly where the caller
  actually heard it (frame-granularity ≈ the inbound chunk size, ~20 ms).

Alignment is therefore *duration-based*, not wall-clock-based: cross-channel
skew is bounded by the carrier playout backlog (audio we pushed that the
caller has not heard yet). Audio cleared by a barge-in ``send_clear`` may
remain in the recording — see "known gaps" in the PR description.

Memory profile (allocation-light)
---------------------------------
Writes are streamed to disk per inbound frame — nothing accumulates for the
call duration. Internal state is bounded:

* caller path: at most one odd carry byte (PCM16 alignment),
* agent path: FIFO capped at :data:`AGENT_BACKLOG_CAP_S` seconds
  (~1.9 MB); overflow force-flushes the oldest excess paired with left-channel
  silence so long monologues cannot grow the buffer unboundedly,
* per-channel stateful resamplers (fixed-size filter state).

Any I/O error disables the recorder for the rest of the call (logged once) —
recording must never take down a live phone call.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Optional

logger = logging.getLogger("getpatter")

__all__ = ["LocalCallRecorder", "RECORDING_SAMPLE_RATE", "AGENT_BACKLOG_CAP_S"]

#: Output sample rate (both channels). Matches the SDK's internal PCM16 16 kHz
#: processing rate, so the common tap path is a pure pass-through.
RECORDING_SAMPLE_RATE: int = 16000

#: Maximum buffered agent audio (seconds) awaiting caller-clock drain. The
#: agent FIFO mirrors the carrier playout buffer; 60 s covers even very long
#: single-turn monologues. Overflow is force-flushed (silence on the caller
#: channel), bounding memory at ~1.9 MB per recorded call.
AGENT_BACKLOG_CAP_S: int = 60

_BYTES_PER_SAMPLE = 2
_CHANNELS = 2
_WAV_HEADER_BYTES = 44

#: Userspace write-buffer size handed to :func:`open`. A 20 ms stereo frame
#: is 1 280 bytes, so 64 KiB batches ~1 s of audio per disk write — the hot
#: path never issues per-frame disk I/O (CPython's default 8 KiB buffer
#: would already batch ~6 frames; we make the batching explicit and larger).
_WRITE_BUFFER_BYTES = 64 * 1024

#: Accepted ``encoding`` values for :meth:`LocalCallRecorder.add_caller_audio`
#: / :meth:`LocalCallRecorder.add_agent_audio`.
_ENCODINGS = ("pcm16_16k", "mulaw_8k", "pcm16_8k", "pcm16_24k")


def _build_wav_header(data_size: int) -> bytes:
    """Return a 44-byte canonical PCM WAV header for the recorder format."""
    byte_rate = RECORDING_SAMPLE_RATE * _CHANNELS * _BYTES_PER_SAMPLE
    block_align = _CHANNELS * _BYTES_PER_SAMPLE
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,  # PCM fmt chunk size
        1,  # audio format: PCM
        _CHANNELS,
        RECORDING_SAMPLE_RATE,
        byte_rate,
        block_align,
        _BYTES_PER_SAMPLE * 8,
        b"data",
        data_size,
    )


def _interleave(left: bytes, right: bytes) -> bytes:
    """Interleave two equal-length mono PCM16 buffers into stereo frames.

    Uses ``memoryview.cast`` strided assignment (C-speed, stdlib-only).
    Native byte order is assumed little-endian, like the rest of the SDK's
    PCM plumbing (audioop / numpy paths).
    """
    out = bytearray(len(left) + len(right))
    mv = memoryview(out).cast("h")
    mv[0::2] = memoryview(left).cast("h")
    mv[1::2] = memoryview(right).cast("h")
    return bytes(out)


class _ChannelDecoder:
    """Per-channel decode pipeline: any supported encoding → PCM16 @ 16 kHz.

    Owns the channel's :class:`StatefulResampler` (filter state preserved
    across chunks) and an odd-byte carry for the pass-through path. The
    encoding is fixed after the first chunk — taps always feed a constant
    wire format for the lifetime of a call.
    """

    __slots__ = ("_encoding", "_resampler", "_carry", "_mulaw_to_pcm16")

    def __init__(self) -> None:
        self._encoding: Optional[str] = None
        self._resampler = None
        self._carry = None
        self._mulaw_to_pcm16 = None

    def decode(self, data: bytes, encoding: str) -> bytes:
        if encoding not in _ENCODINGS:
            raise ValueError(
                f"Unsupported recording encoding {encoding!r}; expected one of {_ENCODINGS}"
            )
        if self._encoding is None:
            self._init(encoding)
        elif encoding != self._encoding:
            # Wire format flipped mid-call (never happens today) — restart the
            # pipeline rather than feeding a mis-rated resampler.
            self._init(encoding)

        if encoding == "pcm16_16k":
            return self._carry.feed(data)  # type: ignore[union-attr]
        if encoding == "mulaw_8k":
            pcm8k = self._mulaw_to_pcm16(data)  # type: ignore[misc]
            return self._resampler.process(pcm8k)  # type: ignore[union-attr]
        # pcm16_8k / pcm16_24k — StatefulResampler aligns odd bytes itself.
        return self._resampler.process(data)  # type: ignore[union-attr]

    def _init(self, encoding: str) -> None:
        from getpatter.audio.transcoding import PcmCarry, StatefulResampler

        self._encoding = encoding
        self._carry = PcmCarry()
        self._resampler = None
        if encoding == "mulaw_8k":
            from getpatter.audio.transcoding import mulaw_to_pcm16

            self._mulaw_to_pcm16 = mulaw_to_pcm16
            self._resampler = StatefulResampler(8000, RECORDING_SAMPLE_RATE)
        elif encoding == "pcm16_8k":
            self._resampler = StatefulResampler(8000, RECORDING_SAMPLE_RATE)
        elif encoding == "pcm16_24k":
            self._resampler = StatefulResampler(24000, RECORDING_SAMPLE_RATE)


class LocalCallRecorder:
    """Incremental stereo WAV writer for one call (left=caller, right=agent).

    Parameters
    ----------
    path:
        Target ``.wav`` file. Parent directories are created. The file is
        opened (and the placeholder header written) eagerly so permission
        errors surface at call start, not mid-call.

    All ``add_*`` methods are synchronous and cheap (couple of µs per 20 ms
    frame: one decode + one buffered ``write``); they are intended to be
    called inline from the async audio paths. Errors never propagate — the
    recorder disables itself and logs a single warning.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Buffered writer: add_* calls append to a 64 KiB userspace buffer;
        # actual disk writes happen ~once per second of audio, never per frame.
        self._fh = open(  # noqa: SIM115 - lifetime spans the call
            self._path, "wb", buffering=_WRITE_BUFFER_BYTES
        )
        self._fh.write(_build_wav_header(0))
        # Flush the placeholder header eagerly: disk-full / permission
        # problems surface at call start, and an abnormally-killed process
        # still leaves a recognizable (if size-zero) WAV on disk.
        self._fh.flush()
        self._data_bytes = 0
        self._closed = False
        self._broken = False
        self._caller_decoder = _ChannelDecoder()
        self._agent_decoder = _ChannelDecoder()
        # Bounded FIFO of decoded agent PCM16@16k awaiting caller-clock drain.
        self._agent_backlog = bytearray()
        self._max_backlog_bytes = (
            AGENT_BACKLOG_CAP_S * RECORDING_SAMPLE_RATE * _BYTES_PER_SAMPLE
        )
        # Per-channel real (non-padding) samples ingested — exposed for tests
        # and diagnostics ("track per-channel written duration").
        self._caller_samples = 0
        self._agent_samples = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """Target WAV path."""
        return self._path

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has run (or the recorder broke)."""
        return self._closed

    @property
    def frames_written(self) -> int:
        """Stereo frames appended so far (the 64 KiB write buffer flushes
        roughly once per second of audio and always on :meth:`close`)."""
        return self._data_bytes // (_CHANNELS * _BYTES_PER_SAMPLE)

    @property
    def caller_samples(self) -> int:
        """Real caller samples ingested (excludes padding)."""
        return self._caller_samples

    @property
    def agent_samples(self) -> int:
        """Real agent samples ingested (excludes padding)."""
        return self._agent_samples

    @property
    def duration_seconds(self) -> float:
        """Recorded duration so far (committed stereo frames / 16 kHz)."""
        return self.frames_written / RECORDING_SAMPLE_RATE

    # ------------------------------------------------------------------
    # Ingestion taps
    # ------------------------------------------------------------------

    def add_caller_audio(self, data: bytes, encoding: str = "pcm16_16k") -> None:
        """Record a caller-side chunk; advances the file by its duration.

        The right channel for the span is drained from the agent FIFO and
        zero-padded (silence) when the agent is not speaking.
        """
        if self._closed or self._broken or not data:
            return
        try:
            pcm = self._caller_decoder.decode(data, encoding)
            if not pcm:
                return
            self._caller_samples += len(pcm) // _BYTES_PER_SAMPLE
            right = self._pop_agent_backlog(len(pcm))
            self._write_frames(pcm, right)
        except Exception as exc:  # noqa: BLE001 - recording must never break a call
            self._mark_broken(exc)

    def add_agent_audio(self, data: bytes, encoding: str = "pcm16_16k") -> None:
        """Buffer an agent-side chunk; it drains at the caller clock rate."""
        if self._closed or self._broken or not data:
            return
        try:
            pcm = self._agent_decoder.decode(data, encoding)
            if not pcm:
                return
            self._agent_samples += len(pcm) // _BYTES_PER_SAMPLE
            self._agent_backlog.extend(pcm)
            overflow = len(self._agent_backlog) - self._max_backlog_bytes
            if overflow > 0:
                # Bound memory: commit the oldest excess now, paired with
                # silence on the caller channel (degrades alignment only in
                # the pathological >60 s-monologue case).
                if overflow % _BYTES_PER_SAMPLE:
                    overflow += 1
                excess = bytes(self._agent_backlog[:overflow])
                del self._agent_backlog[:overflow]
                self._write_frames(bytes(len(excess)), excess)
        except Exception as exc:  # noqa: BLE001 - recording must never break a call
            self._mark_broken(exc)

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def close(self) -> Optional[str]:
        """Drain the agent FIFO, patch the WAV header, and close the file.

        Idempotent and exception-safe — callable from every teardown path
        (graceful ``stop``, carrier WS drop, handler cleanup). Returns the
        recording path as ``str`` when a playable file exists, else ``None``.
        """
        if self._closed:
            return str(self._path) if not self._broken else None
        self._closed = True
        if self._broken:
            # The recorder already tore itself down mid-call (``_mark_broken``
            # closed the file handle) — nothing left to finalize.
            return None
        try:
            if self._agent_backlog:
                # Remaining agent audio never had caller frames to clock it
                # out (e.g. hangup mid-monologue) — commit it against silence.
                tail = bytes(self._agent_backlog)
                self._agent_backlog.clear()
                self._write_frames(bytes(len(tail)), tail, force=True)
            self._fh.seek(4)
            self._fh.write(struct.pack("<I", 36 + self._data_bytes))
            self._fh.seek(40)
            self._fh.write(struct.pack("<I", self._data_bytes))
            self._fh.flush()
        except Exception as exc:  # noqa: BLE001 - best-effort finalize
            logger.warning("Local recording finalize failed (%s): %s", self._path, exc)
            self._broken = True
        finally:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001 - already closing
                pass
        if self._broken:
            return None
        logger.info(
            "Local recording saved: %s (%.1fs)", self._path, self.duration_seconds
        )
        return str(self._path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pop_agent_backlog(self, num_bytes: int) -> bytes:
        """Pop up to ``num_bytes`` from the agent FIFO, zero-padded to size."""
        if not self._agent_backlog:
            return bytes(num_bytes)
        take = min(num_bytes, len(self._agent_backlog))
        chunk = bytes(self._agent_backlog[:take])
        del self._agent_backlog[:take]
        if take < num_bytes:
            chunk += bytes(num_bytes - take)
        return chunk

    def _write_frames(self, left: bytes, right: bytes, force: bool = False) -> None:
        """Interleave equal-length mono buffers and append to the file."""
        if (self._closed and not force) or self._broken or not left:
            return
        stereo = _interleave(left, right)
        self._fh.write(stereo)
        self._data_bytes += len(stereo)

    def _mark_broken(self, exc: Exception) -> None:
        if self._broken:
            return
        self._broken = True
        logger.warning(
            "Local recording disabled for the rest of the call (%s): %s",
            self._path,
            exc,
        )
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001 - best-effort
            pass
