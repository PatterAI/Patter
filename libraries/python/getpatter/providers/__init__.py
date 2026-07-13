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


def xai_stt(
    api_key: str,
    language: str = "en",
    *,
    keyterms: list[str] | None = None,
    smart_turn: float | None = None,
    smart_turn_timeout_ms: int | None = None,
) -> STTConfig:
    """xAI (Grok) streaming STT config (``wss://api.x.ai/v1/stt``).

    ``keyterms`` biases transcription toward domain vocabulary (max 100
    terms). ``smart_turn`` (0.0–1.0) enables xAI's ML end-of-turn detection —
    pair with ``smart_turn_timeout_ms`` so extended silence still finalizes.
    """
    options: dict = {}
    if keyterms is not None:
        options["keyterms"] = keyterms
    if smart_turn is not None:
        options["smart_turn"] = smart_turn
    if smart_turn_timeout_ms is not None:
        options["smart_turn_timeout_ms"] = smart_turn_timeout_ms
    return STTConfig(
        provider="xai", api_key=api_key, language=language, options=options
    )


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


def xai_tts(
    api_key: str,
    voice: str = "eve",
    *,
    language: str = "en",
) -> TTSConfig:
    """xAI (Grok) TTS config (streaming WebSocket, ``eve`` voice by default).

    ``voice`` accepts any built-in xAI voice or a custom ``voice_id`` cloned
    via the Custom Voices API. ``language`` is a BCP-47 code or ``"auto"``.
    """
    return TTSConfig(
        provider="xai", api_key=api_key, voice=voice, options={"language": language}
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
    "soniox_tts",
    "sarvam",
    "xai_stt",
    "xai_tts",
    "AnthropicLLMProvider",
    "GroqLLMProvider",
    "CerebrasLLMProvider",
    "GoogleLLMProvider",
    "LiteLLMProvider",
]

# ``soniox_tts`` / ``xai_stt`` / ``xai_tts`` (the helpers above) share their
# names with same-named submodules. Importing a submodule — which
# ``_create_stt_from_config`` / ``_create_tts_from_config`` do lazily — binds
# the MODULE object onto this package, shadowing the helper and making
# ``providers.soniox_tts(...)`` uncallable. Force the submodule imports now
# (so the binding happens once) and then restore the helpers. Subsequent
# ``from getpatter.providers.<name> import ...`` calls find the module already
# cached in ``sys.modules`` and do NOT re-bind the attribute, so the helpers
# stay callable. ``sarvam`` needs no such guard (its submodule is
# ``sarvam_tts``, a different name).
import importlib as _importlib  # noqa: E402

for _shadowed_name in ("soniox_tts", "xai_stt", "xai_tts"):
    _helper = globals()[_shadowed_name]
    try:  # pragma: no cover - import side-effect guard
        _importlib.import_module(__name__ + "." + _shadowed_name)
    except Exception:  # pragma: no cover - defensive (optional dep may be absent)
        pass
    globals()[_shadowed_name] = _helper
del _importlib, _helper, _shadowed_name
