"""Deepgram streaming STT for Patter pipeline mode."""

from __future__ import annotations

import os

from patter.providers.deepgram_stt import DeepgramSTT as _DeepgramSTT

__all__ = ["STT"]


class STT(_DeepgramSTT):
    """Deepgram streaming STT.

    Example::

        from patter.stt import deepgram

        stt = deepgram.STT()                # reads DEEPGRAM_API_KEY
        stt = deepgram.STT(api_key="dg_...", endpointing_ms=80)
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        language: str = "en",
        model: str = "nova-3",
        encoding: str = "linear16",
        sample_rate: int = 16000,
        endpointing_ms: int = 150,
        utterance_end_ms: int | None = 1000,
        smart_format: bool = True,
        interim_results: bool = True,
        vad_events: bool = True,
    ) -> None:
        key = api_key or os.environ.get("DEEPGRAM_API_KEY")
        if not key:
            raise ValueError(
                "Deepgram STT requires an api_key. Pass api_key='dg_...' or "
                "set DEEPGRAM_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            language=language,
            model=model,
            encoding=encoding,
            sample_rate=sample_rate,
            endpointing_ms=endpointing_ms,
            utterance_end_ms=utterance_end_ms,
            smart_format=smart_format,
            interim_results=interim_results,
            vad_events=vad_events,
        )
