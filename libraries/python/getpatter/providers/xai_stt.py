"""xAI (Grok) streaming STT adapter.

Implements :class:`getpatter.providers.base.STTProvider` against xAI's
``wss://api.x.ai/v1/stt`` WebSocket endpoint. The client streams raw audio
as binary WebSocket frames (no base64) and receives JSON transcript events
(``transcript.created`` / ``transcript.partial`` / ``transcript.done``).

Protocol notes (https://docs.x.ai/developers/model-capabilities/audio/speech-to-text):

* Configuration is done entirely via URL query parameters — no setup
  message is required.
* The server sends ``transcript.created`` once it is ready; audio sent
  before that event may be dropped, so :meth:`connect` waits for it.
* ``transcript.partial`` carries ``is_final`` / ``speech_final`` with the
  same three-state semantics Patter already models on
  :class:`~getpatter.providers.base.Transcript` (interim → chunk final →
  utterance final).
* ``{"type": "finalize"}`` forces the in-flight utterance to finalize
  immediately (mirrors Deepgram's ``Finalize`` fast-path on VAD
  ``speech_end``).
* ``{"type": "audio.done"}`` flushes the remaining transcript as
  ``transcript.done`` and closes the socket.

Pricing: $0.20/hr of streamed audio (the REST batch endpoint is $0.10/hr).
See ``DEFAULT_PRICING["xai_stt"]``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import IntEnum, StrEnum
from typing import AsyncIterator, ClassVar, Sequence, Union
from urllib.parse import urlencode

import websockets
from websockets.exceptions import InvalidStatus

from getpatter.exceptions import (
    AuthenticationError,
    PatterConnectionError,
    RateLimitError,
)
from getpatter.providers.base import STTProvider, Transcript

logger = logging.getLogger("getpatter.providers.xai_stt")

XAI_STT_WS_URL = "wss://api.x.ai/v1/stt"

# xAI keyterm limits (per docs): max 100 terms, each up to 50 characters.
_MAX_KEYTERMS = 100
_MAX_KEYTERM_LEN = 50

# After sending audio.done on close() we give the server a short window to
# flush the trailing transcript.done before tearing down the socket. Kept
# well below the 500 ms close-latency budget.
_DONE_DRAIN_SECONDS = 0.1


class XAISTTEncoding(StrEnum):
    """Audio encodings accepted by xAI's streaming STT endpoint."""

    PCM = "pcm"
    MULAW = "mulaw"
    ALAW = "alaw"


class XAISTTSampleRate(IntEnum):
    """Sample rates accepted by xAI's streaming STT endpoint."""

    HZ_8000 = 8000
    HZ_16000 = 16000
    HZ_22050 = 22050
    HZ_24000 = 24000
    HZ_44100 = 44100
    HZ_48000 = 48000


class XAISTTServerEvent(StrEnum):
    """Inbound (server → client) event ``type`` values."""

    CREATED = "transcript.created"
    PARTIAL = "transcript.partial"
    DONE = "transcript.done"
    ERROR = "error"


class XAISTTClientFrame(StrEnum):
    """Outbound (client → server) JSON control frame ``type`` values."""

    FINALIZE = "finalize"
    AUDIO_DONE = "audio.done"


class XAISTT(STTProvider):
    """Streaming STT adapter for xAI's ``wss://api.x.ai/v1/stt`` WebSocket API."""

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "xai_stt"

    def __init__(
        self,
        api_key: str,
        language: str | None = "en",
        encoding: Union[XAISTTEncoding, str] = XAISTTEncoding.PCM,
        sample_rate: Union[XAISTTSampleRate, int] = XAISTTSampleRate.HZ_16000,
        *,
        interim_results: bool = True,
        endpointing_ms: int | None = None,
        keyterms: Sequence[str] | None = None,
        diarize: bool = False,
        filler_words: bool = False,
        smart_turn: float | None = None,
        smart_turn_timeout_ms: int | None = None,
    ):
        # ``language`` enables xAI's Inverse Text Normalization (spoken
        # numbers/currency → written form) on the stream. The model
        # transcribes any supported language regardless of this value; pass
        # ``language=None`` to skip formatting entirely.
        #
        # ``smart_turn`` (0.0–1.0) opts into xAI's ML end-of-turn detector:
        # silence pauses below the confidence threshold are demoted to chunk
        # finals instead of utterance finals, so mid-sentence pauses (numbers,
        # thinking between clauses) stop triggering ``speech_final``. Pair it
        # with ``smart_turn_timeout_ms`` so extended silence still finalizes.
        if keyterms is not None:
            if len(keyterms) > _MAX_KEYTERMS:
                raise ValueError(
                    f"xAI STT accepts at most {_MAX_KEYTERMS} keyterms, "
                    f"got {len(keyterms)}."
                )
            for term in keyterms:
                if len(term) > _MAX_KEYTERM_LEN:
                    raise ValueError(
                        f"xAI STT keyterms are limited to {_MAX_KEYTERM_LEN} "
                        f"characters, got {len(term)} for {term[:60]!r}."
                    )
        if smart_turn is not None and not 0.0 <= smart_turn <= 1.0:
            raise ValueError(f"smart_turn must be in [0.0, 1.0], got {smart_turn}")
        if smart_turn_timeout_ms is not None and not 1 <= smart_turn_timeout_ms <= 5000:
            raise ValueError(
                f"smart_turn_timeout_ms must be in [1, 5000], got {smart_turn_timeout_ms}"
            )
        self.api_key = api_key
        self.language = language
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.interim_results = interim_results
        self.endpointing_ms = endpointing_ms
        self.keyterms = list(keyterms) if keyterms else []
        self.diarize = diarize
        self.filler_words = filler_words
        self.smart_turn = smart_turn
        self.smart_turn_timeout_ms = smart_turn_timeout_ms
        self._ws = None
        self.request_id: str | None = None
        # Bytes of audio forwarded to xAI since the last cost emission. Used
        # by ``_record_transcript_cost`` to compute ``patter.cost.stt_seconds``.
        self._audio_bytes_sent: int = 0

    def __repr__(self) -> str:
        return (
            f"XAISTT(language={self.language!r}, encoding={self.encoding!r}, "
            f"sample_rate={self.sample_rate})"
        )

    @classmethod
    def for_twilio(
        cls,
        api_key: str,
        language: str | None = "en",
        **kwargs,
    ) -> "XAISTT":
        """Create an xAI STT adapter configured for Twilio mulaw 8 kHz."""
        return cls(
            api_key=api_key,
            language=language,
            encoding=XAISTTEncoding.MULAW,
            sample_rate=XAISTTSampleRate.HZ_8000,
            **kwargs,
        )

    def _build_url(self) -> str:
        params: list[tuple[str, str]] = [
            ("sample_rate", str(int(self.sample_rate))),
            ("encoding", str(self.encoding)),
            ("interim_results", "true" if self.interim_results else "false"),
        ]
        if self.language:
            params.append(("language", self.language))
        if self.endpointing_ms is not None:
            params.append(("endpointing", str(self.endpointing_ms)))
        if self.diarize:
            params.append(("diarize", "true"))
        if self.filler_words:
            params.append(("filler_words", "true"))
        if self.smart_turn is not None:
            params.append(("smart_turn", str(self.smart_turn)))
            if self.smart_turn_timeout_ms is not None:
                params.append(("smart_turn_timeout", str(self.smart_turn_timeout_ms)))
        # ``keyterm`` is a repeated parameter — one entry per term.
        for term in self.keyterms:
            params.append(("keyterm", term))
        return f"{XAI_STT_WS_URL}?{urlencode(params)}"

    def _record_transcript_cost(self) -> None:
        """Emit ``patter.cost.stt_seconds`` for audio buffered since the last
        final transcript. No-op when OTel is missing / no scope active."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            bytes_per_sample = 1 if self.encoding in ("mulaw", "alaw") else 2
            seconds = self._audio_bytes_sent / float(
                int(self.sample_rate) * bytes_per_sample
            )
            record_patter_attrs(
                {
                    "patter.cost.stt_seconds": seconds,
                    "patter.stt.provider": "xai_stt",
                }
            )
            self._audio_bytes_sent = 0
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_transcript_cost failed", exc_info=True)

    async def warmup(self) -> None:
        """Pre-call WebSocket warmup for the xAI ``/v1/stt`` endpoint.

        Opens the WS (DNS + TLS + auth handshake), waits briefly for the
        ``transcript.created`` ready signal, then closes cleanly. By the time
        :meth:`connect` runs at call-pickup the resolver and TLS session are
        hot — net wire time saving of 200-500 ms vs a cold open.

        Billing safety: xAI bills streaming STT per hour of streamed audio
        (https://docs.x.ai/developers/pricing). Opening + closing the socket
        without sending audio frames does not consume billable time.
        Best-effort: failures are logged at DEBUG and never raised.
        """
        ws = None
        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    self._build_url(),
                    additional_headers={"Authorization": f"Bearer {self.api_key}"},
                ),
                timeout=5.0,
            )
            # Wait for the ready signal so the server-side ASR backend is
            # initialised (and stays warm at the edge) before we close.
            try:
                await asyncio.wait_for(ws.recv(), timeout=2.0)
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("xAI STT warmup failed (best-effort): %s", exc)
        finally:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

    async def connect(self) -> None:
        """Open the xAI STT WebSocket and wait for ``transcript.created``."""
        try:
            self._ws = await websockets.connect(
                self._build_url(),
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except InvalidStatus as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (401, 403):
                raise AuthenticationError(
                    f"xAI rejected the API key (HTTP {status_code})."
                ) from exc
            if status_code == 429:
                raise RateLimitError("xAI rate limit exceeded (HTTP 429).") from exc
            raise PatterConnectionError(
                f"xAI STT WebSocket upgrade failed (HTTP {status_code})."
            ) from exc
        except OSError as exc:
            raise PatterConnectionError(f"Failed to connect to xAI: {exc}") from exc

        # The server needs to initialise its ASR backend before accepting
        # audio — per the docs, wait for ``transcript.created`` before
        # streaming. Audio sent earlier may be dropped.
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            data = json.loads(raw)
            if data.get("type") != XAISTTServerEvent.CREATED:
                if data.get("type") == XAISTTServerEvent.ERROR:
                    message = data.get("message", "unknown error")
                    raise PatterConnectionError(f"xAI STT setup error: {message}")
                logger.warning(
                    "xAI STT: expected transcript.created, got %r", data.get("type")
                )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            await self._ws.close()
            self._ws = None
            raise PatterConnectionError(
                "xAI STT did not send transcript.created within 10s."
            ) from exc
        except PatterConnectionError:
            await self._ws.close()
            self._ws = None
            raise

    async def send_audio(self, audio_chunk: bytes) -> None:
        """Send a raw binary audio chunk (no base64). Empty chunks are dropped."""
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")
        if len(audio_chunk) == 0:
            return
        self._audio_bytes_sent += len(audio_chunk)
        await self._ws.send(audio_chunk)

    async def finalize(self) -> None:
        """Force xAI to finalize the in-flight utterance as ``speech_final``
        immediately, rather than waiting for its own endpointing / Smart Turn
        heuristics. Called by the SDK on VAD ``speech_end`` and after
        barge-in cancel — both moments where the SDK already knows the user
        stopped speaking and waiting for server endpointing only adds dead
        air.

        Idempotent: safe to call when the socket is closed/closing.
        """
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps({"type": XAISTTClientFrame.FINALIZE}))
        except Exception:
            # Socket changed state between the check and send — safe to
            # ignore; the next utterance offers another finalize opportunity.
            pass

    def _parse_message(self, raw_message: str) -> Transcript | None:
        data = json.loads(raw_message)
        msg_type = data.get("type", "")

        if msg_type == XAISTTServerEvent.ERROR:
            # The connection stays open after an error event — log and skip.
            logger.error("xAI STT error: %s", data.get("message", "unknown"))
            return None

        if msg_type == XAISTTServerEvent.DONE:
            text = (data.get("text") or "").strip()
            if not text:
                return None
            # transcript.done is the stitched full-session transcript sent
            # after audio.done. The per-utterance finals were already emitted
            # via transcript.partial, so this is only surfaced on the
            # explicit-close path (close() drains it before teardown).
            return Transcript(
                text=text,
                is_final=True,
                confidence=1.0,
                speech_final=True,
                request_id=self.request_id,
                words=tuple(data.get("words", []) or []),
            )

        if msg_type != XAISTTServerEvent.PARTIAL:
            return None

        text = (data.get("text") or "").strip()
        if not text:
            return None

        is_final = bool(data.get("is_final", False))
        speech_final = bool(data.get("speech_final", False))
        # xAI does not emit a per-transcript confidence; use the Smart Turn
        # end-of-turn confidence when available so callers gating on
        # ``confidence`` still see a meaningful signal, else 1.0.
        confidence = float(data.get("end_of_turn_confidence", 1.0) or 1.0)
        return Transcript(
            text=text,
            # speech_final implies utterance completion — accept either so
            # the pipeline doesn't wait on chunk-final stitching.
            is_final=is_final or speech_final,
            confidence=confidence,
            speech_final=speech_final,
            request_id=self.request_id,
            words=tuple(data.get("words", []) or []),
        )

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        """Yield :class:`Transcript` events parsed from the xAI stream."""
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")

        async for raw_message in self._ws:
            if isinstance(raw_message, bytes):
                continue  # Skip binary frames
            transcript = self._parse_message(raw_message)
            if transcript is not None:
                if transcript.is_final:
                    self._record_transcript_cost()
                yield transcript

    async def close(self) -> None:
        """Send finalize + audio.done, then close the xAI WebSocket."""
        if self._ws is not None:
            ws = self._ws
            # finalize flushes any trailing partial into a speech_final
            # transcript.partial; audio.done then triggers transcript.done
            # and the server closes the socket. Give the server a short
            # drain window (bounded well below the 500 ms close-latency
            # budget).
            try:
                await ws.send(json.dumps({"type": XAISTTClientFrame.FINALIZE}))
                await ws.send(json.dumps({"type": XAISTTClientFrame.AUDIO_DONE}))
                await asyncio.sleep(_DONE_DRAIN_SECONDS)
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass
            self._ws = None
