"""Cartesia TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar, Optional

from getpatter.providers.cartesia_tts import CartesiaTTS as _CartesiaTTS

__all__ = ["TTS"]


def _resolve_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("CARTESIA_API_KEY")
    if not key:
        raise ValueError(
            "Cartesia TTS requires an api_key. Pass api_key='...' or "
            "set CARTESIA_API_KEY in the environment."
        )
    return key


class TTS(_CartesiaTTS):
    """Cartesia HTTP TTS (``sonic-3`` GA, ~90 ms TTFB).

    The default model is ``sonic-3`` — Cartesia's current GA model. Voice IDs
    from the previous ``sonic-2`` family (including the default Katie voice)
    remain compatible.

    Example::

        from getpatter.tts import cartesia

        tts = cartesia.TTS()                # reads CARTESIA_API_KEY
        tts = cartesia.TTS(api_key="...", voice="f786b574-...")

    Telephony optimization
    ----------------------
    Use :meth:`for_twilio` (PCM @ 8 kHz, skipping the SDK-side
    16 kHz → 8 kHz resample before μ-law transcoding) or
    :meth:`for_telnyx` (PCM @ 16 kHz, native Telnyx default) on phone
    calls.
    """

    provider_key: ClassVar[str] = "cartesia_tts"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "sonic-3",
        voice: str = "f786b574-daa5-4673-aa0c-cbe3e8534c02",
        language: str = "en",
        sample_rate: int = 16000,
        speed: Optional[str | float] = None,
    ) -> None:
        super().__init__(
            api_key=_resolve_api_key(api_key),
            model=model,
            voice=voice,
            language=language,
            sample_rate=sample_rate,
            speed=speed,
        )

    @classmethod
    def for_twilio(
        cls,
        api_key: str | None = None,
        *,
        model: str = "sonic-3",
        voice: str = "f786b574-daa5-4673-aa0c-cbe3e8534c02",
        language: str = "en",
        speed: Optional[str | float] = None,
    ) -> "TTS":
        """Pipeline TTS pre-configured for Twilio Media Streams (PCM @ 8 kHz).

        Falls back to ``CARTESIA_API_KEY`` from the env when ``api_key``
        is omitted. See
        :class:`getpatter.providers.cartesia_tts.CartesiaTTS.for_twilio`
        for rationale.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            model=model,
            voice=voice,
            language=language,
            sample_rate=8000,
            speed=speed,
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: str | None = None,
        *,
        model: str = "sonic-3",
        voice: str = "f786b574-daa5-4673-aa0c-cbe3e8534c02",
        language: str = "en",
        speed: Optional[str | float] = None,
    ) -> "TTS":
        """Pipeline TTS pre-configured for Telnyx (PCM @ 16 kHz).

        Falls back to ``CARTESIA_API_KEY`` from the env when ``api_key``
        is omitted. See
        :class:`getpatter.providers.cartesia_tts.CartesiaTTS.for_telnyx`
        for the trade-off.
        """
        return cls(
            api_key=_resolve_api_key(api_key),
            model=model,
            voice=voice,
            language=language,
            sample_rate=16000,
            speed=speed,
        )
