"""Anthropic Claude LLM for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.anthropic_llm import (
    CLAUDE_HAIKU_45_ALIAS,
    CLAUDE_OPUS_47_ALIAS,
    CLAUDE_SONNET_46_ALIAS,
    AnthropicLLMProvider as _AnthropicLLM,
)

__all__ = [
    "LLM",
    "CLAUDE_HAIKU_45_ALIAS",
    "CLAUDE_SONNET_46_ALIAS",
    "CLAUDE_OPUS_47_ALIAS",
]


class LLM(_AnthropicLLM):
    """Anthropic Claude LLM provider (Messages API, streaming).

    Prompt caching is **enabled by default** (``prompt_caching=True``).
    For voice agents with long instruction-dense system prompts, this
    saves ~100-400 ms TTFT and ~90% input-token cost on cached turns.
    Disable with ``prompt_caching=False`` if your system prompt + tools
    are below Anthropic's minimum cacheable size (~1024 tokens for
    Sonnet/Opus, ~2048 for Haiku) — caching has no effect below that
    threshold.

    Example::

        from getpatter.llm import anthropic

        llm = anthropic.LLM()                         # reads ANTHROPIC_API_KEY
        llm = anthropic.LLM(api_key="sk-ant-...", model="claude-haiku-4-5-20251001")
        llm = anthropic.LLM(prompt_caching=False)     # opt out of caching
    """

    provider_key: ClassVar[str] = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "claude-haiku-4-5-20251001",
        prompt_caching: bool = True,
        **kwargs,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError(
                "Anthropic LLM requires an api_key. Pass api_key='sk-ant-...' or "
                "set ANTHROPIC_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            model=model,
            prompt_caching=prompt_caching,
            **kwargs,
        )
