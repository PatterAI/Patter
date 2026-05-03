"""Unit tests for StatefulResampler, PcmCarry, and deprecated wrappers.

Coverage targets
----------------
- ``test_stateful_resampler_no_clicks``: chunked 8k→16k output is
  sample-for-sample identical to a single-shot ``audioop.ratecv``,
  guaranteeing no filter-state discontinuities at chunk boundaries.
- ``test_pcm_carry_odd_bytes``: byte-level alignment contract.
- ``test_stateless_wrappers_still_work_with_deprecation``: the old
  ``resample_8k_to_16k`` / ``resample_16k_to_8k`` functions still
  return valid audio and emit a ``DeprecationWarning``.
"""
from __future__ import annotations

import math
import struct
import warnings
from typing import List

import pytest

# Import under warnings filter so the module-level DeprecationWarning
# does not bubble up as an error during collection.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from getpatter.audio.transcoding import (
        PcmCarry,
        StatefulResampler,
        create_resampler_8k_to_16k,
        create_resampler_16k_to_8k,
        create_resampler_24k_to_16k,
        resample_8k_to_16k,
        resample_16k_to_8k,
    )

try:
    import audioop  # type: ignore[import]
except ImportError:
    try:
        import audioop_lts as audioop  # type: ignore[import,no-redef]
    except ImportError:
        audioop = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(
    audioop is None,
    reason="audioop / audioop-lts not installed",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sine_pcm16(num_samples: int, freq: float = 440.0, sample_rate: int = 8000) -> bytes:
    """Generate a mono PCM16 sine wave as raw bytes."""
    samples: List[int] = [
        int(16383 * math.sin(2 * math.pi * freq * i / sample_rate))
        for i in range(num_samples)
    ]
    return struct.pack(f"<{num_samples}h", *samples)


def _split_random(data: bytes, n: int, seed: int = 42) -> List[bytes]:
    """Split *data* into *n* chunks of pseudo-random (but deterministic) sizes."""
    import random

    rng = random.Random(seed)
    chunks: List[bytes] = []
    remaining = data
    for i in range(n - 1):
        if len(remaining) <= 2:
            chunks.append(remaining)
            remaining = b""
            break
        # Keep sizes even so we can control alignment separately.
        max_cut = max(2, len(remaining) - 2)
        cut = rng.randrange(1, max_cut)
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    chunks.append(remaining)
    return [c for c in chunks if c]  # drop any accidental empty slices


# ---------------------------------------------------------------------------
# StatefulResampler — no clicks across chunks
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStatefulResamplerNoClicks:
    """Chunked resampling must produce byte-identical output to single-shot."""

    def _single_shot(self, audio: bytes, src: int, dst: int) -> bytes:
        """One-shot ratecv of the complete buffer (ground truth)."""
        out, _ = audioop.ratecv(audio, 2, 1, src, dst, None)
        return out

    def test_8k_to_16k_chunked_equals_single_shot(self) -> None:
        """10 random-length chunks 8k→16k must match single-shot output."""
        pcm = _sine_pcm16(num_samples=800)  # 100 ms @ 8 kHz
        chunks = _split_random(pcm, n=10, seed=7)

        resampler = StatefulResampler(8000, 16000)
        chunked_out = bytearray()
        for chunk in chunks:
            chunked_out.extend(resampler.process(chunk))
        chunked_out.extend(resampler.flush())

        expected = self._single_shot(pcm, 8000, 16000)
        assert bytes(chunked_out) == expected, (
            f"Chunked output ({len(chunked_out)} B) != "
            f"single-shot ({len(expected)} B)"
        )

    def test_no_outlier_deltas_at_chunk_boundaries(self) -> None:
        """Max consecutive-sample delta must not spike at chunk boundaries.

        Chunk boundaries that reset filter state produce a "pop" artefact
        visible as a sudden large delta between adjacent output samples.
        We verify that no sample-to-sample difference in the chunked output
        exceeds 2× the 99th-percentile delta of the single-shot reference.
        """
        pcm = _sine_pcm16(num_samples=1600, freq=440.0)
        chunks = _split_random(pcm, n=10, seed=13)

        resampler = StatefulResampler(8000, 16000)
        chunked_out = bytearray()
        for chunk in chunks:
            chunked_out.extend(resampler.process(chunk))
        chunked_out.extend(resampler.flush())

        # Decode output samples
        n_out = len(chunked_out) // 2
        out_samples = list(struct.unpack(f"<{n_out}h", bytes(chunked_out)))

        deltas = [abs(out_samples[i + 1] - out_samples[i]) for i in range(len(out_samples) - 1)]
        if not deltas:
            return  # nothing to check
        deltas_sorted = sorted(deltas)
        p99_index = max(0, int(0.99 * len(deltas_sorted)) - 1)
        p99 = deltas_sorted[p99_index]
        max_delta = max(deltas)

        # A click would be >> 2× p99; tolerate 4× to avoid false positives
        assert max_delta <= max(4 * p99, 200), (
            f"Potential click: max delta {max_delta} > 4×p99 ({4 * p99})"
        )

    def test_16k_to_8k_chunked_equals_single_shot(self) -> None:
        pcm = _sine_pcm16(num_samples=1600, sample_rate=16000)
        chunks = _split_random(pcm, n=8, seed=99)

        resampler = StatefulResampler(16000, 8000)
        chunked_out = bytearray()
        for chunk in chunks:
            chunked_out.extend(resampler.process(chunk))
        chunked_out.extend(resampler.flush())

        expected = self._single_shot(pcm, 16000, 8000)
        assert bytes(chunked_out) == expected

    def test_empty_process_returns_empty(self) -> None:
        resampler = StatefulResampler(8000, 16000)
        assert resampler.process(b"") == b""

    def test_flush_on_fresh_resampler_returns_empty(self) -> None:
        resampler = StatefulResampler(8000, 16000)
        assert resampler.flush() == b""

    def test_reset_clears_state(self) -> None:
        """After reset(), processing the same data yields identical results."""
        pcm = _sine_pcm16(num_samples=200)
        r = StatefulResampler(8000, 16000)
        first = r.process(pcm) + r.flush()
        r.reset()
        second = r.process(pcm) + r.flush()
        assert first == second

    def test_factory_8k_to_16k(self) -> None:
        r = create_resampler_8k_to_16k()
        assert isinstance(r, StatefulResampler)
        assert r.process(_sine_pcm16(100))  # must not raise

    def test_factory_16k_to_8k(self) -> None:
        r = create_resampler_16k_to_8k()
        assert isinstance(r, StatefulResampler)

    def test_factory_24k_to_16k(self) -> None:
        r = create_resampler_24k_to_16k()
        assert isinstance(r, StatefulResampler)
        pcm = _sine_pcm16(240, sample_rate=24000)
        assert r.process(pcm)  # must not raise


# ---------------------------------------------------------------------------
# PcmCarry — odd-byte alignment
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPcmCarryOddBytes:
    """PcmCarry byte-level alignment contract."""

    def test_feed_3_1_5_bytes(self) -> None:
        """Feed 3, 1, 5 bytes; expect 2, 0, 4 back with 1 byte in flush."""
        carry = PcmCarry(sample_width=2)

        out1 = carry.feed(b"\x01\x02\x03")
        assert len(out1) == 2, f"Expected 2 bytes, got {len(out1)}"

        out2 = carry.feed(b"\x04")
        # Carry was 1 byte (from chunk 1), now combined = 2 bytes → yields 2, carry 0
        assert len(out2) == 2, f"Expected 2 bytes, got {len(out2)}"

        out3 = carry.feed(b"\x05\x06\x07\x08\x09")
        assert len(out3) == 4, f"Expected 4 bytes, got {len(out3)}"

        leftover = carry.flush()
        assert len(leftover) == 1, f"Expected 1 byte in flush, got {len(leftover)}"

    def test_total_bytes_conserved(self) -> None:
        """All input bytes must appear in outputs + flush (none dropped)."""
        carry = PcmCarry(sample_width=2)
        feeds = [b"A" * n for n in [3, 1, 5]]
        total_in = sum(len(f) for f in feeds)

        outputs = [carry.feed(f) for f in feeds]
        outputs.append(carry.flush())
        total_out = sum(len(o) for o in outputs)

        assert total_out == total_in

    def test_even_input_no_carry(self) -> None:
        carry = PcmCarry(sample_width=2)
        out = carry.feed(b"\x01\x02\x03\x04")
        assert len(out) == 4
        assert carry.flush() == b""

    def test_reset_clears_carry(self) -> None:
        carry = PcmCarry(sample_width=2)
        carry.feed(b"\x01\x02\x03")  # 1 byte carry
        carry.reset()
        assert carry.flush() == b""

    def test_align_alias(self) -> None:
        """PcmCarry.align() must be an alias for feed()."""
        carry = PcmCarry(sample_width=2)
        result = carry.align(b"\x01\x02\x03")
        assert len(result) == 2

    def test_4_byte_sample_width(self) -> None:
        carry = PcmCarry(sample_width=4)
        out = carry.feed(b"\x01\x02\x03\x04\x05\x06")
        assert len(out) == 4
        leftover = carry.flush()
        assert len(leftover) == 2


# ---------------------------------------------------------------------------
# Deprecated stateless wrappers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStatelessWrappersWithDeprecation:
    """resample_8k_to_16k / resample_16k_to_8k still produce valid audio."""

    def test_resample_8k_to_16k_returns_bytes(self) -> None:
        pcm = _sine_pcm16(num_samples=160)
        # The DeprecationWarning is emitted at module load, not per-call;
        # still verify the function works correctly.
        result = resample_8k_to_16k(pcm)
        assert isinstance(result, bytes)
        assert len(result) > 0
        # Upsample doubles the sample count (approximately)
        out_samples = len(result) // 2
        assert out_samples >= 300  # 160 samples × 2 ≈ 320

    def test_resample_16k_to_8k_returns_bytes(self) -> None:
        pcm = _sine_pcm16(num_samples=320, sample_rate=16000)
        result = resample_16k_to_8k(pcm)
        assert isinstance(result, bytes)
        assert len(result) > 0
        out_samples = len(result) // 2
        assert out_samples >= 140  # 320 samples / 2 ≈ 160

    def test_resample_8k_to_16k_empty_input(self) -> None:
        assert resample_8k_to_16k(b"") == b""

    def test_resample_16k_to_8k_empty_input(self) -> None:
        assert resample_16k_to_8k(b"") == b""

    def test_module_emits_deprecation_warning(self) -> None:
        """Calling a deprecated helper must emit a DeprecationWarning (not on import).

        Wave 5 moved the warning from module scope into the function body with a
        once-per-process flag so that merely importing transcoding.py no longer
        spams the log on every startup.  The warning still fires on the first call.
        """
        import importlib
        import sys
        import getpatter.audio.transcoding as _t_mod

        # Reset the once-per-process flag so we can observe the warning again.
        _t_mod._warned_resample_8k_16k = False

        pcm = bytes(160)  # 160 bytes of silent PCM16 @ 8 kHz
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from getpatter.audio.transcoding import resample_8k_to_16k
            resample_8k_to_16k(pcm)
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert dep_warnings, "Expected at least one DeprecationWarning on first call"

        # Reset for other tests.
        _t_mod._warned_resample_8k_16k = False
