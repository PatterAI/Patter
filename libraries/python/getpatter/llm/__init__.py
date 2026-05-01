"""Namespaced LLM adapters for Patter pipeline mode.

Each submodule exposes a thin ``LLM`` subclass of the corresponding provider
adapter with:

* An environment-variable fallback on ``api_key``.
* A clear error message when credentials are missing.

Usage::

    from getpatter.llm import anthropic
    llm = anthropic.LLM()  # reads ANTHROPIC_API_KEY
"""

from __future__ import annotations

__all__ = [
    "openai",
    "anthropic",
    "groq",
    "cerebras",
    "google",
]
