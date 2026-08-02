"""Tests for the Fish Audio WebSocket TTS provider.

These run against a **real in-process ``websockets`` server** on 127.0.0.1 that
speaks the real MessagePack framing — only Fish's synthesis is simulated. The
handshake headers, frame encode/decode, protocol ordering, audio reassembly,
error surfacing and stall timeout all execute for real over a real socket.
See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest
import websockets

from getpatter.providers.fish_audio_ws_tts import (
    FishAudioWebSocketTTS,
    FishAudioWSError,
    _decode_server_frame,
)

ormsgpack = pytest.importorskip(
    "ormsgpack", reason="the [fish_audio] extra provides the msgpack codec"
)

pytestmark = pytest.mark.mocked


class _WSRecorder:
    def __init__(self) -> None:
        # The raw ``websockets`` Headers object (case-insensitive lookup).
        self.headers: Any = {}
        self.events: list[dict[str, Any]] = []
        self.audio_frames: list[bytes] = [b"\xaa\xbb", b"\xcc\xdd"]
        self.finish_reason: str = "stop"
        self.finish_message: str | None = None
        self.stall: bool = False


@pytest.fixture
async def fish_ws_server() -> AsyncIterator[tuple[str, _WSRecorder]]:
    rec = _WSRecorder()

    async def handler(ws: Any) -> None:
        rec.headers = ws.request.headers
        try:
            async for raw in ws:
                event = ormsgpack.unpackb(raw)
                rec.events.append(event)
                if event.get("event") != "stop":
                    continue
                if rec.stall:
                    await asyncio.sleep(5)
                    return
                for frame in rec.audio_frames:
                    await ws.send(ormsgpack.packb({"event": "audio", "audio": frame}))
                finish: dict[str, Any] = {
                    "event": "finish",
                    "reason": rec.finish_reason,
                }
                if rec.finish_message is not None:
                    finish["message"] = rec.finish_message
                await ws.send(ormsgpack.packb(finish))
                return
        except websockets.exceptions.ConnectionClosed:  # pragma: no cover - teardown
            return

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}", rec


def _ws_tts(url: str, **kwargs: Any) -> FishAudioWebSocketTTS:
    return FishAudioWebSocketTTS(api_key="fish-test-key", ws_url=url, **kwargs)


async def _collect(tts: FishAudioWebSocketTTS, text: str) -> bytes:
    out = bytearray()
    async for chunk in tts.synthesize(text):
        out.extend(chunk)
    return bytes(out)


# ---------------------------------------------------------------------------
# Model guard — the reason this class exists as a separate adapter.
# ---------------------------------------------------------------------------


def test_s21_pro_is_rejected_up_front_with_a_pointer_to_the_http_adapter() -> None:
    with pytest.raises(ValueError) as exc:
        FishAudioWebSocketTTS(api_key="k", model="s2.1-pro")
    assert "FishAudioTTS" in str(exc.value)
    assert "/v1/tts/live" in str(exc.value)


def test_s21_pro_free_is_rejected_too() -> None:
    with pytest.raises(ValueError):
        FishAudioWebSocketTTS(api_key="k", model="s2.1-pro-free")


@pytest.mark.parametrize("model", ["s1", "s2-pro"])
def test_socket_supported_models_are_accepted(model: str) -> None:
    assert FishAudioWebSocketTTS(api_key="k", model=model).model == model


def test_default_model_is_s2_pro_the_fastest_socket_model() -> None:
    assert FishAudioWebSocketTTS(api_key="k").model == "s2-pro"


# ---------------------------------------------------------------------------
# Live protocol against a real socket.
# ---------------------------------------------------------------------------


async def test_audio_frames_are_reassembled_in_order(fish_ws_server) -> None:
    url, rec = fish_ws_server
    tts = _ws_tts(url)
    try:
        assert await _collect(tts, "ciao") == b"\xaa\xbb\xcc\xdd"
    finally:
        await tts.close()


async def test_protocol_is_start_text_flush_stop(fish_ws_server) -> None:
    url, rec = fish_ws_server
    tts = _ws_tts(url)
    try:
        await _collect(tts, "ciao mondo")
    finally:
        await tts.close()
    assert [e["event"] for e in rec.events] == ["start", "text", "flush", "stop"]
    assert rec.events[1]["text"] == "ciao mondo"


async def test_start_frame_carries_the_config_with_empty_text(fish_ws_server) -> None:
    url, rec = fish_ws_server
    tts = _ws_tts(url, voice="ref-1", sample_rate=8000)
    try:
        await _collect(tts, "ciao")
    finally:
        await tts.close()
    request = rec.events[0]["request"]
    assert request["text"] == ""
    assert request["format"] == "pcm"
    assert request["sample_rate"] == 8000
    assert request["reference_id"] == "ref-1"


async def test_handshake_sends_bearer_auth_and_the_model_header(fish_ws_server) -> None:
    url, rec = fish_ws_server
    tts = _ws_tts(url, model="s1")
    try:
        await _collect(tts, "ciao")
    finally:
        await tts.close()
    assert rec.headers["Authorization"] == "Bearer fish-test-key"
    assert rec.headers["model"] == "s1"


async def test_finish_with_reason_error_raises(fish_ws_server) -> None:
    url, rec = fish_ws_server
    rec.finish_reason = "error"
    rec.finish_message = "voice model not found"
    tts = _ws_tts(url)
    try:
        with pytest.raises(FishAudioWSError, match="voice model not found"):
            await _collect(tts, "ciao")
    finally:
        await tts.close()


async def test_a_stalled_server_raises_instead_of_hanging_the_call(
    fish_ws_server,
) -> None:
    url, rec = fish_ws_server
    rec.stall = True
    tts = _ws_tts(url, frame_timeout=0.2)
    try:
        with pytest.raises(FishAudioWSError, match="stalled"):
            await _collect(tts, "ciao")
    finally:
        await tts.close()


async def test_a_server_that_closes_without_finish_ends_the_stream_cleanly(
    fish_ws_server,
) -> None:
    url, rec = fish_ws_server
    rec.audio_frames = [b"\x01"]
    rec.finish_reason = "stop"
    tts = _ws_tts(url)
    try:
        assert await _collect(tts, "ciao") == b"\x01"
    finally:
        await tts.close()


# ---------------------------------------------------------------------------
# Frame decoding edge cases.
# ---------------------------------------------------------------------------


def test_log_and_unknown_frames_are_skipped_without_ending_the_stream() -> None:
    unpackb = ormsgpack.unpackb
    for frame in (
        ormsgpack.packb({"event": "log", "message": "warming up"}),
        ormsgpack.packb({"event": "some-future-event"}),
        ormsgpack.packb([1, 2, 3]),
    ):
        assert _decode_server_frame(frame, unpackb) == (None, False)


def test_text_frames_are_ignored_rather_than_crashing_the_stream() -> None:
    assert _decode_server_frame("unexpected text", ormsgpack.unpackb) == (None, False)


def test_an_undecodable_frame_raises_a_typed_error() -> None:
    with pytest.raises(FishAudioWSError, match="undecodable"):
        # 0x81 announces a 1-pair map and then ends — a truncated frame.
        _decode_server_frame(b"\x81", ormsgpack.unpackb)


def test_an_oversized_audio_frame_is_rejected() -> None:
    from getpatter.providers.fish_audio_ws_tts import MAX_AUDIO_FRAME_SIZE

    huge = ormsgpack.packb(
        {"event": "audio", "audio": b"\x00" * (MAX_AUDIO_FRAME_SIZE + 1)}
    )
    with pytest.raises(FishAudioWSError, match="sanity limit"):
        _decode_server_frame(huge, ormsgpack.unpackb)


def test_an_audio_frame_without_bytes_is_skipped() -> None:
    frame = ormsgpack.packb({"event": "audio", "audio": None})
    assert _decode_server_frame(frame, ormsgpack.unpackb) == (None, False)


def test_repr_never_leaks_the_api_key() -> None:
    assert "super-secret" not in repr(FishAudioWebSocketTTS(api_key="super-secret"))
