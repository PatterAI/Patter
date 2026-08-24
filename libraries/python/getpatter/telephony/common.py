"""Shared utility functions for telephony handlers."""

from __future__ import annotations
import logging

logger = logging.getLogger("getpatter")

import re

from getpatter.providers.base import STTProvider, TTSProvider

# SECURITY (#204): names of the per-call media-stream auth token as it travels
# on each carrier's custom-param channel. Kept in one place so the mint sites
# (server / adapters) and the WS read sites stay in lock-step. Mirrors the
# TypeScript SDK constants of the same value.
#
#   * Twilio: a ``<Parameter name="patter_stream_token" .../>`` that arrives in
#     ``start.customParameters`` (Twilio strips query params from <Stream>).
#   * Telnyx: a ``?...&patter_stream_token=`` query param on the stream URL
#     (Telnyx preserves the query string), read at WS accept.
#   * Plivo: an ``X-Patter-Stream-Token`` extra header echoed on the start frame.
STREAM_TOKEN_PARAM = "patter_stream_token"
STREAM_TOKEN_HEADER = "X-Patter-Stream-Token"


def _validate_e164(number: str) -> bool:
    """Return True if *number* is a valid E.164 phone number."""
    return bool(re.match(r"^\+[1-9]\d{6,14}$", number))


def _transfer_destination_allowed(
    number: str,
    allowed_numbers: "tuple[str, ...] | list[str] | None",
    allowed_prefixes: "tuple[str, ...] | list[str] | None",
) -> bool:
    """Return True if *number* passes the opt-in transfer destination policy.

    ``transfer_call``'s destination is chosen by the LLM (caller-steerable via
    prompt injection), so the E.164 format gate alone cannot stop
    attacker-directed toll-fraud transfers. When either allowlist is
    configured the destination must be an exact member of *allowed_numbers*
    OR start with one of *allowed_prefixes* (union). ``None`` for both
    (default) keeps destinations unrestricted; a configured-but-empty policy
    denies every destination (explicit lockdown).
    """
    if allowed_numbers is None and allowed_prefixes is None:
        return True
    if allowed_numbers and number in allowed_numbers:
        return True
    if allowed_prefixes and any(number.startswith(p) for p in allowed_prefixes):
        return True
    return False


def _sanitize_variable_value(value: str) -> str:
    """Strip control characters and limit length to prevent prompt injection.

    Removes C0 controls + DEL **and** the C1 controls / Unicode line separators
    (U+0085 NEL is within ``\\x7f-\\x9f``; plus U+2028, U+2029) that a plain
    ``\\n``/``\\r`` filter misses — any of these can open a new line in the
    system prompt the value is interpolated into. Mirrors TS ``sanitizeVariables``.
    """
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", "", str(value))[:500]


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
    # CLONE it: the documented pattern hands ONE instance to ONE agent served
    # for MANY calls, but the adapters are stateful per-connection objects —
    # concurrent calls sharing one instance overwrote each other's sockets
    # and queues (cross-call transcript bleed; the first hangup closed the
    # surviving call's socket). Fall back to the shared instance with a loud
    # warning when the adapter can't be cloned.
    if isinstance(config, STTProvider):
        try:
            return config.clone()
        except Exception as exc:  # noqa: BLE001 - degrade to legacy sharing
            logger.warning(
                "STT adapter %s could not be cloned per call (%s) — falling "
                "back to the SHARED instance. Concurrent calls on this agent "
                "may interfere with each other; implement clone() on the "
                "adapter to fix.",
                type(config).__name__,
                exc,
            )
            return config
    provider = config.provider
    opts = dict(config.options or {})

    if provider == "deepgram":
        from getpatter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]

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
        from getpatter.providers.whisper_stt import WhisperSTT  # type: ignore[import]

        return WhisperSTT(api_key=config.api_key, language=config.language)

    if provider == "cartesia":
        from getpatter.providers.cartesia_stt import CartesiaSTT  # type: ignore[import]

        allowed = {"model", "encoding", "sample_rate", "base_url"}
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return CartesiaSTT(api_key=config.api_key, language=config.language, **kwargs)

    if provider == "soniox":
        from getpatter.providers.soniox_stt import SonioxSTT  # type: ignore[import]

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
        # ``STTConfig.language`` (set by the ``providers.soniox(...)`` helper)
        # was silently discarded — SonioxSTT has no ``language`` parameter;
        # map it to ``language_hints`` unless hints were given explicitly.
        if "language_hints" not in kwargs and config.language:
            kwargs["language_hints"] = [config.language]
        return SonioxSTT(api_key=config.api_key, **kwargs)

    if provider == "speechmatics":
        from getpatter.providers.speechmatics_stt import SpeechmaticsSTT  # type: ignore[import]

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
        return SpeechmaticsSTT(
            api_key=config.api_key, language=config.language, **kwargs
        )

    if provider == "assemblyai":
        from getpatter.providers.assemblyai_stt import AssemblyAISTT  # type: ignore[import]

        allowed = {"model", "encoding", "sample_rate", "base_url"}
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        if for_twilio and hasattr(AssemblyAISTT, "for_twilio"):
            return AssemblyAISTT.for_twilio(
                api_key=config.api_key, language=config.language, **kwargs
            )
        return AssemblyAISTT(api_key=config.api_key, language=config.language, **kwargs)

    if provider == "fish_audio":
        from getpatter.providers.fish_audio_stt import FishAudioSTT  # type: ignore[import]

        allowed = {"ignore_timestamps", "buffer_size_bytes", "base_url"}
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return FishAudioSTT(api_key=config.api_key, language=config.language, **kwargs)

    if provider == "xai":
        from getpatter.providers.xai_stt import XaiSTT  # type: ignore[import]

        allowed = {
            "encoding",
            "sample_rate",
            "interim_results",
            "endpointing_ms",
            "smart_turn",
            "smart_turn_timeout_ms",
            "keyterms",
            "diarize",
            "filler_words",
            "base_url",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return XaiSTT(api_key=config.api_key, language=config.language, **kwargs)

    raise ValueError(
        f"Unknown STT provider '{provider}'. "
        "Supported: deepgram, whisper, cartesia, soniox, speechmatics, "
        "assemblyai, fish_audio, xai."
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
        from getpatter.providers.elevenlabs_tts import ElevenLabsTTS  # type: ignore[import]

        return ElevenLabsTTS(api_key=config.api_key, voice_id=config.voice)

    if provider == "openai":
        from getpatter.providers.openai_tts import OpenAITTS  # type: ignore[import]

        return OpenAITTS(api_key=config.api_key, voice=config.voice)

    if provider == "cartesia":
        from getpatter.providers.cartesia_tts import CartesiaTTS  # type: ignore[import]

        allowed = {"model", "language", "sample_rate", "speed", "emotion", "volume"}
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return CartesiaTTS(api_key=config.api_key, voice=config.voice, **kwargs)

    if provider == "rime":
        from getpatter.providers.rime_tts import RimeTTS  # type: ignore[import]

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
        from getpatter.providers.lmnt_tts import LMNTTTS  # type: ignore[import]

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

    if provider == "inworld":
        from getpatter.providers.inworld_tts import InworldTTS  # type: ignore[import]

        allowed = {
            "model",
            "language",
            "audio_encoding",
            "sample_rate",
            "bitrate",
            "temperature",
            "speaking_rate",
            "delivery_mode",
            "base_url",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        # ``api_key`` carries the Base64 ``Authorization: Basic`` token.
        return InworldTTS(auth_token=config.api_key, voice=config.voice, **kwargs)

    if provider == "soniox_tts":
        from getpatter.providers.soniox_tts import SonioxTTS  # type: ignore[import]

        allowed = {
            "model",
            "language",
            "audio_format",
            "sample_rate",
            "bitrate",
            "speed",
            "base_url",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return SonioxTTS(api_key=config.api_key, voice=config.voice, **kwargs)

    if provider == "sarvam":
        from getpatter.providers.sarvam_tts import SarvamTTS  # type: ignore[import]

        allowed = {
            "model",
            "language",
            "codec",
            "sample_rate",
            "pace",
            "pitch",
            "loudness",
            "temperature",
            "enable_preprocessing",
            "dict_id",
            "base_url",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        # The Sarvam adapter calls the user-facing ``voice`` a ``speaker``.
        return SarvamTTS(api_key=config.api_key, speaker=config.voice, **kwargs)

    if provider == "fish_audio":
        allowed = {
            "model",
            "format",
            "sample_rate",
            "latency",
            "speed",
            "volume",
            "normalize_loudness",
            "temperature",
            "top_p",
            "chunk_length",
            "min_chunk_length",
            "normalize",
            "max_new_tokens",
            "repetition_penalty",
            "condition_on_previous_chunks",
            "early_stop_threshold",
            "mp3_bitrate",
            "opus_bitrate",
            "base_url",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        # ``transport="websocket"`` selects the /v1/tts/live socket (s1 /
        # s2-pro only); anything else uses the HTTP streaming endpoint, which
        # is the only transport that serves s2.1-pro.
        if opts.get("transport") == "websocket":
            from getpatter.providers.fish_audio_ws_tts import (  # type: ignore[import]
                FishAudioWebSocketTTS,
            )

            kwargs.setdefault("model", "s2-pro")
            return FishAudioWebSocketTTS(
                api_key=config.api_key, voice=config.voice or None, **kwargs
            )

        from getpatter.providers.fish_audio_tts import FishAudioTTS  # type: ignore[import]

        # Fish's ``voice`` is a reference_id; an empty string means "use the
        # model's built-in voice", which the adapter expresses as None.
        return FishAudioTTS(
            api_key=config.api_key, voice=config.voice or None, **kwargs
        )

    if provider == "xai_tts":
        from getpatter.providers.xai_tts import XaiTTS  # type: ignore[import]

        allowed = {
            "language",
            "speed",
            "optimize_streaming_latency",
            "text_normalization",
            "codec",
            "sample_rate",
            "bit_rate",
            "base_url",
        }
        kwargs = {k: v for k, v in opts.items() if k in allowed}
        return XaiTTS(api_key=config.api_key, voice=config.voice, **kwargs)

    raise ValueError(
        f"Unknown TTS provider '{provider}'. "
        "Supported: elevenlabs, openai, cartesia, rime, lmnt, inworld, "
        "soniox_tts, sarvam, fish_audio, xai_tts."
    )
