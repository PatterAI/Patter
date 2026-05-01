"""
Soniox Speech-to-Text adapter for the Patter SDK pipeline mode.

Connects to the Soniox real-time WebSocket API and streams PCM audio in,
yielding :class:`~getpatter.providers.base.Transcript` events. The adapter
accumulates ``is_final`` tokens into segments and flushes them when an
``<end>`` / ``<fin>`` endpoint token is received, following the Soniox
real-time protocol.

Implementation surfaces only the raw transcript text + confidence required
by Patter pipelines (no translation metadata or language-identification
events).
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import IntEnum, StrEnum
from typing import Any, AsyncIterator

import aiohttp

from getpatter.providers.base import STTProvider, Transcript

logger = logging.getLogger("getpatter")

# === Constants ===

# Base URL for Soniox Speech-to-Text API (WebSocket).
SONIOX_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"


class SonioxModel(StrEnum):
    """Known Soniox real-time STT models."""

    STT_RT_V4 = "stt-rt-v4"
    STT_RT_V3 = "stt-rt-v3"
    STT_RT_V2 = "stt-rt-v2"


class SonioxAudioFormat(StrEnum):
    """Audio formats accepted by Soniox real-time API."""

    PCM_S16LE = "pcm_s16le"


class SonioxSampleRate(IntEnum):
    """Common PCM sample rates for Soniox streaming input."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_24000 = 24000


class SonioxClientFrame(StrEnum):
    """Soniox real-time client message ``type`` values."""

    KEEPALIVE = "keepalive"


class SonioxEndpointToken(StrEnum):
    """Soniox token markers that signal a speech-segment endpoint."""

    END = "<end>"
    FIN = "<fin>"


# Soniox keepalive payload (JSON).
KEEPALIVE_MESSAGE = json.dumps({"type": SonioxClientFrame.KEEPALIVE.value})
# Tokens that mark a speech segment endpoint in the Soniox stream.
END_TOKEN = SonioxEndpointToken.END.value
FINALIZED_TOKEN = SonioxEndpointToken.FIN.value
# Interval between keepalive messages while no audio is being sent.
KEEPALIVE_INTERVAL_SEC = 5.0


def _is_end_token(token: dict[str, Any]) -> bool:
    """Return True if the token marks an end or finalized event."""
    return token.get("text") in (
        SonioxEndpointToken.END.value,
        SonioxEndpointToken.FIN.value,
    )


class _TokenAccumulator:
    """Accumulates token metadata (text + rolling confidence).

    Tokens are assumed to arrive in chronological order. The accumulator
    concatenates the ``text`` field and maintains a running average of
    per-token confidences.
    """

    __slots__ = ("text", "_confidence_sum", "_confidence_count")

    def __init__(self) -> None:
        self.text: str = ""
        self._confidence_sum: float = 0.0
        self._confidence_count: int = 0

    def update(self, token: dict[str, Any]) -> None:
        """Append a Soniox token's text and accumulate its confidence."""
        self.text += token.get("text", "")
        confidence = token.get("confidence")
        if confidence is not None:
            self._confidence_sum += float(confidence)
            self._confidence_count += 1

    @property
    def confidence(self) -> float:
        """Mean per-token confidence in ``[0.0, 1.0]`` (0.0 when no tokens)."""
        if self._confidence_count == 0:
            return 0.0
        return self._confidence_sum / self._confidence_count

    @property
    def raw(self) -> tuple[float, int]:
        """Expose the accumulator's running ``(sum, count)`` pair.

        Mirrors the TypeScript ``TokenAccumulator.raw`` shape so external
        consumers can blend confidences from multiple accumulators without
        reaching into private attributes.
        """
        return (self._confidence_sum, self._confidence_count)

    def reset(self) -> None:
        """Clear accumulated text and confidence state."""
        self.text = ""
        self._confidence_sum = 0.0
        self._confidence_count = 0


class SonioxSTT(STTProvider):
    """Soniox real-time STT adapter.

    Connects to the Soniox Speech-to-Text WebSocket API and yields
    :class:`~getpatter.providers.base.Transcript` objects.

    Args:
        api_key: Soniox API key.
        model: Soniox STT model (default ``"stt-rt-v4"``).
        language_hints: Optional BCP-47 language hints (e.g. ``["en", "it"]``).
        language_hints_strict: When True, restrict to the hints supplied.
        sample_rate: PCM sample rate (Hz). Defaults to 16 kHz, matching
            Patter's pipeline mode.
        num_channels: Audio channels (default 1).
        enable_speaker_diarization: Turn on server-side speaker diarization.
        enable_language_identification: Attach language codes to tokens.
        max_endpoint_delay_ms: Silence, in ms, before Soniox reports an
            endpoint. Must be in ``[500, 3000]`` (validated by the server).
        client_reference_id: Optional correlation ID for Soniox dashboards.
        base_url: Override the Soniox WebSocket URL (used by tests).
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: Union[SonioxModel, str] = SonioxModel.STT_RT_V4,
        language_hints: list[str] | None = None,
        language_hints_strict: bool = False,
        sample_rate: Union[SonioxSampleRate, int] = SonioxSampleRate.HZ_16000,
        num_channels: int = 1,
        enable_speaker_diarization: bool = False,
        enable_language_identification: bool = True,
        max_endpoint_delay_ms: int = 500,
        client_reference_id: str | None = None,
        base_url: str = SONIOX_WS_URL,
    ) -> None:
        if not api_key:
            raise ValueError("Soniox api_key is required")
        if not (500 <= max_endpoint_delay_ms <= 3000):
            raise ValueError("max_endpoint_delay_ms must be between 500 and 3000")

        self.api_key = api_key
        self.model = model
        self.language_hints = language_hints
        self.language_hints_strict = language_hints_strict
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.enable_speaker_diarization = enable_speaker_diarization
        self.enable_language_identification = enable_language_identification
        self.max_endpoint_delay_ms = max_endpoint_delay_ms
        self.client_reference_id = client_reference_id
        self.base_url = base_url

        self._session: aiohttp.ClientSession | None = None
        self._owns_session: bool = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return (
            f"SonioxSTT(model={self.model!r}, sample_rate={self.sample_rate}, "
            f"diarization={self.enable_speaker_diarization})"
        )

    @classmethod
    def for_twilio(
        cls,
        api_key: str,
        language_hints: list[str] | None = None,
        model: Union[SonioxModel, str] = SonioxModel.STT_RT_V4,
    ) -> "SonioxSTT":
        """Create a Soniox adapter configured for Twilio-style 8 kHz linear PCM.

        Soniox supports 8 kHz PCM directly via ``sample_rate=8000``; Patter's
        Twilio bridge converts inbound mulaw to linear16 before hitting the
        STT, so pass 8000 here to avoid needless resampling.
        """
        return cls(
            api_key=api_key,
            model=model,
            language_hints=language_hints,
            sample_rate=SonioxSampleRate.HZ_8000,
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _build_config(self) -> dict[str, Any]:
        """Build the initial configuration payload sent after ws_connect."""
        config: dict[str, Any] = {
            "api_key": self.api_key,
            "model": self.model,
            "audio_format": SonioxAudioFormat.PCM_S16LE.value,
            "num_channels": self.num_channels,
            "sample_rate": self.sample_rate,
            "enable_endpoint_detection": True,
            "enable_speaker_diarization": self.enable_speaker_diarization,
            "enable_language_identification": self.enable_language_identification,
            "max_endpoint_delay_ms": self.max_endpoint_delay_ms,
        }
        if self.language_hints:
            config["language_hints"] = self.language_hints
            config["language_hints_strict"] = self.language_hints_strict
        if self.client_reference_id:
            config["client_reference_id"] = self.client_reference_id
        return config

    async def connect(self) -> None:
        """Open the WebSocket and send the initial configuration payload."""
        if self._ws is not None:
            return  # Already connected — idempotent.

        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True

        try:
            self._ws = await self._session.ws_connect(self.base_url)
            await self._ws.send_str(json.dumps(self._build_config()))
        except Exception:
            # Clean up partial state if connect fails.
            if self._owns_session and self._session is not None:
                await self._session.close()
                self._session = None
                self._owns_session = False
            self._ws = None
            raise

        # Spawn keepalive task so idle streams don't get dropped.
        self._keepalive_task = asyncio.create_task(self._run_keepalive())

    async def _run_keepalive(self) -> None:
        """Send periodic keepalive frames while the socket is open."""
        try:
            while self._ws is not None and not self._ws.closed:
                await asyncio.sleep(KEEPALIVE_INTERVAL_SEC)
                if self._ws is None or self._ws.closed:
                    break
                try:
                    await self._ws.send_str(KEEPALIVE_MESSAGE)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("SonioxSTT keepalive send failed: %s", exc)
                    break
        except asyncio.CancelledError:
            pass

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Send a PCM audio chunk (signed 16-bit little-endian)."""
        if self._ws is None:
            raise RuntimeError("SonioxSTT is not connected. Call connect() first.")
        if not audio_chunk:
            return
        await self._ws.send_bytes(audio_chunk)

    # ------------------------------------------------------------------
    # Transcript stream
    # ------------------------------------------------------------------

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Yield :class:`Transcript` objects from the Soniox stream.

        Behaviour:
        - Final transcripts are emitted when an ``<end>``/``<fin>`` token
          is received, containing all accumulated final tokens.
        - Interim transcripts combine accumulated final tokens with any
          current non-final tokens, so callers always see the best
          in-flight hypothesis.
        """
        if self._ws is None:
            raise RuntimeError("SonioxSTT is not connected. Call connect() first.")

        final_acc = _TokenAccumulator()

        async for msg in self._ws:
            if msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
            ):
                break
            if msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("SonioxSTT WebSocket error: %s", self._ws.exception())
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue

            try:
                content = json.loads(msg.data)
            except json.JSONDecodeError:
                logger.warning("SonioxSTT: received non-JSON message")
                continue

            if content.get("error_code") or content.get("error_message"):
                logger.error(
                    "SonioxSTT error %s: %s",
                    content.get("error_code"),
                    content.get("error_message"),
                )

            tokens = content.get("tokens") or []
            non_final = _TokenAccumulator()
            emitted_final_this_msg = False

            for token in tokens:
                if token.get("is_final"):
                    if _is_end_token(token):
                        # Endpoint detected — flush the accumulated final text.
                        if final_acc.text:
                            yield Transcript(
                                text=final_acc.text.strip(),
                                is_final=True,
                                confidence=final_acc.confidence,
                            )
                            final_acc.reset()
                            emitted_final_this_msg = True
                    else:
                        final_acc.update(token)
                else:
                    non_final.update(token)

            # Emit an interim update with ``final_acc + non_final`` so
            # downstream consumers see the best in-flight hypothesis.
            interim_text = (final_acc.text + non_final.text).strip()
            if interim_text and not emitted_final_this_msg:
                # Blended confidence: average of whichever sides contributed.
                final_sum, final_count = final_acc.raw
                non_final_sum, non_final_count = non_final.raw
                total_count = final_count + non_final_count
                total_sum = final_sum + non_final_sum
                confidence = total_sum / total_count if total_count else 0.0
                yield Transcript(
                    text=interim_text,
                    is_final=False,
                    confidence=confidence,
                )

            if content.get("finished"):
                # Final flush on server-side finish.
                if final_acc.text:
                    yield Transcript(
                        text=final_acc.text.strip(),
                        is_final=True,
                        confidence=final_acc.confidence,
                    )
                    final_acc.reset()
                break

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the WebSocket and release owned resources."""
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._keepalive_task = None

        if self._ws is not None:
            try:
                # Soniox finishes cleanly when the client sends an empty
                # binary frame; emulate the official client behaviour.
                await self._ws.send_bytes(b"")
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False
