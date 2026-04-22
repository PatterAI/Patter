"""Cartesia streaming STT for Patter pipeline mode."""

from __future__ import annotations

import os

from patter.providers.cartesia_stt import CartesiaSTT as _CartesiaSTT

__all__ = ["STT"]


class STT(_CartesiaSTT):
    """Cartesia ``ink-whisper`` streaming STT.

    Example::

        from patter.stt import cartesia

        stt = cartesia.STT()                # reads CARTESIA_API_KEY
        stt = cartesia.STT(api_key="...", language="es")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        language: str = "en",
        model: str = "ink-whisper",
        sample_rate: int = 16000,
    ) -> None:
        key = api_key or os.environ.get("CARTESIA_API_KEY")
        if not key:
            raise ValueError(
                "Cartesia STT requires an api_key. Pass api_key='...' or "
                "set CARTESIA_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            language=language,
            model=model,
            sample_rate=sample_rate,
        )
