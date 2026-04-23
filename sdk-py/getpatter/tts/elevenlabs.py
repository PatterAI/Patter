"""ElevenLabs TTS for Patter pipeline mode."""

from __future__ import annotations

import os

from getpatter.providers.elevenlabs_tts import ElevenLabsTTS as _ElevenLabsTTS

__all__ = ["TTS"]


class TTS(_ElevenLabsTTS):
    """ElevenLabs streaming TTS.

    Example::

        from getpatter.tts import elevenlabs

        tts = elevenlabs.TTS()              # reads ELEVENLABS_API_KEY
        tts = elevenlabs.TTS(api_key="...", voice_id="EXAVITQu4vr4xnSDxMaL")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model_id: str = "eleven_flash_v2_5",
        output_format: str = "pcm_16000",
    ) -> None:
        key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        if not key:
            raise ValueError(
                "ElevenLabs TTS requires an api_key. Pass api_key='...' or "
                "set ELEVENLABS_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            voice_id=voice_id,
            model_id=model_id,
            output_format=output_format,
        )
