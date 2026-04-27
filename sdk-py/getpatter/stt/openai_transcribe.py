"""OpenAI GPT-4o Transcribe STT for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.openai_transcribe_stt import (
    OpenAITranscribeSTT as _OpenAITranscribeSTT,
)

__all__ = ["STT"]


class STT(_OpenAITranscribeSTT):
    """OpenAI GPT-4o Transcribe STT (buffered HTTP transcription).

    ~10x faster than Whisper-1 — drop-in replacement for ``whisper.STT``.

    Example::

        from getpatter.stt import openai_transcribe

        stt = openai_transcribe.STT()                 # reads OPENAI_API_KEY
        stt = openai_transcribe.STT(api_key="sk-...", language="it")
    """

    provider_key: ClassVar[str] = "openai_transcribe"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        language: str = "en",
        model: str = "gpt-4o-transcribe",
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError(
                "OpenAI Transcribe STT requires an api_key. Pass api_key='sk-...' or "
                "set OPENAI_API_KEY in the environment."
            )
        super().__init__(api_key=key, language=language, model=model)
