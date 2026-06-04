"""Gemini Live engine marker for Patter."""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["GeminiLive"]


@dataclass(frozen=True)
class GeminiLive:
    """Google Gemini Live native-audio engine config.

    Holds the minimal settings needed by the Patter server to instantiate
    :class:`getpatter.providers.gemini_live.GeminiLiveAdapter` at call time.

    Example::

        from getpatter.engines import gemini_live

        engine = gemini_live.GeminiLive()                     # reads GEMINI_API_KEY
        engine = gemini_live.GeminiLive(voice="Puck", model="gemini-2.5-flash-native-audio-preview-09-2025")
    """

    api_key: str = ""
    voice: str = "Puck"
    model: str = "gemini-2.5-flash-native-audio-preview-09-2025"
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    response_modalities: tuple[str, ...] = ("AUDIO",)

    def __post_init__(self) -> None:
        key = self.api_key or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise ValueError(
                "Gemini Live engine requires an api_key. Pass "
                "api_key='...' or set GEMINI_API_KEY in the environment."
            )
        # Normalize list inputs to tuple for frozen-dataclass hashability.
        if isinstance(self.response_modalities, list):
            object.__setattr__(
                self, "response_modalities", tuple(self.response_modalities)
            )
        object.__setattr__(self, "api_key", key)

    @property
    def kind(self) -> str:
        """Stable discriminator used for Phase 2 dispatch."""
        return "gemini_live"
