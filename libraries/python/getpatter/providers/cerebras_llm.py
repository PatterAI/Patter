"""Cerebras LLM provider for Patter's pipeline mode.

Cerebras exposes an OpenAI-compatible Chat Completions API at
``https://api.cerebras.ai/v1``, so this provider is a thin wrapper
around :class:`getpatter.services.llm_loop.OpenAILLMProvider` with a
Cerebras-specific base URL.  Payload compression (msgpack + gzip) is
supported and enabled by default to reduce TTFT for large prompts —
see https://inference-docs.cerebras.ai/payload-optimization.

All OpenAI-spec sampling kwargs accepted by the parent
(``response_format``, ``parallel_tool_calls``, ``tool_choice``, ``seed``,
``top_p``, ``frequency_penalty``, ``presence_penalty``, ``stop``,
``temperature``, ``max_tokens``) are inherited and forwarded to
``chat.completions.create`` automatically — see
:class:`OpenAILLMProvider` for the full list.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
from enum import StrEnum
from typing import Any, AsyncIterator

from getpatter.services.llm_loop import OpenAILLMProvider

__all__ = ["CerebrasLLMProvider", "CerebrasModel"]

logger = logging.getLogger("getpatter.providers.cerebras_llm")


class CerebrasModel(StrEnum):
    """Known Cerebras Inference API models. Account tier gates availability."""

    GPT_OSS_120B = "gpt-oss-120b"
    LLAMA_3_1_8B = "llama3.1-8b"
    LLAMA_3_3_70B = "llama-3.3-70b"
    QWEN_3_235B_INSTRUCT = "qwen-3-235b-a22b-instruct-2507"
    ZAI_GLM_4_7 = "zai-glm-4.7"


_CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
# Default to ``gpt-oss-120b`` — the highest-throughput production model on
# Cerebras's WSE-3 hardware (~3000 tok/sec, well above TTS consumption rate)
# and not on a deprecation schedule. On the WSE-3 chip the model size is
# bottlenecked by TTS consumption (~150-300 tok/sec) regardless of weights,
# so a 120B model and an 8B model both saturate the downstream TTS pipeline
# — picking the larger one buys higher answer quality at no realtime cost.
#
# ``llama3.1-8b`` (deprecating 2026-05-27) and the preview models
# ``qwen-3-235b-a22b-instruct-2507`` and ``zai-glm-4.7`` are reachable via
# ``model="..."``. If your account tier returns 404 for ``gpt-oss-120b``
# the provider's stream() logs a recovery hint listing override candidates.
_DEFAULT_MODEL = CerebrasModel.GPT_OSS_120B.value


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

        Overrides ``_build_request`` to serialise ``json_data`` directly into
        the target binary format and sets the appropriate ``Content-Type``
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
    format as :class:`OpenAILLMProvider`. All OpenAI-spec sampling kwargs
    accepted by the parent are forwarded transparently.

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
        model: Cerebras chat model ID. Defaults to ``gpt-oss-120b`` (highest
            throughput on Cerebras WSE-3 at ~3000 tok/sec, no deprecation).
            Override with ``llama3.1-8b`` for a smaller/free-tier model
            (deprecating 2026-05-27), ``qwen-3-235b-a22b-instruct-2507`` for
            a preview multilingual model, or query ``GET /v1/models`` to
            discover tier-available IDs.
        base_url: Optional Cerebras base URL override.
        gzip_compression: Gzip request payloads for faster TTFT.
        msgpack_encoding: Encode request payloads with msgpack for smaller
            wire size.  Requires ``msgpack>=1.0``.
        **kwargs: Sampling kwargs forwarded to :class:`OpenAILLMProvider`
            (``response_format``, ``parallel_tool_calls``, ``tool_choice``,
            ``seed``, ``top_p``, ``frequency_penalty``, ``presence_penalty``,
            ``stop``, ``temperature``, ``max_tokens``, ``user_agent``).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: Union[CerebrasModel, str] = _DEFAULT_MODEL,
        base_url: str = _CEREBRAS_BASE_URL,
        gzip_compression: bool = True,
        msgpack_encoding: bool = True,
        **kwargs,
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

        # Initialise parent state (model, sampling kwargs, _user_agent).
        # The parent constructs an OpenAI-pointed AsyncOpenAI client which we
        # immediately replace below with a Cerebras-pointed (and optionally
        # compressing) client.
        super().__init__(api_key=resolved_key, model=model, **kwargs)

        ua_headers = {"User-Agent": self._user_agent}

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

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict]:
        """Stream from Cerebras, delegating SSE consumption to the parent.

        404 ``model_not_found`` on Cerebras almost always means the model
        name isn't available on the caller's tier (Cerebras gates models per
        plan). The error is logged with a recovery hint at ERROR level and
        the generator returns silently — voice pipelines treat LLM provider
        failures as recoverable (the call continues; the user just hears no
        LLM response), so raising would be a behavioural change.
        """
        try:
            async for chunk in super().stream(
                messages, tools, cancel_event=cancel_event
            ):
                yield chunk
        except Exception as exc:
            text = str(exc)
            if "404" in text and "model_not_found" in text:
                logger.error(
                    'Cerebras: model "%s" not available on your tier. Override '
                    "via `CerebrasLLM(model='<id>')` and list tier-available "
                    "ids with `GET %s/models` (common: llama3.1-8b, "
                    "qwen-3-235b-a22b-instruct-2507, llama-3.3-70b on paid). "
                    "Upstream: %s",
                    self._model,
                    _CEREBRAS_BASE_URL,
                    text,
                )
                return
            raise
