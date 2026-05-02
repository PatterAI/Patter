"""Tests for the PatterTool integration adapter.

Mirrors `libraries/typescript/tests/patter-tool.test.ts` so the cross-SDK contract stays
in lockstep. The full call flow needs a live carrier+webhook, so these tests
focus on the deterministic surface: schema shape, option validation, the
call_id dispatcher / future lifecycle (using a fake Patter), and the Hermes
handler envelope.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from getpatter.integrations import PatterTool


class _MetricsStub:
    """Bare-minimum MetricsStore stub: subscribe → asyncio.Queue, publish via ``emit``."""

    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def emit(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            q.put_nowait(event)


class _FakePatter:
    """In-memory Patter double that mimics serve/call/metrics_store."""

    def __init__(self, defer_end_seconds: float = 0.05) -> None:
        self._metrics = _MetricsStub()
        self._serve_kwargs: dict[str, Any] = {}
        self._calls_issued: list[dict[str, Any]] = []
        self._counter = 0
        self._defer_end_seconds = defer_end_seconds
        self.never_end = False

    @property
    def metrics_store(self) -> _MetricsStub:
        return self._metrics

    def agent(self, **kwargs: Any) -> dict[str, Any]:
        return {"__agent": True, **kwargs}

    async def serve(self, **kwargs: Any) -> None:
        self._serve_kwargs = kwargs

    async def call(self, *, to: str, agent: dict[str, Any]) -> None:
        self._counter += 1
        call_id = f"CA-{self._counter}"
        self._calls_issued.append({"to": to, "agent": agent, "call_id": call_id})
        self._metrics.emit({"type": "call_initiated", "data": {"call_id": call_id, "callee": to}})
        if self.never_end:
            return

        async def _fire_end() -> None:
            await asyncio.sleep(self._defer_end_seconds)
            on_end = self._serve_kwargs.get("on_call_end")
            if on_end is not None:
                await on_end(
                    {
                        "call_id": call_id,
                        "status": "completed",
                        "transcript": [
                            {"role": "agent", "text": "Hello!"},
                            {"role": "user", "text": "Hi."},
                        ],
                        "metrics": {"duration_seconds": 12.3, "cost": {"total": 0.0123}},
                    }
                )

        asyncio.create_task(_fire_end())


# --- Schema exporters ---------------------------------------------------


def test_openai_schema_shape() -> None:
    tool = PatterTool(phone=_FakePatter(), agent={"system_prompt": "be polite"})
    schema = tool.openai_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "make_phone_call"
    assert schema["function"]["parameters"]["required"] == ["to"]
    assert set(schema["function"]["parameters"]["properties"].keys()) == {
        "to",
        "goal",
        "first_message",
        "max_duration_sec",
    }


def test_anthropic_schema_uses_input_schema() -> None:
    tool = PatterTool(phone=_FakePatter(), agent={"system_prompt": "x"})
    schema = tool.anthropic_schema()
    assert schema["name"] == "make_phone_call"
    assert "input_schema" in schema
    assert schema["input_schema"]["required"] == ["to"]


def test_hermes_schema_uses_parameters() -> None:
    tool = PatterTool(phone=_FakePatter(), agent={"system_prompt": "x"})
    schema = tool.hermes_schema()
    assert schema["name"] == "make_phone_call"
    assert "parameters" in schema
    assert schema["parameters"]["required"] == ["to"]


def test_custom_name_and_description() -> None:
    tool = PatterTool(
        phone=_FakePatter(),
        agent={"system_prompt": "x"},
        name="dial",
        description="ring it",
    )
    assert tool.openai_schema()["function"]["name"] == "dial"
    assert tool.openai_schema()["function"]["description"] == "ring it"


# --- execute() ---------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_rejects_non_e164() -> None:
    tool = PatterTool(phone=_FakePatter(), agent={"system_prompt": "x"})
    with pytest.raises(ValueError, match="E.164"):
        await tool.execute(to="not-e164")
    with pytest.raises(ValueError, match="E.164"):
        await tool.execute(to="5551234567")


@pytest.mark.asyncio
async def test_execute_dials_and_returns_result_envelope() -> None:
    phone = _FakePatter()
    tool = PatterTool(phone=phone, agent={"system_prompt": "x"})
    result = await tool.execute(to="+15551234567", goal="book dentist")

    assert len(phone._calls_issued) == 1
    assert phone._calls_issued[0]["to"] == "+15551234567"
    assert result.call_id == "CA-1"
    assert result.status == "completed"
    assert result.duration_seconds == 12.3
    assert result.cost_usd == 0.0123
    assert len(result.transcript) == 2


@pytest.mark.asyncio
async def test_hermes_handler_returns_json_string_on_success() -> None:
    tool = PatterTool(phone=_FakePatter(), agent={"system_prompt": "x"})
    handler = tool.hermes_handler()
    out = await handler({"to": "+15551234567"})
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed["call_id"] == "CA-1"
    assert parsed["status"] == "completed"
    assert "error" not in parsed


@pytest.mark.asyncio
async def test_hermes_handler_returns_error_envelope_on_failure() -> None:
    tool = PatterTool(phone=_FakePatter(), agent={"system_prompt": "x"})
    handler = tool.hermes_handler()
    out = await handler({"to": "not-e164"})
    parsed = json.loads(out)
    assert "E.164" in parsed["error"]
    assert "call_id" not in parsed


@pytest.mark.asyncio
async def test_execute_times_out_when_no_call_end() -> None:
    phone = _FakePatter()
    phone.never_end = True
    tool = PatterTool(phone=phone, agent={"system_prompt": "x"}, max_duration_sec=5)
    with pytest.raises(TimeoutError, match="timeout"):
        await tool.execute(to="+15551234567", max_duration_sec=1)


# --- start/stop --------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    phone = _FakePatter()
    tool = PatterTool(phone=phone, agent={"system_prompt": "x"})
    await tool.start()
    await tool.start()
    result = await tool.execute(to="+15551234567")
    assert result.call_id == "CA-1"


@pytest.mark.asyncio
async def test_start_without_agent_raises() -> None:
    tool = PatterTool(phone=_FakePatter())
    with pytest.raises(ValueError, match="agent"):
        await tool.start()


# --- Hermes registry helper -------------------------------------------


def test_register_hermes_calls_registry_register() -> None:
    captured: dict[str, Any] = {}

    class _RegistryStub:
        def register(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    tool = PatterTool(phone=_FakePatter(), agent={"system_prompt": "x"})
    tool.register_hermes(_RegistryStub(), toolset="phone")

    assert captured["name"] == "make_phone_call"
    assert captured["toolset"] == "phone"
    assert captured["schema"]["parameters"]["required"] == ["to"]
    assert callable(captured["handler"])


def test_register_hermes_rejects_non_registry() -> None:
    tool = PatterTool(phone=_FakePatter(), agent={"system_prompt": "x"})
    with pytest.raises(TypeError, match="Registry"):
        tool.register_hermes("not a registry")


# --- concurrency / cleanup --------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_execute_serializes_dials() -> None:
    """Two parallel execute() calls must each capture their own call_id;
    without the lock the second would clobber _next_call_id and hang."""

    phone = _FakePatter()
    tool = PatterTool(phone=phone, agent={"system_prompt": "x"})
    a, b = await asyncio.gather(
        tool.execute(to="+15551111111"),
        tool.execute(to="+15552222222"),
    )
    assert a.call_id != b.call_id
    assert {a.call_id, b.call_id} == {"CA-1", "CA-2"}


@pytest.mark.asyncio
async def test_next_call_id_resets_when_capture_times_out() -> None:
    """If the call_initiated SSE never arrives, asyncio.wait_for raises
    TimeoutError. The provider must clear ``_next_call_id`` so the next
    legitimate execute() can run; otherwise the next call hangs on the
    stale future."""

    class _NoEmitPatter(_FakePatter):
        async def call(self, *, to: str, agent: dict[str, Any]) -> None:
            # Intentionally do nothing — no call_initiated, no call_end.
            self._calls_issued.append({"to": to, "agent": agent, "call_id": ""})

    phone = _NoEmitPatter()
    tool = PatterTool(phone=phone, agent={"system_prompt": "x"})

    # Patch asyncio.wait_for so the dial capture fails immediately instead
    # of waiting 10s real-time. The TimeoutError must propagate AND the
    # `_next_call_id` slot must be cleared so the next execute() works.
    real_wait_for = asyncio.wait_for

    async def _instant_timeout(awaitable: Any, timeout: float) -> Any:
        # Cancel the awaitable to avoid the "task was destroyed but it is
        # pending" warning, then raise.
        if hasattr(awaitable, "cancel"):
            awaitable.cancel()
        raise asyncio.TimeoutError

    import unittest.mock as _mock

    with _mock.patch.object(asyncio, "wait_for", side_effect=_instant_timeout):
        with pytest.raises(asyncio.TimeoutError):
            await tool.execute(to="+15551234567")

    # Slot must be cleared so the next legitimate dial isn't blocked.
    assert tool._next_call_id is None
    # And the unused real_wait_for stays referenced (linter happy).
    _ = real_wait_for
