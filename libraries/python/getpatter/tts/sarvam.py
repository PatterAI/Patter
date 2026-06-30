"""Sarvam AI TTS for Patter pipeline mode."""

from __future__ import annotations

import os
from typing import ClassVar, Optional, Union

from getpatter.providers.sarvam_tts import (
    SarvamAudioCodec,
    SarvamLanguage,
    SarvamModel,
    SarvamSampleRate,
    SarvamTTS as _SarvamTTS,
)

__all__ = ["TTS"]


class TTS(_SarvamTTS):
    """Sarvam Bulbul TTS for Indian languages — defaults to the bulbul:v3 model.

    Synthesizes Hindi, Bengali, Tamil, Telugu, Kannada, Malayalam, Marathi,
    Gujarati, Punjabi, Odia and Indian English (plus code-mixed text). Select
    the language via ``language`` (a Sarvam BCP-47 code such as ``"hi-IN"`` —
    see :class:`~getpatter.providers.sarvam_tts.SarvamLanguage`).

    Example::

        from getpatter.tts import sarvam

        tts = sarvam.TTS()                                  # reads SARVAM_API_KEY
        tts = sarvam.TTS(api_key="...", language="hi-IN", speaker="shubh")

        # Telephony (mu-law @ 8 kHz, carrier-native passthrough):
        tts = sarvam.TTS.for_twilio(language="ta-IN")
    """

    provider_key: ClassVar[str] = "sarvam"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Union[SarvamModel, str] = SarvamModel.BULBUL_V3,
        speaker: str = "shubh",
        language: Union[SarvamLanguage, str] = SarvamLanguage.ENGLISH,
        codec: Union[SarvamAudioCodec, str] = SarvamAudioCodec.LINEAR16,
        sample_rate: Union[SarvamSampleRate, int] = SarvamSampleRate.HZ_16000,
        pace: Optional[float] = None,
        pitch: Optional[float] = None,
        loudness: Optional[float] = None,
        temperature: Optional[float] = None,
        enable_preprocessing: Optional[bool] = None,
        dict_id: Optional[str] = None,
    ) -> None:
        key = api_key or os.environ.get("SARVAM_API_KEY")
        if not key:
            raise ValueError(
                "Sarvam TTS requires an api_key. Pass api_key='...' or "
                "set SARVAM_API_KEY in the environment."
            )
        super().__init__(
            api_key=key,
            model=model,
            speaker=speaker,
            language=language,
            codec=codec,
            sample_rate=sample_rate,
            pace=pace,
            pitch=pitch,
            loudness=loudness,
            temperature=temperature,
            enable_preprocessing=enable_preprocessing,
            dict_id=dict_id,
        )
