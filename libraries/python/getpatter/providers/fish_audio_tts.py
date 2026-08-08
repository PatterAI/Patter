"""Fish Audio Text-to-Speech for the Patter SDK — HTTP streaming endpoint.

Beta: validated against the Fish Audio API spec (docs.fish.audio); not yet
exercised against a live phone call.

Targets ``POST https://api.fish.audio/v1/tts``, which streams the synthesised
audio back with chunked transfer encoding. The model is selected with a
``model:`` request header (NOT a body field) — the same key can therefore serve
``s2.1-pro``, ``s2-pro``, ``s1`` and the free tier without reconnecting.

Default output is raw PCM-16-LE @ 16 kHz (``format="pcm"``) so chunks drop
straight into the Patter pipeline with no transcoding — the same choice made
for LMNT (``raw``) and Inworld (``PCM``).

Model families (see :class:`FishAudioModel`):

* ``s2.1-pro``      — recommended production model. 83 languages, multi-speaker,
                      natural-language expression control via ``[brackets]``.
* ``s2.1-pro-free`` — same model on the free tier. No time-to-first-audio
                      guarantee, so it is NOT the default here: a voice agent
                      that stalls mid-call is worse than one that costs money.
* ``s2-pro``        — previous generation, ~100 ms time-to-first-audio. Also the
                      only S2 model reachable over the WebSocket transport
                      (see :class:`~getpatter.providers.fish_audio_ws_tts.FishAudioWebSocketTTS`).
* ``s1``            — legacy. Emotion control uses ``(parentheses)`` instead of
                      brackets.

Credential: ``FISH_AUDIO_API_KEY`` env var / ``api_key`` argument. Auth is an
``Authorization: Bearer <key>`` header.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    ClassVar,
    Optional,
    Sequence,
    Union,
)

from getpatter.providers.base import TTSProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from getpatter.audio.format import AudioFormat

logger = logging.getLogger("getpatter.providers.fish_audio_tts")

# Lazy import: aiohttp is declared as an optional dep for this provider
# (shared ``[fish_audio]`` extra with the WebSocket TTS and ASR adapters).
try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

# API origin — shared by the TTS, ASR and model-listing endpoints.
FISH_AUDIO_API_ORIGIN = "https://api.fish.audio"
# HTTP streaming text-to-speech endpoint.
FISH_AUDIO_TTS_URL = f"{FISH_AUDIO_API_ORIGIN}/v1/tts"
# Voice-model listing — a free metadata read used as the warmup target.
FISH_AUDIO_MODELS_URL = f"{FISH_AUDIO_API_ORIGIN}/model"

# Pipeline-friendly defaults: raw PCM-16-LE @ 16 kHz (NOT Fish's own mp3
# default, which the pipeline cannot decode).
FISH_AUDIO_DEFAULT_FORMAT = "pcm"
FISH_AUDIO_DEFAULT_SAMPLE_RATE = 16000


class FishAudioModel(StrEnum):
    """Fish Audio TTS model ids, passed via the ``model:`` request header."""

    S2_1_PRO = "s2.1-pro"
    S2_1_PRO_FREE = "s2.1-pro-free"
    S2_PRO = "s2-pro"
    S1 = "s1"


class FishAudioFormat(StrEnum):
    """Output container/codec. Only ``PCM`` is pipeline-compatible."""

    PCM = "pcm"
    WAV = "wav"
    MP3 = "mp3"
    OPUS = "opus"


class FishAudioLatency(StrEnum):
    """Latency/quality trade-off.

    ``NORMAL`` favours quality (~500 ms), ``BALANCED`` is the interactive
    sweet spot (~300 ms) and is Patter's default, ``LOW`` trades quality for
    the fastest possible first chunk.
    """

    LOW = "low"
    BALANCED = "balanced"
    NORMAL = "normal"


class FishAudioTTS(TTSProvider):
    """Fish Audio TTS over the HTTP ``/v1/tts`` streaming endpoint (Beta).

    Default output is raw PCM-16-LE @ 16 kHz with ``model="s2.1-pro"`` and
    ``latency="balanced"``.

    Voice selection
    ---------------
    ``voice`` maps to Fish's ``reference_id`` — the id of a voice model from the
    Fish voice library or one you cloned yourself. Leave it unset to use the
    model's built-in default voice. Pass a sequence of ids for multi-speaker
    synthesis, and mark the speakers inline in the text with
    ``<|speaker:0|>`` / ``<|speaker:1|>``.

    Telephony
    ---------
    Fish emits linear PCM only (no native G.711), so the pipeline always runs
    the mu-law encode. :meth:`for_twilio` requests PCM directly at 8 kHz so the
    resample step is skipped; :meth:`for_telnyx` keeps the 16 kHz pipeline rate.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "fish_audio"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Union[FishAudioModel, str] = FishAudioModel.S2_1_PRO,
        voice: Optional[Union[str, Sequence[str]]] = None,
        format: Union[FishAudioFormat, str] = FISH_AUDIO_DEFAULT_FORMAT,
        sample_rate: int = FISH_AUDIO_DEFAULT_SAMPLE_RATE,
        latency: Union[FishAudioLatency, str] = FishAudioLatency.BALANCED,
        speed: Optional[float] = None,
        volume: Optional[float] = None,
        normalize_loudness: Optional[bool] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        chunk_length: Optional[int] = None,
        min_chunk_length: Optional[int] = None,
        normalize: Optional[bool] = None,
        max_new_tokens: Optional[int] = None,
        repetition_penalty: Optional[float] = None,
        condition_on_previous_chunks: Optional[bool] = None,
        early_stop_threshold: Optional[float] = None,
        mp3_bitrate: Optional[int] = None,
        opus_bitrate: Optional[int] = None,
        base_url: str = FISH_AUDIO_TTS_URL,
        session: Optional["aiohttp.ClientSession"] = None,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for FishAudioTTS. "
                "Install with: pip install getpatter[fish_audio]"
            )

        resolved_key = api_key or os.environ.get("FISH_AUDIO_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Fish Audio TTS requires an api_key. Pass api_key='...' or "
                "set FISH_AUDIO_API_KEY in the environment."
            )

        self.api_key = resolved_key
        self.model = model
        self.voice = voice
        self.format = format
        self.sample_rate = int(sample_rate)
        self.latency = latency
        self.speed = speed
        self.volume = volume
        self.normalize_loudness = normalize_loudness
        self.temperature = temperature
        self.top_p = top_p
        self.chunk_length = chunk_length
        self.min_chunk_length = min_chunk_length
        self.normalize = normalize
        self.max_new_tokens = max_new_tokens
        self.repetition_penalty = repetition_penalty
        self.condition_on_previous_chunks = condition_on_previous_chunks
        self.early_stop_threshold = early_stop_threshold
        self.mp3_bitrate = mp3_bitrate
        self.opus_bitrate = opus_bitrate
        self.base_url = base_url
        self._owns_session = session is None
        self._session = session

    def __repr__(self) -> str:
        # Never leak the API key in repr / logs.
        return (
            f"FishAudioTTS(model={self.model!r}, voice={self.voice!r}, "
            f"format={self.format!r}, sample_rate={self.sample_rate}, "
            f"latency={self.latency!r})"
        )

    # ------------------------------------------------------------------
    # Telephony factories
    # ------------------------------------------------------------------

    @classmethod
    def for_twilio(cls, api_key: Optional[str] = None, **kwargs: Any) -> "FishAudioTTS":
        """Build an instance pre-configured for Twilio Media Streams.

        Requests PCM directly at 8 kHz — the carrier wire rate — so the pipeline
        skips the 16 kHz -> 8 kHz resample and only runs the mu-law encode. Fish
        has no native G.711 output, so a fully bit-clean passthrough is not
        available on this provider.
        """
        kwargs.pop("format", None)
        kwargs.pop("sample_rate", None)
        return cls(
            api_key=api_key, format=FishAudioFormat.PCM, sample_rate=8000, **kwargs
        )

    @classmethod
    def for_telnyx(cls, api_key: Optional[str] = None, **kwargs: Any) -> "FishAudioTTS":
        """Build an instance pre-configured for Telnyx bidirectional media.

        Emits PCM @ 16 kHz to match the Telnyx PCM16 pipeline; the SDK resamples
        to the 8 kHz PCMU wire once, downstream.
        """
        kwargs.pop("format", None)
        kwargs.pop("sample_rate", None)
        return cls(
            api_key=api_key,
            format=FishAudioFormat.PCM,
            sample_rate=FISH_AUDIO_DEFAULT_SAMPLE_RATE,
            **kwargs,
        )

    def source_audio_format(self) -> "AudioFormat":
        """Declare the audio format this adapter emits so the pipeline sender
        derives the correct resample ratio instead of assuming 16 kHz. See
        ``getpatter.audio.format``.

        Only ``format="pcm"`` produces pipeline-consumable bytes; ``wav`` /
        ``mp3`` / ``opus`` are container formats intended for offline use and
        are reported here as their underlying PCM rate.
        """
        from getpatter.audio.format import AudioFormat

        return AudioFormat(encoding="pcm_s16le", sample_rate=self.sample_rate)

    def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Fish selects the model with a header, not a body field.
            "model": str(self.model),
        }

    def _build_payload(self, text: str) -> dict[str, Any]:
        """Build the ``TTSRequest`` body.

        Every optional knob is omitted when unset so Fish applies its own
        documented default rather than a value we guessed.
        """
        payload: dict[str, Any] = {
            "text": text,
            "format": str(self.format),
            "latency": str(self.latency),
        }
        # ``sample_rate`` is only meaningful for the raw/uncompressed formats;
        # mp3 and opus derive it from the bitrate instead.
        if str(self.format) in (FishAudioFormat.PCM, FishAudioFormat.WAV):
            payload["sample_rate"] = self.sample_rate
        if self.voice:
            # str -> single speaker; sequence -> multi-speaker (s2 models).
            payload["reference_id"] = (
                self.voice if isinstance(self.voice, str) else list(self.voice)
            )

        prosody = self._build_prosody()
        if prosody:
            payload["prosody"] = prosody

        optional: dict[str, Any] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "chunk_length": self.chunk_length,
            "min_chunk_length": self.min_chunk_length,
            "normalize": self.normalize,
            "max_new_tokens": self.max_new_tokens,
            "repetition_penalty": self.repetition_penalty,
            "condition_on_previous_chunks": self.condition_on_previous_chunks,
            "early_stop_threshold": self.early_stop_threshold,
            "mp3_bitrate": self.mp3_bitrate,
            "opus_bitrate": self.opus_bitrate,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})
        return payload

    def _build_prosody(self) -> dict[str, Any]:
        """Collapse the flat speed/volume/loudness args into Fish's nested
        ``prosody`` object. Returns ``{}`` when none of them is set."""
        prosody: dict[str, Any] = {}
        if self.speed is not None:
            prosody["speed"] = self.speed
        if self.volume is not None:
            prosody["volume"] = self.volume
        if self.normalize_loudness is not None:
            prosody["normalize_loudness"] = self.normalize_loudness
        return prosody

    def _record_synthesis_cost(self, text: str) -> None:
        """Emit ``patter.cost.tts_chars`` for the synthesised text.

        Fish bills UTF-8 *bytes*, so we report the byte length rather than the
        character count — they diverge by ~3x on CJK text.
        """
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.tts_chars": len(text.encode("utf-8")),
                    "patter.tts.provider": "fish_audio",
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_synthesis_cost failed", exc_info=True)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio bytes for ``text``.

        With the default ``format="pcm"`` these are raw PCM-16-LE chunks at the
        configured ``sample_rate``.
        """
        self._record_synthesis_cost(text)
        session = self._ensure_session()

        async with session.post(
            self.base_url,
            headers=self._build_headers(),
            json=self._build_payload(text),
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Fish Audio TTS error {resp.status}: {body[:500]}")
            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    yield chunk

    async def warmup(self) -> None:
        """Best-effort pre-call HTTP warmup.

        Issues ``GET /model?page_size=1`` — the voice-model listing — so DNS,
        TLS and the HTTP connection are already established when the first
        :meth:`synthesize` POST lands. Billing safety: listing voice models is a
        free metadata read; synthesis is billed only by ``POST /v1/tts``.

        A ``HEAD`` against ``/v1/tts`` would be cheaper still but that endpoint
        is POST-only and answers 405 — the same trap already documented on the
        Inworld adapter. Best-effort throughout: 5 s timeout, every exception
        swallowed at DEBUG.
        """
        try:
            session = self._ensure_session()
            async with session.get(
                FISH_AUDIO_MODELS_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                params={"page_size": "1"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                # Drain so the connection returns cleanly to the pool.
                await resp.read()
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("Fish Audio TTS warmup failed (best-effort): %s", exc)

    async def close(self) -> None:
        """Close the underlying session (idempotent)."""
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None
