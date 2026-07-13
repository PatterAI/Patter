"""xAI (Grok) streaming STT for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar, Sequence

from getpatter.providers.xai_stt import XAISTT as _XAISTT

__all__ = ["STT"]


class STT(_XAISTT):
    """xAI streaming STT (``wss://api.x.ai/v1/stt``).

    Example::

        from getpatter.stt import xai

        stt = xai.STT()                          # reads XAI_API_KEY
        stt = xai.STT(api_key="xai-...", keyterms=["Patter"])
    """

    # Stable provider key for cost attribution / metrics. Matches the
    # entry in ``pricing.py`` so handlers can resolve pricing without
    # falling back to fragile ``__name__`` stripping.
    provider_key: ClassVar[str] = "xai_stt"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        language: str | None = "en",
        encoding: str = "pcm",
        sample_rate: int = 16000,
        interim_results: bool = True,
        endpointing_ms: int | None = None,
        keyterms: Sequence[str] | None = None,
        diarize: bool = False,
        filler_words: bool = False,
        smart_turn: float | None = None,
        smart_turn_timeout_ms: int | None = None,
    ) -> None:
        key = api_key or os.environ.get("XAI_API_KEY")
        if not key:
            raise ValueError(
                "xAI STT requires an api_key. Pass api_key='xai-...' or "
                "set XAI_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            language=language,
            encoding=encoding,
            sample_rate=sample_rate,
            interim_results=interim_results,
            endpointing_ms=endpointing_ms,
            keyterms=keyterms,
            diarize=diarize,
            filler_words=filler_words,
            smart_turn=smart_turn,
            smart_turn_timeout_ms=smart_turn_timeout_ms,
        )
