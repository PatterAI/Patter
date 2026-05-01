"""Unit tests for :mod:`getpatter.audio.pcm_mixer`.

All tests are deterministic and use synthetic PCM (sine waves + silence).
No audio playback or external assets are required.
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("numpy", reason="pcm_mixer requires numpy")

import numpy as np  # noqa: E402

from getpatter.audio.pcm_mixer import PcmMixer  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sine_pcm(freq: float, duration_ms: int, sample_rate: int, amplitude: int = 10_000) -> bytes:
    """Generate a mono 16-bit PCM sine wave of the given frequency/duration."""
    n = int(sample_rate * duration_ms / 1000)
    t = np.arange(n) / sample_rate
    samples = (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.int16)
    return samples.tobytes()


def _rms_int16(pcm: bytes) -> float:
    """Root-mean-square amplitude of an int16 PCM buffer."""
    if not pcm:
        return 0.0
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(arr**2)))


# ---------------------------------------------------------------------------
# Core mix math
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mix_silent_background_is_identity():
    mixer = PcmMixer()
    agent = _sine_pcm(440, 20, 16000)
    silence = b"\x00\x00" * (16000 * 20 // 1000)
    assert mixer.mix(agent, silence, ratio=0.5) == agent


@pytest.mark.unit
def test_mix_zero_ratio_is_identity():
    mixer = PcmMixer()
    agent = _sine_pcm(440, 20, 16000)
    bg = _sine_pcm(220, 20, 16000)
    assert mixer.mix(agent, bg, ratio=0.0) == agent


@pytest.mark.unit
def test_mix_empty_background_is_identity():
    mixer = PcmMixer()
    agent = _sine_pcm(440, 20, 16000)
    assert mixer.mix(agent, b"", ratio=0.5) == agent


@pytest.mark.unit
def test_mix_empty_agent_returns_empty():
    mixer = PcmMixer()
    bg = _sine_pcm(440, 20, 16000)
    assert mixer.mix(b"", bg, ratio=0.5) == b""


@pytest.mark.unit
def test_mix_preserves_output_length():
    """Output length must equal agent length regardless of bg length."""
    mixer = PcmMixer()
    agent = _sine_pcm(440, 20, 16000)  # 640 bytes

    # Shorter bg — must be zero-padded.
    short_bg = _sine_pcm(220, 10, 16000)
    assert len(mixer.mix(agent, short_bg, ratio=0.5)) == len(agent)

    # Longer bg — must be truncated.
    long_bg = _sine_pcm(220, 40, 16000)
    assert len(mixer.mix(agent, long_bg, ratio=0.5)) == len(agent)


@pytest.mark.unit
def test_mix_known_input_analytic_rms():
    """Verify RMS of ``sine_a + sine_b * 0.25`` against closed-form value.

    Two orthogonal sine waves (different frequency, integer cycles in the
    window) have ``RMS(a + k*b)^2 = RMS(a)^2 + k^2 * RMS(b)^2`` because the
    cross term integrates to zero. We check this within 2%.
    """
    mixer = PcmMixer()
    sr = 16000
    # Use frequencies with integer cycles in a 100 ms window so they are
    # orthogonal: 440 Hz * 0.1 s = 44 cycles; 880 Hz * 0.1 s = 88 cycles.
    agent = _sine_pcm(440, 100, sr, amplitude=10_000)
    bg = _sine_pcm(880, 100, sr, amplitude=10_000)
    ratio = 0.25

    mixed = mixer.mix(agent, bg, ratio=ratio)

    rms_agent = _rms_int16(agent)
    rms_bg = _rms_int16(bg)
    expected = math.sqrt(rms_agent**2 + (ratio * rms_bg) ** 2)
    actual = _rms_int16(mixed)

    # 2% tolerance absorbs int16 quantisation.
    assert abs(actual - expected) / expected < 0.02, (
        f"RMS mismatch: expected~{expected:.1f}, got {actual:.1f}"
    )


@pytest.mark.unit
def test_mix_clips_to_int16_range():
    """Full-scale agent + full-scale bg with ratio=1.0 must clip, not wrap."""
    mixer = PcmMixer(clip=True)

    # 200 samples of near-max positive value.
    full_scale = np.full(200, 30_000, dtype=np.int16).tobytes()
    mixed = mixer.mix(full_scale, full_scale, ratio=1.0)
    arr = np.frombuffer(mixed, dtype=np.int16)

    assert arr.min() >= -32768
    assert arr.max() <= 32767
    # Agent+bg should be 60 000 which clips to 32 767.
    assert arr.max() == 32767
    assert (arr == 32767).all()


@pytest.mark.unit
def test_mix_no_clip_allows_overflow_for_inspection():
    """clip=False is a testing hook; resulting bytes are int16-truncated."""
    mixer = PcmMixer(clip=False)
    full_scale = np.full(8, 30_000, dtype=np.int16).tobytes()
    mixed = mixer.mix(full_scale, full_scale, ratio=1.0)
    arr = np.frombuffer(mixed, dtype=np.int16)
    # 60 000 overflows into int16 as -5536.
    assert arr[0] == -5536


@pytest.mark.unit
def test_mix_rejects_odd_byte_lengths():
    mixer = PcmMixer()
    with pytest.raises(ValueError, match="whole number of 16-bit samples"):
        mixer.mix(b"\x00\x00\x00", b"\x00\x00", ratio=0.1)
    with pytest.raises(ValueError, match="whole number of 16-bit samples"):
        mixer.mix(b"\x00\x00", b"\x00", ratio=0.1)


@pytest.mark.unit
def test_mix_output_contains_both_frequencies_via_fft():
    """FFT spot check — mixed signal must carry energy at both input freqs."""
    sr = 16000
    agent_freq = 500  # integer cycles in 100 ms
    bg_freq = 1500
    agent = _sine_pcm(agent_freq, 100, sr, amplitude=8_000)
    bg = _sine_pcm(bg_freq, 100, sr, amplitude=8_000)

    mixer = PcmMixer()
    mixed = mixer.mix(agent, bg, ratio=0.5)

    arr = np.frombuffer(mixed, dtype=np.int16).astype(np.float64)
    spectrum = np.abs(np.fft.rfft(arr))
    freqs = np.fft.rfftfreq(arr.shape[0], d=1 / sr)

    def _peak_near(target: float) -> float:
        idx = int(np.argmin(np.abs(freqs - target)))
        return float(spectrum[max(idx - 1, 0) : idx + 2].max())

    noise_floor = float(np.median(spectrum))
    assert _peak_near(agent_freq) > 20 * noise_floor
    assert _peak_near(bg_freq) > 5 * noise_floor
