"""Unit tests for :class:`getpatter.audio.aec.NlmsEchoCanceller`.

Tests use synthetic audio (deterministic sine + noise mixture) to verify
convergence and double-talk preservation without depending on a real
acoustic environment.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from getpatter.audio.aec import NlmsEchoCanceller  # noqa: E402


SR = 16000
RNG = np.random.default_rng(seed=0xA1C)


def _voicelike(num_samples: int) -> np.ndarray:
    """Synthesize a deterministic narrowband voice-like signal."""
    t = np.arange(num_samples).astype(np.float32) / SR
    return (
        0.3 * np.sin(2 * np.pi * 220 * t)
        + 0.2 * np.sin(2 * np.pi * 440 * t)
        + 0.05 * RNG.standard_normal(num_samples).astype(np.float32)
    ).astype(np.float32)


def _to_int16(arr: np.ndarray) -> bytes:
    return (np.clip(arr, -1, 1) * 32000).astype(np.int16).tobytes()


def _from_int16(buf: bytes) -> np.ndarray:
    return np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32000


def _make_echo(
    far: np.ndarray, delay_samples: int = 1280, gain: float = 0.5
) -> np.ndarray:
    """Inject a delayed + low-passed copy of ``far`` to simulate the
    speakerphone-to-mic acoustic path.
    """
    echo = np.zeros_like(far, dtype=np.float32)
    if delay_samples < far.size:
        echo[delay_samples:] = gain * far[: far.size - delay_samples]
    # Crude low-pass to mimic the smearing of a real room/handset.
    return np.convolve(echo, np.ones(8) / 8, mode="same").astype(np.float32)


@pytest.mark.unit
class TestNlmsEchoCanceller:
    """NLMS adaptive filter behaviour."""

    def test_rejects_unsupported_sample_rate(self) -> None:
        with pytest.raises(ValueError, match="8000 Hz or 16000 Hz only"):
            NlmsEchoCanceller(sample_rate=44100)

    def test_rejects_too_few_taps(self) -> None:
        with pytest.raises(ValueError, match="filter_taps must be"):
            NlmsEchoCanceller(filter_taps=32)

    def test_rejects_invalid_step_size(self) -> None:
        with pytest.raises(ValueError, match="step_size"):
            NlmsEchoCanceller(step_size=0.0)

    def test_pass_through_until_far_end_buffer_fills(self) -> None:
        """No far-end pushed yet → near-end returns unchanged bytes."""
        aec = NlmsEchoCanceller(filter_taps=512)
        near = _to_int16(_voicelike(1024))
        out = aec.process_near_end(near)
        assert out == near, "AEC must be a pass-through before convergence"

    def test_converges_to_at_least_10_db_erle(self) -> None:
        """After 1 s of TTS-only echo training, the filter should achieve
        at least 10 dB ERLE (echo-return loss enhancement). 24 dB is
        typical on this synthetic test signal."""
        aec = NlmsEchoCanceller(filter_taps=512, step_size=0.2)
        far = _voicelike(SR)
        echo = _make_echo(far)
        aec.push_far_end(_to_int16(far))
        cleaned = _from_int16(aec.process_near_end(_to_int16(echo)))

        # Skip the convergence period (first 70%); look at residual on
        # the tail where the filter should have settled.
        tail_start = int(0.7 * SR)
        in_pwr = float(np.mean(echo[tail_start:] ** 2))
        out_pwr = float(np.mean(cleaned[tail_start:] ** 2))
        erle_db = 10 * np.log10(in_pwr / max(out_pwr, 1e-10))
        assert erle_db >= 10.0, f"ERLE only {erle_db:.1f} dB after 1s training"

    def test_double_talk_preserves_near_speech(self) -> None:
        """When the caller speaks DURING TTS, the Geigel detector freezes
        adaptation so the near-end signal isn't cancelled along with
        the echo."""
        aec = NlmsEchoCanceller(filter_taps=512, step_size=0.2)
        far = _voicelike(SR)
        echo = _make_echo(far)

        # Phase 1 — train on echo-only audio for 0.7 s.
        n_train = int(0.7 * SR)
        aec.push_far_end(_to_int16(far[:n_train]))
        _ = aec.process_near_end(_to_int16(echo[:n_train]))

        # Phase 2 — caller speaks (different frequency than far-end).
        t = np.arange(SR - n_train).astype(np.float32) / SR
        near_speech = (0.4 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
        combined = echo[n_train:] + near_speech

        aec.push_far_end(_to_int16(far[n_train:]))
        cleaned = _from_int16(aec.process_near_end(_to_int16(combined)))

        speech_in_pwr = float(np.mean(near_speech**2))
        cleaned_pwr = float(np.mean(cleaned**2))

        # The near-end speech power must survive — within 6 dB of the
        # original. Less than 50 % of the input power means the filter
        # adapted the user's voice into its echo estimate.
        assert cleaned_pwr >= 0.5 * speech_in_pwr, (
            f"Near speech attenuated to {cleaned_pwr / speech_in_pwr:.0%} "
            f"of input — Geigel detector failed."
        )
        # And the detector should have observed at least one double-talk
        # frame in the second phase.
        assert aec.double_talk_frames >= 1

    def test_reset_clears_filter_state(self) -> None:
        """``reset()`` returns the canceller to a fresh-construction state."""
        aec = NlmsEchoCanceller(filter_taps=512, step_size=0.2)
        far = _voicelike(SR // 2)
        aec.push_far_end(_to_int16(far))
        _ = aec.process_near_end(_to_int16(_make_echo(far)))
        assert aec.frames_processed >= 1

        aec.reset()
        assert aec.frames_processed == 0
        assert aec.double_talk_frames == 0
        # Re-priming should see a full pass-through period again.
        # ``_voicelike`` uses a non-deterministic RNG, so capture the
        # bytes once and reuse for both the call and the comparison.
        probe = _to_int16(_voicelike(1024))
        out = aec.process_near_end(probe)
        # Equivalent to the original int16 because no far-end has been
        # pushed since reset.
        assert out == probe

    def test_handles_empty_buffer_input(self) -> None:
        """Empty far-end push and empty near-end process are no-ops."""
        aec = NlmsEchoCanceller(filter_taps=512)
        aec.push_far_end(b"")  # must not raise
        out = aec.process_near_end(b"")
        assert out == b""

    def test_warmup_converges_within_first_second(self) -> None:
        """The default warmup (5× step for 0.5 s) must deliver ≥10 dB ERLE
        within the FIRST 250 ms window when fed broadband audio in
        realistic 20 ms frames.

        This is the regression-guard for the cellular-call slow-convergence
        bug observed on 0.6.0 with 2048 taps + constant step: a real call
        showed 8–12 s convergence and the user's first turn was lost.
        """
        # Broadband signal — sinusoidal-only inputs are rank-deficient and
        # converge slowly under NLMS regardless of step / taps.
        rng = np.random.default_rng(seed=42)
        t = np.arange(SR).astype(np.float32) / SR
        far = (
            0.4 * np.sin(2 * np.pi * 220 * t)
            + 0.3 * np.sin(2 * np.pi * 440 * t)
            + 0.2 * np.sin(2 * np.pi * 880 * t)
            + 0.15 * rng.standard_normal(SR).astype(np.float32)
        ).astype(np.float32)
        echo = _make_echo(far, delay_samples=int(0.05 * SR))

        aec = NlmsEchoCanceller()  # default 512 taps + warmup
        # Process in realistic 20 ms frames (320 samples) to mimic the SDK.
        frame = 320
        out_chunks: list[np.ndarray] = []
        for i in range(0, far.size - frame, frame):
            aec.push_far_end(_to_int16(far[i : i + frame]))
            out_chunks.append(
                _from_int16(aec.process_near_end(_to_int16(echo[i : i + frame])))
            )
        out = np.concatenate(out_chunks)

        # First 250 ms of OUTPUT — this is when the user's first turn lives.
        first = 250 * SR // 1000
        in_pwr = float(np.mean(echo[:first] ** 2))
        out_pwr = float(np.mean(out[:first] ** 2))
        erle = 10 * np.log10(in_pwr / max(out_pwr, 1e-10))
        assert erle >= 10.0, (
            f"Warmup did not converge fast enough: ERLE only {erle:.1f} dB "
            f"in the first 250 ms (target >= 10 dB)."
        )
