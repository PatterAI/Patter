"""Shared utility functions for telephony handlers."""

from __future__ import annotations

import re


def _validate_e164(number: str) -> bool:
    """Return True if *number* is a valid E.164 phone number."""
    return bool(re.match(r'^\+[1-9]\d{6,14}$', number))


def _sanitize_variable_value(value: str) -> str:
    """Strip control characters and limit length to prevent prompt injection."""
    return re.sub(r'[\x00-\x1f\x7f]', '', str(value))[:500]


def _resolve_variables(template: str, variables: dict) -> str:
    """Replace ``{key}`` placeholders in *template* with values from *variables*.

    Args:
        template: A string that may contain ``{key}`` placeholders.
        variables: Mapping of placeholder names to replacement values.

    Returns:
        A new string with all matching placeholders substituted.
    """
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def _create_stt_from_config(config, for_twilio: bool = False):
    """Create an STT adapter from an STTConfig object.

    Args:
        config: An ``STTConfig`` instance (or ``None``).
        for_twilio: When ``True``, configure for Twilio's mulaw 8 kHz stream.
    """
    if config is None:
        return None
    provider = config.provider
    if provider == "deepgram":
        from patter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]

        if for_twilio:
            return DeepgramSTT.for_twilio(api_key=config.api_key, language=config.language)
        return DeepgramSTT(api_key=config.api_key, language=config.language)
    elif provider == "whisper":
        from patter.providers.whisper_stt import WhisperSTT  # type: ignore[import]

        return WhisperSTT(api_key=config.api_key, language=config.language)
    return None


def _create_tts_from_config(config):
    """Create a TTS adapter from a TTSConfig object.

    Args:
        config: A ``TTSConfig`` instance (or ``None``).
    """
    if config is None:
        return None
    provider = config.provider
    if provider == "elevenlabs":
        from patter.providers.elevenlabs_tts import ElevenLabsTTS  # type: ignore[import]

        return ElevenLabsTTS(api_key=config.api_key, voice_id=config.voice)
    elif provider == "openai":
        from patter.providers.openai_tts import OpenAITTS  # type: ignore[import]

        return OpenAITTS(api_key=config.api_key, voice=config.voice)
    return None
