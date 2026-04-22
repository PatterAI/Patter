"""Anthropic Claude LLM for Patter pipeline mode."""

from __future__ import annotations

import os

from getpatter.providers.anthropic_llm import AnthropicLLMProvider as _AnthropicLLM

__all__ = ["LLM"]


class LLM(_AnthropicLLM):
    """Anthropic Claude LLM provider (Messages API, streaming).

    Example::

        from getpatter.llm import anthropic

        llm = anthropic.LLM()                         # reads ANTHROPIC_API_KEY
        llm = anthropic.LLM(api_key="sk-ant-...", model="claude-3-5-sonnet-20241022")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "claude-3-5-sonnet-20241022",
        **kwargs,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError(
                "Anthropic LLM requires an api_key. Pass api_key='sk-ant-...' or "
                "set ANTHROPIC_API_KEY in the environment."
            )
        super().__init__(api_key=key, model=model, **kwargs)
