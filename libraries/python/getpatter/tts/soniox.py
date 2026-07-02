"""Soniox TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar, Optional

from getpatter.providers.soniox_tts import SonioxTTS as _SonioxTTS

__all__ = ["TTS"]


def _resolve_api_key(api_key: str | None) -> str:
    # Shared credential with Soniox STT — same SONIOX_API_KEY env var.
    key = api_key or os.environ.get("SONIOX_API_KEY")
    if not key:
        raise ValueError(
            "Soniox TTS requires an api_key. Pass api_key='...' or "
            "set SONIOX_API_KEY in the environment."
        )
    return key


class TTS(_SonioxTTS):
    """Soniox HTTP TTS (``tts-rt-v1``, default voice ``Adrian``).

    Shares the ``SONIOX_API_KEY`` credential with the Soniox STT adapter.

    Example::

        from getpatter.tts import soniox

        tts = soniox.TTS()                          # reads SONIOX_API_KEY
        tts = soniox.TTS(api_key="...", voice="Maya", language="en")

    Telephony optimization
    ----------------------
    Use :meth:`for_twilio` or :meth:`for_telnyx` on phone calls. Both emit
    mu-law @ 8 kHz natively (Soniox supports G.711 directly), so the pipeline
    skips resampling and PCM -> mu-law encoding entirely (bit-clean
    passthrough).
    """

    provider_key: ClassVar[str] = "soniox_tts"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "tts-rt-v1",
        voice: str = "Adrian",
        language: str = "en",
        audio_format: str = "pcm_s16le",
        sample_rate: int = 16000,
        bitrate: Optional[int] = None,
        speed: Optional[float] = None,
    ) -> None:
        super().__init__(
            api_key=_resolve_api_key(api_key),
            model=model,
            voice=voice,
            language=language,
            audio_format=audio_format,
            sample_rate=sample_rate,
            bitrate=bitrate,
            speed=speed,
        )

    @classmethod
    def for_twilio(
        cls,
        api_key: str | None = None,
        *,
        model: str = "tts-rt-v1",
        voice: str = "Adrian",
        language: str = "en",
        speed: Optional[float] = None,
    ) -> "TTS":
        """Pipeline TTS pre-configured for Twilio Media Streams.

        Emits mu-law @ 8 kHz natively — Twilio's wire codec — so the pipeline
        passes the bytes straight through (no resample, no PCM -> mu-law
        encode). Falls back to ``SONIOX_API_KEY`` from the env when ``api_key``
        is omitted.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            model=model,
            voice=voice,
            language=language,
            audio_format="pcm_mulaw",
            sample_rate=8000,
            speed=speed,
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: str | None = None,
        *,
        model: str = "tts-rt-v1",
        voice: str = "Adrian",
        language: str = "en",
        speed: Optional[float] = None,
    ) -> "TTS":
        """Pipeline TTS pre-configured for Telnyx bidirectional media.

        Emits mu-law @ 8 kHz natively — the SDK pins the Telnyx wire to
        PCMU/mu-law @ 8 kHz — so audio flows end-to-end with zero resampling or
        transcoding. Falls back to ``SONIOX_API_KEY`` from the env when
        ``api_key`` is omitted.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            model=model,
            voice=voice,
            language=language,
            audio_format="pcm_mulaw",
            sample_rate=8000,
            speed=speed,
        )
