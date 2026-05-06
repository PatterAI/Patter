"""Inworld TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar, Optional

from getpatter.providers.inworld_tts import InworldTTS as _InworldTTS

__all__ = ["TTS"]


class TTS(_InworldTTS):
    """Inworld HTTP NDJSON TTS — defaults to the TTS-2 model.

    Example::

        from getpatter.tts import inworld

        tts = inworld.TTS()                                    # reads INWORLD_API_KEY
        tts = inworld.TTS(api_key="...", voice="Olivia", language="en")
    """

    provider_key: ClassVar[str] = "inworld"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "inworld-tts-2",
        voice: str = "Ashley",
        language: Optional[str] = None,
        audio_encoding: str = "PCM",
        sample_rate: int = 16000,
        bitrate: int = 64000,
        temperature: Optional[float] = None,
        speaking_rate: float = 1.0,
        delivery_mode: Optional[str] = None,
    ) -> None:
        key = api_key or os.environ.get("INWORLD_API_KEY")
        if not key:
            raise ValueError(
                "Inworld TTS requires an api_key. Pass api_key='...' or "
                "set INWORLD_API_KEY in the environment."
            )
        super().__init__(
            auth_token=key,
            model=model,
            voice=voice,
            language=language,
            audio_encoding=audio_encoding,
            sample_rate=sample_rate,
            bitrate=bitrate,
            temperature=temperature,
            speaking_rate=speaking_rate,
            delivery_mode=delivery_mode,
        )
