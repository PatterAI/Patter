"""Provider config helpers and adapters."""

from getpatter.models import STTConfig, TTSConfig


def deepgram(
    api_key: str,
    language: str = "en",
    *,
    model: str = "nova-3",  # accepts DeepgramModel or any string for forward-compat
    endpointing_ms: int = 150,
    utterance_end_ms: int | None = 1000,
    smart_format: bool = True,
    interim_results: bool = True,
    vad_events: bool | None = None,
) -> STTConfig:
    """Deepgram STT config. Tune latency via ``endpointing_ms`` / ``utterance_end_ms``."""
    options: dict = {
        "model": model,
        "endpointing_ms": endpointing_ms,
        "utterance_end_ms": utterance_end_ms,
        "smart_format": smart_format,
        "interim_results": interim_results,
    }
    if vad_events is not None:
        options["vad_events"] = vad_events
    return STTConfig(
        provider="deepgram", api_key=api_key, language=language, options=options
    )


def whisper(api_key: str, language: str = "en") -> STTConfig:
    """Config helper for OpenAI Whisper STT."""
    return STTConfig(provider="whisper", api_key=api_key, language=language)


def soniox(api_key: str, language: str = "en") -> STTConfig:
    """Soniox real-time STT config (requires the ``soniox`` optional extra)."""
    return STTConfig(provider="soniox", api_key=api_key, language=language)


def speechmatics(api_key: str, language: str = "en") -> STTConfig:
    """Speechmatics real-time STT config (requires the ``speechmatics`` optional extra)."""
    return STTConfig(provider="speechmatics", api_key=api_key, language=language)


def elevenlabs(api_key: str, voice: str = "rachel") -> TTSConfig:
    """Config helper for ElevenLabs TTS."""
    return TTSConfig(provider="elevenlabs", api_key=api_key, voice=voice)


def openai_tts(api_key: str, voice: str = "alloy") -> TTSConfig:
    """Config helper for OpenAI TTS."""
    return TTSConfig(provider="openai", api_key=api_key, voice=voice)


def cartesia(
    api_key: str, voice: str = "f786b574-daa5-4673-aa0c-cbe3e8534c02"
) -> TTSConfig:
    """Config helper for Cartesia TTS."""
    return TTSConfig(provider="cartesia", api_key=api_key, voice=voice)


def rime(api_key: str, voice: str = "astra") -> TTSConfig:
    """Config helper for Rime TTS."""
    return TTSConfig(provider="rime", api_key=api_key, voice=voice)


def lmnt(api_key: str, voice: str = "leah") -> TTSConfig:
    """Config helper for LMNT TTS."""
    return TTSConfig(provider="lmnt", api_key=api_key, voice=voice)


def _load_anthropic_llm():
    from getpatter.providers.anthropic_llm import AnthropicLLMProvider

    return AnthropicLLMProvider


def _load_groq_llm():
    from getpatter.providers.groq_llm import GroqLLMProvider

    return GroqLLMProvider


def _load_cerebras_llm():
    from getpatter.providers.cerebras_llm import CerebrasLLMProvider

    return CerebrasLLMProvider


def _load_google_llm():
    from getpatter.providers.google_llm import GoogleLLMProvider

    return GoogleLLMProvider


def __getattr__(name: str):
    """Lazy-load optional LLM providers to avoid importing heavy vendor SDKs."""
    loaders = {
        "AnthropicLLMProvider": _load_anthropic_llm,
        "GroqLLMProvider": _load_groq_llm,
        "CerebrasLLMProvider": _load_cerebras_llm,
        "GoogleLLMProvider": _load_google_llm,
    }
    if name in loaders:
        return loaders[name]()
    raise AttributeError(f"module 'getpatter.providers' has no attribute {name!r}")


# Prevent submodule names from shadowing the helper functions above.
# Python's package import mechanism can bind submodule objects (e.g.
# getpatter.providers.openai_tts) onto this package's namespace, which would
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
    "AnthropicLLMProvider",
    "GroqLLMProvider",
    "CerebrasLLMProvider",
    "GoogleLLMProvider",
]
