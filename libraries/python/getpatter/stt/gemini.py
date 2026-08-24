"""Gemini multimodal STT for Patter pipeline mode (Beta)."""

from __future__ import annotations

from typing import ClassVar

from getpatter.providers.gemini_stt import (
    GEMINI_STT_DEFAULT_MODEL,
    GEMINI_STT_DEFAULT_SAMPLE_RATE,
    GeminiSTT as _GeminiSTT,
)

__all__ = ["STT"]


class STT(_GeminiSTT):
    """Gemini multimodal STT (``gemini-2.5-flash``) (Beta).

    Transcribes AND tags the caller's vocal tone (``[tone: ...]``) so the agent
    can mirror mood.

    Example::

        from getpatter.stt import gemini

        stt = gemini.STT()                      # reads GEMINI_API_KEY
        stt = gemini.STT(api_key="...", model="gemini-2.5-flash")

    One request per turn, fired at VAD speech-end: there are no interim
    partials. Prefer Deepgram / Soniox / AssemblyAI when turn latency matters
    more than tone.
    """

    provider_key: ClassVar[str] = "gemini_stt"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = GEMINI_STT_DEFAULT_MODEL,
        sample_rate: int = GEMINI_STT_DEFAULT_SAMPLE_RATE,
    ) -> None:
        super().__init__(api_key=api_key, model=model, sample_rate=sample_rate)
