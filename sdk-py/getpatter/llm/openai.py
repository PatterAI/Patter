"""OpenAI LLM for Patter pipeline mode."""

from __future__ import annotations

import os

from getpatter.services.llm_loop import OpenAILLMProvider as _OpenAILLM

__all__ = ["LLM"]


class LLM(_OpenAILLM):
    """OpenAI Chat Completions LLM provider.

    Example::

        from getpatter.llm import openai

        llm = openai.LLM()                            # reads OPENAI_API_KEY
        llm = openai.LLM(api_key="sk-...", model="gpt-4o-mini")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "gpt-4o-mini",
        **kwargs,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError(
                "OpenAI LLM requires an api_key. Pass api_key='sk-...' or "
                "set OPENAI_API_KEY in the environment."
            )
        super().__init__(api_key=key, model=model, **kwargs)
