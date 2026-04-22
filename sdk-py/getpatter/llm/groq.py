"""Groq LLM for Patter pipeline mode."""

from __future__ import annotations

import os

from getpatter.providers.groq_llm import GroqLLMProvider as _GroqLLM

__all__ = ["LLM"]


class LLM(_GroqLLM):
    """Groq LLM provider (OpenAI-compatible Chat Completions).

    Example::

        from getpatter.llm import groq

        llm = groq.LLM()                              # reads GROQ_API_KEY
        llm = groq.LLM(api_key="gsk_...", model="llama-3.3-70b-versatile")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "llama-3.3-70b-versatile",
        **kwargs,
    ) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise ValueError(
                "Groq LLM requires an api_key. Pass api_key='gsk_...' or "
                "set GROQ_API_KEY in the environment."
            )
        super().__init__(api_key=key, model=model, **kwargs)
