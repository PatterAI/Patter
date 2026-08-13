"""Regression coverage for European Telnyx PCMA caller audio."""

from __future__ import annotations

import pytest

from getpatter.audio.transcoding import (
    alaw_to_mulaw,
    alaw_to_pcm16,
    pcm16_to_mulaw,
)


def test_alaw_silence_decodes_near_zero() -> None:
    pcm = alaw_to_pcm16(b"\xd5")

    assert len(pcm) == 2
    assert abs(int.from_bytes(pcm, "little", signed=True)) <= 8


def test_alaw_known_vector_normalizes_to_mulaw() -> None:
    pcma = bytes([0xD5, 0x55, 0x80, 0x00, 0xAA, 0x2A])

    assert alaw_to_mulaw(pcma) == bytes([0xFE, 0x7E, 0xA9, 0x29, 0x80, 0x00])


def test_alaw_to_mulaw_is_expansion_then_compression() -> None:
    pcma = bytes(range(256))

    try:
        assert alaw_to_mulaw(pcma) == pcm16_to_mulaw(alaw_to_pcm16(pcma))
    except ImportError as exc:
        pytest.skip(str(exc))
