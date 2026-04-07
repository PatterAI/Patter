import pytest
import json
from unittest.mock import AsyncMock
from patter.connection import PatterConnection
from patter.models import IncomingMessage
from patter.exceptions import PatterConnectionError


def test_connection_init():
    conn = PatterConnection(api_key="ap_test123", backend_url="wss://api.patter.dev")
    assert conn._api_key == "ap_test123"
    # api_key should not appear in repr (privacy)
    assert "ap_test123" not in repr(conn)


def test_connection_builds_correct_url():
    conn = PatterConnection(api_key="ap_test123", backend_url="wss://api.patter.dev")
    assert conn._ws_url == "wss://api.patter.dev/ws/sdk"


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
