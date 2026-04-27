"""Cartesia TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar, Optional

from getpatter.providers.cartesia_tts import CartesiaTTS as _CartesiaTTS

__all__ = ["TTS"]


class TTS(_CartesiaTTS):
    """Cartesia HTTP TTS (``sonic-3`` GA, ~90 ms TTFB).

    The default model is ``sonic-3`` — Cartesia's current GA model. Voice IDs
    from the previous ``sonic-2`` family (including the default Katie voice)
    remain compatible.

    Example::

        from getpatter.tts import cartesia

        tts = cartesia.TTS()                # reads CARTESIA_API_KEY
        tts = cartesia.TTS(api_key="...", voice="f786b574-...")
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
        key = api_key or os.environ.get("CARTESIA_API_KEY")
        if not key:
            raise ValueError(
                "Cartesia TTS requires an api_key. Pass api_key='...' or "
                "set CARTESIA_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            model=model,
            voice=voice,
            language=language,
            sample_rate=sample_rate,
            speed=speed,
        )
