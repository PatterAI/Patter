"""The ``tool_call_preambles`` knob must be wired into the Realtime session
``instructions`` at the adapter-build site in ``OpenAIRealtimeStreamHandler.start``.

These tests run the REAL ``start()`` code path. The only thing stubbed is the
``OpenAIRealtime2Adapter`` class itself (the paid OpenAI-WS boundary): a fake
records the ``instructions`` kwarg it was built with and no-ops ``connect`` /
``receive_events`` so no socket is opened. Tagged ``@pytest.mark.mocked``.
"""

from __future__ import annotations

import pytest

from getpatter import OpenAIRealtime2, Patter, Twilio
from getpatter.stream_handler import (
    DEFAULT_TOOL_CALL_PREAMBLE_BLOCK,
    OpenAIRealtimeStreamHandler,
)

pytestmark = pytest.mark.mocked


class _RecordingAdapter:
    """Captures ctor kwargs; stands in for the real GA adapter (OpenAI WS)."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        type(self).last_kwargs = kwargs
        self.instructions = kwargs.get("instructions")
        self.tools = kwargs.get("tools", [])

    async def connect(self) -> None:
        return None

    def adopt_websocket(self, ws) -> None:  # pragma: no cover - no parked WS
        return None

    async def receive_events(self):
        if False:  # pragma: no cover - empty async generator
            yield None

    async def send_first_message(self, text: str) -> None:  # pragma: no cover
        return None

    async def send_text(self, text: str) -> None:  # pragma: no cover
        return None

    async def close(self) -> None:  # pragma: no cover
        return None


class _NullAudioSender:
    async def send_audio(self, audio_bytes: bytes) -> None:  # pragma: no cover
        return None

    async def send_clear(self) -> None:  # pragma: no cover
        return None

    def reset_pcm_carry(self) -> None:  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


@pytest.fixture
def _patched_adapter(monkeypatch: pytest.MonkeyPatch):
    import getpatter.providers.openai_realtime_2 as mod

    _RecordingAdapter.last_kwargs = {}
    monkeypatch.setattr(mod, "OpenAIRealtime2Adapter", _RecordingAdapter)
    return _RecordingAdapter


async def _run_start(agent, resolved_prompt: str) -> _RecordingAdapter:
    handler = OpenAIRealtimeStreamHandler(
        agent=agent,
        audio_sender=_NullAudioSender(),
        call_id="c1",
        caller="+15555550100",
        callee="+15555550101",
        resolved_prompt=resolved_prompt,
        metrics=None,
        openai_key="sk-test",
    )
    await handler.start()
    # Stop the background forward task — receive_events is an empty generator
    # so it finishes immediately, but cancel defensively.
    if handler._background_task is not None:
        handler._background_task.cancel()
    return handler._adapter  # type: ignore[return-value]


def _agent(**kwargs):
    phone = Patter(
        carrier=Twilio(account_sid="ACtest", auth_token="tok"),
        phone_number="+15555550100",
    )
    # OpenAIRealtime2 engine reads OPENAI_API_KEY into the local config so the
    # Realtime-mode key guard in agent() passes; no network is touched.
    return phone.agent(
        system_prompt=kwargs.pop("system_prompt", "You are a receptionist."),
        engine=OpenAIRealtime2(),
        **kwargs,
    )


async def test_preamble_block_prepended_when_true(_patched_adapter) -> None:
    agent = _agent(system_prompt="You are a receptionist.", tool_call_preambles=True)
    adapter = await _run_start(agent, "You are a receptionist.")
    instr = adapter.instructions
    assert instr.startswith("# Preambles")
    assert instr == f"{DEFAULT_TOOL_CALL_PREAMBLE_BLOCK}\n\nYou are a receptionist."


async def test_instructions_byte_identical_when_false(_patched_adapter) -> None:
    agent = _agent(system_prompt="You are a receptionist.")
    assert agent.tool_call_preambles is False
    adapter = await _run_start(agent, "You are a receptionist.")
    assert adapter.instructions == "You are a receptionist."


async def test_str_override_injected_verbatim(_patched_adapter) -> None:
    block = "# Preambles\n\nSay one short line before slow tools."
    agent = _agent(system_prompt="You are a receptionist.", tool_call_preambles=block)
    adapter = await _run_start(agent, "You are a receptionist.")
    assert adapter.instructions == f"{block}\n\nYou are a receptionist."


async def test_per_tool_sample_phrase_only_with_reassurance_and_knob_on(
    _patched_adapter,
) -> None:
    from getpatter import tool

    async def _noop(arguments: dict, call_context: dict) -> str:
        return "{}"

    t_with = tool(
        name="check_order",
        handler=_noop,
        reassurance="One moment while I check that for you.",
    )
    t_without = tool(name="lookup", handler=_noop)

    agent = _agent(
        system_prompt="You are a receptionist.",
        tools=[t_with, t_without],
        tool_call_preambles=True,
    )
    adapter = await _run_start(agent, "You are a receptionist.")
    by_name = {t["name"]: t for t in adapter.tools}
    assert "Preamble sample phrases:" in by_name["check_order"]["description"]
    assert "One moment while I check that for you." in by_name["check_order"]["description"]
    # Tool without reassurance gets no hint.
    assert "Preamble sample phrases:" not in by_name["lookup"]["description"]


async def test_per_tool_sample_phrase_absent_when_knob_off(_patched_adapter) -> None:
    from getpatter import tool

    async def _noop(arguments: dict, call_context: dict) -> str:
        return "{}"

    t_with = tool(
        name="check_order",
        handler=_noop,
        reassurance="One moment while I check that for you.",
    )
    agent = _agent(system_prompt="You are a receptionist.", tools=[t_with])
    adapter = await _run_start(agent, "You are a receptionist.")
    by_name = {t["name"]: t for t in adapter.tools}
    assert "Preamble sample phrases:" not in by_name["check_order"]["description"]
