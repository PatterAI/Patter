"""Unit tests for ``Patter.call(wait=True)`` → ``CallResult`` and the async
context manager.

Mirrors ``libraries/typescript/tests/call-wait.test.ts``. These exercise the
REAL completion registry on a REAL ``EmbeddedServer`` and the REAL
``_on_call_end`` wrapper — the only mocked surface is the carrier adapter
(``TwilioAdapter.initiate_call``), which is the external boundary we are never
allowed to hit in CI. Per the authentic-tests rule: swap a live carrier back
in and these still pass unchanged, because the correlation logic under test is
all real SDK code.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from getpatter import Twilio
from getpatter.client import Patter
from getpatter.exceptions import PatterConnectionError
from getpatter.models import (
    Agent,
    CallMetrics,
    CallResult,
    CostBreakdown,
    LatencyBreakdown,
)
from getpatter.server import (
    EmbeddedServer,
    _telnyx_hangup_outcome,
    _twilio_status_to_outcome,
)


def _local_phone(**kwargs) -> Patter:
    defaults = dict(
        carrier=Twilio(
            account_sid="ACtest000000000000000000000000000", auth_token="tok"
        ),
        phone_number="+15550001234",
        webhook_url="abc.ngrok.io",
    )
    defaults.update(kwargs)
    return Patter(**defaults)


def _attach_real_server(phone: Patter, agent: Agent) -> EmbeddedServer:
    """Give the phone a real EmbeddedServer (the completion registry lives
    here). We don't bind a socket — call(wait=True) only needs the registry
    + the terminal-signal handlers, all of which are pure in-process code."""
    server = EmbeddedServer(phone._local_config, agent, dashboard=False)
    phone._server = server
    return server


def _patch_twilio(call_id: str):
    """Patch the carrier boundary so initiate_call returns a fixed call_id."""
    mock_adapter = AsyncMock()
    mock_adapter.initiate_call = AsyncMock(return_value=call_id)
    return patch(
        "getpatter.providers.twilio_adapter.TwilioAdapter",
        return_value=mock_adapter,
    )


def _metrics(call_id: str, *, duration: float = 12.3, cost: float = 0.0123) -> CallMetrics:
    return CallMetrics(
        call_id=call_id,
        duration_seconds=duration,
        turns=(),
        cost=CostBreakdown(total=cost),
        latency_avg=LatencyBreakdown(),
        latency_p95=LatencyBreakdown(),
        provider_mode="pipeline",
    )


async def _wait_until_registered(server: EmbeddedServer, call_id: str) -> None:
    """Yield to the loop until call(wait=True) has registered its future."""
    for _ in range(100):
        if call_id in server._completions:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"completion for {call_id} was never registered")


# ---------------------------------------------------------------------------
# Backward compatibility — wait=False
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_wait_false_returns_none() -> None:
    """Default (fire-and-forget) returns None the instant the dial is accepted."""
    phone = _local_phone()
    agent = Agent(system_prompt="x", prewarm=False)
    with _patch_twilio("CA_x"):
        result = await phone.call(to="+15550009999", agent=agent)
    assert result is None


@pytest.mark.unit
async def test_call_wait_true_requires_active_server() -> None:
    """wait=True without serve()/async-with must raise a clear error rather
    than awaiting a future no webhook can ever resolve."""
    phone = _local_phone()
    agent = Agent(system_prompt="x", prewarm=False)
    with pytest.raises(PatterConnectionError, match="requires an active server"):
        await phone.call(to="+15550009999", agent=agent, wait=True)


# ---------------------------------------------------------------------------
# wait=True — connected call resolves via the real _on_call_end wrapper
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_wait_true_returns_answered_callresult() -> None:
    phone = _local_phone()
    agent = Agent(system_prompt="x", prewarm=False)
    server = _attach_real_server(phone, agent)
    _, on_call_end, _ = server._wrap_callbacks()

    with _patch_twilio("CA_answer"):
        task = asyncio.create_task(
            phone.call(to="+15550009999", agent=agent, wait=True)
        )
        await _wait_until_registered(server, "CA_answer")
        # Real media-stream-end payload → the real wrapper resolves the future.
        await on_call_end(
            {
                "call_id": "CA_answer",
                "caller": "+15550001234",
                "callee": "+15550009999",
                "ended_at": 1000.0,
                "transcript": [
                    {"role": "agent", "text": "Hello!"},
                    {"role": "user", "text": "Hi."},
                ],
                "metrics": _metrics("CA_answer"),
            }
        )
        result = await asyncio.wait_for(task, timeout=2.0)

    assert isinstance(result, CallResult)
    assert result.call_id == "CA_answer"
    assert result.outcome == "answered"
    assert result.status == "completed"
    assert result.duration_seconds == 12.3
    assert result.cost is not None and result.cost.total == 0.0123
    assert len(result.transcript) == 2


@pytest.mark.unit
async def test_call_wait_true_voicemail_when_amd_machine() -> None:
    phone = _local_phone()
    agent = Agent(system_prompt="x", prewarm=False)
    server = _attach_real_server(phone, agent)
    _, on_call_end, _ = server._wrap_callbacks()

    with _patch_twilio("CA_vm"):
        task = asyncio.create_task(
            phone.call(to="+15550009999", agent=agent, wait=True)
        )
        await _wait_until_registered(server, "CA_vm")
        # The AMD webhook records the classification before the stream ends.
        server._amd_class["CA_vm"] = "machine"
        await on_call_end(
            {
                "call_id": "CA_vm",
                "transcript": [],
                "metrics": _metrics("CA_vm", duration=4.0),
            }
        )
        result = await asyncio.wait_for(task, timeout=2.0)

    assert result.outcome == "voicemail"
    assert result.status == "completed"


# ---------------------------------------------------------------------------
# wait=True — no-media outcomes resolve via the status-callback path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_call_wait_true_no_answer_via_status() -> None:
    phone = _local_phone()
    agent = Agent(system_prompt="x", prewarm=False)
    server = _attach_real_server(phone, agent)

    with _patch_twilio("CA_noans"):
        task = asyncio.create_task(
            phone.call(to="+15550009999", agent=agent, wait=True)
        )
        await _wait_until_registered(server, "CA_noans")
        # This is exactly what the Twilio status-callback route does for a
        # call that never reaches media.
        server._resolve_completion(
            "CA_noans",
            outcome=_twilio_status_to_outcome("no-answer"),
            status="no-answer",
        )
        result = await asyncio.wait_for(task, timeout=2.0)

    assert result.outcome == "no_answer"
    assert result.status == "no-answer"
    assert result.transcript == ()
    assert result.cost is None
    assert result.metrics is None


@pytest.mark.unit
async def test_call_wait_true_failed_when_disconnected_midflight() -> None:
    """disconnect() while a wait is in flight fails the awaiter rather than
    hanging it until the backstop."""
    phone = _local_phone()
    agent = Agent(system_prompt="x", prewarm=False)
    server = _attach_real_server(phone, agent)

    async def _stub_stop() -> None:
        return None

    server.stop = _stub_stop  # type: ignore[assignment]

    with _patch_twilio("CA_disc"):
        task = asyncio.create_task(
            phone.call(to="+15550009999", agent=agent, wait=True)
        )
        await _wait_until_registered(server, "CA_disc")
        await phone.disconnect()
        with pytest.raises(PatterConnectionError, match="still in flight"):
            await asyncio.wait_for(task, timeout=2.0)


# ---------------------------------------------------------------------------
# Outcome mapping helpers (pure functions)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_twilio_status_to_outcome_mapping() -> None:
    assert _twilio_status_to_outcome("no-answer") == "no_answer"
    assert _twilio_status_to_outcome("busy") == "busy"
    assert _twilio_status_to_outcome("failed") == "failed"
    assert _twilio_status_to_outcome("canceled") == "failed"


@pytest.mark.unit
def test_telnyx_hangup_outcome_mapping() -> None:
    assert _telnyx_hangup_outcome("no_answer") == "no_answer"
    assert _telnyx_hangup_outcome("timeout") == "no_answer"
    assert _telnyx_hangup_outcome("user_busy") == "busy"
    assert _telnyx_hangup_outcome("call_rejected") == "failed"
    # normal_clearing implies the call connected → resolved via on_call_end.
    assert _telnyx_hangup_outcome("normal_clearing") is None


# ---------------------------------------------------------------------------
# Async context manager — guaranteed disconnect()
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_async_context_manager_calls_disconnect() -> None:
    phone = _local_phone()
    calls = {"n": 0}

    async def _spy() -> None:
        calls["n"] += 1

    phone.disconnect = _spy  # type: ignore[assignment]

    async with phone as entered:
        assert entered is phone
    assert calls["n"] == 1


@pytest.mark.unit
async def test_async_context_manager_disconnects_on_exception() -> None:
    phone = _local_phone()
    calls = {"n": 0}

    async def _spy() -> None:
        calls["n"] += 1

    phone.disconnect = _spy  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="boom"):
        async with phone:
            raise RuntimeError("boom")
    assert calls["n"] == 1
