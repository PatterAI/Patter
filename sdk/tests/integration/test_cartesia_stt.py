"""Integration tests for CartesiaSTT — skipped unless ``CARTESIA_API_KEY`` is set.

Real API calls. These tests send ~1 s of synthetic PCM audio (mostly silence
plus a 440 Hz sine wave). Since the audio contains no actual speech, we only
assert that the stream completes cleanly without errors — a real transcript
can't reasonably be expected from a tone.
"""

from __future__ import annotations

import asyncio
import math
import os
import struct

import pytest

pytest.importorskip("aiohttp")

from patter.providers.cartesia_stt import CartesiaSTT  # noqa: E402

HAS_API_KEY = bool(os.environ.get("CARTESIA_API_KEY"))


def _pcm_silence_plus_sine(
    duration_s: float = 1.0,
    sample_rate: int = 16000,
    tone_hz: float = 440.0,
) -> bytes:
    n_samples = int(duration_s * sample_rate)
    half = n_samples // 2
    frames: list[int] = [0] * half
    amplitude = 12000
    for i in range(n_samples - half):
        sample = int(amplitude * math.sin(2 * math.pi * tone_hz * i / sample_rate))
        frames.append(sample)
    return struct.pack("<%dh" % len(frames), *frames)


@pytest.mark.integration
@pytest.mark.skipif(not HAS_API_KEY, reason="CARTESIA_API_KEY not set")
async def test_real_api_connects_and_streams_audio() -> None:
    api_key = os.environ["CARTESIA_API_KEY"]
    stt = CartesiaSTT(api_key=api_key, sample_rate=16000)
    await stt.connect()

    audio = _pcm_silence_plus_sine(duration_s=1.0, sample_rate=16000)
    chunk_size = 16000 * 2 // 20
    for i in range(0, len(audio), chunk_size):
        await stt.send_audio(audio[i : i + chunk_size])
        await asyncio.sleep(0.05)

    # Collect up to 6 s.
    received: list[tuple[str, bool]] = []

    async def collect() -> None:
        async for t in stt.receive_transcripts():
            received.append((t.text, t.is_final))
            if t.is_final:
                return

    try:
        await asyncio.wait_for(collect(), timeout=6.0)
    except asyncio.TimeoutError:
        pass
    finally:
        await stt.close()

    # The test passes if we connected, sent audio, and shut down cleanly.
    # Cartesia may or may not emit a transcript for a pure tone.
    assert stt._ws is None, "WebSocket should be closed after close()"
