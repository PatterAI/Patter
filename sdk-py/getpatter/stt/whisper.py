"""OpenAI Whisper STT for Patter pipeline mode."""

from __future__ import annotations

import os

from getpatter.providers.whisper_stt import WhisperSTT as _WhisperSTT

__all__ = ["STT"]


class STT(_WhisperSTT):
    """OpenAI Whisper STT (buffered HTTP transcription).

    Example::

        from getpatter.stt import whisper

        stt = whisper.STT()                 # reads OPENAI_API_KEY
        stt = whisper.STT(api_key="sk-...", language="it")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        language: str = "en",
        model: str = "whisper-1",
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError(
                "Whisper STT requires an api_key. Pass api_key='sk-...' or "
                "set OPENAI_API_KEY in the environment."
            )
        super().__init__(api_key=key, language=language, model=model)
