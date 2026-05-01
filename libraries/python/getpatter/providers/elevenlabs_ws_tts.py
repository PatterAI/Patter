"""WebSocket-based ElevenLabs TTS provider — opt-in low-latency variant.

This adapter targets the ElevenLabs streaming-input WebSocket endpoint
(``/v1/text-to-speech/{voice_id}/stream-input``) instead of the HTTP
``/stream`` endpoint used by :class:`ElevenLabsTTS`. The WebSocket path
saves the HTTP request setup time on each utterance (~50 ms per request)
and avoids the HTTP cold-start TLS handshake when calls are bursty.

Usage matches :class:`ElevenLabsTTS` exactly:

.. code-block:: python

    from getpatter.providers.elevenlabs_ws_tts import ElevenLabsWebSocketTTS

    tts = ElevenLabsWebSocketTTS.for_twilio(api_key=...)
    async for audio in tts.synthesize("Hello world"):
        ...

Behaviour notes
---------------
* The endpoint is opened **per-utterance** (matching the HTTP semantics).
  A future revision may introduce a pooled WS shared across utterances of
  the same call session — see roadmap Phase 5b.
* ``auto_mode=true`` is enabled by default — ElevenLabs handles internal
  chunk scheduling so we don't need to override
  ``chunk_length_schedule``. Pass ``auto_mode=False`` to take manual
  control. See https://elevenlabs.io/docs/eleven-api/guides/how-to/best-practices/latency-optimization
* ``output_format`` is exposed as a query parameter so ``ulaw_8000``
  (Twilio native) and ``pcm_16000`` (Telnyx native) both work without
  client-side resampling.
* ``eleven_v3`` is **not** supported by this WS endpoint — use the HTTP
  :class:`ElevenLabsTTS` for v3.
* ``optimize_streaming_latency`` is officially deprecated by ElevenLabs
  and is not exposed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from enum import StrEnum
from typing import AsyncGenerator, Optional, Union
from urllib.parse import quote, urlencode

try:
    import websockets
except ImportError as e:  # pragma: no cover - websockets is in main deps
    raise ImportError(
        "websockets is required for ElevenLabsWebSocketTTS — it is already "
        "a runtime dependency of getpatter, so this should never happen."
    ) from e

from getpatter.providers.base import TTSProvider
from getpatter.providers.elevenlabs_tts import (
    ElevenLabsModel,
    ElevenLabsOutputFormat,
    resolve_voice_id,
)

logger = logging.getLogger("getpatter")


class ElevenLabsWSServerError(StrEnum):
    """Error string values reported by the ElevenLabs streaming-input WS."""

    PAYMENT_REQUIRED = "payment_required"


class ElevenLabsWSField(StrEnum):
    """Outbound JSON field names used in the streaming-input WS protocol."""

    TEXT = "text"
    FLUSH = "flush"
    VOICE_SETTINGS = "voice_settings"
    GENERATION_CONFIG = "generation_config"
    CHUNK_LENGTH_SCHEDULE = "chunk_length_schedule"


_WS_BASE = "wss://api.elevenlabs.io/v1/text-to-speech"

# ElevenLabs documents a 20s default WebSocket inactivity timeout (extendable
# to 180s via ``inactivity_timeout``); we go higher than the default since
# voice agents may pause for tool-call latency.
DEFAULT_INACTIVITY_TIMEOUT = 60

# Connect timeout — 5s keeps the carrier WebSocket from sitting in dead air
# while a stuck DNS / TLS handshake is retried. The previous 15s default was
# long enough for a caller to notice the silence.
DEFAULT_OPEN_TIMEOUT = 5.0

# Per-frame receive timeout. If the server stalls (no audio, no isFinal,
# no close) the generator would otherwise hang until carrier hangup.
DEFAULT_FRAME_TIMEOUT = 30.0

# Maximum size of a single base64 audio frame from the server. Real frames
# are at most ~75 KB decoded (~100 KB base64) — anything beyond ~512 KB is
# almost certainly a malicious / malformed payload trying to OOM us.
MAX_AUDIO_B64_SIZE = 512 * 1024


class ElevenLabsTTSError(Exception):
    """Raised when the ElevenLabs WebSocket reports a server-side error."""


class ElevenLabsPlanError(ElevenLabsTTSError):
    """Raised when the WS endpoint refuses synthesis because the account
    plan does not include WS streaming.

    Free / Starter plans get ``payment_required`` from the server on the
    first synthesise call. The HTTP :class:`ElevenLabsTTS` class works on
    every plan, so the simplest fix is to swap the import:

    .. code-block:: python

        # before — fails on Free / Starter:
        from getpatter import ElevenLabsWebSocketTTS as TTS
        # after:
        from getpatter import ElevenLabsTTS as TTS
    """


_PLAN_REQUIRED_MSG = (
    "ElevenLabs WS streaming requires a Pro plan or higher (the WS endpoint "
    "returned `payment_required`). Either upgrade at "
    "https://elevenlabs.io/pricing, or use the HTTP `ElevenLabsTTS` class "
    "which works on all plans (drop-in API)."
)


def _sanitise_log_str(value: object, *, limit: int = 200) -> str:
    """Render an untrusted server-supplied string for safe single-line logging.

    Strips CR / LF / NUL so a malicious or buggy server cannot inject fake
    log lines, then truncates so a multi-MB error string cannot fill disk.
    """
    text = str(value)
    return text.replace("\r", " ").replace("\n", " ").replace("\x00", " ")[:limit]


class ElevenLabsWebSocketTTS(TTSProvider):
    """ElevenLabs streaming TTS via WebSocket (``/stream-input`` endpoint).

    Drop-in replacement for :class:`ElevenLabsTTS` with WebSocket transport
    instead of HTTP. Emits raw audio bytes in the requested ``output_format``
    via the ``synthesize`` async iterator, identically to the HTTP variant.
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",
        model_id: Union[ElevenLabsModel, str] = ElevenLabsModel.FLASH_V2_5,
        output_format: Optional[Union[ElevenLabsOutputFormat, str]] = None,
        voice_settings: Optional[dict] = None,
        language_code: Optional[str] = None,
        *,
        auto_mode: bool = True,
        inactivity_timeout: int = DEFAULT_INACTIVITY_TIMEOUT,
        chunk_length_schedule: Optional[list[int]] = None,
        open_timeout: float = DEFAULT_OPEN_TIMEOUT,
        frame_timeout: float = DEFAULT_FRAME_TIMEOUT,
    ):
        # Reject every variant of v3 (eleven_v3, eleven_v3_preview, …) — the
        # WS stream-input endpoint rejects them all with an opaque server
        # error otherwise.
        if str(model_id).startswith("eleven_v3"):
            raise ValueError(
                f"{model_id!r} is not supported by the WebSocket stream-input "
                "endpoint — use the HTTP ElevenLabsTTS class instead."
            )
        # Stored privately so it is not surfaced via ``vars(tts)`` or accidental
        # log serialisation. Public read access goes through ``api_key`` below.
        self._api_key = api_key
        self.voice_id = resolve_voice_id(voice_id)
        self.model_id = model_id
        # Track whether the caller explicitly chose an ``output_format``. When
        # left unset (``None``), we default to PCM 16 kHz for backward-compat
        # but allow ``set_telephony_carrier`` to auto-flip to the carrier's
        # native format (``ulaw_8000`` for Twilio) so ElevenLabs encodes
        # server-side and we skip a client-side mulaw transcode. When the
        # caller passed an explicit value, ``set_telephony_carrier`` is a
        # no-op — the user's choice is respected.
        self._output_format_explicit = output_format is not None
        self.output_format = (
            output_format
            if output_format is not None
            else ElevenLabsOutputFormat.PCM_16000
        )
        self.voice_settings = voice_settings
        self.language_code = language_code
        self.auto_mode = auto_mode
        self.inactivity_timeout = inactivity_timeout
        # ElevenLabs default is [120, 160, 250, 290]; we let the server use it
        # unless the caller overrides. Each value must be in 5–500 inclusive.
        self.chunk_length_schedule = chunk_length_schedule
        self.open_timeout = open_timeout
        self.frame_timeout = frame_timeout

    @property
    def api_key(self) -> str:
        """Return the configured API key.

        Exposed as a property (not an attribute) so ``vars(tts)`` and
        ``dataclasses.asdict``-style introspection do not surface the secret
        in their output.
        """
        return self._api_key

    def __repr__(self) -> str:
        return (
            f"ElevenLabsWebSocketTTS(model_id={self.model_id!r}, "
            f"voice_id={self.voice_id!r}, output_format={self.output_format!r})"
        )

    # ------------------------------------------------------------------
    # Telephony factories — mirror ElevenLabsTTS for drop-in parity.
    # ------------------------------------------------------------------

    @classmethod
    def for_twilio(
        cls,
        api_key: str,
        *,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",
        model_id: Union[ElevenLabsModel, str] = ElevenLabsModel.FLASH_V2_5,
        voice_settings: Optional[dict] = None,
        language_code: Optional[str] = None,
        auto_mode: bool = True,
        inactivity_timeout: int = DEFAULT_INACTIVITY_TIMEOUT,
    ) -> "ElevenLabsWebSocketTTS":
        """WebSocket variant pre-configured for Twilio Media Streams.

        Sets ``output_format='ulaw_8000'`` so ElevenLabs emits μ-law @ 8 kHz
        directly — matching Twilio's wire format, no client resampling.
        Voice settings are tuned for low-bandwidth μ-law (speaker boost off,
        moderate stability) to avoid high-frequency aliasing.
        """
        if voice_settings is None:
            voice_settings = {
                "stability": 0.6,
                "similarity_boost": 0.75,
                "use_speaker_boost": False,
            }
        return cls(
            api_key=api_key,
            voice_id=voice_id,
            model_id=model_id,
            output_format=ElevenLabsOutputFormat.ULAW_8000,
            voice_settings=voice_settings,
            language_code=language_code,
            auto_mode=auto_mode,
            inactivity_timeout=inactivity_timeout,
        )

    @classmethod
    def for_telnyx(
        cls,
        api_key: str,
        *,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",
        model_id: Union[ElevenLabsModel, str] = ElevenLabsModel.FLASH_V2_5,
        voice_settings: Optional[dict] = None,
        language_code: Optional[str] = None,
        auto_mode: bool = True,
        inactivity_timeout: int = DEFAULT_INACTIVITY_TIMEOUT,
    ) -> "ElevenLabsWebSocketTTS":
        """WebSocket variant pre-configured for Telnyx (PCM 16 kHz native)."""
        return cls(
            api_key=api_key,
            voice_id=voice_id,
            model_id=model_id,
            output_format=ElevenLabsOutputFormat.PCM_16000,
            voice_settings=voice_settings,
            language_code=language_code,
            auto_mode=auto_mode,
            inactivity_timeout=inactivity_timeout,
        )

    # ------------------------------------------------------------------
    # Carrier auto-detect — wired by StreamHandler.start()
    # ------------------------------------------------------------------

    # Map of telephony carrier → ElevenLabs WS-native ``output_format`` for
    # zero-transcode delivery to the carrier wire. Twilio Media Streams
    # speaks PCMU/μ-law @ 8 kHz; Telnyx negotiates linear PCM 16 kHz.
    _CARRIER_NATIVE_FORMAT: dict[str, ElevenLabsOutputFormat] = {
        "twilio": ElevenLabsOutputFormat.ULAW_8000,
        "telnyx": ElevenLabsOutputFormat.PCM_16000,
    }

    def set_telephony_carrier(self, carrier: str) -> None:
        """Hook called by ``StreamHandler`` to advise the carrier wire format.

        When the user did NOT pass an explicit ``output_format`` to
        ``__init__``, this flips the format to the carrier's native wire
        codec — saving a client-side transcode step. Calling with an
        unknown carrier (``""`` / ``"custom"``) is a no-op.

        When ``output_format`` was explicitly passed (incl. via the
        ``for_twilio`` / ``for_telnyx`` factories), this method is a no-op
        — the user's choice always wins.
        """
        if self._output_format_explicit:
            return
        native = self._CARRIER_NATIVE_FORMAT.get(carrier)
        if native is None:
            return
        self.output_format = native

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        params: dict[str, str] = {
            "model_id": str(self.model_id),
            "output_format": self.output_format,
            "inactivity_timeout": str(self.inactivity_timeout),
        }
        if self.auto_mode:
            params["auto_mode"] = "true"
        if self.language_code:
            params["language_code"] = self.language_code
        return f"{_WS_BASE}/{quote(self.voice_id)}/stream-input?{urlencode(params)}"

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """Open a WebSocket, stream ``text``, yield raw audio bytes, then close.

        Per-utterance lifecycle. The initial ``{"text": " "}`` keep-alive
        message is required by the endpoint protocol; the actual text is
        sent with ``flush: true`` to commit synthesis immediately.

        Resilience contract:

        * Connection bounded by ``open_timeout`` (default 5 s).
        * Each subsequent frame bounded by ``frame_timeout`` (default 30 s)
          so a stalled server cannot hang the call coroutine indefinitely.
        * On consumer cancellation (``aclose`` / ``break``) the WebSocket
          is closed in ``finally`` and a best-effort ``close_context``
          message is sent so ElevenLabs stops billing for unconsumed audio.
        * Server-reported ``error`` raises :class:`ElevenLabsTTSError`
          rather than silently completing the generator.
        * Per-frame audio payload is capped at :data:`MAX_AUDIO_B64_SIZE`
          to prevent OOM via a malicious or malformed base64 payload.
        """
        url = self._build_url()
        headers = {"xi-api-key": self._api_key}

        ws = await asyncio.wait_for(
            websockets.connect(
                url,
                additional_headers=headers,
                open_timeout=self.open_timeout,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=2,
            ),
            timeout=self.open_timeout,
        )
        try:
            # Initial keep-alive packet establishes the session. Per the
            # ElevenLabs docs the first message must contain a single space
            # ``" "`` — sending ``""`` would close the socket immediately.
            init: dict = {"text": " "}
            if self.voice_settings:
                init["voice_settings"] = self.voice_settings
            if self.chunk_length_schedule and not self.auto_mode:
                init["generation_config"] = {
                    "chunk_length_schedule": self.chunk_length_schedule,
                }
            await ws.send(json.dumps(init))

            # Send the actual text + flush so ElevenLabs commits the
            # synthesis without waiting for further chunks. EOS
            # ``{"text": ""}`` is intentionally NOT sent here — sending it
            # immediately after ``flush:true`` can cause auto_mode to
            # truncate the tail audio. The socket is closed in ``finally``
            # after the consumer drains, which serves as the EOS.
            await ws.send(json.dumps({"text": text + " ", "flush": True}))

            from websockets.exceptions import ConnectionClosedOK

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.frame_timeout)
                except asyncio.TimeoutError as exc:
                    raise ElevenLabsTTSError(
                        f"ElevenLabs WS no frame for {self.frame_timeout}s"
                    ) from exc
                except ConnectionClosedOK:
                    # Server closed cleanly — treat as end-of-stream.
                    return

                # WebSocket frames may be text (JSON) or bytes (rare —
                # some deployments may emit binary audio frames directly).
                if isinstance(raw, bytes):
                    if len(raw) > MAX_AUDIO_B64_SIZE:
                        logger.warning(
                            "ElevenLabs WS binary frame too large (%d bytes), skipping",
                            len(raw),
                        )
                        continue
                    yield raw
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("ElevenLabs WS sent non-JSON text frame")
                    continue

                # Process error FIRST (before audio / isFinal) so a single
                # frame containing both isFinal and error is not misread as
                # a clean end-of-stream.
                if msg.get("error"):
                    err_str = _sanitise_log_str(msg["error"])
                    # Recognise plan-gated rejections so callers can catch
                    # them separately and either upgrade or fall back to
                    # the HTTP class.
                    if (
                        err_str == ElevenLabsWSServerError.PAYMENT_REQUIRED
                        or "payment" in err_str.lower()
                    ):
                        raise ElevenLabsPlanError(_PLAN_REQUIRED_MSG)
                    raise ElevenLabsTTSError(f"ElevenLabs WS reported error: {err_str}")

                audio_b64 = msg.get("audio")
                if audio_b64:
                    if (
                        not isinstance(audio_b64, str)
                        or len(audio_b64) > MAX_AUDIO_B64_SIZE
                    ):
                        logger.warning(
                            "ElevenLabs WS audio frame too large or malformed, skipping"
                        )
                    else:
                        try:
                            yield base64.b64decode(audio_b64)
                        except (ValueError, TypeError):
                            logger.warning("ElevenLabs WS sent malformed base64 audio")

                if msg.get("isFinal"):
                    return
        finally:
            # Best-effort: tell the server to stop synthesising any
            # buffered text the consumer is no longer interested in.
            # Failure to send is non-fatal — the socket close below
            # achieves the same end goal.
            try:
                await ws.send(json.dumps({"text": ""}))
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass

    async def close(self) -> None:
        """No-op: connections are per-utterance and closed inline."""
        # No persistent state to clean up — connections are per-utterance.
        return None
