"""Groq LLM provider for Patter's pipeline mode.

Groq's Chat Completions API is OpenAI-compatible, so this provider is a
thin wrapper around :class:`getpatter.services.llm_loop.OpenAILLMProvider`
that points at ``https://api.groq.com/openai/v1``. All sampling kwargs
(``response_format``, ``parallel_tool_calls``, ``tool_choice``, ``seed``,
``top_p``, ``frequency_penalty``, ``presence_penalty``, ``stop``,
``temperature``, ``max_tokens``) are inherited from the parent and
forwarded to ``chat.completions.create`` automatically.
"""

from __future__ import annotations

import os

from getpatter.services.llm_loop import OpenAILLMProvider

__all__ = ["GroqLLMProvider"]


_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.3-70b-versatile"


class GroqLLMProvider(OpenAILLMProvider):
    """LLM provider backed by Groq's OpenAI-compatible Chat Completions API.

    Streams in the same ``{"type": "text" | "tool_call" | "done"}`` chunk
    format as :class:`OpenAILLMProvider`. All OpenAI-spec sampling kwargs
    accepted by the parent (``response_format``, ``parallel_tool_calls``,
    ``tool_choice``, ``seed``, ``top_p``, ``frequency_penalty``,
    ``presence_penalty``, ``stop``, ``temperature``, ``max_tokens``) are
    forwarded transparently — see :class:`OpenAILLMProvider` for details.

    Args:
        api_key: Groq API key. If omitted, ``GROQ_API_KEY`` is read from
            the environment.
        model: Groq chat model ID. Defaults to ``llama-3.3-70b-versatile``.
        base_url: Optional Groq base URL override.
        **kwargs: Sampling kwargs forwarded to
            :class:`OpenAILLMProvider`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        base_url: str = _GROQ_BASE_URL,
        **kwargs,
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

        # Initialise parent state (model, sampling kwargs, _user_agent)
        # without using its OpenAI-pointed client. We swap in a Groq-pointed
        # client below using the same User-Agent the parent computed.
        super().__init__(api_key=resolved_key, model=model, **kwargs)
        self._client = AsyncOpenAI(
            api_key=resolved_key,
            base_url=base_url,
            default_headers={"User-Agent": self._user_agent},
        )
