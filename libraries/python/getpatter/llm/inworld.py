"""Inworld Realtime Router LLM for Patter's pipeline mode.

Inworld Router exposes an OpenAI-compatible ``/chat/completions`` endpoint at
``https://api.inworld.ai/v1`` that routes to 220+ models from OpenAI,
Anthropic, Google, Mistral, DeepSeek, Meta and more behind a single key — see
https://inworld.ai/models and https://docs.inworld.ai/router/openai-compatibility.

This is a thin preset over :class:`getpatter.llm.openai_compatible.LLM`: it
defaults the base URL and reads ``INWORLD_API_KEY``. The model string is the
Inworld target — either a configured router (``"inworld/<router-id>"``) or a
request-level ``"<provider>/<model>"`` slug from the models catalogue. Auth is
the Base64 Inworld key sent as a Bearer token (the form the OpenAI SDK uses).
"""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.llm.openai_compatible import OpenAICompatibleLLMProvider as _OpenAICompatLLM

__all__ = ["LLM"]

#: OpenAI-compatible Router endpoint (base URL only — change vs api.openai.com).
INWORLD_ROUTER_BASE_URL = "https://api.inworld.ai/v1"


class LLM(_OpenAICompatLLM):
    """Inworld Realtime Router LLM (OpenAI-compatible) for pipeline mode.

    Example::

        from getpatter.llm import inworld

        # reads INWORLD_API_KEY; pick any model from the Router catalogue:
        llm = inworld.LLM(model="openai/gpt-4o-mini")
        llm = inworld.LLM(model="anthropic/claude-haiku-4-5-20251001")
        # or a router you configured in the Inworld portal:
        llm = inworld.LLM(api_key="...", model="inworld/my-router")
    """

    #: Distinct from the TTS ``inworld`` key so token-based LLM cost is never
    #: confused with the char-based TTS pricing entry.
    provider_key: ClassVar[str] = "inworld_router"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str,
        base_url: str = INWORLD_ROUTER_BASE_URL,
        **kwargs,
    ) -> None:
        key = api_key or os.environ.get("INWORLD_API_KEY")
        if not key:
            raise ValueError(
                "Inworld Router LLM requires an api_key. Pass api_key='...' or "
                "set INWORLD_API_KEY in the environment."
            )
        super().__init__(api_key=key, base_url=base_url, model=model, **kwargs)
