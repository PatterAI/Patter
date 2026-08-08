"""Provider config helpers and adapters."""

from getpatter.models import STTConfig, TTSConfig


def deepgram(
    api_key: str,
    language: str = "en",
    *,
    model: str = "nova-3",  # accepts DeepgramModel or any string for forward-compat
    endpointing_ms: int = 150,
    utterance_end_ms: int | None = 1000,
    # Default False — matches the DeepgramSTT class default (punctuation /
    # numeral formatting adds latency and the text goes into an LLM anyway).
    # The helper previously defaulted True, so the two entry points behaved
    # differently for the same nominal config.
    smart_format: bool = False,
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


def inworld(
    api_key: str, voice: str = "Ashley", *, model: str = "inworld-tts-2"
) -> TTSConfig:
    """Config helper for Inworld TTS (requires the ``inworld`` optional extra).

    ``api_key`` is the Base64 ``Authorization: Basic`` token from the Inworld
    dashboard. ``model`` defaults to ``inworld-tts-2`` (100+ languages,
    steering); pass ``inworld-tts-1.5-mini`` for the lowest-latency model or
    ``inworld-tts-1.5-max`` for the prior flagship.
    """
    return TTSConfig(
        provider="inworld", api_key=api_key, voice=voice, options={"model": model}
    )


def fish_audio(
    api_key: str,
    voice: str = "",
    *,
    model: str = "s2.1-pro",
    latency: str = "balanced",
) -> TTSConfig:
    """Config helper for Fish Audio TTS (requires the ``fish_audio`` extra).

    ``voice`` is a Fish ``reference_id`` — a voice-model id from the Fish voice
    library or one you cloned. Leave it empty to use the model's built-in
    voice. ``model`` defaults to ``s2.1-pro`` (the recommended production
    model); pass ``s2-pro`` for the ~100 ms time-to-first-audio generation or
    ``s2.1-pro-free`` for the free tier (no latency guarantee).
    """
    return TTSConfig(
        provider="fish_audio",
        api_key=api_key,
        voice=voice,
        options={"model": model, "latency": latency},
    )


def fish_audio_asr(
    api_key: str,
    language: str = "en",
    *,
    ignore_timestamps: bool = True,
) -> STTConfig:
    """Config helper for Fish Audio ASR (requires the ``fish_audio`` extra).

    Fish transcription is a batch endpoint, so this adapter uploads short
    windows instead of streaming — see ``FishAudioSTT`` for the latency
    trade-off against Deepgram / Soniox / AssemblyAI.

    Named ``_asr`` (Fish's own term for the endpoint) rather than ``_stt`` on
    purpose: a helper called ``fish_audio_stt`` would be shadowed by the
    ``providers.fish_audio_stt`` submodule as soon as the factory imports it —
    the same trap that ``soniox_tts`` needs an explicit re-bind to survive.
    """
    return STTConfig(
        provider="fish_audio",
        api_key=api_key,
        language=language,
        options={"ignore_timestamps": ignore_timestamps},
    )


def soniox_tts(
    api_key: str,
    voice: str = "Adrian",
    *,
    model: str = "tts-rt-v1",
    language: str = "en",
) -> TTSConfig:
    """Config helper for Soniox TTS (requires the ``soniox`` optional extra).

    Shares the ``SONIOX_API_KEY`` credential family with Soniox STT. ``voice``
    defaults to ``Adrian`` (a single voice keeps its timbre across 60+
    languages). For telephony the carrier-native path is ``SonioxTTS.for_twilio``
    / ``for_telnyx`` (mu-law @ 8 kHz, no resampling).
    """
    return TTSConfig(
        provider="soniox_tts",
        api_key=api_key,
        voice=voice,
        options={"model": model, "language": language},
    )


def sarvam(
    api_key: str,
    voice: str = "shubh",
    *,
    model: str = "bulbul:v3",
    language: str = "en-IN",
) -> TTSConfig:
    """Config helper for Sarvam AI TTS (requires the ``sarvam`` optional extra).

    Synthesizes 11 Indian languages (Hindi, Bengali, Tamil, Telugu, Kannada,
    Malayalam, Marathi, Gujarati, Punjabi, Odia, Indian English) plus code-mixed
    text. ``voice`` is the Sarvam speaker id (default ``shubh``); ``language`` is
    a BCP-47 code such as ``"hi-IN"``. ``model`` defaults to ``bulbul:v3`` (pass
    ``bulbul:v2`` for the legacy generation).
    """
    return TTSConfig(
        provider="sarvam",
        api_key=api_key,
        voice=voice,
        options={"model": model, "language": language},
    )


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


def _load_litellm():
    from getpatter.providers.litellm_llm import LiteLLMProvider

    return LiteLLMProvider


def __getattr__(name: str):
    """Lazy-load optional LLM providers to avoid importing heavy vendor SDKs."""
    loaders = {
        "AnthropicLLMProvider": _load_anthropic_llm,
        "GroqLLMProvider": _load_groq_llm,
        "CerebrasLLMProvider": _load_cerebras_llm,
        "GoogleLLMProvider": _load_google_llm,
        "LiteLLMProvider": _load_litellm,
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
    "inworld",
    "fish_audio",
    "fish_audio_asr",
    "soniox_tts",
    "sarvam",
    "AnthropicLLMProvider",
    "GroqLLMProvider",
    "CerebrasLLMProvider",
    "GoogleLLMProvider",
    "LiteLLMProvider",
]

# ``soniox_tts`` (the helper above) shares its name with the ``soniox_tts``
# submodule. Importing that submodule — which ``_create_tts_from_config`` does
# lazily — binds the MODULE object onto this package, shadowing the helper and
# making ``providers.soniox_tts(...)`` uncallable. Force the submodule import
# now (so the binding happens once) and then restore the helper. Subsequent
# ``from getpatter.providers.soniox_tts import ...`` calls find the module
# already cached in ``sys.modules`` and do NOT re-bind the attribute, so the
# helper stays callable. ``sarvam`` needs no such guard (its submodule is
# ``sarvam_tts``, a different name).
import importlib as _importlib  # noqa: E402

_soniox_tts_helper = soniox_tts
try:  # pragma: no cover - import side-effect guard
    _importlib.import_module(__name__ + ".soniox_tts")
except Exception:  # pragma: no cover - defensive (optional dep may be absent)
    pass
soniox_tts = _soniox_tts_helper
del _importlib, _soniox_tts_helper
