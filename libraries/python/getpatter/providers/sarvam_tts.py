"""Sarvam AI TTS provider — HTTP REST one-shot endpoint, pure aiohttp.

Sarvam's Bulbul models synthesize 11 Indian languages (Hindi, Bengali, Tamil,
Telugu, Kannada, Malayalam, Marathi, Gujarati, Punjabi, Odia, Indian English)
plus code-mixed text. This adapter targets the REST one-shot endpoint
``POST https://api.sarvam.ai/text-to-speech`` which returns JSON of the form
``{"request_id": "...", "audios": ["<base64-audio>", ...]}`` — we base64-decode
each entry and yield the raw bytes, mapping cleanly onto Patter's
``TTSProvider.synthesize(text) -> AsyncIterator[bytes]`` contract with no vendor
SDK.

Sarvam also exposes a WebSocket streaming transport
(``wss://api.sarvam.ai/text-to-speech/ws?model=<model_id>``) with sub-250 ms
first-byte; like the Cartesia adapter we use the REST path because it already
meets Patter's TTFB target while keeping the dependency surface minimal (just
``aiohttp``, no websocket client).

Auth is an API subscription key sent as the ``api-subscription-key`` HTTP
header (no Bearer scheme). The key is read from the ``api_key`` argument or the
``SARVAM_API_KEY`` environment variable.

Default config requests ``codec="linear16"`` (raw PCM_S16LE) at 16 kHz so the
output drops straight into the Patter pipeline without transcoding — the stream
handler does the final 16 kHz -> 8 kHz mu-law step for the carrier. For real
phone calls the :meth:`for_twilio` / :meth:`for_telnyx` factories request
``mulaw`` @ 8 kHz directly (Sarvam supports both natively), so the pipeline
takes the carrier-native passthrough path with zero resampling.

Bulbul v3 is the default model (latest; 35+ voices; ``temperature`` control).
Pass ``model="bulbul:v2"`` for the legacy generation, which instead exposes
``pitch`` / ``loudness`` / ``enable_preprocessing``. Those controls are gated
per-model so we never send fields the selected model rejects.
"""

from __future__ import annotations

import base64
import logging
import os
from enum import IntEnum, StrEnum
from typing import ClassVar, Any, AsyncIterator, Optional, Union
from urllib.parse import urlsplit

from getpatter.providers.base import TTSProvider

logger = logging.getLogger("getpatter.providers.sarvam_tts")

try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

SARVAM_BASE_URL = "https://api.sarvam.ai/text-to-speech"


class SarvamModel(StrEnum):
    """Sarvam Bulbul TTS model families."""

    BULBUL_V3 = "bulbul:v3"
    BULBUL_V2 = "bulbul:v2"


class SarvamAudioCodec(StrEnum):
    """``output_audio_codec`` values accepted by the REST API.

    ``LINEAR16`` is headerless 16-bit PCM (the pipeline-friendly default);
    ``MULAW`` / ``ALAW`` are G.711 telephony codecs.
    """

    WAV = "wav"
    MP3 = "mp3"
    LINEAR16 = "linear16"
    MULAW = "mulaw"
    ALAW = "alaw"
    OPUS = "opus"
    FLAC = "flac"
    AAC = "aac"


class SarvamLanguage(StrEnum):
    """BCP-47 ``target_language_code`` values for the 11 supported languages.

    Note Sarvam uses ``od-IN`` for Odia (not the more common ``or-IN``).
    """

    HINDI = "hi-IN"
    BENGALI = "bn-IN"
    TAMIL = "ta-IN"
    TELUGU = "te-IN"
    KANNADA = "kn-IN"
    MALAYALAM = "ml-IN"
    MARATHI = "mr-IN"
    GUJARATI = "gu-IN"
    PUNJABI = "pa-IN"
    ODIA = "od-IN"
    ENGLISH = "en-IN"


class SarvamSampleRate(IntEnum):
    """Output sample rates (Hz) accepted by the REST ``speech_sample_rate``."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_22050 = 22050
    HZ_24000 = 24000
    HZ_32000 = 32000
    HZ_44100 = 44100
    HZ_48000 = 48000


class SarvamTTS(TTSProvider):
    """Sarvam AI TTS over the REST ``/text-to-speech`` one-shot endpoint.

    Output is base64 audio decoded to raw bytes. With the default
    ``codec="linear16"`` at 16 kHz these are PCM_S16LE samples that line up
    with Patter's pipeline without a resample step.

    Telephony optimization
    ----------------------
    The constructor default ``sample_rate=16000`` is correct for web playback
    and 16 kHz pipelines. For real phone calls use the carrier-specific
    factories instead:

    * :meth:`for_twilio` / :meth:`for_telnyx` — emit ``mulaw`` @ 8 kHz, the
      carrier wire codec, so the pipeline skips resampling AND PCM -> mu-law
      encoding (bit-clean passthrough). Sarvam supports ``mulaw`` + 8 kHz
      natively, so no transcoding is needed on either side.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "sarvam"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Union[SarvamModel, str] = SarvamModel.BULBUL_V3,
        speaker: str = "shubh",
        language: Union[SarvamLanguage, str] = SarvamLanguage.ENGLISH,
        codec: Union[SarvamAudioCodec, str] = SarvamAudioCodec.LINEAR16,
        sample_rate: Union[SarvamSampleRate, int] = SarvamSampleRate.HZ_16000,
        pace: Optional[float] = None,
        pitch: Optional[float] = None,
        loudness: Optional[float] = None,
        temperature: Optional[float] = None,
        enable_preprocessing: Optional[bool] = None,
        dict_id: Optional[str] = None,
        base_url: str = SARVAM_BASE_URL,
        session: Optional["aiohttp.ClientSession"] = None,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for SarvamTTS. "
                "Install with: pip install getpatter[sarvam]"
            )

        resolved_key = api_key or os.environ.get("SARVAM_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Sarvam API key is required, either as argument or set "
                "SARVAM_API_KEY environment variable"
            )

        self.api_key = resolved_key
        self.model = model
        self.speaker = speaker
        self.language = language
        self.codec = codec
        self.sample_rate = sample_rate
        self.pace = pace
        self.pitch = pitch
        self.loudness = loudness
        self.temperature = temperature
        self.enable_preprocessing = enable_preprocessing
        self.dict_id = dict_id
        self.base_url = base_url
        self._owns_session = session is None
        self._session = session

    def __repr__(self) -> str:
        # Never leak the API key in repr / logs.
        return (
            f"SarvamTTS(model={self.model!r}, speaker={self.speaker!r}, "
            f"language={self.language!r}, codec={self.codec!r}, "
            f"sample_rate={self.sample_rate})"
        )

    # ------------------------------------------------------------------
    # Telephony factories
    # ------------------------------------------------------------------

    @classmethod
    def for_twilio(
        cls,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "SarvamTTS":
        """Build an instance pre-configured for Twilio Media Streams.

        Emits ``mulaw`` @ 8 kHz — exactly Twilio's wire codec — so the
        pipeline takes the passthrough path: zero resampling, zero PCM ->
        mu-law encoding. Sarvam supports mu-law / 8 kHz natively, so this is a
        true carrier-native path (no resample on Sarvam's side either).
        """
        kwargs.pop("sample_rate", None)
        kwargs.pop("codec", None)
        return cls(
            api_key=api_key,
            codec=SarvamAudioCodec.MULAW,
            sample_rate=SarvamSampleRate.HZ_8000,
            **kwargs,
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "SarvamTTS":
        """Build an instance pre-configured for Telnyx bidirectional media.

        Telnyx pins the wire to PCMU/mu-law @ 8 kHz, so emitting ``mulaw`` @
        8 kHz flows end-to-end with zero resampling — the same passthrough win
        as :meth:`for_twilio`.
        """
        kwargs.pop("sample_rate", None)
        kwargs.pop("codec", None)
        return cls(
            api_key=api_key,
            codec=SarvamAudioCodec.MULAW,
            sample_rate=SarvamSampleRate.HZ_8000,
            **kwargs,
        )

    def source_audio_format(self) -> "AudioFormat":
        """Declare the audio format this adapter emits so the pipeline sender
        derives the correct resample ratio (or skips it for mu-law / a-law
        passthrough) instead of assuming a fixed 16 kHz source. See
        ``getpatter.audio.format``.
        """
        from getpatter.audio.format import AudioFormat

        codec = str(self.codec)
        if codec == SarvamAudioCodec.MULAW.value:
            return AudioFormat(encoding="mulaw", sample_rate=int(self.sample_rate))
        if codec == SarvamAudioCodec.ALAW.value:
            return AudioFormat(encoding="alaw", sample_rate=int(self.sample_rate))
        return AudioFormat(encoding="pcm_s16le", sample_rate=int(self.sample_rate))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_v2(self) -> bool:
        """True when the configured model is the legacy ``bulbul:v2``."""
        return str(self.model) == SarvamModel.BULBUL_V2.value

    def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _build_payload(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": text,
            "target_language_code": str(self.language),
            "model": str(self.model),
            "speaker": self.speaker,
            "output_audio_codec": str(self.codec),
            "speech_sample_rate": int(self.sample_rate),
        }

        if self.pace is not None:
            payload["pace"] = self.pace

        # Per-model controls: pitch / loudness / enable_preprocessing are
        # bulbul:v2-only; temperature / dict_id are bulbul:v3-only. Gate them so
        # we never POST a field the selected model rejects.
        if self._is_v2():
            if self.pitch is not None:
                payload["pitch"] = self.pitch
            if self.loudness is not None:
                payload["loudness"] = self.loudness
            if self.enable_preprocessing is not None:
                payload["enable_preprocessing"] = self.enable_preprocessing
        else:
            if self.temperature is not None:
                payload["temperature"] = self.temperature
            if self.dict_id is not None:
                payload["dict_id"] = self.dict_id

        return payload

    def _record_synthesis_cost(self, text: str) -> None:
        """Emit ``patter.cost.tts_chars`` for the synthesised text.

        NO PII: only the character COUNT is recorded, never the text itself.
        """
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.tts_chars": len(text),
                    "patter.tts.provider": "sarvam",
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_synthesis_cost failed", exc_info=True)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Synthesize ``text`` and yield decoded audio bytes.

        With the default ``codec="linear16"`` these are raw PCM_S16LE samples
        at ``sample_rate``. The REST endpoint returns the whole clip in one
        JSON response (``audios`` is a list of base64 strings); we decode and
        yield each entry in order.
        """
        self._record_synthesis_cost(text)
        session = self._ensure_session()

        headers = {
            "Content-Type": "application/json",
            "api-subscription-key": self.api_key,
        }

        async with session.post(
            self.base_url,
            headers=headers,
            json=self._build_payload(text),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Sarvam TTS error {resp.status}: {body[:500]}")
            data = await resp.json()
            for audio_b64 in _iter_audio_b64(data):
                try:
                    decoded = base64.b64decode(audio_b64)
                except (ValueError, TypeError):
                    continue
                if decoded:
                    yield decoded

    async def warmup(self) -> None:
        """Pre-call HTTP warmup for the Sarvam TTS API.

        Issues a lightweight ``GET`` against the API origin so DNS + TLS +
        HTTP/2 are already up by the time the first :meth:`synthesize` POST
        lands. Best-effort: 5 s timeout, all exceptions swallowed at DEBUG.

        Billing safety: Sarvam does not document a free voice/metadata GET, so
        this only primes the connection against the API origin root — it never
        hits the synthesis endpoint, so no characters are billed. The actual
        synthesis is billed only when ``POST /text-to-speech`` runs with
        non-empty ``text``.

        Note: like the Cartesia / Inworld adapters this uses the HTTP path
        (Sarvam also exposes a WebSocket transport we don't use here), so the
        latency win is the HTTP connection prime (~50-150 ms) rather than a WS
        pre-handshake.
        """
        try:
            session = self._ensure_session()
            parts = urlsplit(self.base_url)
            origin = f"{parts.scheme}://{parts.netloc}/"
            headers = {"api-subscription-key": self.api_key}
            async with session.get(
                origin,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                # Drain so the underlying connection returns cleanly to the pool.
                await resp.read()
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("Sarvam TTS warmup failed (best-effort): %s", exc)

    async def close(self) -> None:
        """Close the underlying session (idempotent)."""
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None


def _iter_audio_b64(data: Any) -> list[str]:
    """Extract the base64 audio strings from a Sarvam REST response.

    Accepts the documented ``{"audios": [...]}`` shape and is defensive about a
    single-string ``audios`` value. Returns ``[]`` for anything unexpected so
    the caller yields nothing rather than raising on a malformed body.
    """
    if not isinstance(data, dict):
        return []
    audios = data.get("audios")
    if isinstance(audios, str):
        return [audios] if audios else []
    if isinstance(audios, list):
        return [a for a in audios if isinstance(a, str) and a]
    return []
