"""LMNT TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar, Optional

from getpatter.providers.lmnt_tts import LMNTTTS as _LMNTTTS

__all__ = ["TTS"]


class TTS(_LMNTTTS):
    """LMNT HTTP TTS.

    Example::

        from getpatter.tts import lmnt

        tts = lmnt.TTS()                    # reads LMNT_API_KEY
        tts = lmnt.TTS(api_key="...", voice="leah")
    """

    provider_key: ClassVar[str] = "lmnt"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "blizzard",
        voice: str = "leah",
        language: Optional[str] = None,
        format: str = "raw",
        sample_rate: int = 16000,
    ) -> None:
        key = api_key or os.environ.get("LMNT_API_KEY")
        if not key:
            raise ValueError(
                "LMNT TTS requires an api_key. Pass api_key='...' or "
                "set LMNT_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            model=model,
            voice=voice,
            language=language,
            format=format,
            sample_rate=sample_rate,
        )
