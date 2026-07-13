"""xAI (Grok) TTS provider — WebSocket streaming with an HTTP fallback.

The default transport targets the bidirectional streaming endpoint
(``wss://api.x.ai/v1/tts``): the adapter opens a WebSocket per utterance,
sends the text as ``text.delta`` + ``text.done`` and yields the decoded
``audio.delta`` chunks as they arrive — the lowest time-to-first-audio path,
mirroring how :class:`~getpatter.providers.elevenlabs_ws_tts.ElevenLabsWebSocketTTS`
uses ElevenLabs' stream-input endpoint.

``transport="http"`` switches to the unary ``POST https://api.x.ai/v1/tts``
endpoint (same parameters as JSON body fields, raw audio bytes streamed in
the response) for environments where WebSockets are unavailable.

Notes (https://docs.x.ai/developers/model-capabilities/audio/text-to-speech):

* 25+ built-in voices (see :class:`XAIVoice`; ``eve`` is the default) plus
  custom voice IDs cloned via ``POST /v1/custom-voices`` — any custom
  ``voice_id`` works wherever a built-in voice does.
* Inline speech tags (``[pause]``, ``[laugh]``, ``<whisper>…</whisper>``, …)
  pass through in the text.
* Output codecs: ``mp3`` / ``wav`` / ``pcm`` / ``mulaw`` / ``alaw``. The
  G.711 codecs are carrier-native at 8 kHz — see :meth:`XAITTS.for_twilio`.
* ``language`` is required by the API; ``"auto"`` enables automatic
  language detection.

Pricing: $15.00 per 1M input characters ($0.015/1k). See
``DEFAULT_PRICING["xai_tts"]``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, AsyncIterator, ClassVar, Literal, Optional, Union
from urllib.parse import urlencode

import websockets

from getpatter.providers.base import TTSProvider

logger = logging.getLogger("getpatter.providers.xai_tts")

XAI_TTS_HTTP_URL = "https://api.x.ai/v1/tts"
XAI_TTS_WS_URL = "wss://api.x.ai/v1/tts"
XAI_TTS_VOICES_URL = "https://api.x.ai/v1/tts/voices"

# Connect timeout — 5s keeps the carrier WebSocket from sitting in dead air
# while a stuck DNS / TLS handshake is retried (parity with the ElevenLabs
# WS adapter).
DEFAULT_OPEN_TIMEOUT = 5.0

# Per-frame receive timeout. If the server stalls (no audio, no audio.done,
# no close) the generator would otherwise hang until carrier hangup.
DEFAULT_FRAME_TIMEOUT = 30.0

# Maximum size of a single base64 audio frame from the server — anything
# beyond ~512 KB is almost certainly a malformed payload trying to OOM us.
MAX_AUDIO_B64_SIZE = 512 * 1024

# xAI caps a single TTS request / text.delta at 15,000 characters.
MAX_TEXT_LEN = 15_000


class XAIVoice(StrEnum):
    """Built-in xAI voice IDs (case-insensitive on the wire; ``eve`` is the
    default). The same roster works across TTS and the Voice Agent API —
    fetch it programmatically via :meth:`XAITTS.list_voices`."""

    ARA = "ara"
    EVE = "eve"
    LEO = "leo"
    REX = "rex"
    SAL = "sal"
    ALTAIR = "altair"
    ATLAS = "atlas"
    CARINA = "carina"
    CASTOR = "castor"
    CELESTE = "celeste"
    COSMO = "cosmo"
    HELIOS = "helios"
    HELIX = "helix"
    IRIS = "iris"
    KEPLER = "kepler"
    LUMEN = "lumen"
    LUNA = "luna"
    LUX = "lux"
    NAKSH = "naksh"
    ORION = "orion"
    PERSEUS = "perseus"
    RIGEL = "rigel"
    SIRIUS = "sirius"
    URSA = "ursa"
    ZAGAN = "zagan"
    ZENITH = "zenith"


class XAITTSCodec(StrEnum):
    """Output codecs accepted by xAI TTS. ``mulaw`` / ``alaw`` @ 8 kHz are
    the carrier-native G.711 codecs (telephony passthrough)."""

    MP3 = "mp3"
    WAV = "wav"
    PCM = "pcm"
    MULAW = "mulaw"
    ALAW = "alaw"


class XAITTSSampleRate(IntEnum):
    """Sample rates (Hz) accepted by xAI TTS."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_22050 = 22050
    HZ_24000 = 24000
    HZ_44100 = 44100
    HZ_48000 = 48000


class XAITTSError(Exception):
    """Raised when the xAI TTS endpoint reports a server-side error."""


@dataclass(frozen=True)
class AudioFormat:
    """Source audio format descriptor consumed by the pipeline sender."""

    encoding: str
    sample_rate: int


class XAITTS(TTSProvider):
    """xAI (Grok) TTS adapter — WebSocket streaming by default.

    Drop-in :class:`~getpatter.providers.base.TTSProvider`: ``synthesize``
    yields raw audio bytes in the configured ``codec`` / ``sample_rate``.
    """

    # Stable provider key for pricing / metrics lookup. Read by
    # ``stream_handler`` via ``getattr(type(agent.tts), "provider_key", None)``.
    provider_key: ClassVar[str] = "xai_tts"

    def __init__(
        self,
        api_key: str,
        voice: Union[XAIVoice, str] = XAIVoice.EVE,
        language: str = "en",
        codec: Union[XAITTSCodec, str] = XAITTSCodec.PCM,
        sample_rate: Union[XAITTSSampleRate, int] = XAITTSSampleRate.HZ_16000,
        *,
        bit_rate: int | None = None,
        speed: float | None = None,
        text_normalization: bool = False,
        optimize_streaming_latency: int = 1,
        transport: Literal["websocket", "http"] = "websocket",
        open_timeout: float = DEFAULT_OPEN_TIMEOUT,
        frame_timeout: float = DEFAULT_FRAME_TIMEOUT,
    ):
        # ``optimize_streaming_latency`` defaults to 1 (reduced first-chunk
        # size) because this adapter feeds live calls where time-to-first-
        # audio dominates perceived latency; the quality tradeoff at chunk
        # boundaries is inaudible on the telephony codecs. Pass 0 to opt
        # back into the best-quality chunking for offline synthesis.
        #
        # ``voice`` accepts any built-in :class:`XAIVoice` or a custom voice
        # ID from ``POST /v1/custom-voices``.
        if speed is not None and not 0.7 <= speed <= 1.5:
            raise ValueError(f"speed must be in [0.7, 1.5], got {speed}")
        if transport not in ("websocket", "http"):
            raise ValueError(
                f"transport must be 'websocket' or 'http', got {transport!r}"
            )
        # Stored privately so the key is not surfaced via ``vars(tts)`` or
        # accidental log serialisation.
        self._api_key = api_key
        self.voice = str(voice)
        self.language = language
        self.codec = codec
        self.sample_rate = sample_rate
        self.bit_rate = bit_rate
        self.speed = speed
        self.text_normalization = text_normalization
        self.optimize_streaming_latency = optimize_streaming_latency
        self.transport = transport
        self.open_timeout = open_timeout
        self.frame_timeout = frame_timeout
        # Track whether the caller explicitly chose a codec/sample-rate so
        # ``set_telephony_carrier`` only auto-flips unconfigured instances
        # (parity with the ElevenLabs WS adapter).
        self._format_explicit = False
        # Reference to the in-flight synthesis WS so ``cancel_active_stream``
        # can force-close it from outside the generator (barge-in path).
        self._active_stream_ws: Any = None
        self._stream_loop: Any = None

    @property
    def api_key(self) -> str:
        """Return the configured API key (property so introspection of
        ``vars(tts)`` does not surface the secret)."""
        return self._api_key

    def __repr__(self) -> str:
        return (
            f"XAITTS(voice={self.voice!r}, language={self.language!r}, "
            f"codec={self.codec!r}, sample_rate={self.sample_rate}, "
            f"transport={self.transport!r})"
        )

    # ------------------------------------------------------------------
    # Telephony factories
    # ------------------------------------------------------------------

    @classmethod
    def for_twilio(
        cls,
        api_key: str,
        *,
        voice: Union[XAIVoice, str] = XAIVoice.EVE,
        language: str = "en",
        **kwargs,
    ) -> "XAITTS":
        """xAI TTS pre-configured for Twilio Media Streams.

        Emits G.711 μ-law @ 8 kHz — exactly Twilio's wire codec — so the
        pipeline skips resampling and PCM → μ-law encoding entirely
        (bit-clean passthrough).
        """
        kwargs.pop("codec", None)
        kwargs.pop("sample_rate", None)
        instance = cls(
            api_key=api_key,
            voice=voice,
            language=language,
            codec=XAITTSCodec.MULAW,
            sample_rate=XAITTSSampleRate.HZ_8000,
            **kwargs,
        )
        instance._format_explicit = True
        return instance

    @classmethod
    def for_telnyx(
        cls,
        api_key: str,
        *,
        voice: Union[XAIVoice, str] = XAIVoice.EVE,
        language: str = "en",
        **kwargs,
    ) -> "XAITTS":
        """xAI TTS pre-configured for Telnyx (PCMU/μ-law @ 8 kHz wire)."""
        kwargs.pop("codec", None)
        kwargs.pop("sample_rate", None)
        instance = cls(
            api_key=api_key,
            voice=voice,
            language=language,
            codec=XAITTSCodec.MULAW,
            sample_rate=XAITTSSampleRate.HZ_8000,
            **kwargs,
        )
        instance._format_explicit = True
        return instance

    # ------------------------------------------------------------------
    # Carrier auto-detect — wired by StreamHandler.start()
    # ------------------------------------------------------------------

    # All three supported carriers speak PCMU/μ-law @ 8 kHz on the wire;
    # xAI synthesises μ-law natively so the pipeline gets zero-transcode
    # delivery.
    _CARRIER_NATIVE: dict[str, tuple[XAITTSCodec, XAITTSSampleRate]] = {
        "twilio": (XAITTSCodec.MULAW, XAITTSSampleRate.HZ_8000),
        "telnyx": (XAITTSCodec.MULAW, XAITTSSampleRate.HZ_8000),
        "plivo": (XAITTSCodec.MULAW, XAITTSSampleRate.HZ_8000),
    }

    def set_telephony_carrier(self, carrier: str) -> None:
        """Hook called by ``StreamHandler`` to advise the carrier wire format.

        Flips unconfigured instances to the carrier's native codec (saving a
        client-side transcode). No-op when the user pinned the format via the
        constructor factories or an unknown carrier is supplied.
        """
        if self._format_explicit:
            return
        native = self._CARRIER_NATIVE.get(carrier)
        if native is None:
            return
        self.codec, self.sample_rate = native

    def source_audio_format(self) -> AudioFormat:
        """Declare the audio format this adapter emits so the pipeline
        sender can derive the correct resample ratio (or skip it for μ-law
        passthrough)."""
        if self.codec == XAITTSCodec.MULAW:
            return AudioFormat(encoding="mulaw", sample_rate=int(self.sample_rate))
        if self.codec == XAITTSCodec.ALAW:
            return AudioFormat(encoding="alaw", sample_rate=int(self.sample_rate))
        return AudioFormat(encoding="pcm_s16le", sample_rate=int(self.sample_rate))

    # ------------------------------------------------------------------
    # Voice listing
    # ------------------------------------------------------------------

    @classmethod
    async def list_voices(cls, api_key: str) -> list[dict[str, Any]]:
        """Fetch the built-in voice roster from ``GET /v1/tts/voices``.

        Returns the raw voice dicts (``voice_id`` / ``name`` / ``language``).
        Custom voices are returned by ``GET /v1/custom-voices`` only and do
        not appear in this list.
        """
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                XAI_TTS_VOICES_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            return resp.json().get("voices", [])

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    def _build_ws_url(self) -> str:
        params: dict[str, str] = {
            "voice": self.voice,
            "language": self.language,
            "codec": str(self.codec),
            "sample_rate": str(int(self.sample_rate)),
            "optimize_streaming_latency": str(self.optimize_streaming_latency),
        }
        if self.bit_rate is not None:
            params["bit_rate"] = str(self.bit_rate)
        if self.speed is not None:
            params["speed"] = str(self.speed)
        if self.text_normalization:
            params["text_normalization"] = "true"
        return f"{XAI_TTS_WS_URL}?{urlencode(params)}"

    def _build_http_payload(self, text: str) -> dict[str, Any]:
        output_format: dict[str, Any] = {
            "codec": str(self.codec),
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

    def cancel_active_stream(self) -> None:
        """Force-close the currently open synthesis WebSocket (if any).

        Called by ``StreamHandler`` on barge-in / stop / ws-close to unblock
        the in-flight ``synthesize`` generator's ``await ws.recv()``
        immediately instead of waiting up to ``frame_timeout``. No-op when
        no synthesis is in progress. Parity with
        ``ElevenLabsWebSocketTTS.cancel_active_stream``.
        """
        ws = self._active_stream_ws
        if ws is None:
            return
        self._active_stream_ws = None
        loop = self._stream_loop
        self._stream_loop = None
        try:

            def _schedule_close() -> None:
                task = asyncio.get_running_loop().create_task(ws.close())
                task.add_done_callback(_log_close_failure)

            def _log_close_failure(task: asyncio.Task) -> None:
                if task.cancelled():
                    return
                exc = task.exception()
                if exc is not None:
                    logger.debug("xAI TTS WS close failed: %s", exc)

            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(_schedule_close)
            else:
                asyncio.run(ws.close())
        except Exception:
            pass  # defensive — socket may already be closed

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Synthesize *text*, yielding raw audio bytes as they arrive.

        WebSocket transport (default): opens a per-utterance connection,
        sends ``text.delta`` + ``text.done`` and yields decoded
        ``audio.delta`` chunks until ``audio.done``. HTTP transport: streams
        the unary ``POST /v1/tts`` response body.
        """
        if len(text) > MAX_TEXT_LEN:
            raise ValueError(
                f"xAI TTS accepts at most {MAX_TEXT_LEN} characters per "
                f"request, got {len(text)}."
            )
        self._record_synthesis_cost(text)
        if self.transport == "http":
            async for chunk in self._synthesize_http(text):
                yield chunk
            return
        async for chunk in self._synthesize_ws(text):
            yield chunk

    async def _synthesize_http(self, text: str) -> AsyncIterator[bytes]:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                XAI_TTS_HTTP_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=self._build_http_payload(text),
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise XAITTSError(
                        f"xAI TTS error {resp.status_code}: {body[:500]!r}"
                    )
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk

    async def _synthesize_ws(self, text: str) -> AsyncIterator[bytes]:
        ws = await asyncio.wait_for(
            websockets.connect(
                self._build_ws_url(),
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
                open_timeout=self.open_timeout,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=2,
            ),
            timeout=self.open_timeout,
        )
        try:
            await ws.send(json.dumps({"type": "text.delta", "delta": text}))
            await ws.send(json.dumps({"type": "text.done"}))

            from websockets.exceptions import ConnectionClosedOK

            # Expose the in-flight WS so ``cancel_active_stream`` can
            # force-close it and unblock the ``recv()`` below.
            self._active_stream_ws = ws
            self._stream_loop = asyncio.get_running_loop()

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.frame_timeout)
                except (TimeoutError, asyncio.TimeoutError) as exc:
                    raise XAITTSError(
                        f"xAI TTS WS no frame for {self.frame_timeout}s"
                    ) from exc
                except ConnectionClosedOK:
                    # Server closed cleanly — treat as end-of-stream.
                    return

                if isinstance(raw, bytes):
                    # The documented protocol is JSON text frames; tolerate
                    # binary audio frames defensively.
                    if len(raw) > MAX_AUDIO_B64_SIZE:
                        logger.warning(
                            "xAI TTS binary frame too large (%d bytes), skipping",
                            len(raw),
                        )
                        continue
                    yield raw
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("xAI TTS WS sent non-JSON text frame")
                    continue

                msg_type = msg.get("type", "")
                if msg_type == "error":
                    message = str(msg.get("message", "unknown xAI error"))
                    raise XAITTSError(
                        f"xAI TTS WS reported error: {message[:200]}"
                    )
                if msg_type == "audio.delta":
                    audio_b64 = msg.get("delta")
                    if (
                        not isinstance(audio_b64, str)
                        or len(audio_b64) > MAX_AUDIO_B64_SIZE
                    ):
                        logger.warning(
                            "xAI TTS WS audio frame too large or malformed, skipping"
                        )
                        continue
                    try:
                        yield base64.b64decode(audio_b64)
                    except (ValueError, TypeError):
                        logger.warning("xAI TTS WS sent malformed base64 audio")
                elif msg_type == "audio.done":
                    return
        finally:
            if self._active_stream_ws is ws:
                self._active_stream_ws = None
                self._stream_loop = None
            try:
                await ws.close()
            except Exception:
                pass

    async def warmup(self) -> None:
        """Pre-call WebSocket warmup for the xAI ``/v1/tts`` endpoint.

        Opens the WS (DNS + TLS + auth handshake), idles ~250 ms so the
        provider edge keeps session state warm, then closes. By the time the
        first :meth:`synthesize` call lands the connection pool is hot — net
        wire time saving of 200-500 ms.

        Billing safety: xAI TTS bills per input character
        (https://docs.x.ai/developers/pricing). Opening + closing the socket
        without sending any ``text.delta`` does not consume billable
        characters. Best-effort: failures are logged at DEBUG.
        """
        if self.transport != "websocket":
            return
        ws = None
        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    self._build_ws_url(),
                    additional_headers={"Authorization": f"Bearer {self._api_key}"},
                    open_timeout=self.open_timeout,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=2,
                ),
                timeout=self.open_timeout,
            )
            await asyncio.sleep(0.25)
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("xAI TTS warmup failed (best-effort): %s", exc)
        finally:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

    async def close(self) -> None:
        """No-op: connections are per-utterance and closed inline."""
        return None


async def create_custom_voice(
    api_key: str,
    file: bytes,
    *,
    filename: str = "reference.wav",
    content_type: str = "audio/wav",
    name: str | None = None,
    description: str | None = None,
    gender: str | None = None,
    accent: str | None = None,
    age: str | None = None,
    language: str | None = None,
    use_case: str | None = None,
    tone: str | None = None,
) -> dict[str, Any]:
    """Clone a voice from a reference clip via ``POST /v1/custom-voices``.

    ``file`` is the reference audio (max 120 s; 90+ s recommended, WAV
    24 kHz mono preferred). Returns the created voice object — its
    ``voice_id`` works as ``voice`` on :class:`XAITTS` and
    :class:`~getpatter.providers.xai_realtime.XAIRealtimeAdapter` exactly
    like a built-in voice.

    Note: the API endpoint is gated to xAI Enterprise plans; voices can
    also be created for free in the xAI console.
    """
    import httpx

    data: dict[str, str] = {}
    for key, value in (
        ("name", name),
        ("description", description),
        ("gender", gender),
        ("accent", accent),
        ("age", age),
        ("language", language),
        ("use_case", use_case),
        ("tone", tone),
    ):
        if value is not None:
            data[key] = value
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.x.ai/v1/custom-voices",
            headers={"Authorization": f"Bearer {api_key}"},
            data=data,
            files={"file": (filename, file, content_type)},
        )
        resp.raise_for_status()
        return resp.json()


async def list_custom_voices(
    api_key: str,
    *,
    limit: int = 100,
    pagination_token: str | None = None,
) -> dict[str, Any]:
    """List the team's cloned voices via ``GET /v1/custom-voices``.

    Returns the raw response (``voices`` list plus an optional
    ``pagination_token`` for the next page).
    """
    import httpx

    params: dict[str, str] = {"limit": str(limit)}
    if pagination_token:
        params["pagination_token"] = pagination_token
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://api.x.ai/v1/custom-voices",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
        resp.raise_for_status()
        return resp.json()
