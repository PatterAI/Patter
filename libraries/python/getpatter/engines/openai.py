"""OpenAI Realtime engine marker for Patter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

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
        engine = openai.Realtime(
            model="gpt-realtime-2",
            reasoning_effort="low",                     # gpt-realtime-2 only
            input_audio_transcription_model="gpt-realtime-whisper",
        )
    """

    api_key: str = ""
    voice: str = "alloy"
    model: str = "gpt-4o-mini-realtime-preview"
    # Reasoning-effort tier for ``gpt-realtime-2``. ``None`` leaves the
    # ``session.reasoning`` field unset (server default applies). OpenAI
    # recommends ``"low"`` for production voice flows — higher tiers add
    # measurable per-turn latency. No effect on models that ignore the field.
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    # Override for the Realtime session's ``input_audio_transcription.model``.
    # ``None`` keeps the adapter default (``whisper-1``). Use
    # ``"gpt-realtime-whisper"`` for low-latency partials, ``"gpt-4o-transcribe"``
    # for higher accuracy.
    input_audio_transcription_model: str | None = None

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
