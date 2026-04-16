"""Tests for the remote message handler (webhook/WebSocket on_message)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.services.remote_message import (
    RemoteMessageHandler,
    is_remote_url,
    is_websocket_url,
)


# --- URL detection ---

def test_is_remote_url_http():
    assert is_remote_url("https://api.example.com/message") is True
    assert is_remote_url("http://localhost:9000/msg") is True


def test_is_remote_url_ws():
    assert is_remote_url("ws://localhost:9000/stream") is True
    assert is_remote_url("wss://api.example.com/ws") is True


def test_is_remote_url_callable():
    assert is_remote_url(lambda data: "hi") is False


def test_is_remote_url_none():
    assert is_remote_url(None) is False


def test_is_websocket_url():
    assert is_websocket_url("ws://localhost:9000") is True
    assert is_websocket_url("wss://example.com/ws") is True
    assert is_websocket_url("https://example.com") is False
    assert is_websocket_url("http://localhost") is False


# --- HTTP webhook ---

@pytest.mark.asyncio
async def test_webhook_json_response():
    """Webhook returning JSON {"text": "..."} extracts text."""
    handler = RemoteMessageHandler()
    data = {"text": "hello", "call_id": "test123", "caller": "+1234567890"}

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"text": "Agent says hi"}'
    mock_response.headers = {"content-type": "application/json"}
    mock_response.json.return_value = {"text": "Agent says hi"}
    mock_response.raise_for_status = MagicMock()

    handler._client = AsyncMock()
    handler._client.post = AsyncMock(return_value=mock_response)

    result = await handler.call_webhook("https://api.example.com/msg", data)
    assert result == "Agent says hi"


@pytest.mark.asyncio
async def test_webhook_plain_text_response():
    """Webhook returning plain text returns as-is."""
    handler = RemoteMessageHandler()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"Plain text response"
    mock_response.headers = {"content-type": "text/plain"}
    mock_response.text = "Plain text response"
    mock_response.raise_for_status = MagicMock()

    handler._client = AsyncMock()
    handler._client.post = AsyncMock(return_value=mock_response)

    result = await handler.call_webhook("https://api.example.com/msg", {"text": "hi"})
    assert result == "Plain text response"


@pytest.mark.asyncio
async def test_webhook_too_large():
    """Webhook response exceeding max size raises ValueError."""
    handler = RemoteMessageHandler()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"x" * (64 * 1024 + 1)
    mock_response.raise_for_status = MagicMock()

    handler._client = AsyncMock()
    handler._client.post = AsyncMock(return_value=mock_response)

    with pytest.raises(ValueError, match="too large"):
        await handler.call_webhook("https://api.example.com/msg", {"text": "hi"})


@pytest.mark.asyncio
async def test_webhook_signature_header():
    """When webhook_secret is set, X-Patter-Signature header is included."""
    import hashlib
    import hmac as hmac_mod
    import json

    secret = "test-secret-key"
    handler = RemoteMessageHandler(webhook_secret=secret)
    data = {"text": "hello", "call_id": "c1"}

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"text": "ok"}'
    mock_response.headers = {"content-type": "application/json"}
    mock_response.json.return_value = {"text": "ok"}
    mock_response.raise_for_status = MagicMock()

    handler._client = AsyncMock()
    handler._client.post = AsyncMock(return_value=mock_response)

    await handler.call_webhook("https://api.example.com/msg", data)

    call_args = handler._client.post.call_args
    headers = call_args.kwargs.get("headers", {})
    body = call_args.kwargs.get("content", b"")

    expected_sig = hmac_mod.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    assert headers["X-Patter-Signature"] == expected_sig


@pytest.mark.asyncio
async def test_webhook_no_signature_without_secret():
    """When webhook_secret is not set, no signature header is added."""
    handler = RemoteMessageHandler()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b"ok"
    mock_response.headers = {"content-type": "text/plain"}
    mock_response.text = "ok"
    mock_response.raise_for_status = MagicMock()

    handler._client = AsyncMock()
    handler._client.post = AsyncMock(return_value=mock_response)

    await handler.call_webhook("https://api.example.com/msg", {"text": "hi"})

    call_args = handler._client.post.call_args
    headers = call_args.kwargs.get("headers", {})
    assert "X-Patter-Signature" not in headers


# --- WebSocket ---

class _AsyncIter:
    """Helper to create an async iterator from a list."""
    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


class _MockWS:
    """Mock WebSocket that supports async iteration and async context manager."""
    def __init__(self, frames):
        self._frames = frames
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return _AsyncIter(self._frames)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_websocket_streaming():
    """WebSocket yields text chunks and stops on done frame."""
    handler = RemoteMessageHandler()

    frames = [
        json.dumps({"text": "Hello "}),
        json.dumps({"text": "world!"}),
        json.dumps({"done": True}),
    ]

    mock_ws = _MockWS(frames)

    with patch("websockets.connect", return_value=mock_ws):
        chunks = []
        async for chunk in handler.call_websocket("ws://example.com:9000", {"text": "hi"}):
            chunks.append(chunk)

    assert chunks == ["Hello ", "world!"]
    assert len(mock_ws.sent) == 1


@pytest.mark.asyncio
async def test_websocket_plain_text_frames():
    """WebSocket with plain text frames yields text directly."""
    handler = RemoteMessageHandler()

    frames = ["Hello", "world"]
    mock_ws = _MockWS(frames)

    with patch("websockets.connect", return_value=mock_ws):
        chunks = []
        async for chunk in handler.call_websocket("ws://example.com:9000", {"text": "hi"}):
            chunks.append(chunk)

    assert chunks == ["Hello", "world"]
