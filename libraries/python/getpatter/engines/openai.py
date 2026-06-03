"""OpenAI Realtime engine marker for Patter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    pass

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
    model: str = "gpt-realtime-mini"
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
    # Input noise reduction for speakerphone / conference audio. ``None``
    # (default) omits the field (no reduction). ``"far_field"`` recommended
    # for phone calls. Mirrors ``Patter.agent(openai_realtime_noise_reduction=)``.
    noise_reduction: Literal["near_field", "far_field"] | None = None
    # Turn-detection tuning (raise the VAD threshold to reject speakerphone
    # noise, or switch to ``semantic_vad`` eagerness='low'). ``None`` (default)
    # keeps the adapter's current turn_detection.
    turn_detection: "RealtimeTurnDetection | None" = None

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
