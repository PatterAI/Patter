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
# ``llama3.1-8b`` was retired by Cerebras; default to the current
# production-grade model. Override via ``model=`` if you need a different one.
_DEFAULT_MODEL = "llama-3.3-70b"


def _build_cerebras_client(
    api_key: str,
    base_url: str,
    use_msgpack: bool,
    use_gzip: bool,
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

    return _CerebrasClient(api_key=api_key, base_url=base_url)


class CerebrasLLMProvider(OpenAILLMProvider):
    """LLM provider backed by Cerebras's OpenAI-compatible Inference API.

    Streams in the same ``{"type": "text" | "tool_call" | "done"}`` chunk
    format as :class:`OpenAILLMProvider`.

    Args:
        api_key: Cerebras API key. Reads ``CEREBRAS_API_KEY`` if omitted.
        model: Cerebras chat model ID. Defaults to ``llama-3.3-70b``.
        base_url: Optional Cerebras base URL override.
        gzip_compression: Gzip request payloads for faster TTFT.
        msgpack_encoding: Encode request payloads with msgpack for smaller
            wire size.  Requires ``msgpack>=1.0``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        base_url: str = _CEREBRAS_BASE_URL,
        gzip_compression: bool = True,
        msgpack_encoding: bool = True,
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

        if gzip_compression or msgpack_encoding:
            self._client: Any = _build_cerebras_client(
                api_key=resolved_key,
                base_url=base_url,
                use_msgpack=msgpack_encoding,
                use_gzip=gzip_compression,
            )
        else:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=resolved_key, base_url=base_url)

        self._model = model

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        async for chunk in super().stream(messages, tools):
            yield chunk
