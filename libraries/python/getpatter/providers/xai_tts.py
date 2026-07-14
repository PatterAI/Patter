"""xAI Text-to-Speech for the Patter SDK — REST one-shot bytes endpoint.

Beta: validated against the xAI TTS API spec (docs.x.ai); not yet exercised
against a live phone call.

Targets ``POST https://api.x.ai/v1/tts`` which returns raw audio bytes in the
requested codec. This maps cleanly onto Patter's
``TTSProvider.synthesize(text) -> AsyncIterator[bytes]`` contract and needs no
vendor SDK — only ``aiohttp`` (mirrors :class:`SonioxTTS`). xAI also exposes a
bidirectional WebSocket streaming variant; that is out of scope for v1 (a future
``xai_ws_tts`` provider, mirroring ``elevenlabs_ws``, could add it).

Credential: ``XAI_API_KEY`` env var / ``api_key`` argument. REST auth is an
``Authorization: Bearer <key>`` header.

Telephony: xAI natively supports G.711 mu-law @ 8 kHz — request ``codec="mulaw"``
+ ``sample_rate=8000`` (the :meth:`for_twilio` / :meth:`for_telnyx` factories do
this) to emit carrier-native audio with NO resampling, exactly matching Patter's
mu-law / 8 kHz wire format.
"""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, ClassVar, Optional

from getpatter.providers.base import TTSProvider

logger = logging.getLogger("getpatter.providers.xai_tts")

# Lazy import: aiohttp is declared as an optional dep for this provider
# (shared ``[xai]`` extra with the xAI STT adapter).
try:  # pragma: no cover - trivial import guard
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

# REST one-shot text-to-speech endpoint. Returns raw audio bytes.
XAI_TTS_REST_URL = "https://api.x.ai/v1/tts"
# Custom-voice creation endpoint (multipart upload of a reference clip).
XAI_CUSTOM_VOICES_URL = "https://api.x.ai/v1/custom-voices"

# xAI's default voice. The full 26-voice roster is shared with the Voice Agent
# API; ``eve`` is the documented default.
XAI_DEFAULT_VOICE = "eve"

# Pipeline-friendly defaults when the caller leaves codec / sample_rate unset:
# raw PCM-16-LE @ 16 kHz (NOT xAI's own mp3 default, which the pipeline can't
# resample). Telephony uses the carrier factories below instead.
XAI_DEFAULT_CODEC = "pcm"
XAI_DEFAULT_SAMPLE_RATE = 16000

# Maximum text length accepted by a single REST request (xAI limit).
XAI_TTS_MAX_CHARS = 15000


class XaiTTS(TTSProvider):
    """xAI TTS over the HTTP one-shot ``/tts`` bytes endpoint (Beta).

    Default output is raw PCM-16-LE @ 16 kHz so it drops into Patter's pipeline
    without transcoding. Default voice is ``eve`` and default ``language`` is
    ``"auto"`` (xAI auto-detects the language of the text).

    Telephony optimization
    ----------------------
    The constructor default ``codec="pcm"`` / ``sample_rate=16000`` is correct
    for web playback and 16 kHz pipelines. For real phone calls use the carrier
    factories:

    * :meth:`for_twilio` — emits ``mulaw`` @ 8 kHz, Twilio's exact wire codec,
      so the pipeline skips resampling AND PCM -> mu-law encoding (bit-clean
      passthrough). The sender reads the declared output format via
      :meth:`source_audio_format`.
    * :meth:`for_telnyx` — emits ``pcm`` @ 16 kHz for the Telnyx PCM16 pipeline.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "xai_tts"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        voice: str = XAI_DEFAULT_VOICE,
        language: str = "auto",
        speed: Optional[float] = None,
        optimize_streaming_latency: Optional[int] = None,
        text_normalization: bool = False,
        codec: Optional[str] = None,
        sample_rate: Optional[int] = None,
        bit_rate: Optional[int] = None,
        base_url: str = XAI_TTS_REST_URL,
        session: Optional["aiohttp.ClientSession"] = None,
    ) -> None:
        if aiohttp is None:
            raise ImportError(
                "aiohttp is required for XaiTTS. "
                "Install with: pip install getpatter[xai]"
            )
        resolved_key = api_key or os.environ.get("XAI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "xAI TTS requires an api_key. Pass api_key='...' or set "
                "XAI_API_KEY in the environment."
            )

        self.api_key = resolved_key
        self.voice = voice
        self.language = language
        self.speed = speed
        self.optimize_streaming_latency = optimize_streaming_latency
        self.text_normalization = text_normalization
        self.codec = codec or XAI_DEFAULT_CODEC
        self.sample_rate = sample_rate or XAI_DEFAULT_SAMPLE_RATE
        # MP3-only bit rate (bits/sec); nested under ``output_format`` when set.
        self.bit_rate = bit_rate
        self.base_url = base_url
        self._owns_session = session is None
        self._session = session

    def __repr__(self) -> str:
        # Never leak the API key in repr / logs.
        return (
            f"XaiTTS(voice={self.voice!r}, language={self.language!r}, "
            f"codec={self.codec!r}, sample_rate={self.sample_rate})"
        )

    # ------------------------------------------------------------------
    # Telephony factories
    # ------------------------------------------------------------------

    @classmethod
    def for_twilio(
        cls,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "XaiTTS":
        """Build an instance pre-configured for Twilio Media Streams.

        Emits ``mulaw`` @ 8 kHz — exactly Twilio's wire codec — so the pipeline
        takes the passthrough path: zero resampling, zero PCM -> mu-law
        encoding, bit-clean audio.
        """
        kwargs.pop("codec", None)
        kwargs.pop("sample_rate", None)
        return cls(api_key=api_key, codec="mulaw", sample_rate=8000, **kwargs)

    @classmethod
    def for_telnyx(
        cls,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "XaiTTS":
        """Build an instance pre-configured for Telnyx bidirectional media.

        Emits ``pcm`` @ 16 kHz to match the Telnyx PCM16 pipeline; the SDK
        resamples to the 8 kHz PCMU wire once, downstream.
        """
        kwargs.pop("codec", None)
        kwargs.pop("sample_rate", None)
        return cls(api_key=api_key, codec="pcm", sample_rate=16000, **kwargs)

    def source_audio_format(self) -> "AudioFormat":
        """Declare the audio format this adapter emits so the pipeline sender
        derives the correct resample ratio (or skips it for mu-law passthrough)
        instead of assuming a fixed 16 kHz source. See ``getpatter.audio.format``.
        """
        from getpatter.audio.format import AudioFormat

        if self.codec == "mulaw":
            return AudioFormat(encoding="mulaw", sample_rate=int(self.sample_rate))
        if self.codec == "alaw":
            return AudioFormat(encoding="alaw", sample_rate=int(self.sample_rate))
        return AudioFormat(encoding="pcm_s16le", sample_rate=int(self.sample_rate))

    def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    def _build_payload(self, text: str) -> dict[str, Any]:
        output_format: dict[str, Any] = {
            "codec": self.codec,
            "sample_rate": int(self.sample_rate),
        }
        if self.bit_rate is not None:
            output_format["bit_rate"] = self.bit_rate
        payload: dict[str, Any] = {
            "text": text,
            "voice_id": self.voice,
            "language": self.language,
            "output_format": output_format,
        }
        if self.speed is not None:
            payload["speed"] = self.speed
        if self.optimize_streaming_latency is not None:
            payload["optimize_streaming_latency"] = self.optimize_streaming_latency
        if self.text_normalization:
            payload["text_normalization"] = True
        return payload

    def _record_synthesis_cost(self, text: str) -> None:
        """Emit ``patter.cost.tts_chars`` for the synthesised text."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.tts_chars": len(text),
                    "patter.tts.provider": "xai_tts",
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_synthesis_cost failed", exc_info=True)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream raw audio bytes for ``text`` over HTTP.

        With the default ``codec="pcm"`` these are raw PCM-16-LE chunks at the
        configured ``sample_rate``; with ``mulaw`` (see :meth:`for_twilio`) they
        are carrier-native G.711 mu-law bytes.
        """
        if len(text) > XAI_TTS_MAX_CHARS:
            # xAI rejects a single request over 15,000 chars. Splitting text is
            # not this adapter's job (the pipeline synthesises per-utterance),
            # so warn and send as-is — the API error is surfaced below.
            logger.warning(
                "xAI TTS text is %d chars, over the %d-char per-request limit; "
                "the request will likely be rejected.",
                len(text),
                XAI_TTS_MAX_CHARS,
            )
        self._record_synthesis_cost(text)
        session = self._ensure_session()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with session.post(
            self.base_url,
            headers=headers,
            json=self._build_payload(text),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"xAI TTS error {resp.status}: {body[:500]}")
            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    yield chunk

    async def close(self) -> None:
        """Close the underlying session (idempotent)."""
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None


async def create_custom_voice(
    name: str,
    language: str,
    audio: bytes,
    *,
    api_key: str | None = None,
    filename: str = "reference.wav",
) -> str:
    """Create an xAI custom voice from a short reference clip (Beta).

    POSTs multipart to ``https://api.x.ai/v1/custom-voices`` with the scalar
    ``name`` / ``language`` fields first and the ``file`` (audio/wav, clip
    ≤120 s) LAST — the xAI multipart requirement. Returns the resulting
    ``voice_id`` string, usable as ``voice`` on both :class:`XaiTTS` and the
    xAI Voice Agent engine.

    Args:
        name: Display name for the custom voice.
        language: BCP-47 language code of the reference clip.
        audio: WAV audio bytes of the reference clip (≤120 s).
        api_key: xAI API key. Falls back to ``XAI_API_KEY``.
        filename: Multipart filename for the uploaded ``file`` part.
    """
    if aiohttp is None:
        raise ImportError(
            "aiohttp is required for xAI create_custom_voice(). "
            "Install with: pip install getpatter[xai]"
        )
    resolved_key = api_key or os.environ.get("XAI_API_KEY")
    if not resolved_key:
        raise ValueError(
            "xAI create_custom_voice() requires an api_key. Pass api_key='...' "
            "or set XAI_API_KEY in the environment."
        )

    form = aiohttp.FormData()
    form.add_field("name", name)
    form.add_field("language", language)
    # ``file`` LAST — xAI requires the binary part to be the final field.
    form.add_field("file", audio, filename=filename, content_type="audio/wav")

    headers = {"Authorization": f"Bearer {resolved_key}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            XAI_CUSTOM_VOICES_URL,
            headers=headers,
            data=form,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"xAI custom-voice error {resp.status}: {body[:500]}"
                )
            payload = await resp.json()
    return payload.get("voice_id", "")
