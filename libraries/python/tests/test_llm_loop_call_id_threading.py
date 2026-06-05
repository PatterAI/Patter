"""The LLM loop threads call_id from call_context into provider.stream().

Real ``LLMLoop`` end to end. The "provider" is a tiny in-process recording
double that records the ``call_id`` kwarg it received — it is NOT a mock of the
unit under test (the loop is real); it only stands in for the external LLM
endpoint, exactly like the existing FakeLLMProvider in test_llm_loop.py.
"""

from __future__ import annotations

import pytest

from getpatter.services.llm_loop import LLMLoop


def _make_loop(provider) -> LLMLoop:
    """Construct a real LLMLoop around a recording provider (no network)."""
    loop = LLMLoop.__new__(LLMLoop)
    loop._provider = provider
    loop._system_prompt = "You are a test assistant."
    loop._tools = None
    loop._tool_executor = None
    loop._metrics = None
    loop._event_bus = None
    loop._model = "fake-model"
    loop._provider_name = "fake"
    loop._openai_tools = None
    loop._tool_map = {}
    loop._on_tool_call = None
    loop._usage_missing_count = 0
    loop._logged_usage_fallback = False
    return loop


class _RecordingProvider:
    """Records the call_id it was streamed with, then yields one text chunk."""

    def __init__(self) -> None:
        self.seen_call_id: object = "<<unset>>"
        self.stream_called = False

    async def stream(self, messages, tools=None, *, cancel_event=None, call_id=None):
        self.stream_called = True
        self.seen_call_id = call_id
        yield {"type": "text", "content": "ok"}


class _LegacyProvider:
    """An older provider that only reads **kwargs (mirrors a stock
    OpenAILLMProvider-shaped double) — no session config, no `user` emitted."""

    def __init__(self) -> None:
        self.seen_kwargs: dict = {}

    async def stream(self, messages, tools=None, **kwargs):
        self.seen_kwargs = dict(kwargs)
        yield {"type": "text", "content": "legacy"}


@pytest.mark.unit
async def test_run_forwards_call_id_from_context_into_provider_stream() -> None:
    provider = _RecordingProvider()
    loop = _make_loop(provider)

    tokens = []
    async for token in loop.run("Hi", [], {"call_id": "xyz"}):
        tokens.append(token)

    assert provider.stream_called is True
    assert provider.seen_call_id == "xyz"
    assert tokens == ["ok"]


@pytest.mark.unit
async def test_run_passes_none_call_id_when_context_lacks_it() -> None:
    provider = _RecordingProvider()
    loop = _make_loop(provider)

    async for _ in loop.run("Hi", [], {}):  # no call_id key
        pass

    assert provider.seen_call_id is None


@pytest.mark.unit
async def test_legacy_provider_ignores_call_id_without_error() -> None:
    """A provider that only takes **kwargs still works — the added call_id
    kwarg is swallowed harmlessly (backward compatibility of the protocol)."""
    provider = _LegacyProvider()
    loop = _make_loop(provider)

    tokens = []
    async for token in loop.run("Hi", [], {"call_id": "abc"}):
        tokens.append(token)

    assert tokens == ["legacy"]
    # The loop did pass call_id; the legacy provider absorbed it via **kwargs.
    assert provider.seen_kwargs.get("call_id") == "abc"
