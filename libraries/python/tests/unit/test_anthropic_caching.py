"""Unit tests for the Anthropic prompt-caching opt-out switch.

The provider rewrites the request payload when ``prompt_caching=True``
(the default) so that:

* The system prompt becomes a single ``text`` block tagged with
  ``cache_control={"type": "ephemeral"}``.
* The LAST tool block carries the same marker (caches the whole list).
* The ``anthropic-beta: prompt-caching-2024-07-31`` header is sent.

These tests intercept the call to ``client.messages.stream(...)`` and
inspect the kwargs/headers without ever touching the network.
"""

from __future__ import annotations

import importlib.util
from typing import Any
from unittest.mock import MagicMock

import pytest

_ANTHROPIC_AVAILABLE = importlib.util.find_spec("anthropic") is not None
pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not _ANTHROPIC_AVAILABLE,
        reason="anthropic package not installed — run with getpatter[anthropic]",
    ),
    pytest.mark.asyncio,
]


# ---------------------------------------------------------------------------
# Fake Anthropic stream — minimal async-iterable + async-context-manager.
# ---------------------------------------------------------------------------


class _FakeStream:
    """Drop-in for ``anthropic.AsyncAnthropic.messages.stream(...)``.

    Captures the kwargs (so the test can inspect them) and yields a
    single ``content_block_stop`` event so the provider's loop exits
    cleanly.
    """

    def __init__(self, captured_kwargs: dict[str, Any], **kwargs: Any) -> None:
        captured_kwargs.update(kwargs)

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self) -> "_FakeStream":
        self._events = iter(
            [MagicMock(type="content_block_stop")]
        )
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration


def _patch_provider(provider: Any) -> dict[str, Any]:
    """Replace ``provider._client.messages.stream`` with a capturing fake.

    Returns the dict that captures the kwargs of the last call.
    """
    captured: dict[str, Any] = {}

    def _stream(**kwargs: Any) -> _FakeStream:  # synchronous: returns CM
        return _FakeStream(captured, **kwargs)

    provider._client = MagicMock()
    provider._client.messages.stream = _stream
    return captured


async def _drain(provider: Any, messages: list[dict], tools: list[dict] | None = None) -> None:
    """Consume the provider's ``stream(...)`` generator end-to-end."""
    async for _ in provider.stream(messages, tools):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_prompt_caching_default_on_wraps_system_block() -> None:
    """With caching ON (default), system becomes a list of blocks with cache_control."""
    from getpatter.providers.anthropic_llm import AnthropicLLMProvider

    provider = AnthropicLLMProvider(api_key="sk-test")
    captured = _patch_provider(provider)

    await _drain(
        provider,
        messages=[
            {"role": "system", "content": "You are a long instruction-dense agent."},
            {"role": "user", "content": "Hi"},
        ],
    )

    assert isinstance(captured["system"], list), "system must be an array of blocks"
    assert len(captured["system"]) == 1
    block = captured["system"][0]
    assert block["type"] == "text"
    assert block["text"] == "You are a long instruction-dense agent."
    assert block["cache_control"] == {"type": "ephemeral"}


async def test_prompt_caching_default_on_marks_last_tool() -> None:
    """When tools are present, only the LAST block gets cache_control."""
    from getpatter.providers.anthropic_llm import AnthropicLLMProvider

    provider = AnthropicLLMProvider(api_key="sk-test")
    captured = _patch_provider(provider)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_order",
                "description": "Look up order",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "transfer_to_human",
                "description": "Hand off",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]

    await _drain(
        provider,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hi"},
        ],
        tools=tools,
    )

    sent_tools = captured["tools"]
    assert len(sent_tools) == 2
    # Only the LAST tool block carries cache_control.
    assert "cache_control" not in sent_tools[0]
    assert sent_tools[1]["cache_control"] == {"type": "ephemeral"}
    # Other tool fields are preserved.
    assert sent_tools[1]["name"] == "transfer_to_human"


async def test_prompt_caching_default_on_sends_beta_header() -> None:
    """Caching ON ships the ``anthropic-beta: prompt-caching-2024-07-31`` header."""
    from getpatter.providers.anthropic_llm import AnthropicLLMProvider

    provider = AnthropicLLMProvider(api_key="sk-test")
    captured = _patch_provider(provider)

    await _drain(
        provider,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hi"},
        ],
    )

    headers = captured.get("extra_headers", {})
    assert headers.get("anthropic-beta") == "prompt-caching-2024-07-31"


async def test_prompt_caching_off_falls_back_to_string_system() -> None:
    """With caching OFF, system stays a plain string and no beta header is sent."""
    from getpatter.providers.anthropic_llm import AnthropicLLMProvider

    provider = AnthropicLLMProvider(api_key="sk-test", prompt_caching=False)
    captured = _patch_provider(provider)

    await _drain(
        provider,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hi"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "noop",
                    "description": "noop",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    # Plain string system prompt — original behaviour preserved.
    assert captured["system"] == "sys"
    # No cache_control on the tool either.
    assert "cache_control" not in captured["tools"][0]
    # No beta header.
    assert "extra_headers" not in captured


async def test_prompt_caching_public_wrapper_threads_option() -> None:
    """``llm.anthropic.LLM(prompt_caching=False)`` reaches the underlying provider."""
    from getpatter.llm.anthropic import LLM

    llm = LLM(api_key="sk-test", prompt_caching=False)
    captured = _patch_provider(llm)

    await _drain(
        llm,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Hi"},
        ],
    )

    # Wrapper forwarded prompt_caching=False all the way through.
    assert captured["system"] == "sys"
    assert "extra_headers" not in captured
