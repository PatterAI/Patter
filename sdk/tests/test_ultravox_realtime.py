"""Tests for UltravoxRealtimeAdapter.

The adapter uses aiohttp for both the REST create-call call and the
WebSocket. We stub those surfaces with lightweight fakes.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from patter.providers.ultravox_realtime import (
    DEFAULT_API_BASE,
    DEFAULT_SAMPLE_RATE_HZ,
    UltravoxRealtimeAdapter,
    _tool_params_to_ultravox,
)


pytest.importorskip("aiohttp")


class _FakeMsg:
    def __init__(self, type_, data):
        self.type = type_
        self.data = data


class _FakeWS:
    def __init__(self, messages):
        self._messages = messages
        self.sent_bytes: list[bytes] = []
        self.sent_strs: list[str] = []
        self.closed = False

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def send_str(self, data):
        self.sent_strs.append(data)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self._iter_impl()

    async def _iter_impl(self):
        for m in self._messages:
            yield m


class _FakeResp:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)


class _FakeSession:
    def __init__(self, call_response: dict, ws: _FakeWS):
        self._call_response = call_response
        self._ws = ws
        self.closed = False

    def post(self, url, json=None):
        return _FakeResp(200, self._call_response)

    async def ws_connect(self, url, heartbeat=None):
        return self._ws

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_connect_posts_call_then_opens_websocket(monkeypatch):
    ws = _FakeWS([])
    session = _FakeSession({"joinUrl": "ws://ultravox/join/1"}, ws)

    import patter.providers.ultravox_realtime as uvx

    class _FakeAiohttp:
        WSMsgType = MagicMock(BINARY=1, TEXT=2, CLOSED=3, ERROR=4, CLOSING=5)

        def ClientSession(self, headers=None):  # noqa: N802 - matches aiohttp.ClientSession
            return session

    monkeypatch.setattr(uvx, "aiohttp", _FakeAiohttp(), raising=False)
    # Replace the lazy import: overwrite the builtin lookup via sys.modules.
    # Use monkeypatch.setitem so pytest restores the real aiohttp after the
    # test, preventing pollution of unrelated tests that import aiohttp.
    import sys

    monkeypatch.setitem(sys.modules, "aiohttp", _FakeAiohttp())

    adapter = UltravoxRealtimeAdapter(api_key="fake", instructions="be brief")
    await adapter.connect()

    assert adapter._ws is ws
    assert adapter._session is session


@pytest.mark.asyncio
async def test_send_audio_is_binary(monkeypatch):
    import sys

    class _FakeAiohttp:
        WSMsgType = MagicMock(BINARY=1, TEXT=2, CLOSED=3, ERROR=4, CLOSING=5)

        def ClientSession(self, headers=None):  # noqa: N802
            return session

    ws = _FakeWS([])
    session = _FakeSession({"joinUrl": "ws://x"}, ws)
    monkeypatch.setitem(sys.modules, "aiohttp", _FakeAiohttp())

    adapter = UltravoxRealtimeAdapter(api_key="fake")
    await adapter.connect()
    await adapter.send_audio(b"\x00\x01\x02")

    assert ws.sent_bytes == [b"\x00\x01\x02"]


@pytest.mark.asyncio
async def test_translate_event_yields_expected_tuples():
    adapter = UltravoxRealtimeAdapter(api_key="fake")

    async def collect(event):
        return [item async for item in adapter._translate_event(event)]

    user_transcript = await collect({
        "type": "transcript",
        "role": "user",
        "text": "hello world",
        "final": True,
    })
    assert user_transcript == [("transcript_input", "hello world")]

    agent_transcript = await collect({
        "type": "transcript",
        "role": "agent",
        "text": "hi!",
    })
    assert agent_transcript == [("transcript_output", "hi!")]

    tool = await collect({
        "type": "client_tool_invocation",
        "invocationId": "call-1",
        "toolName": "weather",
        "parameters": {"city": "Rome"},
    })
    assert tool[0][0] == "function_call"
    assert tool[0][1]["call_id"] == "call-1"
    assert tool[0][1]["name"] == "weather"
    assert json.loads(tool[0][1]["arguments"]) == {"city": "Rome"}


def test_tool_params_to_ultravox_respects_required():
    params = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "units": {"type": "string"}},
        "required": ["city"],
    }
    out = _tool_params_to_ultravox(params)
    names = {p["name"] for p in out}
    assert names == {"city", "units"}
    city = next(p for p in out if p["name"] == "city")
    assert city["required"] is True
    units = next(p for p in out if p["name"] == "units")
    assert units["required"] is False


def test_defaults_are_sane():
    adapter = UltravoxRealtimeAdapter(api_key="fake")
    assert adapter.sample_rate == DEFAULT_SAMPLE_RATE_HZ
    assert adapter.model == "fixie-ai/ultravox"
    assert adapter.language == "en"
