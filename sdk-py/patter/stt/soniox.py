"""Soniox streaming STT for Patter pipeline mode."""

from __future__ import annotations

import os

from patter.providers.soniox_stt import SonioxSTT as _SonioxSTT

__all__ = ["STT"]


class STT(_SonioxSTT):
    """Soniox real-time streaming STT.

    Example::

        from patter.stt import soniox

        stt = soniox.STT()                  # reads SONIOX_API_KEY
        stt = soniox.STT(api_key="...", language_hints=["en", "it"])
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "stt-rt-v4",
        language_hints: list[str] | None = None,
        language_hints_strict: bool = False,
        sample_rate: int = 16000,
        enable_speaker_diarization: bool = False,
        enable_language_identification: bool = True,
        max_endpoint_delay_ms: int = 500,
    ) -> None:
        key = api_key or os.environ.get("SONIOX_API_KEY")
        if not key:
            raise ValueError(
                "Soniox STT requires an api_key. Pass api_key='...' or "
                "set SONIOX_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            model=model,
            language_hints=language_hints,
            language_hints_strict=language_hints_strict,
            sample_rate=sample_rate,
            enable_speaker_diarization=enable_speaker_diarization,
            enable_language_identification=enable_language_identification,
            max_endpoint_delay_ms=max_endpoint_delay_ms,
        )
