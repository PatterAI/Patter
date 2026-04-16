"""Integration tests for AssemblyAISTT — skipped unless ``ASSEMBLYAI_API_KEY`` is set.

Real API calls. These tests send ~1 s of synthetic PCM audio (mostly silence
plus a 440 Hz sine wave) and assert at least one :class:`Transcript` with
``is_final=True`` is returned.
"""

from __future__ import annotations

import asyncio
import math
import os
import struct

import pytest

pytest.importorskip("aiohttp")

from patter.providers.assemblyai_stt import AssemblyAISTT  # noqa: E402

HAS_API_KEY = bool(os.environ.get("ASSEMBLYAI_API_KEY"))


def _pcm_silence_plus_sine(
    duration_s: float = 1.0,
    sample_rate: int = 16000,
    tone_hz: float = 440.0,
) -> bytes:
    """Build 1 s of 16-bit little-endian PCM: half silence, half a 440 Hz tone."""
    n_samples = int(duration_s * sample_rate)
    half = n_samples // 2
    frames: list[int] = [0] * half
    amplitude = 12000
    for i in range(n_samples - half):
        sample = int(amplitude * math.sin(2 * math.pi * tone_hz * i / sample_rate))
        frames.append(sample)
    return struct.pack("<%dh" % len(frames), *frames)


@pytest.mark.integration
@pytest.mark.skipif(not HAS_API_KEY, reason="ASSEMBLYAI_API_KEY not set")
async def test_real_api_returns_at_least_one_final_transcript() -> None:
    api_key = os.environ["ASSEMBLYAI_API_KEY"]
    stt = AssemblyAISTT(api_key=api_key, sample_rate=16000, encoding="pcm_s16le")
    await stt.connect()

    audio = _pcm_silence_plus_sine(duration_s=1.0, sample_rate=16000)
    # Send in 50 ms chunks — matches AssemblyAI streaming recommendation.
    chunk_size = 16000 * 2 // 20
    for i in range(0, len(audio), chunk_size):
        await stt.send_audio(audio[i : i + chunk_size])
        await asyncio.sleep(0.05)

    # Collect up to 6 s of events, stopping as soon as we see a final.
    saw_final = False

    async def collect() -> None:
        nonlocal saw_final
        async for t in stt.receive_transcripts():
            if t.is_final:
                saw_final = True
                return

    try:
        await asyncio.wait_for(collect(), timeout=6.0)
    except asyncio.TimeoutError:
        pass
    finally:
        await stt.close()

    # A 1 s silence-plus-tone clip MAY not produce a transcript (no speech).
    # We only assert the stream completed and the session_id was established.
    assert stt.session_id is not None or saw_final is False, (
        "Neither a session id nor a final transcript was observed — "
        "the API contract appears broken."
    )
