"""Unit tests for getpatter.audio.call_recorder — LocalCallRecorder.

Feeds scripted PCM / mulaw frames through a recorder instance and parses the
resulting WAV with the stdlib ``wave`` module, asserting stereo layout,
sample rate, per-channel sample counts, and silence-padding alignment.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from getpatter.audio.call_recorder import (
    AGENT_BACKLOG_CAP_S,
    RECORDING_SAMPLE_RATE,
    LocalCallRecorder,
)
from getpatter.audio.transcoding import mulaw_to_pcm16

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FRAME_SAMPLES_20MS = RECORDING_SAMPLE_RATE // 50  # 320 samples


def pcm_frame(value: int, samples: int = FRAME_SAMPLES_20MS) -> bytes:
    """Constant-amplitude PCM16 LE mono frame."""
    return struct.pack(f"<{samples}h", *([value] * samples))


def mulaw_frame(byte: int = 0x55, num_bytes: int = 160) -> bytes:
    """20 ms of constant mulaw 8 kHz (160 bytes)."""
    return bytes([byte]) * num_bytes


def read_wav(path: Path) -> tuple[wave._wave_params, list[int], list[int]]:
    """Parse a WAV file → (params, left_samples, right_samples)."""
    with wave.open(str(path), "rb") as wf:
        params = wf.getparams()
        raw = wf.readframes(wf.getnframes())
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    return params, list(samples[0::2]), list(samples[1::2])


# ---------------------------------------------------------------------------
# Format + counts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWavFormat:
    def test_stereo_16k_pcm16_header(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "recording.wav")
        for _ in range(10):  # 200 ms of caller audio
            rec.add_caller_audio(pcm_frame(1000))
        path = rec.close()

        assert path == str(tmp_path / "recording.wav")
        params, left, right = read_wav(tmp_path / "recording.wav")
        assert params.nchannels == 2
        assert params.framerate == RECORDING_SAMPLE_RATE
        assert params.sampwidth == 2
        assert params.nframes == 10 * FRAME_SAMPLES_20MS

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "recording.wav"
        rec = LocalCallRecorder(nested)
        rec.add_caller_audio(pcm_frame(7))
        rec.close()
        assert nested.exists()

    def test_left_is_caller_right_is_agent(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        # Agent first so its FIFO is primed, then one caller frame clocks
        # the pair out together.
        rec.add_agent_audio(pcm_frame(-2000))
        rec.add_caller_audio(pcm_frame(1000))
        rec.close()

        _, left, right = read_wav(tmp_path / "r.wav")
        assert set(left) == {1000}
        assert set(right) == {-2000}


# ---------------------------------------------------------------------------
# Alignment + silence padding
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAlignment:
    def test_right_channel_padded_with_silence_when_agent_idle(
        self, tmp_path: Path
    ) -> None:
        """Caller-only audio → right channel is pure silence of equal length."""
        rec = LocalCallRecorder(tmp_path / "r.wav")
        for _ in range(50):  # 1 s
            rec.add_caller_audio(pcm_frame(123))
        rec.close()

        params, left, right = read_wav(tmp_path / "r.wav")
        assert params.nframes == RECORDING_SAMPLE_RATE  # 1 s
        assert set(left) == {123}
        assert set(right) == {0}

    def test_agent_audio_aligns_to_caller_clock_then_silence(
        self, tmp_path: Path
    ) -> None:
        """1 s caller + 0.5 s agent → 1 s file; agent occupies the first half
        of the right channel, the second half is zero-padded."""
        rec = LocalCallRecorder(tmp_path / "r.wav")
        # Agent burst arrives up-front (TTS pushes faster than realtime).
        for _ in range(25):  # 0.5 s of agent audio
            rec.add_agent_audio(pcm_frame(-7))
        for _ in range(50):  # 1 s of caller frames clock the file
            rec.add_caller_audio(pcm_frame(9))
        rec.close()

        params, left, right = read_wav(tmp_path / "r.wav")
        assert params.nframes == RECORDING_SAMPLE_RATE
        assert set(left) == {9}
        half = RECORDING_SAMPLE_RATE // 2
        assert set(right[:half]) == {-7}
        assert set(right[half:]) == {0}
        assert rec.caller_samples == RECORDING_SAMPLE_RATE
        assert rec.agent_samples == half

    def test_agent_only_audio_drained_on_close_with_left_silence(
        self, tmp_path: Path
    ) -> None:
        """Agent audio with no caller frames is committed at close, padded
        with silence on the (lagging) caller channel."""
        rec = LocalCallRecorder(tmp_path / "r.wav")
        for _ in range(10):
            rec.add_agent_audio(pcm_frame(42))
        assert rec.frames_written == 0  # nothing clocked out yet
        rec.close()

        params, left, right = read_wav(tmp_path / "r.wav")
        assert params.nframes == 10 * FRAME_SAMPLES_20MS
        assert set(left) == {0}
        assert set(right) == {42}

    def test_interleaved_conversation_keeps_channels_separate(
        self, tmp_path: Path
    ) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        for i in range(20):
            if i % 2 == 0:
                rec.add_agent_audio(pcm_frame(-1))
            rec.add_caller_audio(pcm_frame(1))
        rec.close()

        _, left, right = read_wav(tmp_path / "r.wav")
        assert set(left) == {1}
        assert set(right) == {-1, 0}  # agent audio + padding, never mixed


# ---------------------------------------------------------------------------
# Encodings (mulaw 8k / pcm16 8k / pcm16 24k → PCM16 16 kHz)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEncodings:
    def test_mulaw_8k_both_channels_upsampled_to_16k(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        for _ in range(50):  # 1 s of 20 ms mulaw frames on both sides
            rec.add_agent_audio(mulaw_frame(0x10), encoding="mulaw_8k")
            rec.add_caller_audio(mulaw_frame(0x55), encoding="mulaw_8k")
        rec.close()

        params, left, right = read_wav(tmp_path / "r.wav")
        # 50 frames × 160 mulaw bytes @8k → ×2 upsample ≈ 16000 samples (1 s).
        # audioop.ratecv holds back up to a couple of samples of filter
        # state, hence the small tolerance.
        assert abs(params.nframes - RECORDING_SAMPLE_RATE) <= 2
        # Decoded mulaw 0x55 has a known non-zero PCM value — the caller
        # channel must carry energy, not silence.
        expected = struct.unpack("<h", mulaw_to_pcm16(b"\x55"))[0]
        assert expected != 0
        assert expected in set(left)
        assert set(right) != {0}

    def test_pcm16_24k_agent_resampled(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        # 480 samples @24k == 20 ms == 320 samples @16k
        rec.add_agent_audio(pcm_frame(5, samples=480), encoding="pcm16_24k")
        rec.add_caller_audio(pcm_frame(1))  # 20 ms clock
        rec.close()

        params, _, right = read_wav(tmp_path / "r.wav")
        assert params.nframes == FRAME_SAMPLES_20MS
        assert any(v != 0 for v in right)

    def test_odd_length_pcm_chunks_carry(self, tmp_path: Path) -> None:
        """Odd-byte PCM chunks never crash; the carry keeps sample alignment."""
        rec = LocalCallRecorder(tmp_path / "r.wav")
        blob = pcm_frame(99, samples=100)  # 200 bytes
        rec.add_caller_audio(blob[:33])
        rec.add_caller_audio(blob[33:])
        rec.close()
        params, left, _ = read_wav(tmp_path / "r.wav")
        assert params.nframes == 100
        assert set(left) == {99}

    def test_unknown_encoding_disables_not_raises(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        rec.add_caller_audio(b"\x00\x00", encoding="vorbis")  # type: ignore[arg-type]
        # Recorder disabled itself; subsequent calls are no-ops, close() -> None
        rec.add_caller_audio(pcm_frame(1))
        assert rec.close() is None


# ---------------------------------------------------------------------------
# Bounded buffers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBoundedBuffers:
    def test_agent_backlog_capped_with_forced_flush(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        # Shrink the cap so the test stays fast: 100 ms of backlog max.
        rec._max_backlog_bytes = RECORDING_SAMPLE_RATE // 10 * 2
        for _ in range(20):  # 400 ms of agent audio, no caller frames
            rec.add_agent_audio(pcm_frame(3))
            assert len(rec._agent_backlog) <= rec._max_backlog_bytes
        # Overflow was committed to disk (paired with left silence).
        assert rec.frames_written > 0
        rec.close()
        params, left, right = read_wav(tmp_path / "r.wav")
        assert params.nframes == 20 * FRAME_SAMPLES_20MS  # nothing lost
        assert set(left) == {0}
        assert set(right) == {3}

    def test_default_backlog_cap_value(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        assert rec._max_backlog_bytes == AGENT_BACKLOG_CAP_S * RECORDING_SAMPLE_RATE * 2
        rec.close()


# ---------------------------------------------------------------------------
# Buffered writes (hot-path I/O profile)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBufferedWrites:
    def test_no_per_frame_disk_io(self, tmp_path: Path) -> None:
        """Frames accumulate in the 64 KiB userspace buffer — the on-disk
        file holds only the (eagerly flushed) header until the buffer fills
        or the recorder closes. Pins the 'no per-frame disk I/O' contract."""
        path = tmp_path / "r.wav"
        rec = LocalCallRecorder(path)
        for _ in range(10):  # 200 ms = 12.8 KB stereo, well under 64 KiB
            rec.add_caller_audio(pcm_frame(5))
        assert rec.frames_written == 10 * FRAME_SAMPLES_20MS  # logically appended
        assert path.stat().st_size == 44  # ... but not yet hitting disk
        rec.close()
        assert path.stat().st_size == 44 + 10 * FRAME_SAMPLES_20MS * 4

    def test_buffer_flushes_to_disk_once_full(self, tmp_path: Path) -> None:
        path = tmp_path / "r.wav"
        rec = LocalCallRecorder(path)
        # 64 KiB / 1280 B per 20 ms stereo frame = 51.2 frames to fill the
        # buffer; 60 frames guarantees at least one real disk write.
        for _ in range(60):
            rec.add_caller_audio(pcm_frame(5))
        assert path.stat().st_size > 44
        rec.close()
        params, left, _ = read_wav(path)
        assert params.nframes == 60 * FRAME_SAMPLES_20MS
        assert set(left) == {5}


# ---------------------------------------------------------------------------
# Finalization + teardown robustness
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFinalization:
    def test_placeholder_header_patched_on_close(self, tmp_path: Path) -> None:
        path = tmp_path / "r.wav"
        rec = LocalCallRecorder(path)
        rec.add_caller_audio(pcm_frame(1))
        # Before close the placeholder header carries zero sizes.
        with path.open("rb") as fh:
            header = fh.read(44)
        assert struct.unpack_from("<I", header, 40)[0] == 0
        rec.close()
        with path.open("rb") as fh:
            header = fh.read(44)
        data_size = struct.unpack_from("<I", header, 40)[0]
        assert data_size == FRAME_SAMPLES_20MS * 4  # stereo 16-bit
        assert struct.unpack_from("<I", header, 4)[0] == 36 + data_size

    def test_truncated_teardown_still_parseable(self, tmp_path: Path) -> None:
        """A call torn down abruptly mid-stream (cleanup path runs close())
        leaves a parseable WAV with whatever audio made it to disk."""
        path = tmp_path / "r.wav"
        rec = LocalCallRecorder(path)
        rec.add_caller_audio(pcm_frame(11))
        rec.add_agent_audio(pcm_frame(-11))  # still buffered — simulates mid-call
        rec.close()  # the abnormal-teardown cleanup path

        params, left, right = read_wav(path)
        # Caller frame + close-drained agent tail are both present.
        assert params.nframes == 2 * FRAME_SAMPLES_20MS
        assert -11 in set(right)

    def test_close_idempotent_and_returns_path(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        rec.add_caller_audio(pcm_frame(1))
        first = rec.close()
        second = rec.close()
        assert first == second == str(tmp_path / "r.wav")
        assert rec.closed

    def test_adds_after_close_are_noops(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        rec.add_caller_audio(pcm_frame(1))
        rec.close()
        rec.add_caller_audio(pcm_frame(2))
        rec.add_agent_audio(pcm_frame(2))
        params, _, _ = read_wav(tmp_path / "r.wav")
        assert params.nframes == FRAME_SAMPLES_20MS

    def test_io_error_disables_recorder_without_raising(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        rec._fh.close()  # simulate the descriptor dying mid-call
        rec.add_caller_audio(pcm_frame(1))  # must not raise
        assert rec._broken
        assert rec.close() is None

    def test_duration_seconds_tracks_committed_frames(self, tmp_path: Path) -> None:
        rec = LocalCallRecorder(tmp_path / "r.wav")
        for _ in range(25):  # 0.5 s
            rec.add_caller_audio(pcm_frame(1))
        assert rec.duration_seconds == pytest.approx(0.5)
        rec.close()
