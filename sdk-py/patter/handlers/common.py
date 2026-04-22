"""Shared utility functions for telephony handlers."""

from __future__ import annotations

import re

from patter.providers.base import STTProvider, TTSProvider


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
        config: An ``STTConfig`` instance, a pre-built :class:`STTProvider`
            instance, or ``None``.
        for_twilio: When ``True``, configure for Twilio's mulaw 8 kHz stream
            where the provider supports it. Ignored when *config* is already
            an :class:`STTProvider` instance — the user is responsible for
            constructing it with the right encoding.
    """
    if config is None:
        return None
    # v0.5.0 — already-built provider instance (e.g. ``deepgram.STT(...)``).
    if isinstance(config, STTProvider):
        return config
    provider = config.provider
    opts = dict(config.options or {})

    if provider == "deepgram":
        from patter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]

        allowed = {
            "model",
            "endpointing_ms",
            "utterance_end_ms",
            "smart_format",
            "interim_results",
            "vad_events",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        if for_twilio:
            return DeepgramSTT.for_twilio(
                api_key=config.api_key, language=config.language, **kwargs
            )
        return DeepgramSTT(api_key=config.api_key, language=config.language, **kwargs)

    if provider == "whisper":
        from patter.providers.whisper_stt import WhisperSTT  # type: ignore[import]

        return WhisperSTT(api_key=config.api_key, language=config.language)

    if provider == "cartesia":
        from patter.providers.cartesia_stt import CartesiaSTT  # type: ignore[import]

        allowed = {"model", "encoding", "sample_rate", "base_url"}
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return CartesiaSTT(api_key=config.api_key, language=config.language, **kwargs)

    if provider == "soniox":
        from patter.providers.soniox_stt import SonioxSTT  # type: ignore[import]

        allowed = {
            "model",
            "language_hints",
            "language_hints_strict",
            "sample_rate",
            "num_channels",
            "enable_speaker_diarization",
            "enable_language_identification",
            "max_endpoint_delay_ms",
            "client_reference_id",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return SonioxSTT(api_key=config.api_key, **kwargs)

    if provider == "speechmatics":
        from patter.providers.speechmatics_stt import SpeechmaticsSTT  # type: ignore[import]

        allowed = {
            "base_url",
            "turn_detection_mode",
            "sample_rate",
            "enable_diarization",
            "max_delay",
            "end_of_utterance_silence_trigger",
            "end_of_utterance_max_delay",
            "include_partials",
            "additional_vocab",
            "operating_point",
            "domain",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return SpeechmaticsSTT(api_key=config.api_key, language=config.language, **kwargs)

    if provider == "assemblyai":
        from patter.providers.assemblyai_stt import AssemblyAISTT  # type: ignore[import]

        allowed = {"model", "encoding", "sample_rate", "base_url"}
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        if for_twilio and hasattr(AssemblyAISTT, "for_twilio"):
            return AssemblyAISTT.for_twilio(
                api_key=config.api_key, language=config.language, **kwargs
            )
        return AssemblyAISTT(api_key=config.api_key, language=config.language, **kwargs)

    raise ValueError(
        f"Unknown STT provider '{provider}'. "
        "Supported: deepgram, whisper, cartesia, soniox, speechmatics, assemblyai."
    )


def _create_tts_from_config(config):
    """Create a TTS adapter from a TTSConfig object.

    Args:
        config: A ``TTSConfig`` instance, a pre-built :class:`TTSProvider`
            instance, or ``None``.
    """
    if config is None:
        return None
    # v0.5.0 — already-built provider instance (e.g. ``elevenlabs.TTS(...)``).
    if isinstance(config, TTSProvider):
        return config
    provider = config.provider
    opts = dict(getattr(config, "options", None) or {})

    if provider == "elevenlabs":
        from patter.providers.elevenlabs_tts import ElevenLabsTTS  # type: ignore[import]

        return ElevenLabsTTS(api_key=config.api_key, voice_id=config.voice)

    if provider == "openai":
        from patter.providers.openai_tts import OpenAITTS  # type: ignore[import]

        return OpenAITTS(api_key=config.api_key, voice=config.voice)

    if provider == "cartesia":
        from patter.providers.cartesia_tts import CartesiaTTS  # type: ignore[import]

        allowed = {"model", "language", "sample_rate", "speed", "emotion", "volume"}
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return CartesiaTTS(api_key=config.api_key, voice=config.voice, **kwargs)

    if provider == "rime":
        from patter.providers.rime_tts import RimeTTS  # type: ignore[import]

        allowed = {
            "model",
            "speaker",
            "lang",
            "sample_rate",
            "repetition_penalty",
            "temperature",
            "top_p",
            "max_tokens",
            "speed_alpha",
            "reduce_latency",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        # Pass the user-facing ``voice`` as ``speaker`` unless already overridden.
        kwargs.setdefault("speaker", config.voice)
        return RimeTTS(api_key=config.api_key, **kwargs)

    if provider == "lmnt":
        from patter.providers.lmnt_tts import LMNTTTS  # type: ignore[import]

        allowed = {
            "model",
            "language",
            "format",
            "sample_rate",
            "temperature",
            "top_p",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return LMNTTTS(api_key=config.api_key, voice=config.voice, **kwargs)

    raise ValueError(
        f"Unknown TTS provider '{provider}'. "
        "Supported: elevenlabs, openai, cartesia, rime, lmnt."
    )
