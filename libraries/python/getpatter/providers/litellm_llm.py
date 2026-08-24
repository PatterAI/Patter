"""LiteLLM LLM provider for Patter's pipeline mode.

LiteLLM is an AI gateway that provides a unified interface to 100+ LLM
providers (OpenAI, Anthropic, Google, Azure, AWS Bedrock, Ollama, Cohere,
Mistral, and more) through a single ``litellm.acompletion()`` call.

LiteLLM's streaming response is OpenAI-compatible, so the chunk parsing
is identical to :class:`getpatter.services.llm_loop.OpenAILLMProvider`.
Unlike the Groq / Cerebras providers (which subclass ``OpenAILLMProvider``
and swap the ``AsyncOpenAI`` client), this provider calls the ``litellm``
SDK directly so it does NOT depend on ``openai`` at runtime.

``drop_params=True`` is enabled by default: LiteLLM silently drops
per-provider-unsupported kwargs (``seed``, ``response_format``, etc.)
instead of 400-ing, which is the safe default for a voice AI SDK where
you want things to work across providers without per-model config.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, ClassVar

logger = logging.getLogger("getpatter.providers.litellm_llm")

__all__ = ["LiteLLMProvider"]


class LiteLLMProvider:
    """LLM provider backed by LiteLLM's unified completion API (streaming).

    Implements Patter's :class:`LLMProvider` protocol: ``stream`` yields
    ``{"type": "text" | "tool_call" | "done", ...}`` chunks.

    Args:
        api_key: API key for the underlying provider. If omitted,
            ``LITELLM_API_KEY`` is read from the environment (or the
            provider-specific env var is used by LiteLLM automatically).
        model: LiteLLM model identifier (e.g. ``"gpt-4o"``,
            ``"anthropic/claude-sonnet-5"``, ``"ollama/llama3"``).
        api_base: Optional base URL override (e.g. for a self-hosted
            LiteLLM proxy or Azure endpoint).
        drop_params: Silently drop provider-unsupported kwargs instead
            of erroring. Defaults to ``True`` for maximum cross-provider
            compatibility.
        temperature: Optional sampling temperature.
        max_tokens: Max tokens in the assistant response.
        response_format: Optional OpenAI-style ``response_format`` dict
            for JSON mode / structured outputs.
        parallel_tool_calls: Whether to allow parallel tool calls.
        tool_choice: ``"auto" | "none" | "required"`` or a specific
            tool dict.
        seed: Sampling seed for reproducible outputs.
        top_p: Nucleus sampling cutoff in [0, 1].
        frequency_penalty: Penalty in [-2, 2] applied to repeated tokens.
        presence_penalty: Penalty in [-2, 2] applied to seen tokens.
        stop: Stop sequence(s).
    """

    provider_key: ClassVar[str] = "litellm"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        *,
        api_base: str | None = None,
        drop_params: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        parallel_tool_calls: bool | None = None,
        tool_choice: str | dict | None = None,
        seed: int | None = None,
        top_p: float | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        stop: str | list[str] | None = None,
    ) -> None:
        try:
            import litellm  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "The 'litellm' package is required for LiteLLMProvider. "
                "Install it with: pip install 'getpatter[litellm]'"
            ) from e

        self._api_key = api_key or os.environ.get("LITELLM_API_KEY")
        self._model = model
        self._api_base = api_base
        self._drop_params = drop_params
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._response_format = response_format
        self._parallel_tool_calls = parallel_tool_calls
        self._tool_choice = tool_choice
        self._seed = seed
        self._top_p = top_p
        self._frequency_penalty = frequency_penalty
        self._presence_penalty = presence_penalty
        self._stop = stop

    async def warmup(self) -> None:
        """Best-effort pre-call DNS / TLS warmup.

        Issues a lightweight HTTPS GET so the connection pool is warm by
        the time the first ``acompletion`` call lands. Best-effort: 5 s
        timeout, all exceptions swallowed at DEBUG.
        """
        base = self._api_base
        if not base:
            return
        try:
            import httpx

            url = base.rstrip("/")
            if not url.endswith("/models"):
                url = f"{url}/models"
            headers: dict[str, str] = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            async with httpx.AsyncClient(timeout=5.0) as http:
                await http.get(url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("LiteLLM warmup failed (best-effort): %s", exc)

    def _build_completion_kwargs(
        self,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> dict[str, Any]:
        """Assemble kwargs for ``litellm.acompletion``."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key
        if self._api_base is not None:
            kwargs["api_base"] = self._api_base
        if self._drop_params:
            kwargs["drop_params"] = True
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
        return kwargs

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        cancel_event: asyncio.Event | None = None,
        call_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream chunks from LiteLLM's unified completion API.

        ``call_id`` is accepted for protocol parity with session-aware
        providers but ignored.

        ``cancel_event`` (set on barge-in by the stream handler) is checked
        between chunks and short-circuits the stream so the next user
        transcript is not blocked behind a long-running fetch.
        """
        import litellm

        kwargs = self._build_completion_kwargs(messages, tools)
        response = await litellm.acompletion(**kwargs)

        last_usage = None
        try:
            async for chunk in response:
                if cancel_event is not None and cancel_event.is_set():
                    return

                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    last_usage = usage

                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                content = getattr(delta, "content", None)
                if content:
                    yield {"type": "text", "content": content}

                tool_calls = getattr(delta, "tool_calls", None)
                if tool_calls:
                    for tc in tool_calls:
                        yield {
                            "type": "tool_call",
                            "index": tc.index,
                            "id": tc.id,
                            "name": tc.function.name if tc.function else None,
                            "arguments": (
                                tc.function.arguments if tc.function else None
                            ),
                        }

            if last_usage is not None:
                prompt_tokens = getattr(last_usage, "prompt_tokens", 0) or 0
                completion_tokens = (
                    getattr(last_usage, "completion_tokens", 0) or 0
                )
                cache_read = 0
                details = getattr(last_usage, "prompt_tokens_details", None)
                if details is not None:
                    cache_read = getattr(details, "cached_tokens", 0) or 0
                uncached_input = max(0, prompt_tokens - cache_read)
                self._record_completion_cost(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
                yield {
                    "type": "usage",
                    "input_tokens": uncached_input,
                    "output_tokens": completion_tokens,
                    "cache_read_tokens": cache_read,
                }
        except asyncio.CancelledError:
            raise
        except Exception:
            if cancel_event is not None and cancel_event.is_set():
                return
            raise

        yield {"type": "done"}

    def _record_completion_cost(
        self, *, prompt_tokens: int, completion_tokens: int
    ) -> None:
        """Stamp ``patter.cost.llm_*_tokens`` on the current span."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.llm_input_tokens": prompt_tokens,
                    "patter.cost.llm_output_tokens": completion_tokens,
                    "patter.llm.provider": "litellm",
                }
            )
        except Exception:  # pragma: no cover - defense in depth
            logger.debug("_record_completion_cost failed", exc_info=True)
