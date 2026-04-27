"""Cerebras LLM provider for Patter's pipeline mode.

Cerebras exposes an OpenAI-compatible Chat Completions API at
``https://api.cerebras.ai/v1``, so this provider is a thin wrapper
around :class:`getpatter.services.llm_loop.OpenAILLMProvider` with a
Cerebras-specific base URL.  Payload compression (msgpack + gzip) is
supported and enabled by default to reduce TTFT for large prompts —
see https://inference-docs.cerebras.ai/payload-optimization.

Portions adapted from LiveKit Agents
(https://github.com/livekit/agents, commit 78a66bcf79c5cea82989401c408f1dff4b961a5b,
file livekit-plugins/livekit-plugins-cerebras/livekit/plugins/cerebras/llm.py),
licensed under Apache License 2.0. Copyright 2026 LiveKit, Inc.

Adaptations from the LiveKit source:
  * LiveKit's ``cerebras.LLM`` subclasses the LiveKit OpenAI LLM; Patter's
    analogue subclasses :class:`OpenAILLMProvider`.
  * Ported the ``_CerebrasClient`` helper that overrides ``_build_request``
    to compress payloads with msgpack/gzip.  When either compression
    option is enabled we construct this custom client; otherwise we fall
    back to the stock ``openai.AsyncOpenAI`` client.
  * Dropped LiveKit's ``NotGivenOr`` sentinel plumbing in favour of plain
    ``Optional`` kwargs that match Patter's Python idioms.
"""

from __future__ import annotations

import gzip
import json
import os
from typing import Any, AsyncIterator

from getpatter.services.llm_loop import OpenAILLMProvider

__all__ = ["CerebrasLLMProvider"]


_CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
# Default to ``gpt-oss-120b`` — the highest-throughput production model on
# Cerebras's WSE-3 hardware (~3000 tok/sec, well above TTS consumption rate)
# and not on a deprecation schedule. Override via ``model=`` if you need a
# smaller context window (``llama3.1-8b``) or a preview model.
_DEFAULT_MODEL = "gpt-oss-120b"


def _build_cerebras_client(
    api_key: str,
    base_url: str,
    use_msgpack: bool,
    use_gzip: bool,
    default_headers: dict[str, str] | None = None,
):
    """Return an ``openai.AsyncOpenAI`` subclass that compresses requests."""
    try:
        import openai
        from openai._models import FinalRequestOptions
        from openai._utils import is_mapping
    except ImportError as e:
        raise RuntimeError(
            "The 'openai' package is required for CerebrasLLMProvider. "
            "Install it with: pip install 'getpatter[cerebras]'"
        ) from e

    if use_msgpack:
        try:
            import msgpack  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "The 'msgpack' package is required for Cerebras msgpack "
                "encoding. Install it with: pip install 'getpatter[cerebras]'"
            ) from e
    else:
        msgpack = None  # type: ignore

    class _CerebrasClient(openai.AsyncOpenAI):
        """AsyncOpenAI subclass that compresses requests via msgpack/gzip.

        Adapted from LiveKit's ``cerebras._CerebrasClient``.  Overrides
        ``_build_request`` to serialise ``json_data`` directly into the
        target binary format and sets the appropriate ``Content-Type``
        and ``Content-Encoding`` headers.
        """

        def _build_request(
            self,
            options: FinalRequestOptions,
            *,
            retries_taken: int = 0,
        ):
            if not (use_msgpack or use_gzip):
                return super()._build_request(options, retries_taken=retries_taken)

            json_data = options.json_data
            if json_data is not None:
                if options.extra_json is not None and is_mapping(json_data):
                    json_data = {**json_data, **options.extra_json}

                if use_msgpack and msgpack is not None:
                    body = msgpack.packb(json_data)
                    content_type = "application/vnd.msgpack"
                else:
                    body = json.dumps(
                        json_data, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                    content_type = "application/json"

                if use_gzip:
                    body = gzip.compress(body, compresslevel=5)

                options.json_data = None
                options.extra_json = None
                options.content = body

                existing = dict(options.headers) if options.headers else {}
                overrides: dict[str, str] = {"Content-Type": content_type}
                if use_gzip:
                    overrides["Content-Encoding"] = "gzip"
                options.headers = existing | overrides

            return super()._build_request(options, retries_taken=retries_taken)

    if default_headers:
        return _CerebrasClient(
            api_key=api_key, base_url=base_url, default_headers=default_headers
        )
    return _CerebrasClient(api_key=api_key, base_url=base_url)


class CerebrasLLMProvider(OpenAILLMProvider):
    """LLM provider backed by Cerebras's OpenAI-compatible Inference API.

    Streams in the same ``{"type": "text" | "tool_call" | "done"}`` chunk
    format as :class:`OpenAILLMProvider`.

    Available models on Cerebras (verified against
    https://inference-docs.cerebras.ai/models/overview):

      Production:
        - gpt-oss-120b                          (default — highest throughput on Cerebras, no deprecation)
        - llama3.1-8b                           (smaller context alternative; deprecating 2026-05-27)

      Preview (opt-in):
        - qwen-3-235b-a22b-instruct-2507        (multilingual, strong on European languages)
        - zai-glm-4.7

    Args:
        api_key: Cerebras API key. Reads ``CEREBRAS_API_KEY`` if omitted.
        model: Cerebras chat model ID. Defaults to ``gpt-oss-120b``.
        base_url: Optional Cerebras base URL override.
        gzip_compression: Gzip request payloads for faster TTFT.
        msgpack_encoding: Encode request payloads with msgpack for smaller
            wire size.  Requires ``msgpack>=1.0``.
        response_format: Optional OpenAI-style response_format dict, e.g.
            ``{"type": "json_schema", "json_schema": {...}}`` for structured
            outputs. See https://inference-docs.cerebras.ai/capabilities/structured-outputs.
        parallel_tool_calls: Whether to allow the model to emit multiple
            tool calls in parallel.
        tool_choice: ``"auto" | "none" | "required"`` or a specific tool
            object.
        seed: Sampling seed for reproducible outputs.
        top_p: Nucleus sampling cutoff in [0, 1].
        frequency_penalty: Penalty in [-2, 2] applied to repeated tokens.
        presence_penalty: Penalty in [-2, 2] applied to seen tokens.
        stop: Stop sequence(s) — string or list of strings.
        temperature: Sampling temperature [0, 2].
        max_tokens: Max tokens in the assistant response. Forwarded as
            ``max_completion_tokens`` (Cerebras uses the OpenAI-spec name).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        base_url: str = _CEREBRAS_BASE_URL,
        gzip_compression: bool = True,
        msgpack_encoding: bool = True,
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
            from openai import AsyncOpenAI  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "The 'openai' package is required for CerebrasLLMProvider. "
                "Install it with: pip install 'getpatter[cerebras]'"
            ) from e

        resolved_key = api_key or os.environ.get("CEREBRAS_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Cerebras API key is required, either as the 'api_key' argument "
                "or via the CEREBRAS_API_KEY environment variable."
            )

        # Identify the SDK in upstream logs/rate-limit attribution.
        from getpatter import __version__ as _patter_version

        ua_headers = {"User-Agent": f"getpatter/{_patter_version}"}

        if gzip_compression or msgpack_encoding:
            self._client: Any = _build_cerebras_client(
                api_key=resolved_key,
                base_url=base_url,
                use_msgpack=msgpack_encoding,
                use_gzip=gzip_compression,
                default_headers=ua_headers,
            )
        else:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=resolved_key,
                base_url=base_url,
                default_headers=ua_headers,
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
        """Stream from Cerebras with extra sampling/structured-output kwargs.

        Mirrors the parent OpenAILLMProvider.stream loop but forwards the
        Cerebras-specific options configured on construction. Cerebras's
        current API spec uses ``max_completion_tokens`` (the OpenAI-compat
        layer accepts both, but ``max_tokens`` is now legacy).
        """
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
            # Cerebras's current API spec uses ``max_completion_tokens``.
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
