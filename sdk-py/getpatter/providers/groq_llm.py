"""Groq LLM provider for Patter's pipeline mode.

Groq's Chat Completions API is OpenAI-compatible, so this provider is a
thin wrapper around :class:`getpatter.services.llm_loop.OpenAILLMProvider`
that points at ``https://api.groq.com/openai/v1``.

Portions adapted from LiveKit Agents
(https://github.com/livekit/agents, commit 78a66bcf79c5cea82989401c408f1dff4b961a5b,
file livekit-plugins/livekit-plugins-groq/livekit/plugins/groq/services.py),
licensed under Apache License 2.0. Copyright LiveKit, Inc.

Adaptations from the LiveKit source:
  * LiveKit's ``groq.LLM`` subclasses the LiveKit OpenAI LLM (which
    depends on the ``livekit.agents`` runtime). Patter's analogue
    subclasses :class:`OpenAILLMProvider`, which talks to the Chat
    Completions API directly via the official ``openai`` SDK.
  * Kept Groq-specific defaults (``llama-3.3-70b-versatile`` model,
    ``GROQ_API_KEY`` env var, Groq base URL).
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

from getpatter.services.llm_loop import OpenAILLMProvider

__all__ = ["GroqLLMProvider"]


_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.3-70b-versatile"


class GroqLLMProvider(OpenAILLMProvider):
    """LLM provider backed by Groq's OpenAI-compatible Chat Completions API.

    Streams in the same ``{"type": "text" | "tool_call" | "done"}`` chunk
    format as :class:`OpenAILLMProvider`.

    Args:
        api_key: Groq API key. If omitted, ``GROQ_API_KEY`` is read from
            the environment.
        model: Groq chat model ID. Defaults to ``llama-3.3-70b-versatile``.
        base_url: Optional Groq base URL override.
        response_format: Optional OpenAI-style ``response_format`` dict for
            JSON mode / structured outputs.
        parallel_tool_calls: Whether to allow parallel tool calls.
        tool_choice: ``"auto" | "none" | "required"`` or a specific tool dict.
        seed: Sampling seed for reproducible outputs.
        top_p: Nucleus sampling cutoff in [0, 1].
        frequency_penalty: Penalty in [-2, 2] applied to repeated tokens.
        presence_penalty: Penalty in [-2, 2] applied to seen tokens.
        stop: Stop sequence(s).
        temperature: Sampling temperature [0, 2].
        max_tokens: Max tokens in the assistant response. Forwarded as
            ``max_completion_tokens``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        base_url: str = _GROQ_BASE_URL,
        *,
        response_format: dict | None = None,
        parallel_tool_calls: bool | None = None,
        tool_choice: str | dict | None = None,
        seed: int | None = None,
        top_p: float | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        stop: str | list[str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise RuntimeError(
                "The 'openai' package is required for GroqLLMProvider. "
                "Install it with: pip install 'getpatter[groq]'"
            ) from e

        resolved_key = api_key or os.environ.get("GROQ_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Groq API key is required, either as the 'api_key' argument "
                "or via the GROQ_API_KEY environment variable."
            )

        # Identify the SDK in upstream logs/rate-limit attribution.
        from getpatter import __version__ as _patter_version

        # We don't call super().__init__() because it instantiates a
        # vanilla OpenAI client pointed at api.openai.com. We bind the
        # same underlying attributes with Groq's base URL.
        self._client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=base_url,
            default_headers={"User-Agent": f"getpatter/{_patter_version}"},
        )
        self._model = model
        self._response_format = response_format
        self._parallel_tool_calls = parallel_tool_calls
        self._tool_choice = tool_choice
        self._seed = seed
        self._top_p = top_p
        self._frequency_penalty = frequency_penalty
        self._presence_penalty = presence_penalty
        self._stop = stop
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Stream from Groq with extra sampling/structured-output kwargs."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
        if self._response_format is not None:
            kwargs["response_format"] = self._response_format
        if self._parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = self._parallel_tool_calls
        if self._tool_choice is not None:
            kwargs["tool_choice"] = self._tool_choice
        if self._seed is not None:
            kwargs["seed"] = self._seed
        if self._top_p is not None:
            kwargs["top_p"] = self._top_p
        if self._frequency_penalty is not None:
            kwargs["frequency_penalty"] = self._frequency_penalty
        if self._presence_penalty is not None:
            kwargs["presence_penalty"] = self._presence_penalty
        if self._stop is not None:
            kwargs["stop"] = self._stop
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._max_tokens is not None:
            kwargs["max_completion_tokens"] = self._max_tokens

        response = await self._client.chat.completions.create(**kwargs)

        last_usage = None
        async for chunk in response:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = usage

            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            if delta.content:
                yield {"type": "text", "content": delta.content}

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    yield {
                        "type": "tool_call",
                        "index": tc.index,
                        "id": tc.id,
                        "name": tc.function.name if tc.function else None,
                        "arguments": tc.function.arguments if tc.function else None,
                    }

        if last_usage is not None:
            cache_read = 0
            details = getattr(last_usage, "prompt_tokens_details", None)
            if details is not None:
                cache_read = getattr(details, "cached_tokens", 0) or 0
            prompt_tokens = getattr(last_usage, "prompt_tokens", 0) or 0
            uncached_input = max(0, prompt_tokens - cache_read)
            yield {
                "type": "usage",
                "input_tokens": uncached_input,
                "output_tokens": getattr(last_usage, "completion_tokens", 0) or 0,
                "cache_read_tokens": cache_read,
            }
