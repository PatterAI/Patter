"""Gemini Text-to-Speech for the Patter SDK — streaming speech model adapter.

Beta: ported from the TypeScript adapter (``src/providers/gemini-tts.ts``);
validated against the Google Gen AI SDK surface, not yet exercised against a
live phone call.

Wraps Google's ``gemini-3.1-flash-tts-preview`` speech model via the
``google-genai`` ``generate_content_stream`` API. The model emits PCM L16 @
24 kHz, which this adapter resamples to the pipeline-facing rate (16 kHz by
default; the stream handler does the final 16k -> 8k mu-law step for the
carrier).

Inline square-bracket delivery tags in the text — ``[warm]``, ``[short pause]``,
``[sigh]`` — are honoured by the model and shape prosody rather than being
spoken, which is why the Patter demo persona annotates its replies.

Credential: ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` env var, or the ``api_key``
argument.
"""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, ClassVar, Optional

from getpatter.providers.base import TTSProvider

logger = logging.getLogger("getpatter.providers.gemini_tts")

# Default speech model. Preview surface — re-verify before GA.
# Source: https://ai.google.dev/gemini-api/docs/speech-generation (as of 2026-08-24)
GEMINI_TTS_DEFAULT_MODEL = "gemini-3.1-flash-tts-preview"

# Default prebuilt voice, matching the TypeScript adapter and the demo persona.
GEMINI_TTS_DEFAULT_VOICE = "Kore"

# Native output rate of the Gemini speech models (PCM16-LE mono).
GEMINI_TTS_SOURCE_SAMPLE_RATE = 24000

# Pipeline-facing default; the stream handler resamples 16k -> 8k for Twilio.
GEMINI_TTS_DEFAULT_TARGET_SAMPLE_RATE = 16000

# Rates the pipeline can consume directly. 24 kHz is accepted too (passthrough,
# no resampling) because it is the model's native rate.
GEMINI_TTS_TARGET_SAMPLE_RATES = (8000, 16000, GEMINI_TTS_SOURCE_SAMPLE_RATE)

# Tiny synthesis used by :meth:`GeminiTTS.warmup` to open the HTTP/2 connection
# and warm the model before the first real turn.
_WARMUP_TEXT = "Hello."


def resolve_gemini_api_key(api_key: Optional[str], surface: str) -> str:
    """Return the Gemini key from ``api_key`` or the environment.

    Both ``GEMINI_API_KEY`` and ``GOOGLE_API_KEY`` are accepted, in that order —
    the same fallback chain as the TypeScript adapters and the Google Gen AI
    SDK itself.
    """
    key = (
        api_key
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not key:
        raise ValueError(
            f"{surface} requires an api_key. Pass api_key='...' or set "
            "GEMINI_API_KEY / GOOGLE_API_KEY in the environment."
        )
    return key


def build_genai_client(api_key: str, surface: str) -> Any:
    """Construct an async-capable ``google.genai`` client (lazy import).

    The SDK is imported at call time so installs without the Gemini extra do
    not pay the import cost — mirrors
    :class:`~getpatter.providers.gemini_live.GeminiLiveAdapter`.
    """
    try:
        from google import genai  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - trivial import guard
        raise ImportError(
            f"{surface} requires the 'google-genai' package. "
            "Install with: pip install getpatter[gemini]"
        ) from exc
    return genai.Client(api_key=api_key)


class GeminiTTS(TTSProvider):
    """Streaming Gemini TTS adapter (``Kore`` voice by default).

    Args:
        api_key: Google Generative Language API key. Falls back to
            ``GEMINI_API_KEY`` then ``GOOGLE_API_KEY``.
        voice: Prebuilt Gemini voice name (default ``Kore``).
        model: Speech model id (default ``gemini-3.1-flash-tts-preview``).
        target_sample_rate: Output PCM16-LE rate — 8000, 16000, or 24000
            (the model's native rate, emitted without resampling).
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "gemini_tts"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        voice: str = GEMINI_TTS_DEFAULT_VOICE,
        model: str = GEMINI_TTS_DEFAULT_MODEL,
        target_sample_rate: int = GEMINI_TTS_DEFAULT_TARGET_SAMPLE_RATE,
    ) -> None:
        if target_sample_rate not in GEMINI_TTS_TARGET_SAMPLE_RATES:
            raise ValueError(
                "GeminiTTS: target_sample_rate must be one of "
                f"{GEMINI_TTS_TARGET_SAMPLE_RATES}, got {target_sample_rate}."
            )
        self.api_key = resolve_gemini_api_key(api_key, "Gemini TTS")
        self.voice = voice
        self.model = model
        self.target_sample_rate = target_sample_rate
        self._client: Any = None

    def __repr__(self) -> str:
        # Never leak the API key in repr / logs.
        return (
            f"GeminiTTS(voice={self.voice!r}, model={self.model!r}, "
            f"target_sample_rate={self.target_sample_rate})"
        )

    def source_audio_format(self) -> "AudioFormat":  # noqa: F821 - lazy import
        """Declare the emitted format so the sender derives the resample ratio
        instead of assuming a fixed 16 kHz source. See ``getpatter.audio.format``.
        """
        from getpatter.audio.format import AudioFormat

        return AudioFormat(
            encoding="pcm_s16le", sample_rate=int(self.target_sample_rate)
        )

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = build_genai_client(self.api_key, "GeminiTTS")
        return self._client

    def _build_config(self) -> dict[str, Any]:
        """Audio-only response config selecting the prebuilt voice."""
        return {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {"prebuilt_voice_config": {"voice_name": self.voice}}
            },
        }

    def _make_resampler(self) -> Any:
        """Return a fresh resampler, or ``None`` when no rate change is needed.

        Fresh per synthesis: each call is an independent PCM stream, so carrying
        ``ratecv`` filter state across calls would corrupt the first frames.
        """
        if self.target_sample_rate == GEMINI_TTS_SOURCE_SAMPLE_RATE:
            return None
        from getpatter.audio.transcoding import StatefulResampler

        return StatefulResampler(
            GEMINI_TTS_SOURCE_SAMPLE_RATE, self.target_sample_rate
        )

    def _record_synthesis_cost(self, text: str) -> None:
        """Emit ``patter.cost.tts_chars`` for the synthesised text."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.tts_chars": len(text),
                    "patter.tts.provider": self.provider_key,
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_synthesis_cost failed", exc_info=True)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Synthesise ``text`` and yield PCM16-LE chunks at the target rate."""
        if not text.strip():
            return
        self._record_synthesis_cost(text)
        client = self._ensure_client()
        resampler = self._make_resampler()

        stream = await client.aio.models.generate_content_stream(
            model=self.model,
            contents=[{"role": "user", "parts": [{"text": text}]}],
            config=self._build_config(),
        )
        async for chunk in stream:
            pcm24 = _extract_inline_audio(chunk)
            if not pcm24:
                continue
            out = resampler.process(pcm24) if resampler is not None else pcm24
            if out:
                yield out
        if resampler is not None:
            tail = resampler.flush()
            if tail:
                yield tail

    async def warmup(self) -> None:
        """Drain a tiny synthesis so the first real turn skips connection setup."""
        try:
            async for _chunk in self.synthesize(_WARMUP_TEXT):
                pass  # Discard: the point is the connection, not the audio.
        except Exception as exc:  # noqa: BLE001 - warmup never aborts a call
            logger.warning("GeminiTTS warmup failed: %s", exc)

    async def close(self) -> None:
        """Release the client handle (the SDK owns no persistent socket here)."""
        self._client = None


def _extract_inline_audio(chunk: Any) -> bytes:
    """Pull the inline PCM payload out of one streamed response chunk.

    The Gen AI SDK decodes ``inlineData`` base64 into ``bytes`` on
    ``Part.inline_data.data``; chunks that carry no audio part yield ``b""``.
    Attribute access is defensive because the response objects differ across
    ``google-genai`` releases.
    """
    candidates = getattr(chunk, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                return bytes(data)
    return b""
