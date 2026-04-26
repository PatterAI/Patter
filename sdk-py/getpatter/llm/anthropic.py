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

    Example::

        from getpatter.llm import anthropic

        llm = anthropic.LLM()                         # reads ANTHROPIC_API_KEY
        llm = anthropic.LLM(api_key="sk-ant-...", model="claude-haiku-4-5-20251001")
    """

    provider_key: ClassVar[str] = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "claude-haiku-4-5-20251001",
        **kwargs,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError(
                "Anthropic LLM requires an api_key. Pass api_key='sk-ant-...' or "
                "set ANTHROPIC_API_KEY in the environment."
            )
        super().__init__(api_key=key, model=model, **kwargs)
