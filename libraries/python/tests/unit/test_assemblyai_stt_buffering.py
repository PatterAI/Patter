"""Unit tests for AssemblyAI STT audio-frame coalescing.

Verifies that the SDK batches small Twilio frames (20 ms / 160 bytes mulaw
8 kHz) into 50–1000 ms ws frames before forwarding, so AssemblyAI's v3
streaming endpoint does not emit error 3007 ("audio chunk below minimum").
"""

from __future__ import annotations

from typing import Any

import pytest

from getpatter.providers.assemblyai_stt import (
    DEFAULT_TARGET_CHUNK_MS,
    AssemblyAIEncoding,
    AssemblyAISampleRate,
    AssemblyAISTT,
    AssemblyAISTTOptions,
)


class _FakeWebSocket:
    """Minimal stand-in for ``aiohttp.ClientWebSocketResponse``.

    Captures every ``send_bytes`` payload so the test can assert how many
    frames the SDK forwarded to AssemblyAI and how big each was.
    """

    def __init__(self) -> None:
        self.closed = False
        self.sent_payloads: list[bytes] = []
        self.sent_text: list[str] = []

    async def send_bytes(self, payload: bytes) -> None:
        self.sent_payloads.append(payload)

    async def send_str(self, payload: str) -> None:
        self.sent_text.append(payload)

    async def close(self) -> None:
        self.closed = True


def _make_stt(**opts: Any) -> tuple[AssemblyAISTT, _FakeWebSocket]:
    """Build a connected-stub AssemblyAISTT for unit tests.

    Defaults to Twilio mulaw 8 kHz (the wiring that surfaced the bug);
    callers may override ``sample_rate`` / ``encoding`` for PCM tests.
    """
    final_opts: dict[str, Any] = {
        "sample_rate": AssemblyAISampleRate.HZ_8000,
        "encoding": AssemblyAIEncoding.PCM_MULAW,
    }
    final_opts.update(opts)
    options = AssemblyAISTTOptions(**final_opts)
    stt = AssemblyAISTT(api_key="test-key", options=options)
    fake_ws = _FakeWebSocket()
    stt._ws = fake_ws  # type: ignore[assignment]  # private — set directly for unit test isolation
    stt._running = True  # noqa: SLF001
    return stt, fake_ws


@pytest.mark.unit
async def test_twilio_20ms_chunks_are_coalesced_before_ws_send() -> None:
    """10 Twilio mulaw frames (20 ms / 160 bytes) must NOT produce 10 ws frames.

    DEFAULT_TARGET_CHUNK_MS = 60 ms → batch size 480 bytes (3 Twilio frames).
    10 frames → 1600 bytes → 3 full flushes (1440 bytes) + 160 bytes
    buffered. Asserting <= 4 ws sends would still reveal the bug; we
    additionally assert each flushed frame is at the configured ~60 ms
    target so the AssemblyAI 50 ms floor is never violated.
    """
    stt, fake_ws = _make_stt()
    twilio_frame = b"\xff" * 160  # 160 bytes mulaw 8 kHz = 20 ms

    for _ in range(10):
        await stt.send_audio(twilio_frame)

    # 10 × 20 ms = 200 ms → 3 ws sends of 60 ms each, 40 ms buffered
    assert len(fake_ws.sent_payloads) == 3, (
        f"expected 3 coalesced ws sends, got {len(fake_ws.sent_payloads)} "
        f"(sizes={[len(p) for p in fake_ws.sent_payloads]})"
    )
    for payload in fake_ws.sent_payloads:
        assert len(payload) >= 400, (
            f"flushed payload of {len(payload)} bytes is below the AssemblyAI "
            "50 ms floor (400 bytes for mulaw 8 kHz)"
        )
        assert len(payload) <= 8000, (
            f"flushed payload of {len(payload)} bytes is above the 1000 ms ceiling"
        )


@pytest.mark.unit
async def test_target_chunk_size_is_60ms() -> None:
    """Sanity-check the documented 60 ms target translates to expected bytes."""
    stt, _ws = _make_stt()
    # Trigger lazy compute via one send.
    await stt.send_audio(b"\x00")
    assert DEFAULT_TARGET_CHUNK_MS == 60
    # mulaw 8 kHz: 8 samples/ms × 1 byte/sample × 60 ms = 480 bytes.
    assert stt._audio_buffer_target_bytes == 480  # noqa: SLF001

    stt2, _ws2 = _make_stt(
        sample_rate=AssemblyAISampleRate.HZ_16000,
        encoding=AssemblyAIEncoding.PCM_S16LE,
    )
    await stt2.send_audio(b"\x00\x00")
    # PCM s16le 16 kHz: 16 samples/ms × 2 bytes/sample × 60 ms = 1920 bytes.
    assert stt2._audio_buffer_target_bytes == 1920  # noqa: SLF001


@pytest.mark.unit
async def test_flush_audio_drains_partial_buffer() -> None:
    """Trailing <60 ms tail must be flushed on close, not silently dropped."""
    stt, fake_ws = _make_stt()
    # Send 2 Twilio frames (40 ms / 320 bytes) — under 480-byte threshold.
    await stt.send_audio(b"\xff" * 160)
    await stt.send_audio(b"\xff" * 160)
    assert fake_ws.sent_payloads == []  # not yet flushed

    await stt.flush_audio()
    assert len(fake_ws.sent_payloads) == 1
    assert len(fake_ws.sent_payloads[0]) == 320


@pytest.mark.unit
async def test_send_audio_silently_drops_when_ws_not_open() -> None:
    """Pre-connect calls must NOT raise — Twilio streams audio during the WS handshake."""
    options = AssemblyAISTTOptions(
        sample_rate=AssemblyAISampleRate.HZ_8000,
        encoding=AssemblyAIEncoding.PCM_MULAW,
    )
    stt = AssemblyAISTT(api_key="test-key", options=options)
    # _ws is None: must not raise.
    await stt.send_audio(b"\xff" * 160)
    # buffer should remain empty since we returned before extending it
    assert len(stt._audio_buffer) == 0  # noqa: SLF001


@pytest.mark.unit
async def test_empty_audio_chunk_is_noop() -> None:
    stt, fake_ws = _make_stt()
    await stt.send_audio(b"")
    assert fake_ws.sent_payloads == []
    assert len(stt._audio_buffer) == 0  # noqa: SLF001
