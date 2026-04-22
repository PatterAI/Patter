"""Cerebras LLM for Patter pipeline mode."""

from __future__ import annotations

import os

from getpatter.providers.cerebras_llm import CerebrasLLMProvider as _CerebrasLLM

__all__ = ["LLM"]


class LLM(_CerebrasLLM):
    """Cerebras LLM provider (OpenAI-compatible Inference API).

    Example::

        from getpatter.llm import cerebras

        llm = cerebras.LLM()                          # reads CEREBRAS_API_KEY
        llm = cerebras.LLM(api_key="csk-...", model="llama3.1-8b")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "llama3.1-8b",
        **kwargs,
    ) -> None:
        key = api_key or os.environ.get("CEREBRAS_API_KEY")
        if not key:
            raise ValueError(
                "Cerebras LLM requires an api_key. Pass api_key='csk-...' or "
                "set CEREBRAS_API_KEY in the environment."
            )
        super().__init__(api_key=key, model=model, **kwargs)
