"""xAI streaming STT for Patter pipeline mode (Beta)."""

from __future__ import annotations

import os
from typing import ClassVar, Sequence

from getpatter.providers.xai_stt import XaiSTT as _XaiSTT

__all__ = ["STT"]


class STT(_XaiSTT):
    """xAI real-time streaming STT (Beta).

    Example::

        from getpatter.stt import xai

        stt = xai.STT()                       # reads XAI_API_KEY
        stt = xai.STT(api_key="...", language="en", smart_turn=0.7)
    """

    provider_key: ClassVar[str] = "xai"

    def __init__(
        self,
        api_key: str | None = None,
        language: str | None = None,
        *,
        encoding: str = "pcm",
        sample_rate: int = 16000,
        interim_results: bool = True,
        endpointing_ms: int | None = None,
        smart_turn: float | None = None,
        smart_turn_timeout_ms: int | None = None,
        keyterms: Sequence[str] = (),
        diarize: bool = False,
        filler_words: bool = False,
    ) -> None:
        key = api_key or os.environ.get("XAI_API_KEY")
        if not key:
            raise ValueError(
                "xAI STT requires an api_key. Pass api_key='...' or "
                "set XAI_API_KEY in the environment."
            )
        super().__init__(
            key,
            language,
            encoding=encoding,
            sample_rate=sample_rate,
            interim_results=interim_results,
            endpointing_ms=endpointing_ms,
            smart_turn=smart_turn,
            smart_turn_timeout_ms=smart_turn_timeout_ms,
            keyterms=keyterms,
            diarize=diarize,
            filler_words=filler_words,
        )
