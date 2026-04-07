"""Provider config helpers and adapters."""

from patter.models import STTConfig, TTSConfig


def deepgram(api_key: str, language: str = "en") -> STTConfig:
    return STTConfig(provider="deepgram", api_key=api_key, language=language)


def whisper(api_key: str, language: str = "en") -> STTConfig:
    return STTConfig(provider="whisper", api_key=api_key, language=language)


def elevenlabs(api_key: str, voice: str = "rachel") -> TTSConfig:
    return TTSConfig(provider="elevenlabs", api_key=api_key, voice=voice)


def openai_tts(api_key: str, voice: str = "alloy") -> TTSConfig:
    return TTSConfig(provider="openai", api_key=api_key, voice=voice)


# Prevent submodule names from shadowing the helper functions above.
# Python's package import mechanism can bind submodule objects (e.g.
# patter.providers.openai_tts) onto this package's namespace, which would
# shadow the function of the same name. We re-bind them explicitly here.
__all__ = ["deepgram", "whisper", "elevenlabs", "openai_tts"]
