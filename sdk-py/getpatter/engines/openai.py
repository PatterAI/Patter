"""OpenAI Realtime engine marker for Patter."""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["Realtime"]


@dataclass(frozen=True)
class Realtime:
    """OpenAI Realtime API engine config.

    Holds the minimal settings needed by the Patter server to instantiate
    :class:`getpatter.providers.openai_realtime.OpenAIRealtimeAdapter` at call time.

    Example::

        from getpatter.engines import openai

        engine = openai.Realtime()                     # reads OPENAI_API_KEY
        engine = openai.Realtime(voice="nova", model="gpt-4o-mini-realtime-preview")
    """

    api_key: str = ""
    voice: str = "alloy"
    model: str = "gpt-4o-mini-realtime-preview"

    def __post_init__(self) -> None:
        key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise ValueError(
                "OpenAI Realtime engine requires an api_key. Pass "
                "api_key='sk-...' or set OPENAI_API_KEY in the environment."
            )
        object.__setattr__(self, "api_key", key)

    @property
    def kind(self) -> str:
        """Stable discriminator used for Phase 2 dispatch."""
        return "openai_realtime"
