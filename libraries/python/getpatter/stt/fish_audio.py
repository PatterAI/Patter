"""Fish Audio batch STT for Patter pipeline mode (Beta)."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.fish_audio_stt import (
    BUFFER_SIZE_BYTES,
    FishAudioSTT as _FishAudioSTT,
)

__all__ = ["STT"]


class STT(_FishAudioSTT):
    """Fish Audio ASR — buffered batch transcription (Beta).

    Example::

        from getpatter.stt import fish_audio

        stt = fish_audio.STT()                       # reads FISH_AUDIO_API_KEY
        stt = fish_audio.STT(api_key="...", language="it")

    Fish has no streaming transcription socket, so this adapter uploads ~2 s
    windows and emits one final transcript per window — no interim partials.
    Prefer Deepgram / Soniox / AssemblyAI when turn latency matters most.
    """

    provider_key: ClassVar[str] = "fish_audio_stt"

    def __init__(
        self,
        api_key: str | None = None,
        language: str | None = "en",
        *,
        ignore_timestamps: bool = True,
        buffer_size_bytes: int = BUFFER_SIZE_BYTES,
    ) -> None:
        key = api_key or os.environ.get("FISH_AUDIO_API_KEY")
        if not key:
            raise ValueError(
                "Fish Audio STT requires an api_key. Pass api_key='...' or "
                "set FISH_AUDIO_API_KEY in the environment."
            )
        super().__init__(
            key,
            language,
            ignore_timestamps=ignore_timestamps,
            buffer_size_bytes=buffer_size_bytes,
        )
