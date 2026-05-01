"""Unit tests for :mod:`getpatter.services.background_audio`.

Covers:
* Bundled-asset presence + ``OggS`` magic byte verification.
* ``BuiltinAudioClip`` -> filesystem path resolution via ``importlib.resources``.
* Real decoding of each ``.ogg`` via ``soundfile``.
* ``BackgroundAudioPlayer.start / mix / stop`` round-trips with a short
  synthesised WAV clip, verified via FFT to ensure both agent and background
  frequencies survive the mix.
* Probability-weighted ``AudioConfig`` selection: empirical frequencies must
  match the configured probabilities within 5%.

Tests are marked ``unit`` — they are pure-Python and complete in well under
a second.
"""

from __future__ import annotations

import math
import wave
from collections import Counter
from importlib import resources
from pathlib import Path

import pytest

pytest.importorskip("numpy", reason="background_audio requires numpy")
pytest.importorskip("soundfile", reason="background_audio requires soundfile")

import numpy as np  # noqa: E402

from getpatter.services.background_audio import (  # noqa: E402
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_sine_wav(
    path: Path,
    freq: float,
    duration_ms: int,
    sample_rate: int,
    amplitude: int = 12_000,
) -> None:
    """Write a mono 16-bit PCM WAV with a sine tone."""
    n = int(sample_rate * duration_ms / 1000)
    t = np.arange(n) / sample_rate
    samples = (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def _sine_pcm(freq: float, duration_ms: int, sample_rate: int, amplitude: int = 8_000) -> bytes:
    n = int(sample_rate * duration_ms / 1000)
    t = np.arange(n) / sample_rate
    samples = (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.int16)
    return samples.tobytes()


def _peak_near(
    arr: np.ndarray,
    sample_rate: int,
    target: float,
) -> float:
    spectrum = np.abs(np.fft.rfft(arr))
    freqs = np.fft.rfftfreq(arr.shape[0], d=1 / sample_rate)
    idx = int(np.argmin(np.abs(freqs - target)))
    return float(spectrum[max(idx - 1, 0) : idx + 2].max())


# ---------------------------------------------------------------------------
# Bundled-resource integrity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_builtin_clips_exist_and_start_with_ogg_magic():
    """Every ``BuiltinAudioClip`` points at an ``OggS``-prefixed file."""
    for clip in BuiltinAudioClip:
        path = clip.path()
        with open(path, "rb") as f:
            header = f.read(4)
        assert header == b"OggS", f"{clip.value} missing OggS magic, got {header!r}"


@pytest.mark.unit
def test_resource_files_are_distributed():
    """``getpatter.resources.audio`` must ship 7 .ogg clips + NOTICE."""
    pkg = resources.files("getpatter.resources.audio")
    names = sorted(p.name for p in pkg.iterdir())
    ogg = [n for n in names if n.endswith(".ogg")]
    assert len(ogg) == 7, f"expected 7 .ogg files, got {ogg}"
    assert "NOTICE" in names


@pytest.mark.unit
def test_decode_all_clips():
    """Decoding every bundled clip must yield a non-empty int16 buffer."""
    for clip in BuiltinAudioClip:
        pcm, sr = BackgroundAudioPlayer._decode_file(clip.path())
        assert pcm.dtype == np.int16, f"{clip.value}: dtype {pcm.dtype}"
        assert pcm.size > 0, f"{clip.value}: decoded empty"
        assert sr > 0, f"{clip.value}: sample_rate {sr}"


# ---------------------------------------------------------------------------
# Probability-weighted selection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_select_sound_from_list_empirical_distribution():
    """10 000 draws should match configured probabilities within 5%."""
    configs = [
        AudioConfig(source="a.ogg", probability=0.5),
        AudioConfig(source="b.ogg", probability=0.3),
        AudioConfig(source="c.ogg", probability=0.2),
    ]
    counts: Counter[str] = Counter()
    draws = 10_000
    for _ in range(draws):
        picked = BackgroundAudioPlayer._select_sound_from_list(configs)
        assert picked is not None
        counts[picked.source] += 1

    assert abs(counts["a.ogg"] / draws - 0.5) < 0.05
    assert abs(counts["b.ogg"] / draws - 0.3) < 0.05
    assert abs(counts["c.ogg"] / draws - 0.2) < 0.05


@pytest.mark.unit
def test_select_sound_from_list_allows_silence():
    """Probabilities summing below 1.0 must sometimes return ``None``."""
    configs = [AudioConfig(source="a.ogg", probability=0.3)]
    draws = 10_000
    silence = 0
    picks = 0
    for _ in range(draws):
        if BackgroundAudioPlayer._select_sound_from_list(configs) is None:
            silence += 1
        else:
            picks += 1
    # Expect ~70% silence.
    assert abs(silence / draws - 0.7) < 0.05
    assert picks > 0


@pytest.mark.unit
def test_select_sound_from_list_all_zero_returns_none():
    configs = [AudioConfig(source="a.ogg", probability=0.0)]
    assert BackgroundAudioPlayer._select_sound_from_list(configs) is None


# ---------------------------------------------------------------------------
# start / mix / stop integration (with synthetic WAV)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mix_before_start_is_noop():
    player = BackgroundAudioPlayer(BuiltinAudioClip.HOLD_MUSIC)
    agent = _sine_pcm(440, 20, 16000)
    assert await player.mix(agent, sample_rate=16000) == agent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mix_with_synthetic_wav_preserves_frequencies(tmp_path: Path):
    """Full start/mix round-trip: mixed buffer must carry both frequencies."""
    wav = tmp_path / "tone.wav"
    sr = 16000
    _write_sine_wav(wav, freq=1500, duration_ms=500, sample_rate=sr, amplitude=20_000)

    player = BackgroundAudioPlayer(str(wav), volume=0.5, loop=True)
    await player.start()
    try:
        # Request 100 ms of mix at the same SR -> no resample, clean FFT.
        agent = _sine_pcm(500, 100, sr, amplitude=8_000)
        mixed = await player.mix(agent, sample_rate=sr)
        assert len(mixed) == len(agent)

        arr = np.frombuffer(mixed, dtype=np.int16).astype(np.float64)
        peak_agent = _peak_near(arr, sr, 500)
        peak_bg = _peak_near(arr, sr, 1500)
        noise = float(np.median(np.abs(np.fft.rfft(arr))))

        assert peak_agent > 20 * noise
        assert peak_bg > 5 * noise
    finally:
        await player.stop()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mix_zero_volume_is_noop(tmp_path: Path):
    wav = tmp_path / "tone.wav"
    _write_sine_wav(wav, freq=1500, duration_ms=500, sample_rate=16000)

    player = BackgroundAudioPlayer(str(wav), volume=0.0, loop=True)
    await player.start()
    try:
        agent = _sine_pcm(500, 20, 16000)
        assert await player.mix(agent, sample_rate=16000) == agent
    finally:
        await player.stop()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_loop_source_exhausts(tmp_path: Path):
    """A 20 ms non-loop source must fall back to a no-op after one chunk."""
    wav = tmp_path / "tone.wav"
    sr = 16000
    _write_sine_wav(wav, freq=1500, duration_ms=20, sample_rate=sr)

    player = BackgroundAudioPlayer(str(wav), volume=0.5, loop=False)
    await player.start()
    try:
        agent = _sine_pcm(500, 20, sr)
        first = await player.mix(agent, sample_rate=sr)
        assert first != agent  # first chunk is mixed

        # After exhaustion, subsequent calls return agent unchanged.
        second = await player.mix(agent, sample_rate=sr)
        assert second == agent
    finally:
        await player.stop()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resample_to_different_sample_rate(tmp_path: Path):
    """Source recorded at 16 kHz must mix correctly when requested at 8 kHz."""
    wav = tmp_path / "tone.wav"
    src_sr = 16000
    _write_sine_wav(wav, freq=1000, duration_ms=500, sample_rate=src_sr, amplitude=20_000)

    player = BackgroundAudioPlayer(str(wav), volume=0.5, loop=True)
    await player.start()
    try:
        target_sr = 8000
        agent = _sine_pcm(500, 100, target_sr, amplitude=8_000)
        mixed = await player.mix(agent, sample_rate=target_sr)
        assert len(mixed) == len(agent)

        arr = np.frombuffer(mixed, dtype=np.int16).astype(np.float64)
        peak_agent = _peak_near(arr, target_sr, 500)
        peak_bg = _peak_near(arr, target_sr, 1000)
        noise = float(np.median(np.abs(np.fft.rfft(arr))))

        assert peak_agent > 20 * noise
        assert peak_bg > 5 * noise
    finally:
        await player.stop()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_releases_cached_pcm(tmp_path: Path):
    wav = tmp_path / "tone.wav"
    _write_sine_wav(wav, freq=1500, duration_ms=200, sample_rate=16000)

    player = BackgroundAudioPlayer(str(wav), volume=0.5, loop=True)
    await player.start()
    assert player._source_pcm is not None
    await player.stop()
    assert player._source_pcm is None
    assert not player._started


@pytest.mark.unit
@pytest.mark.asyncio
async def test_double_start_is_idempotent(tmp_path: Path):
    wav = tmp_path / "tone.wav"
    _write_sine_wav(wav, freq=1500, duration_ms=200, sample_rate=16000)

    player = BackgroundAudioPlayer(str(wav), loop=True)
    await player.start()
    first_pcm = player._source_pcm
    await player.start()
    assert player._source_pcm is first_pcm
    await player.stop()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_volume_rejected():
    with pytest.raises(ValueError, match=r"volume"):
        BackgroundAudioPlayer(BuiltinAudioClip.HOLD_MUSIC, volume=1.5)
    with pytest.raises(ValueError, match=r"volume"):
        BackgroundAudioPlayer(BuiltinAudioClip.HOLD_MUSIC, volume=-0.1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_audioconfig_list_silence_selection(monkeypatch):
    """When probability selection picks silence, mix is a no-op."""
    # Force random selection to return > total_probability so silence wins.
    import getpatter.services.background_audio as mod

    monkeypatch.setattr(mod.random, "random", lambda: 0.99)

    configs = [AudioConfig(source=BuiltinAudioClip.HOLD_MUSIC, probability=0.1)]
    player = BackgroundAudioPlayer(configs, volume=0.5)
    await player.start()
    try:
        agent = _sine_pcm(500, 20, 16000)
        assert await player.mix(agent, sample_rate=16000) == agent
    finally:
        await player.stop()
