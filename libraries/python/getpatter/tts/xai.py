"""xAI TTS for Patter pipeline mode (Beta)."""

from __future__ import annotations

import os
from typing import ClassVar, Optional

from getpatter.providers.xai_tts import XaiTTS as _XaiTTS

__all__ = ["TTS"]


def _resolve_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("XAI_API_KEY")
    if not key:
        raise ValueError(
            "xAI TTS requires an api_key. Pass api_key='...' or "
            "set XAI_API_KEY in the environment."
        )
    return key


class TTS(_XaiTTS):
    """xAI HTTP TTS (default voice ``eve``, ``language="auto"``) (Beta).

    Example::

        from getpatter.tts import xai

        tts = xai.TTS()                          # reads XAI_API_KEY
        tts = xai.TTS(api_key="...", voice="ara", language="en")

    Telephony optimization
    ----------------------
    Use :meth:`for_twilio` or :meth:`for_telnyx` on phone calls. ``for_twilio``
    emits mu-law @ 8 kHz natively (xAI supports G.711 directly), so the pipeline
    skips resampling and PCM -> mu-law encoding entirely (bit-clean passthrough).
    """

    provider_key: ClassVar[str] = "xai_tts"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        voice: str = "eve",
        language: str = "auto",
        speed: Optional[float] = None,
        optimize_streaming_latency: Optional[int] = None,
        text_normalization: bool = False,
        codec: Optional[str] = None,
        sample_rate: Optional[int] = None,
        bit_rate: Optional[int] = None,
    ) -> None:
        super().__init__(
            api_key=_resolve_api_key(api_key),
            voice=voice,
            language=language,
            speed=speed,
            optimize_streaming_latency=optimize_streaming_latency,
            text_normalization=text_normalization,
            codec=codec,
            sample_rate=sample_rate,
            bit_rate=bit_rate,
        )

    @classmethod
    def for_twilio(
        cls,
        api_key: str | None = None,
        *,
        voice: str = "eve",
        language: str = "auto",
        speed: Optional[float] = None,
    ) -> "TTS":
        """Pipeline TTS pre-configured for Twilio Media Streams.

        Emits mu-law @ 8 kHz natively — Twilio's wire codec — so the pipeline
        passes the bytes straight through (no resample, no PCM -> mu-law
        encode). Falls back to ``XAI_API_KEY`` from the env when ``api_key`` is
        omitted.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            voice=voice,
            language=language,
            speed=speed,
            codec="mulaw",
            sample_rate=8000,
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: str | None = None,
        *,
        voice: str = "eve",
        language: str = "auto",
        speed: Optional[float] = None,
    ) -> "TTS":
        """Pipeline TTS pre-configured for Telnyx bidirectional media.

        Emits ``pcm`` @ 16 kHz to match the Telnyx PCM16 pipeline. Falls back to
        ``XAI_API_KEY`` from the env when ``api_key`` is omitted.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            voice=voice,
            language=language,
            speed=speed,
            codec="pcm",
            sample_rate=16000,
        )
