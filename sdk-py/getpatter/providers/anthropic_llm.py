"""Anthropic Claude LLM provider for Patter's pipeline mode.

This provider implements the :class:`getpatter.services.llm_loop.LLMProvider`
protocol on top of Anthropic's Messages API with streaming.  Tool calls
are translated from Anthropic's ``tool_use`` content blocks into the
OpenAI-compatible ``{"type": "tool_call", ...}`` chunk format that the
:class:`~getpatter.services.llm_loop.LLMLoop` expects.

Portions adapted from LiveKit Agents
(https://github.com/livekit/agents, commit 78a66bcf79c5cea82989401c408f1dff4b961a5b,
file livekit-plugins/livekit-plugins-anthropic/livekit/plugins/anthropic/llm.py),
licensed under Apache License 2.0. Copyright 2023 LiveKit, Inc.

Adaptations from the LiveKit source:
  * Reshaped the ``llm.LLM`` / ``llm.LLMStream`` class pair into a single
    :class:`AnthropicLLMProvider` that conforms to Patter's duck-typed
    ``LLMProvider`` Protocol (``async def stream(messages, tools)``).
  * Translates OpenAI-formatted messages (``role``/``content``/
    ``tool_calls``/``tool_call_id``) into Anthropic's Messages API shape
    (single ``system`` string, ``user``/``assistant`` turns with
    ``tool_use``/``tool_result`` content blocks) so callers don't need
    two message formats.
  * Maps Anthropic stream events (``content_block_start``,
    ``content_block_delta``, ``content_block_stop``) to the Patter chunk
    protocol ``{"type": "text"|"tool_call"|"done", ...}``.
  * Dropped LiveKit-specific concerns (``APIConnectOptions``,
    ``chat_ctx.to_provider_format``, metrics events, retry wrapping)
    that don't apply to Patter's thinner runtime.
"""

from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator

logger = logging.getLogger("patter")

__all__ = ["AnthropicLLMProvider"]


# Default model. Anthropic requires an explicit max_tokens for every request;
# we use LiveKit's default of 1024 when the caller doesn't provide one.
_DEFAULT_MODEL = "claude-3-5-sonnet-20241022"
_DEFAULT_MAX_TOKENS = 1024


class AnthropicLLMProvider:
    """LLM provider backed by Anthropic's Messages API (streaming).

    Implements Patter's :class:`LLMProvider` protocol: ``stream`` yields
    ``{"type": "text" | "tool_call" | "done", ...}`` chunks.

    Args:
        api_key: Anthropic API key. If omitted, ``ANTHROPIC_API_KEY`` is
            read from the environment.
        model: Model identifier (e.g. ``"claude-3-5-sonnet-20241022"``).
        max_tokens: Maximum tokens to generate per response.  Required
            by the Messages API; defaults to 1024.
        temperature: Optional sampling temperature.
        base_url: Optional Anthropic API base URL override.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        base_url: str | None = None,
    ) -> None:
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "The 'anthropic' package is required for AnthropicLLMProvider. "
                "Install it with: pip install 'getpatter[anthropic]'"
            ) from e

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Anthropic API key is required, either as the 'api_key' "
                "argument or via the ANTHROPIC_API_KEY environment variable."
            )

        self._client = anthropic.AsyncAnthropic(
            api_key=resolved_key,
            base_url=base_url,
        )
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Stream chunks from Anthropic's Messages API.

        Translates OpenAI-style ``messages``/``tools`` to Anthropic's
        shape, then normalises the event stream back into the Patter
        chunk protocol.
        """
        system_prompt, anthropic_messages = _to_anthropic_messages(messages)
        anthropic_tools = _to_anthropic_tools(tools) if tools else None

        kwargs: dict = {
            "model": self._model,
            "messages": anthropic_messages,
            "max_tokens": self._max_tokens,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        # tool_call_id -> stable "index" for the Patter chunk protocol.
        tool_indices: dict[str, int] = {}
        current_tool_id: str | None = None
        current_tool_index: int | None = None

        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                event_type = getattr(event, "type", None)

                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block is not None and getattr(block, "type", None) == "tool_use":
                        tool_id = getattr(block, "id", "") or ""
                        tool_name = getattr(block, "name", "") or ""
                        if tool_id not in tool_indices:
                            tool_indices[tool_id] = len(tool_indices)
                        current_tool_id = tool_id
                        current_tool_index = tool_indices[tool_id]
                        yield {
                            "type": "tool_call",
                            "index": current_tool_index,
                            "id": tool_id,
                            "name": tool_name,
                            "arguments": "",
                        }

                elif event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", None) if delta else None

                    if delta_type == "text_delta":
                        text = getattr(delta, "text", "") or ""
                        if text:
                            yield {"type": "text", "content": text}

                    elif delta_type == "input_json_delta":
                        partial = getattr(delta, "partial_json", "") or ""
                        if partial and current_tool_index is not None:
                            yield {
                                "type": "tool_call",
                                "index": current_tool_index,
                                "id": current_tool_id,
                                "name": None,
                                "arguments": partial,
                            }

                elif event_type == "content_block_stop":
                    current_tool_id = None
                    current_tool_index = None

        yield {"type": "done"}


# ---------------------------------------------------------------------------
# Message / tool translation (OpenAI format -> Anthropic Messages API)
# ---------------------------------------------------------------------------


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Convert OpenAI-style tool definitions to Anthropic tool schema."""
    out: list[dict] = []
    for tool in tools:
        # OpenAI format: {"type": "function", "function": {...}}
        fn = tool.get("function", tool)
        out.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return out


def _to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert OpenAI-style messages to Anthropic (system_str, messages).

    Anthropic expects:
      * A single ``system`` string (sent as a top-level kwarg).
      * ``user`` / ``assistant`` turns only.
      * Assistant tool calls become ``tool_use`` content blocks.
      * Tool results become ``user`` messages with ``tool_result`` blocks.
    """
    system_parts: list[str] = []
    out: list[dict] = []

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            content = msg.get("content")
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue

        if role == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": content})
            continue

        if role == "assistant":
            blocks: list[dict] = []
            text = msg.get("content")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})

            for tc in msg.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    }
                )

            if not blocks:
                # Skip empty assistant turns to keep Anthropic happy.
                continue
            out.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            content = msg.get("content", "")
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call_id,
                            "content": content if isinstance(content, str) else json.dumps(content),
                        }
                    ],
                }
            )
            continue

    return "\n\n".join(system_parts), out
