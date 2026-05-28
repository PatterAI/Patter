"""Tests for the PatterTool integration adapter.

Mirrors `libraries/typescript/tests/patter-tool.test.ts` so the cross-SDK contract stays
in lockstep. The full call flow needs a live carrier+webhook, so these tests
focus on the deterministic surface: schema shape, option validation, the
delegation to ``Patter.call(wait=True)`` (using a fake Patter that honours the
``CallResult`` contract), and the Hermes handler envelope.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from getpatter.integrations import PatterTool
from getpatter.models import CallMetrics, CallResult, CostBreakdown, LatencyBreakdown


def _fake_call_result(call_id: str, outcome: str = "answered") -> CallResult:
    """Build a realistic CallResult like the SDK's completion registry emits."""
    metrics = CallMetrics(
        call_id=call_id,
        duration_seconds=12.3,
        turns=(),
        cost=CostBreakdown(total=0.0123),
        latency_avg=LatencyBreakdown(),
        latency_p95=LatencyBreakdown(),
        provider_mode="pipeline",
    )
    return CallResult(
        call_id=call_id,
        outcome=outcome,  # type: ignore[arg-type]
        status="completed",
        duration_seconds=12.3,
        transcript=(
            {"role": "agent", "text": "Hello!"},
            {"role": "user", "text": "Hi."},
        ),
        cost=CostBreakdown(total=0.0123),
        metrics=metrics,
    )


class _FakePatter:
    """In-memory Patter double that honours the ``call(wait=True)`` contract.

    ``call(wait=True)`` resolves to a ``CallResult`` (what PatterTool now
    consumes); ``wait=False`` returns ``None`` like the real SDK. Set
    ``never_end=True`` to simulate a call that never reaches a terminal signal
    so the ``execute()`` timeout path can be exercised.
    """

    def __init__(self, outcome: str = "answered") -> None:
        self._serve_kwargs: dict[str, Any] = {}
        self._calls_issued: list[dict[str, Any]] = []
        self._counter = 0
        self._outcome = outcome
        self.never_end = False
        # PatterTool never touches this, but a real served Patter has a server.
        self._server = object()

    def agent(self, **kwargs: Any) -> dict[str, Any]:
        return {"__agent": True, **kwargs}

    async def serve(self, **kwargs: Any) -> None:
        self._serve_kwargs = kwargs

    async def disconnect(self) -> None:
        self._server = None

    async def call(
        self, *, to: str, agent: dict[str, Any], wait: bool = False, **_kw: Any
    ) -> CallResult | None:
        # No await between increment and read → each call() gets a distinct id
        # even under asyncio.gather (single-threaded event loop).
        self._counter += 1
        call_id = f"CA-{self._counter}"
        self._calls_issued.append(
            {"to": to, "agent": agent, "call_id": call_id, "wait": wait}
        )
        if not wait:
            return None
        if self.never_end:
            # Simulate a call that never reaches a terminal state so the
            # execute() backstop timeout fires.
            await asyncio.Event().wait()
        return _fake_call_result(call_id, self._outcome)


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


# --- delegation to call(wait=True) ------------------------------------


@pytest.mark.asyncio
async def test_execute_requests_wait_true() -> None:
    """PatterTool must delegate to ``call(wait=True)`` — that's the whole
    point of the refactor (the SDK owns dial→completion correlation now)."""
    phone = _FakePatter()
    tool = PatterTool(phone=phone, agent={"system_prompt": "x"})
    await tool.execute(to="+15551234567")
    assert phone._calls_issued[0]["wait"] is True


@pytest.mark.asyncio
async def test_execute_surfaces_outcome() -> None:
    """The CallResult ``outcome`` flows into the tool envelope (voicemail
    must be distinguishable from answered)."""
    phone = _FakePatter(outcome="voicemail")
    tool = PatterTool(phone=phone, agent={"system_prompt": "x"})
    result = await tool.execute(to="+15551234567")
    assert result.outcome == "voicemail"


@pytest.mark.asyncio
async def test_concurrent_execute_returns_distinct_results() -> None:
    """Two parallel execute() calls each get their own CallResult — the SDK
    correlates each dial to its own completion, no shared mutable slot."""
    phone = _FakePatter()
    tool = PatterTool(phone=phone, agent={"system_prompt": "x"})
    a, b = await asyncio.gather(
        tool.execute(to="+15551111111"),
        tool.execute(to="+15552222222"),
    )
    assert a.call_id != b.call_id
    assert {a.call_id, b.call_id} == {"CA-1", "CA-2"}
