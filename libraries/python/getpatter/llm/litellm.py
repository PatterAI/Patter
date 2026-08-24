"""LiteLLM for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.litellm_llm import LiteLLMProvider as _LiteLLM

__all__ = ["LLM"]


class LLM(_LiteLLM):
    """LiteLLM provider (unified gateway to 100+ LLM providers).

    Routes to any LiteLLM-supported model (OpenAI, Anthropic, Google,
    Azure, Bedrock, Ollama, and more) through a single interface.

    Example::

        from getpatter.llm import litellm

        llm = litellm.LLM(model="gpt-4o")
        llm = litellm.LLM(model="anthropic/claude-sonnet-5", api_key="sk-ant-...")
        llm = litellm.LLM(model="ollama/llama3")
    """

    provider_key: ClassVar[str] = "litellm"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "gpt-4o-mini",
        **kwargs,
    ) -> None:
        key = api_key or os.environ.get("LITELLM_API_KEY")
        super().__init__(api_key=key, model=model, **kwargs)
