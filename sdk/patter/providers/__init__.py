"""Provider config helpers and adapters."""

from patter.models import STTConfig, TTSConfig


def deepgram(api_key: str, language: str = "en") -> STTConfig:
    return STTConfig(provider="deepgram", api_key=api_key, language=language)


def whisper(api_key: str, language: str = "en") -> STTConfig:
    return STTConfig(provider="whisper", api_key=api_key, language=language)


def soniox(api_key: str, language: str = "en") -> STTConfig:
    """Soniox real-time STT config (requires the ``soniox`` optional extra)."""
    return STTConfig(provider="soniox", api_key=api_key, language=language)


def speechmatics(api_key: str, language: str = "en") -> STTConfig:
    """Speechmatics real-time STT config (requires the ``speechmatics`` optional extra)."""
    return STTConfig(provider="speechmatics", api_key=api_key, language=language)


def elevenlabs(api_key: str, voice: str = "rachel") -> TTSConfig:
    return TTSConfig(provider="elevenlabs", api_key=api_key, voice=voice)


def openai_tts(api_key: str, voice: str = "alloy") -> TTSConfig:
    return TTSConfig(provider="openai", api_key=api_key, voice=voice)


def cartesia(api_key: str, voice: str = "f786b574-daa5-4673-aa0c-cbe3e8534c02") -> TTSConfig:
    """Config helper for Cartesia TTS."""
    return TTSConfig(provider="cartesia", api_key=api_key, voice=voice)


def rime(api_key: str, voice: str = "astra") -> TTSConfig:
    """Config helper for Rime TTS."""
    return TTSConfig(provider="rime", api_key=api_key, voice=voice)


def lmnt(api_key: str, voice: str = "leah") -> TTSConfig:
    """Config helper for LMNT TTS."""
    return TTSConfig(provider="lmnt", api_key=api_key, voice=voice)


# Prevent submodule names from shadowing the helper functions above.
# Python's package import mechanism can bind submodule objects (e.g.
# patter.providers.openai_tts) onto this package's namespace, which would
# shadow the function of the same name. We re-bind them explicitly here.
__all__ = [
    "deepgram",
    "whisper",
    "soniox",
    "speechmatics",
    "elevenlabs",
    "openai_tts",
    "cartesia",
    "rime",
    "lmnt",
]
