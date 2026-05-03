"""Anthropic Claude LLM provider for Patter's pipeline mode.

Implements the :class:`getpatter.services.llm_loop.LLMProvider` protocol on
top of Anthropic's Messages API with streaming. The provider:

  * Translates OpenAI-formatted messages (``role``/``content``/
    ``tool_calls``/``tool_call_id``) into Anthropic's Messages API shape
    (single ``system`` string, ``user``/``assistant`` turns with
    ``tool_use``/``tool_result`` content blocks) so callers don't need
    two message formats.
  * Maps Anthropic stream events (``content_block_start``,
    ``content_block_delta``, ``content_block_stop``) to the Patter chunk
    protocol ``{"type": "text"|"tool_call"|"done", ...}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from enum import StrEnum
from typing import AsyncIterator

logger = logging.getLogger("getpatter")

__all__ = ["AnthropicLLMProvider", "AnthropicModel"]


class AnthropicModel(StrEnum):
    """Canonical Anthropic Claude model identifiers and aliases.

    The ``*_ALIAS`` members route to Anthropic's latest snapshot for the named
    model line; the dated members pin a specific snapshot.
    """

    # Aliases — Anthropic resolves to the latest snapshot.
    CLAUDE_HAIKU_4_5_ALIAS = "claude-haiku-4-5"
    CLAUDE_SONNET_4_6_ALIAS = "claude-sonnet-4-6"
    CLAUDE_OPUS_4_7_ALIAS = "claude-opus-4-7"
    CLAUDE_3_5_SONNET_ALIAS = "claude-3-5-sonnet-latest"
    CLAUDE_3_5_HAIKU_ALIAS = "claude-3-5-haiku-latest"

    # Pinned snapshots.
    CLAUDE_HAIKU_4_5_20251001 = "claude-haiku-4-5-20251001"
    CLAUDE_3_5_SONNET_20241022 = "claude-3-5-sonnet-20241022"
    CLAUDE_3_5_HAIKU_20241022 = "claude-3-5-haiku-20241022"


# Default model. Anthropic requires an explicit max_tokens for every request;
# we use a default of 1024 when the caller doesn't provide one.
_DEFAULT_MODEL = AnthropicModel.CLAUDE_HAIKU_4_5_20251001.value
_DEFAULT_MAX_TOKENS = 1024

# Anthropic prompt-caching beta header. Caching is now generally available,
# but the explicit beta opt-in remains supported and ensures consistent
# behaviour across model snapshots that haven't yet promoted the feature.
# See: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
_PROMPT_CACHING_BETA = "prompt-caching-2024-07-31"

# Backwards-compat module-level aliases (kept so existing imports keep working).
CLAUDE_HAIKU_45_ALIAS = AnthropicModel.CLAUDE_HAIKU_4_5_ALIAS.value
CLAUDE_SONNET_46_ALIAS = AnthropicModel.CLAUDE_SONNET_4_6_ALIAS.value
CLAUDE_OPUS_47_ALIAS = AnthropicModel.CLAUDE_OPUS_4_7_ALIAS.value


class AnthropicLLMProvider:
    """LLM provider backed by Anthropic's Messages API (streaming).

    Implements Patter's :class:`LLMProvider` protocol: ``stream`` yields
    ``{"type": "text" | "tool_call" | "done", ...}`` chunks.

    Args:
        api_key: Anthropic API key. If omitted, ``ANTHROPIC_API_KEY`` is
            read from the environment.
        model: Model identifier (e.g. ``"claude-haiku-4-5-20251001"``).
        max_tokens: Maximum tokens to generate per response.  Required
            by the Messages API; defaults to 1024.
        temperature: Optional sampling temperature.
        base_url: Optional Anthropic API base URL override.
        prompt_caching: Enable Anthropic prompt caching for the system
            prompt and tools. Defaults to ``True`` because, for voice
            agents with long instruction-dense system prompts, the cache
            saves ~100-400 ms TTFT and ~90% of input-token cost on every
            cached turn. The cache lives ~5 minutes; the first request
            writes it, subsequent requests within that window hit it.

            Disable (``prompt_caching=False``) when:
              * The system prompt + tools combined are smaller than
                Anthropic's minimum cacheable size (~1024 tokens for
                Sonnet/Opus, ~2048 for Haiku at the time of writing)
                — caching has no effect below that threshold.
              * You explicitly want every turn to bypass the cache for
                debugging or A/B comparisons.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: Union[AnthropicModel, str] = _DEFAULT_MODEL,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        base_url: str | None = None,
        prompt_caching: bool = True,
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
        self._prompt_caching = prompt_caching

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict]:
        """Stream chunks from Anthropic's Messages API.

        Translates OpenAI-style ``messages``/``tools`` to Anthropic's
        shape, then normalises the event stream back into the Patter
        chunk protocol. ``cancel_event`` (set on barge-in by the stream
        handler) is checked between events and short-circuits the stream
        immediately so a follow-up user transcript is not blocked behind
        a long-running fetch.
        """
        system_prompt, anthropic_messages = _to_anthropic_messages(messages)
        anthropic_tools = _to_anthropic_tools(tools) if tools else None

        kwargs: dict = {
            "model": self._model,
            "messages": anthropic_messages,
            "max_tokens": self._max_tokens,
        }
        if system_prompt:
            if self._prompt_caching:
                # Convert the system string into a single text block tagged
                # with ``cache_control: ephemeral``. Anthropic caches every
                # block up to and including the marked one, so a single
                # marker on the only block is sufficient.
                kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                kwargs["system"] = system_prompt
        if anthropic_tools:
            if self._prompt_caching:
                # Per Anthropic's recommended pattern, tagging only the LAST
                # tool block with ``cache_control`` caches the entire tool
                # list (everything before the marker is cached implicitly).
                anthropic_tools = list(anthropic_tools)
                anthropic_tools[-1] = {
                    **anthropic_tools[-1],
                    "cache_control": {"type": "ephemeral"},
                }
            kwargs["tools"] = anthropic_tools
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._prompt_caching:
            kwargs["extra_headers"] = {"anthropic-beta": _PROMPT_CACHING_BETA}

        # tool_call_id -> stable "index" for the Patter chunk protocol.
        tool_indices: dict[str, int] = {}
        current_tool_id: str | None = None
        current_tool_index: int | None = None

        async with self._client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if cancel_event is not None and cancel_event.is_set():
                    return  # exiting the ``async with`` closes the upstream stream
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
                "input_schema": fn.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
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
                            "content": content
                            if isinstance(content, str)
                            else json.dumps(content),
                        }
                    ],
                }
            )
            continue

    return "\n\n".join(system_parts), out
