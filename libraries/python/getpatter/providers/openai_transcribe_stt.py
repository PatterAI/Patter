"""OpenAI GPT-4o Transcribe STT adapter for the Patter SDK pipeline mode.

This is a first-class wrapper around OpenAI's ``gpt-4o-transcribe`` /
``gpt-4o-mini-transcribe`` models. They share the
``POST /v1/audio/transcriptions`` endpoint with Whisper-1 but offer ~10x lower
latency and stronger multilingual quality, making them a drop-in replacement
for :class:`WhisperSTT` whenever speed matters.

Use this class instead of :class:`WhisperSTT` when you specifically want the
GPT-4o Transcribe family — it restricts the accepted models so misconfigured
calls fail fast instead of silently dropping back to ``whisper-1``.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from getpatter.providers.whisper_stt import WhisperSTT

__all__ = ["OpenAITranscribeSTT"]


_ALLOWED_MODELS = {"gpt-4o-transcribe", "gpt-4o-mini-transcribe"}


class OpenAITranscribeSTT(WhisperSTT):
    """OpenAI GPT-4o Transcribe STT — ~10x faster than Whisper-1.

    Subclasses :class:`WhisperSTT` and reuses its buffering + transcription
    logic; only the default model and accepted-model whitelist change.

    Args:
        api_key: OpenAI API key.
        language: BCP-47 language code (e.g. ``"en"``).
        model: One of ``"gpt-4o-transcribe"`` (default) or
            ``"gpt-4o-mini-transcribe"``. ``"whisper-1"`` is intentionally
            rejected here — use :class:`WhisperSTT` for that.
        response_format: ``"json"`` (gpt-4o models reject ``"verbose_json"``).
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "openai_transcribe"

    def __init__(
        self,
        api_key: str,
        language: str = "en",
        model: str = "gpt-4o-transcribe",
        response_format: Literal["json", "verbose_json"] = "json",
    ) -> None:
        if model not in _ALLOWED_MODELS:
            raise ValueError(
                f"OpenAITranscribeSTT: unsupported model {model!r}. "
                f"Expected one of {sorted(_ALLOWED_MODELS)}. "
                f"For 'whisper-1', use WhisperSTT instead."
            )
        if response_format == "verbose_json":
            # OpenAI only supports verbose_json on whisper-1; the
            # gpt-4o(-mini)-transcribe models 400 on it — every chunk failed
            # (errors only logged) while audio kept being buffered/billed.
            raise ValueError(
                "OpenAITranscribeSTT: response_format='verbose_json' is only "
                "supported by whisper-1 (use WhisperSTT). "
                f"{model!r} accepts 'json'."
            )
        super().__init__(
            api_key=api_key,
            language=language,
            model=model,
            response_format=response_format,
        )
        # Observability: ``_record_transcript_cost`` is inherited from
        # ``WhisperSTT`` and emits ``patter.stt.provider="whisper"``. This
        # is intentional — gpt-4o-transcribe shares the Whisper cost table,
        # so a single tag keeps the dashboard's per-provider rollup
        # consistent across the OpenAI transcription family.
