"""Reassurance must NOT inject a phantom ``role:user`` turn (feature #2).

The reassurance filler used to fire ``adapter.send_text(message)``, which
emits a ``conversation.item.create`` with ``role:"user"`` — the transcript
then falsely showed the caller saying "One moment." The fix routes the filler
through a dedicated ``send_reassurance`` that speaks the line as the
assistant's own audio via a bare ``response.create`` (no fake user item).

These tests exercise the REAL ``OpenAIRealtime2Adapter`` (the adapter the
stream-handler actually instantiates) and the REAL ``_schedule_reassurance``
scheduler. Only the OpenAI Realtime WebSocket is mocked — a fake ``_ws`` that
captures every JSON frame ``send()`` is handed. Tagged ``@pytest.mark.mocked``
because that WS is the paid external boundary.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter
from getpatter.providers.openai_realtime_2 import OpenAIRealtime2Adapter

pytestmark = pytest.mark.mocked


class _CapturingWS:
    """Minimal stand-in for the OpenAI Realtime WebSocket — records frames."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:  # pragma: no cover - not exercised here
        pass


def _ga_adapter() -> tuple[OpenAIRealtime2Adapter, _CapturingWS]:
    adapter = OpenAIRealtime2Adapter(api_key="sk-test", voice="cedar")
    ws = _CapturingWS()
    adapter._ws = ws  # inject the captured-frame socket
    return adapter, ws


def _user_items(frames: list[dict]) -> list[dict]:
    return [
        f
        for f in frames
        if f.get("type") == "conversation.item.create"
        and f.get("item", {}).get("role") == "user"
    ]


def _response_creates(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("type") == "response.create"]


async def test_send_reassurance_speaks_filler_without_role_user_item() -> None:
    adapter, ws = _ga_adapter()
    await adapter.send_reassurance("One moment.")

    # No phantom caller turn.
    assert _user_items(ws.sent) == []
    # The filler is spoken via a response.create carrying explicit instructions.
    creates = _response_creates(ws.sent)
    assert len(creates) == 1
    instructions = creates[0]["response"]["instructions"]
    assert "One moment." in instructions


async def test_send_reassurance_ga_shape_matches_send_first_message() -> None:
    """On the GA adapter the filler must use the GA-valid output_modalities +
    re-injected voice (same shape as ``send_first_message``), never the v1
    ``modalities`` key the GA endpoint rejects."""
    adapter, ws = _ga_adapter()
    await adapter.send_reassurance("Let me check.")
    body = _response_creates(ws.sent)[0]["response"]
    assert body["output_modalities"] == ["audio"]
    assert body["audio"]["output"]["voice"] == "cedar"
    assert "modalities" not in body  # the GA endpoint rejects this key


async def test_legacy_send_text_still_injects_role_user_item() -> None:
    """Contrast / regression guard: ``send_text`` is UNCHANGED and still emits
    a ``role:user`` item. Proves the bug class and that the fix did not touch
    the shared ``send_text`` path (4 callers rely on it)."""
    adapter, ws = _ga_adapter()
    await adapter.send_text("hello")
    users = _user_items(ws.sent)
    assert len(users) == 1
    assert users[0]["item"]["content"][0]["text"] == "hello"


async def test_v1_adapter_send_reassurance_uses_v1_shape() -> None:
    """The base (v1-beta) adapter inherits a v1-shape ``send_reassurance``
    that mirrors its own ``send_first_message`` (``modalities`` key) and adds
    no ``role:user`` item."""
    adapter = OpenAIRealtimeAdapter(api_key="sk-test", voice="alloy")
    ws = _CapturingWS()
    adapter._ws = ws
    await adapter.send_reassurance("Just a second.")
    assert _user_items(ws.sent) == []
    body = _response_creates(ws.sent)[0]["response"]
    assert body["modalities"] == ["audio", "text"]
    assert "Just a second." in body["instructions"]


# ---------------------------------------------------------------------------
# Scheduler early-cancel + fire — exercises the real _schedule_reassurance
# ---------------------------------------------------------------------------


class _HandlerStub:
    """Carries just enough state for ``_schedule_reassurance`` to run."""

    def __init__(self, adapter: object) -> None:
        self._adapter = adapter

    # Bind the real method onto this stub.
    from getpatter.stream_handler import (  # noqa: E402
        OpenAIRealtimeStreamHandler as _SH,
    )

    _schedule_reassurance = _SH._schedule_reassurance


async def test_scheduler_fires_send_reassurance_after_delay() -> None:
    adapter, ws = _ga_adapter()
    handler = _HandlerStub(adapter)
    tool_def = {"reassurance": {"message": "Give me a moment.", "after_ms": 10}}
    task = handler._schedule_reassurance(tool_def, "browse")
    assert task is not None
    await task

    assert _user_items(ws.sent) == []
    creates = _response_creates(ws.sent)
    assert len(creates) == 1
    assert "Give me a moment." in creates[0]["response"]["instructions"]


async def test_scheduler_early_cancel_speaks_nothing() -> None:
    """If the tool returns before ``after_ms`` the task is cancelled and the
    filler is never spoken — no frame at all reaches the WS."""
    adapter, ws = _ga_adapter()
    handler = _HandlerStub(adapter)
    tool_def = {"reassurance": {"message": "Let me see.", "after_ms": 5000}}
    task = handler._schedule_reassurance(tool_def, "browse")
    assert task is not None
    # Simulate the tool finishing first.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ws.sent == []
