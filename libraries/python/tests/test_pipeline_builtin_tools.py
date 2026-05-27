"""Regression for upstream issue #110.

Pipeline mode previously passed only the user-provided tools to
``LLMLoop`` — the built-in ``transfer_call`` / ``end_call`` tools that
the realtime path injects were missing, so pipeline LLMs could never
initiate a handoff or hangup regardless of the system prompt.

These tests exercise the helper that bolts the built-ins onto the
tool list with handler closures wired to the telephony-level
``transfer_fn`` / ``hangup_fn``.
"""

from __future__ import annotations

import pytest

from getpatter.stream_handler import (
    END_CALL_TOOL,
    TRANSFER_CALL_TOOL,
    _augment_with_builtin_handoff_tools,
)


def test_augments_empty_user_tools():
    calls: list[tuple[str, str]] = []

    async def fake_transfer(number: str) -> None:
        calls.append(("transfer", number))

    async def fake_hangup() -> None:
        calls.append(("hangup", ""))

    tools = _augment_with_builtin_handoff_tools(
        None, transfer_fn=fake_transfer, hangup_fn=fake_hangup
    )
    names = [t["name"] for t in tools]
    assert names == ["transfer_call", "end_call"]
    # Schema preserved
    assert tools[0]["parameters"] == TRANSFER_CALL_TOOL["parameters"]
    assert tools[1]["parameters"] == END_CALL_TOOL["parameters"]
    # Handlers attached
    assert callable(tools[0]["handler"])
    assert callable(tools[1]["handler"])


def test_preserves_user_tools_order():
    user_tools = [
        {"name": "lookup_customer", "description": "", "parameters": {"type": "object"}},
        {"name": "send_sms", "description": "", "parameters": {"type": "object"}},
    ]
    tools = _augment_with_builtin_handoff_tools(
        user_tools,
        transfer_fn=lambda n: None,
        hangup_fn=lambda: None,
    )
    names = [t["name"] for t in tools]
    assert names == ["lookup_customer", "send_sms", "transfer_call", "end_call"]


def test_skips_builtin_when_fn_missing():
    """If telephony adapter didn't supply a transfer_fn (e.g. non-Twilio
    test harness), the corresponding built-in is not injected."""
    user_tools = [{"name": "lookup_customer", "description": "", "parameters": {}}]
    tools = _augment_with_builtin_handoff_tools(
        user_tools, transfer_fn=None, hangup_fn=None
    )
    assert [t["name"] for t in tools] == ["lookup_customer"]


@pytest.mark.asyncio
async def test_transfer_handler_dispatches_to_transfer_fn():
    captured: list[str] = []

    async def fake_transfer(number: str) -> None:
        captured.append(number)

    tools = _augment_with_builtin_handoff_tools(
        None, transfer_fn=fake_transfer, hangup_fn=None
    )
    transfer = tools[0]
    result = await transfer["handler"](
        {"number": "+14155551234"}, {"call_id": "CAtest"}
    )
    assert captured == ["+14155551234"]
    assert "+14155551234" in result


@pytest.mark.asyncio
async def test_hangup_handler_dispatches_to_hangup_fn():
    called = []

    async def fake_hangup() -> None:
        called.append(True)

    tools = _augment_with_builtin_handoff_tools(
        None, transfer_fn=None, hangup_fn=fake_hangup
    )
    end = tools[0]
    assert end["name"] == "end_call"
    result = await end["handler"]({}, {"call_id": "CAtest"})
    assert called == [True]
    assert "ended" in result.lower()


@pytest.mark.asyncio
async def test_transfer_handler_handles_missing_number_gracefully():
    """LLM occasionally emits transfer_call without a number arg; the
    handler must not crash."""
    called: list[str] = []

    async def fake_transfer(number: str) -> None:
        called.append(number)

    tools = _augment_with_builtin_handoff_tools(
        None, transfer_fn=fake_transfer, hangup_fn=None
    )
    result = await tools[0]["handler"]({}, {"call_id": "CAtest"})
    # Calls through with empty string (downstream _validate_e164 will reject)
    assert called == [""]
    assert "rejected" in result.lower() or result == "Transferring to "
