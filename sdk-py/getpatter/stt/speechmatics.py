"""Speechmatics streaming STT for Patter pipeline mode."""

from __future__ import annotations

import os

from getpatter.providers.speechmatics_stt import (
    SpeechmaticsSTT as _SpeechmaticsSTT,
    TurnDetectionMode,
)

__all__ = ["STT", "TurnDetectionMode"]


class STT(_SpeechmaticsSTT):
    """Speechmatics real-time streaming STT.

    Example::

        from getpatter.stt import speechmatics

        stt = speechmatics.STT()            # reads SPEECHMATICS_API_KEY
        stt = speechmatics.STT(api_key="...", language="en")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        language: str = "en",
        turn_detection_mode: TurnDetectionMode = TurnDetectionMode.ADAPTIVE,
        sample_rate: int = 16000,
        enable_diarization: bool = False,
        include_partials: bool = True,
    ) -> None:
        key = api_key or os.environ.get("SPEECHMATICS_API_KEY")
        if not key:
            raise ValueError(
                "Speechmatics STT requires an api_key. Pass api_key='...' or "
                "set SPEECHMATICS_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            language=language,
            turn_detection_mode=turn_detection_mode,
            sample_rate=sample_rate,
            enable_diarization=enable_diarization,
            include_partials=include_partials,
        )
