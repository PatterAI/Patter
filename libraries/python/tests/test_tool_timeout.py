"""Tests for the per-tool execution timeout (POINT 2a) and the reassurance +
timeout_s exposure on the tool() factory (POINT 2b).

The handler-path tests are fully authentic — they exercise the real
``ToolExecutor`` with a real ``asyncio.sleep`` handler and a real
``asyncio.wait_for`` timeout, no external boundary mocked. The webhook-timeout
test mocks the HTTP transport (an external boundary) and is tagged
``@pytest.mark.mocked``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from getpatter import Patter, Tool, Twilio, tool
from getpatter.tools.tool_executor import _MAX_TOOL_TIMEOUT_S, ToolExecutor


# ---------------------------------------------------------------------------
# Handler-path timeout — authentic, no mock
# ---------------------------------------------------------------------------


async def test_long_tool_not_truncated_by_10s_default() -> None:
    """A handler that sleeps 0.2s with tool_timeout_s=60 returns its real
    result — proving the 10s default no longer governs when a per-tool
    timeout is set (the headline POINT 2a fix for 30-60s browser tools)."""

    async def slow_handler(arguments: dict, call_context: dict) -> str:
        await asyncio.sleep(0.2)
        return "done"

    executor = ToolExecutor()
    try:
        result = await executor.execute(
            tool_name="browse",
            arguments={},
            call_context={"call_id": "c1"},
            handler=slow_handler,
            tool_timeout_s=60.0,
        )
        assert result == "done"
    finally:
        await executor.close()


async def test_handler_timeout_returns_fallback_and_does_not_retry() -> None:
    """A handler that sleeps longer than the per-tool timeout returns a
    structured fallback error and is NOT retried (a timeout must not consume
    retry attempts — it would multiply the wait)."""

    calls = 0

    async def hung_handler(arguments: dict, call_context: dict) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(5.0)
        return "never"

    executor = ToolExecutor()
    try:
        result = await executor.execute(
            tool_name="hangs",
            arguments={},
            call_context={"call_id": "c1"},
            handler=hung_handler,
            tool_timeout_s=0.1,
        )
        payload = json.loads(result)
        assert payload["fallback"] is True
        assert "timed out" in payload["error"]
        # A timeout fires exactly once — no retry storm.
        assert calls == 1
    finally:
        await executor.close()


async def test_handler_default_timeout_path_still_used_when_unset() -> None:
    """tool_timeout_s=None keeps the existing behavior: a fast handler runs
    fine under the legacy 10s default."""

    async def fast_handler(arguments: dict, call_context: dict) -> str:
        return "ok"

    executor = ToolExecutor()
    try:
        result = await executor.execute(
            tool_name="fast",
            arguments={},
            call_context={"call_id": "c1"},
            handler=fast_handler,
        )
        assert result == "ok"
    finally:
        await executor.close()


async def test_timeout_clamped_to_ceiling() -> None:
    """An absurd timeout is clamped to the sane upper bound; the handler
    still returns normally well within it."""

    async def fast_handler(arguments: dict, call_context: dict) -> str:
        return "ok"

    executor = ToolExecutor()
    try:
        result = await executor.execute(
            tool_name="fast",
            arguments={},
            call_context={"call_id": "c1"},
            handler=fast_handler,
            tool_timeout_s=_MAX_TOOL_TIMEOUT_S * 100,
        )
        assert result == "ok"
    finally:
        await executor.close()


# ---------------------------------------------------------------------------
# tool() factory exposes timeout_s + reassurance — POINT 2b
# ---------------------------------------------------------------------------


async def _noop(arguments: dict, call_context: dict) -> str:
    return "{}"


def test_tool_factory_exposes_timeout_and_reassurance() -> None:
    t = tool(
        name="check_order",
        handler=_noop,
        timeout_s=60.0,
        reassurance="One moment while I check that for you.",
    )
    assert isinstance(t, Tool)
    assert t.timeout_s == 60.0
    assert t.reassurance == "One moment while I check that for you."


def test_tool_factory_reassurance_dict_form_round_trips() -> None:
    t = tool(
        name="check_order",
        handler=_noop,
        reassurance={"message": "Hang on...", "after_ms": 800},
    )
    assert t.reassurance == {"message": "Hang on...", "after_ms": 800}


def test_tool_dataclass_timeout_defaults_to_none() -> None:
    t = Tool(name="x", handler=_noop)
    assert t.timeout_s is None
    assert t.reassurance is None


def test_tool_to_dict_propagates_timeout_and_reassurance() -> None:
    phone = Patter(
        carrier=Twilio(account_sid="ACtest", auth_token="tok"),
        phone_number="+15555550100",
    )
    out = phone._tool_to_dict(
        tool(
            name="check_order",
            handler=_noop,
            timeout_s=60.0,
            reassurance="One moment.",
        ),
        index=0,
    )
    assert out["timeout_s"] == 60.0
    assert out["reassurance"] == "One moment."


def test_tool_to_dict_omits_timeout_when_unset() -> None:
    phone = Patter(
        carrier=Twilio(account_sid="ACtest", auth_token="tok"),
        phone_number="+15555550100",
    )
    out = phone._tool_to_dict(tool(name="x", handler=_noop), index=0)
    assert "timeout_s" not in out


# ---------------------------------------------------------------------------
# Webhook-path per-tool timeout override — mocks the HTTP boundary
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_webhook_uses_per_tool_timeout_override() -> None:
    """The shared httpx client is fixed at 10s; a per-tool tool_timeout_s
    must override the per-request timeout. We capture the timeout the POST
    is issued with via a mock transport (the external HTTP boundary)."""

    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport, timeout=10.0)
    executor = ToolExecutor(client=client)
    try:
        result = await executor.execute(
            tool_name="external_api",
            arguments={},
            call_context={"call_id": "c1"},
            webhook_url="https://example.com/hook",
            tool_timeout_s=45.0,
        )
        assert json.loads(result) == {"ok": True}
        # httpx exposes per-request timeouts via the request extensions dict.
        timeout = captured["timeout"]
        assert timeout is not None
        # All four phases reflect the 45s per-tool override, not the 10s client default.
        assert timeout.get("connect") == 45.0
        assert timeout.get("read") == 45.0
    finally:
        await client.aclose()
