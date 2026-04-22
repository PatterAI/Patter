"""Unit tests for OpenAITTS streaming resample (BUG #23).

OpenAI's TTS-1 returns PCM16 at 24 kHz but the telephony bridge expects
PCM16 at 16 kHz. Early versions downsampled each streamed chunk with a
stateless call to :func:`audioop.ratecv`, which discarded the resampler's
filter state between chunks — the caller heard "pops" between chunks, or
empty audio when a chunk's payload was smaller than the filter kernel.

The fix (see :mod:`getpatter.providers.openai_tts`) keeps the ``ratecv``
state across chunks **and** carries an odd trailing byte forward so every
call receives a whole number of 16-bit samples. These tests lock both
invariants in:

  1. **Stream parity** — downsampling a sequence of chunks yields (within
     the ratecv initial-state tolerance) the same bytes as downsampling
     the concatenated audio in one shot. This is the primary regression.
  2. **Carry-byte** — when the boundary between two chunks falls on an
     odd byte, the resample still produces a whole number of output
     samples and the lost byte is not dropped.
  3. **Empty / partial** — empty chunks, single-byte chunks, and chunks
     with no usable sample bytes must not crash the generator.
"""

from __future__ import annotations

import struct
from typing import AsyncIterator, Iterable, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.providers.openai_tts import OpenAITTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pcm16(samples: Iterable[int]) -> bytes:
    """Pack signed 16-bit LE samples into a bytes blob."""
    seq = list(samples)
    return struct.pack(f"<{len(seq)}h", *seq)


def _make_tts() -> OpenAITTS:
    return OpenAITTS(api_key="sk-test", voice="alloy", model="tts-1")


def _wire_mock_stream(tts: OpenAITTS, chunks: List[bytes]) -> AsyncMock:
    """Wire an httpx stream mock that yields ``chunks`` from ``aiter_bytes``."""
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.aclose = AsyncMock()

    async def _aiter_bytes(chunk_size: int = 4096) -> AsyncIterator[bytes]:
        for c in chunks:
            yield c

    mock_resp.aiter_bytes = _aiter_bytes

    tts._client = AsyncMock()
    tts._client.build_request.return_value = MagicMock()
    tts._client.send.return_value = mock_resp
    return mock_resp


async def _drain(tts: OpenAITTS, chunks: List[bytes]) -> bytes:
    """Feed ``chunks`` through synthesize and concatenate the output."""
    _wire_mock_stream(tts, chunks)
    out = bytearray()
    async for resampled in tts.synthesize("irrelevant"):
        out.extend(resampled)
    return bytes(out)


# ---------------------------------------------------------------------------
# Stream parity — cross-chunk filter state
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestStateCarriesAcrossChunks:
    """Splitting the source audio across chunks must not warp the output.

    With preserved ``ratecv`` state, the concatenation of per-chunk outputs
    equals a single-shot downsample of the whole buffer. Without state
    preservation, the second chunk would start from a cold filter and the
    prefix would differ — which is what BUG #23 produced in production.
    """

    async def test_two_chunks_match_single_shot(self) -> None:
        # 600 samples @ 24 kHz = 25 ms of audio — enough to feel the filter.
        all_samples = [int(1000 * (i % 31 - 15)) for i in range(600)]
        full = _pcm16(all_samples)
        mid = (len(full) // 2) // 2 * 2  # split on an even-byte boundary
        split = [full[:mid], full[mid:]]

        tts_single = _make_tts()
        tts_split = _make_tts()
        single_out = await _drain(tts_single, [full])
        split_out = await _drain(tts_split, split)

        # With state preserved the two outputs match exactly byte-for-byte.
        assert split_out == single_out

    async def test_many_small_chunks_match_single_shot(self) -> None:
        """Same invariant across 10 back-to-back chunks of 100 samples each."""
        all_samples = [int(500 * (i % 13 - 6)) for i in range(1000)]
        full = _pcm16(all_samples)
        # 10 chunks of 200 bytes each = 100 samples each.
        chunks = [full[i : i + 200] for i in range(0, len(full), 200)]
        assert len(chunks) == 10

        tts_single = _make_tts()
        tts_split = _make_tts()
        single_out = await _drain(tts_single, [full])
        split_out = await _drain(tts_split, chunks)

        assert split_out == single_out


# ---------------------------------------------------------------------------
# Odd-byte carry across chunk boundaries
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestOddByteCarry:
    """Boundaries on odd byte offsets must still preserve the PCM16 sample.

    When httpx splits the response on an odd byte the implementation must
    buffer the dangling byte and prepend it to the next chunk. A naïve
    ``len // 2 * 2`` truncation drops the byte permanently and the second
    chunk decodes as a shifted sample stream (loud click / static).
    """

    async def test_odd_split_matches_single_shot(self) -> None:
        all_samples = [int(800 * (i % 11 - 5)) for i in range(400)]
        full = _pcm16(all_samples)
        # Split so the first chunk ends on an odd byte.
        odd_boundary = 201
        chunks = [full[:odd_boundary], full[odd_boundary:]]

        tts_single = _make_tts()
        tts_split = _make_tts()
        single_out = await _drain(tts_single, [full])
        split_out = await _drain(tts_split, chunks)

        assert split_out == single_out


# ---------------------------------------------------------------------------
# Degenerate chunks
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestEdgeCases:
    """Tiny and empty chunks must not break the stream."""

    async def test_empty_chunk_is_skipped(self) -> None:
        all_samples = [int(100 * (i % 7 - 3)) for i in range(200)]
        full = _pcm16(all_samples)

        tts_single = _make_tts()
        tts_split = _make_tts()
        baseline = await _drain(tts_single, [full])
        # Interleave an empty chunk — it must be ignored.
        mixed = await _drain(
            tts_split, [full[:100], b"", full[100:], b""]
        )

        assert mixed == baseline

    async def test_single_byte_chunk_buffered(self) -> None:
        """A chunk consisting of exactly one byte must not crash the generator."""
        all_samples = [int(50 * (i % 5 - 2)) for i in range(100)]
        full = _pcm16(all_samples)

        tts_single = _make_tts()
        tts_split = _make_tts()
        baseline = await _drain(tts_single, [full])
        # First chunk is a single byte — forces carry-only on first iteration.
        mixed = await _drain(tts_split, [full[:1], full[1:]])

        assert mixed == baseline

    async def test_stream_closes_response_on_completion(self) -> None:
        """The httpx response must be closed even after a successful stream."""
        tts = _make_tts()
        mock_resp = _wire_mock_stream(tts, [_pcm16([0] * 20)])
        async for _ in tts.synthesize("hi"):
            pass
        mock_resp.aclose.assert_awaited_once()

    async def test_stream_closes_response_on_early_exit(self) -> None:
        """Early generator termination must still drain the response."""
        tts = _make_tts()
        mock_resp = _wire_mock_stream(
            tts, [_pcm16([0] * 20), _pcm16([0] * 20)]
        )
        gen = tts.synthesize("hi")
        # Consume only the first resampled chunk, then close.
        async for _ in gen:
            break
        await gen.aclose()
        mock_resp.aclose.assert_awaited_once()
