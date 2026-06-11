"""OpenAI TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.openai_tts import OpenAITTS as _OpenAITTS

__all__ = ["TTS"]


class TTS(_OpenAITTS):
    """OpenAI streaming TTS.

    Example::

        from getpatter.tts import openai

        tts = openai.TTS()                  # reads OPENAI_API_KEY
        tts = openai.TTS(api_key="sk-...", voice="nova", model="tts-1-hd")
    """

    provider_key: ClassVar[str] = "openai_tts"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice: str = "alloy",
        # gpt-4o-mini-tts: matches the underlying provider default AND the
        # TS facade — the same nominal config previously produced different
        # voice quality/latency/price per SDK (tts-1 here, 4o-mini there).
        model: str = "gpt-4o-mini-tts",
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError(
                "OpenAI TTS requires an api_key. Pass api_key='sk-...' or "
                "set OPENAI_API_KEY in the environment."
            )
        super().__init__(api_key=key, voice=voice, model=model)
