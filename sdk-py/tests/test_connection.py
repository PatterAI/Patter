import asyncio
import json

import pytest
import websockets.exceptions
from unittest.mock import AsyncMock, MagicMock, patch

from patter.connection import PatterConnection
from patter.models import IncomingMessage
from patter.exceptions import PatterConnectionError


def test_connection_init():
    conn = PatterConnection(api_key="ap_test123", backend_url="wss://api.getpatter.com")
    assert conn._api_key == "ap_test123"
    # api_key should not appear in repr (privacy)
    assert "ap_test123" not in repr(conn)


def test_connection_builds_correct_url():
    conn = PatterConnection(api_key="ap_test123", backend_url="wss://api.getpatter.com")
    assert conn._ws_url == "wss://api.getpatter.com/ws/sdk"


def test_connection_parses_incoming_message():
    conn = PatterConnection(api_key="ap_test123")
    raw = json.dumps({"type": "message", "text": "ciao", "call_id": "call_123", "caller": "+39111222333"})
    msg = conn._parse_message(raw)
    assert isinstance(msg, IncomingMessage)
    assert msg.text == "ciao"


@pytest.mark.asyncio
async def test_connection_sends_response():
    conn = PatterConnection(api_key="ap_test123")
    conn._ws = AsyncMock()
    await conn.send_response(call_id="call_123", text="Certo!")
    conn._ws.send.assert_called_once()
    sent = json.loads(conn._ws.send.call_args[0][0])
    assert sent["type"] == "response"
    assert sent["text"] == "Certo!"


def test_connection_parses_non_message_returns_none():
    conn = PatterConnection(api_key="ap_test123")
    raw = json.dumps({"type": "call_start", "call_id": "c1"})
    assert conn._parse_message(raw) is None


@pytest.mark.asyncio
async def test_connection_disconnect():
    conn = PatterConnection(api_key="ap_test123")
    mock_ws = AsyncMock()
    conn._ws = mock_ws
    conn._listen_task = None
    await conn.disconnect()
    mock_ws.close.assert_called_once()
    assert conn._ws is None


# --- connect() ---


@pytest.mark.asyncio
async def test_connect_establishes_websocket():
    conn = PatterConnection(api_key="ap_test123", backend_url="wss://test.patter.dev")
    mock_ws = AsyncMock()
    # Make the mock iterable (for _listen_loop) but immediately stop
    mock_ws.__aiter__ = MagicMock(return_value=iter([]))

    handler = AsyncMock(return_value="reply")

    with patch("patter.connection.websockets.connect", new_callable=AsyncMock, return_value=mock_ws):
        await conn.connect(on_message=handler)

    assert conn._ws is mock_ws
    assert conn._running is True
    assert conn._on_message is handler
    assert conn._listen_task is not None

    # Clean up the background task
    await conn.disconnect()


@pytest.mark.asyncio
async def test_connect_failure_raises_connection_error():
    conn = PatterConnection(api_key="ap_test123")

    with patch(
        "patter.connection.websockets.connect",
        new_callable=AsyncMock,
        side_effect=ConnectionRefusedError("refused"),
    ):
        with pytest.raises(PatterConnectionError, match="Failed to connect"):
            await conn.connect(on_message=AsyncMock())


@pytest.mark.asyncio
async def test_connect_stores_lifecycle_callbacks():
    conn = PatterConnection(api_key="ap_test123")
    mock_ws = AsyncMock()
    mock_ws.__aiter__ = MagicMock(return_value=iter([]))

    on_msg = AsyncMock()
    on_start = AsyncMock()
    on_end = AsyncMock()

    with patch("patter.connection.websockets.connect", new_callable=AsyncMock, return_value=mock_ws):
        await conn.connect(on_message=on_msg, on_call_start=on_start, on_call_end=on_end)

    assert conn._on_call_start is on_start
    assert conn._on_call_end is on_end

    await conn.disconnect()


# --- is_connected ---


def test_is_connected_no_ws():
    conn = PatterConnection(api_key="ap_test123")
    assert conn.is_connected is False


def test_is_connected_open_state():
    conn = PatterConnection(api_key="ap_test123")
    mock_ws = MagicMock()
    mock_ws.state.name = "OPEN"
    conn._ws = mock_ws
    assert conn.is_connected is True


def test_is_connected_closed_state():
    conn = PatterConnection(api_key="ap_test123")
    mock_ws = MagicMock()
    mock_ws.state.name = "CLOSED"
    conn._ws = mock_ws
    assert conn.is_connected is False


def test_is_connected_no_state_attribute():
    """Fallback: if state attribute missing, returns True when ws is not None."""
    conn = PatterConnection(api_key="ap_test123")
    mock_ws = MagicMock(spec=[])  # no attributes at all
    conn._ws = mock_ws
    assert conn.is_connected is True


# --- _listen_loop helpers ---


class _FakeWS:
    """Minimal async-iterable WebSocket mock for _listen_loop tests."""

    def __init__(self, messages: list[str]) -> None:
        self._messages = messages
        self.send = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


# --- _listen_loop ---


@pytest.mark.asyncio
async def test_listen_loop_dispatches_message():
    conn = PatterConnection(api_key="ap_test123")
    on_msg = AsyncMock(return_value="ok")
    conn._on_message = on_msg
    conn._running = True

    raw = json.dumps({"type": "message", "text": "hi", "call_id": "c1", "caller": "+1"})
    fake_ws = _FakeWS([raw])
    conn._ws = fake_ws

    await conn._listen_loop()

    on_msg.assert_awaited_once()
    fake_ws.send.assert_awaited_once()
    sent = json.loads(fake_ws.send.call_args[0][0])
    assert sent["type"] == "response"
    assert sent["text"] == "ok"


@pytest.mark.asyncio
async def test_listen_loop_dispatches_call_start():
    conn = PatterConnection(api_key="ap_test123")
    on_start = AsyncMock()
    conn._on_call_start = on_start
    conn._running = True

    raw = json.dumps({"type": "call_start", "call_id": "c1"})
    conn._ws = _FakeWS([raw])

    await conn._listen_loop()

    on_start.assert_awaited_once()
    assert on_start.call_args[0][0]["type"] == "call_start"


@pytest.mark.asyncio
async def test_listen_loop_dispatches_call_end():
    conn = PatterConnection(api_key="ap_test123")
    on_end = AsyncMock()
    conn._on_call_end = on_end
    conn._running = True

    raw = json.dumps({"type": "call_end", "call_id": "c1"})
    conn._ws = _FakeWS([raw])

    await conn._listen_loop()

    on_end.assert_awaited_once()


@pytest.mark.asyncio
async def test_listen_loop_handler_exception_does_not_crash():
    """on_message handler raising should be logged, not propagated."""
    conn = PatterConnection(api_key="ap_test123")
    on_msg = AsyncMock(side_effect=ValueError("boom"))
    conn._on_message = on_msg
    conn._running = True

    raw = json.dumps({"type": "message", "text": "hi", "call_id": "c1", "caller": "+1"})
    conn._ws = _FakeWS([raw])

    await conn._listen_loop()  # should not raise

    on_msg.assert_awaited_once()


@pytest.mark.asyncio
async def test_listen_loop_call_start_handler_exception_does_not_crash():
    conn = PatterConnection(api_key="ap_test123")
    on_start = AsyncMock(side_effect=RuntimeError("kaboom"))
    conn._on_call_start = on_start
    conn._running = True

    raw = json.dumps({"type": "call_start", "call_id": "c1"})
    conn._ws = _FakeWS([raw])

    await conn._listen_loop()  # should not raise


@pytest.mark.asyncio
async def test_listen_loop_invalid_json_skipped():
    conn = PatterConnection(api_key="ap_test123")
    on_msg = AsyncMock(return_value="reply")
    conn._on_message = on_msg
    conn._running = True

    fake_ws = _FakeWS(["not-json", json.dumps({"type": "message", "text": "hi", "call_id": "c1", "caller": "+1"})])
    conn._ws = fake_ws

    await conn._listen_loop()

    on_msg.assert_awaited_once()


@pytest.mark.asyncio
async def test_listen_loop_connection_closed_sets_running_false():
    conn = PatterConnection(api_key="ap_test123")
    conn._running = True

    class _ClosingWS:
        send = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise websockets.exceptions.ConnectionClosed(None, None)

    conn._ws = _ClosingWS()

    await conn._listen_loop()

    assert conn._running is False


@pytest.mark.asyncio
async def test_listen_loop_message_returns_none_no_send():
    """When on_message returns None, no response should be sent."""
    conn = PatterConnection(api_key="ap_test123")
    on_msg = AsyncMock(return_value=None)
    conn._on_message = on_msg
    conn._running = True

    raw = json.dumps({"type": "message", "text": "hi", "call_id": "c1", "caller": "+1"})
    fake_ws = _FakeWS([raw])
    conn._ws = fake_ws

    await conn._listen_loop()

    on_msg.assert_awaited_once()
    fake_ws.send.assert_not_awaited()
