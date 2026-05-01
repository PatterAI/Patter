"""Unit tests for Telnyx stream bridge track filtering (BUG #19).

Telnyx with ``stream_track=both_tracks`` emits media frames for both the
caller leg (``track=inbound``) and the outbound leg we inject TTS into
(``track=outbound``). If the bridge forwards the outbound echo to STT /
Realtime, the agent hears itself and turn detection collapses.

The filter lives at the top of the ``media`` branch in
:func:`telnyx_stream_bridge` — anything not ``inbound`` is skipped
before decoding. These tests lock that behaviour in by driving the
bridge with hand-crafted media frames and asserting which ones reach
``handler.on_audio_received``.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ws_message(event: str, **kwargs) -> str:
    return json.dumps({"event": event, **kwargs})


def _stream_started_message(call_control_id: str = "v3:track-test") -> str:
    return _ws_message(
        "start",
        start={
            "call_control_id": call_control_id,
            "from": "+15551111111",
            "to": "+15552222222",
        },
    )


def _media_message(audio: bytes, track: str | None) -> str:
    encoded = base64.b64encode(audio).decode("ascii")
    media: dict = {"payload": encoded}
    if track is not None:
        media["track"] = track
    return _ws_message("media", media=media)


def _stream_stopped_message() -> str:
    return _ws_message("stop")


def _make_mock_ws(messages: list[str]) -> AsyncMock:
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.query_params = {"caller": "+15551111111", "callee": "+15552222222"}
    # Append a sentinel Exception so the bridge falls out of the receive loop.
    ws.receive_text = AsyncMock(side_effect=messages + [Exception("stop")])
    ws.send_text = AsyncMock()
    return ws


@pytest.fixture
def _patched_bridge_deps():
    """Patch the heavy dependencies the bridge pulls in."""
    with patch(
        "getpatter.telephony.telnyx.OpenAIRealtimeStreamHandler"
    ) as mock_handler_cls, patch(
        "getpatter.telephony.telnyx.create_metrics_accumulator"
    ) as mock_metrics_fn, patch(
        "getpatter.telephony.telnyx.resolve_agent_prompt",
        return_value="prompt",
    ), patch(
        "getpatter.telephony.telnyx.fetch_deepgram_cost",
        new_callable=AsyncMock,
    ):
        mock_handler = AsyncMock()
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler

        mock_metrics = MagicMock()
        mock_metrics.end_call.return_value = MagicMock()
        mock_metrics_fn.return_value = mock_metrics

        yield mock_handler


# ---------------------------------------------------------------------------
# Track filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestTelnyxTrackFilter:
    """Only ``track=inbound`` media reaches the handler."""

    async def test_inbound_track_forwards_audio(
        self, _patched_bridge_deps
    ) -> None:
        from getpatter.telephony.telnyx import telnyx_stream_bridge

        handler = _patched_bridge_deps
        audio = b"\x00\x01" * 160  # 20 ms of PCMU 8 kHz
        ws = _make_mock_ws(
            [
                _stream_started_message(),
                _media_message(audio, track="inbound"),
                _stream_stopped_message(),
            ]
        )

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        handler.on_audio_received.assert_awaited_once_with(audio)

    async def test_outbound_track_dropped(
        self, _patched_bridge_deps
    ) -> None:
        """The outbound echo must never reach the handler."""
        from getpatter.telephony.telnyx import telnyx_stream_bridge

        handler = _patched_bridge_deps
        echo = b"\xff\xfe" * 160
        ws = _make_mock_ws(
            [
                _stream_started_message(),
                _media_message(echo, track="outbound"),
                _stream_stopped_message(),
            ]
        )

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        handler.on_audio_received.assert_not_awaited()

    async def test_missing_track_defaults_to_inbound(
        self, _patched_bridge_deps
    ) -> None:
        """Older Telnyx payloads omit the field; default to inbound."""
        from getpatter.telephony.telnyx import telnyx_stream_bridge

        handler = _patched_bridge_deps
        audio = b"\x11\x22" * 160
        ws = _make_mock_ws(
            [
                _stream_started_message(),
                _media_message(audio, track=None),
                _stream_stopped_message(),
            ]
        )

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        handler.on_audio_received.assert_awaited_once_with(audio)

    async def test_mixed_stream_forwards_only_inbound(
        self, _patched_bridge_deps
    ) -> None:
        """With both tracks in the same stream, only inbound must pass."""
        from getpatter.telephony.telnyx import telnyx_stream_bridge

        handler = _patched_bridge_deps
        inbound_a = b"\xaa\xaa" * 160
        outbound_b = b"\xbb\xbb" * 160
        inbound_c = b"\xcc\xcc" * 160
        ws = _make_mock_ws(
            [
                _stream_started_message(),
                _media_message(inbound_a, track="inbound"),
                _media_message(outbound_b, track="outbound"),
                _media_message(inbound_c, track="inbound"),
                _stream_stopped_message(),
            ]
        )

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        # Only the two inbound frames reach the handler, in order, never
        # the outbound echo.
        assert handler.on_audio_received.await_count == 2
        actual = [c.args[0] for c in handler.on_audio_received.await_args_list]
        assert actual == [inbound_a, inbound_c]

    async def test_unknown_track_value_dropped(
        self, _patched_bridge_deps
    ) -> None:
        """Defensive — anything that isn't literally 'inbound' is skipped."""
        from getpatter.telephony.telnyx import telnyx_stream_bridge

        handler = _patched_bridge_deps
        ws = _make_mock_ws(
            [
                _stream_started_message(),
                _media_message(b"\x00" * 320, track="weird_track_name"),
                _stream_stopped_message(),
            ]
        )

        await telnyx_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
        )

        handler.on_audio_received.assert_not_awaited()
