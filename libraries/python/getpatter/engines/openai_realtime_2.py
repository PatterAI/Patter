"""OpenAI Realtime 2 engine marker for Patter.

Wraps ``gpt-realtime-2`` (GA Realtime API). Separate marker from
:class:`getpatter.engines.openai.Realtime` because the GA endpoint speaks a
different ``session.update`` wire shape; the client dispatches to
:class:`getpatter.providers.openai_realtime_2.OpenAIRealtime2Adapter` when
this marker is passed to ``Patter.agent(engine=...)``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    pass

__all__ = ["Realtime2"]


@dataclass(frozen=True)
class Realtime2:
    """OpenAI GA Realtime API engine config — selects ``gpt-realtime-2``.

    Holds the minimal settings needed by the Patter server to instantiate
    :class:`getpatter.providers.openai_realtime_2.OpenAIRealtime2Adapter` at
    call time.

    Example::

        from getpatter import Patter, Twilio, OpenAIRealtime2

        phone = Patter(carrier=Twilio(), phone_number="+1...")
        agent = phone.agent(
            engine=OpenAIRealtime2(reasoning_effort="low"),
            system_prompt="You are a friendly receptionist.",
            first_message="Hello! How can I help?",
        )
    """

    api_key: str = ""
    voice: str = "alloy"
    model: str = "gpt-realtime-2"
    # Reasoning-effort tier. ``None`` leaves the field unset (server default
    # applies). OpenAI recommends ``"low"`` for production voice flows —
    # higher tiers add measurable per-turn latency. Has no effect on models
    # that don't support the ``reasoning`` field.
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    # Override for ``audio.input.transcription.model``. ``None`` keeps the
    # adapter default (``whisper-1``). Use ``"gpt-realtime-whisper"`` for
    # low-latency transcript partials.
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
                "OpenAI Realtime 2 engine requires an api_key. Pass "
                "api_key='sk-...' or set OPENAI_API_KEY in the environment."
            )
        object.__setattr__(self, "api_key", key)

    @property
    def kind(self) -> str:
        """Stable discriminator used for engine dispatch."""
        return "openai_realtime_2"
