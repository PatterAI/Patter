"""Rime TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import Optional

from patter.providers.rime_tts import RimeTTS as _RimeTTS

__all__ = ["TTS"]


class TTS(_RimeTTS):
    """Rime HTTP TTS (Arcana / Mist models).

    Example::

        from patter.tts import rime

        tts = rime.TTS()                    # reads RIME_API_KEY
        tts = rime.TTS(api_key="...", speaker="astra")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "arcana",
        speaker: Optional[str] = None,
        lang: str = "eng",
        sample_rate: int = 16000,
    ) -> None:
        key = api_key or os.environ.get("RIME_API_KEY")
        if not key:
            raise ValueError(
                "Rime TTS requires an api_key. Pass api_key='...' or "
                "set RIME_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            model=model,
            speaker=speaker,
            lang=lang,
            sample_rate=sample_rate,
        )
