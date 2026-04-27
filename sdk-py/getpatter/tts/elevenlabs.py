"""ElevenLabs TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.elevenlabs_tts import ElevenLabsTTS as _ElevenLabsTTS

__all__ = ["TTS"]


def _resolve_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise ValueError(
            "ElevenLabs TTS requires an api_key. Pass api_key='...' or "
            "set ELEVENLABS_API_KEY in the environment."
        )
    return key


class TTS(_ElevenLabsTTS):
    """ElevenLabs streaming TTS.

    Example::

        from getpatter.tts import elevenlabs

        tts = elevenlabs.TTS()              # reads ELEVENLABS_API_KEY
        tts = elevenlabs.TTS(api_key="...", voice_id="EXAVITQu4vr4xnSDxMaL")

    Telephony optimization
    ----------------------
    Use :meth:`for_twilio` (μ-law @ 8 kHz, native Twilio Media Streams
    format) or :meth:`for_telnyx` (PCM @ 16 kHz, native Telnyx default)
    to skip the SDK-side resampling / transcoding step on phone calls.
    """

    provider_key: ClassVar[str] = "elevenlabs"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model_id: str = "eleven_flash_v2_5",
        output_format: str = "pcm_16000",
    ) -> None:
        super().__init__(
            api_key=_resolve_api_key(api_key),
            voice_id=voice_id,
            model_id=model_id,
            output_format=output_format,
        )

    @classmethod
    def for_twilio(
        cls,
        api_key: str | None = None,
        *,
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model_id: str = "eleven_flash_v2_5",
    ) -> "TTS":
        """Pipeline TTS pre-configured for Twilio Media Streams (``ulaw_8000``).

        Falls back to ``ELEVENLABS_API_KEY`` from the env when ``api_key``
        is omitted. See :class:`getpatter.providers.elevenlabs_tts.ElevenLabsTTS.for_twilio`
        for rationale.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            voice_id=voice_id,
            model_id=model_id,
            output_format="ulaw_8000",
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: str | None = None,
        *,
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model_id: str = "eleven_flash_v2_5",
    ) -> "TTS":
        """Pipeline TTS pre-configured for Telnyx (``pcm_16000``).

        Falls back to ``ELEVENLABS_API_KEY`` from the env when ``api_key``
        is omitted. See :class:`getpatter.providers.elevenlabs_tts.ElevenLabsTTS.for_telnyx`
        for the trade-off vs. ``ulaw_8000``.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            voice_id=voice_id,
            model_id=model_id,
            output_format="pcm_16000",
        )
