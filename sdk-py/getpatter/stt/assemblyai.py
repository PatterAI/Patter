"""AssemblyAI streaming STT for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar

from getpatter.providers.assemblyai_stt import AssemblyAISTT as _AssemblyAISTT

__all__ = ["STT"]


class STT(_AssemblyAISTT):
    """AssemblyAI Universal Streaming STT.

    Example::

        from getpatter.stt import assemblyai

        stt = assemblyai.STT()              # reads ASSEMBLYAI_API_KEY
        stt = assemblyai.STT(api_key="...", model="universal-streaming-multilingual")
    """

    provider_key: ClassVar[str] = "assemblyai"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        language: str = "en",
        model: str = "universal-streaming-english",
        encoding: str = "pcm_s16le",
        sample_rate: int = 16000,
    ) -> None:
        key = api_key or os.environ.get("ASSEMBLYAI_API_KEY")
        if not key:
            raise ValueError(
                "AssemblyAI STT requires an api_key. Pass api_key='...' or "
                "set ASSEMBLYAI_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            language=language,
            model=model,
            encoding=encoding,
            sample_rate=sample_rate,
        )
