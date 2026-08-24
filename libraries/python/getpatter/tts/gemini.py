"""Gemini TTS for Patter pipeline mode (Beta)."""

from __future__ import annotations

from typing import ClassVar

from getpatter.providers.gemini_tts import (
    GEMINI_TTS_DEFAULT_MODEL,
    GEMINI_TTS_DEFAULT_TARGET_SAMPLE_RATE,
    GEMINI_TTS_DEFAULT_VOICE,
    GeminiTTS as _GeminiTTS,
)

__all__ = ["TTS"]


class TTS(_GeminiTTS):
    """Gemini TTS (``gemini-3.1-flash-tts-preview``) (Beta).

    Example::

        from getpatter.tts import gemini

        tts = gemini.TTS()                        # reads GEMINI_API_KEY
        tts = gemini.TTS(api_key="...", voice="Kore")

    Inline delivery tags in the text — ``[warm]``, ``[short pause]``,
    ``[sigh]`` — shape prosody instead of being spoken.
    """

    provider_key: ClassVar[str] = "gemini_tts"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice: str = GEMINI_TTS_DEFAULT_VOICE,
        model: str = GEMINI_TTS_DEFAULT_MODEL,
        target_sample_rate: int = GEMINI_TTS_DEFAULT_TARGET_SAMPLE_RATE,
    ) -> None:
        super().__init__(
            api_key=api_key,
            voice=voice,
            model=model,
            target_sample_rate=target_sample_rate,
        )
