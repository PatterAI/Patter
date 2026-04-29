import asyncio
import json
from typing import AsyncIterator
from urllib.parse import urlencode

import websockets
from websockets.exceptions import InvalidStatus

from getpatter.exceptions import (
    AuthenticationError,
    PatterConnectionError,
    RateLimitError,
)
from getpatter.providers.base import STTProvider, Transcript

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"

# Deepgram closes idle sockets after 10 s with no audio. Send a KeepAlive
# text frame every 4 s — well inside the 3–5 s window recommended by
# Deepgram's docs.
_KEEPALIVE_INTERVAL_SECONDS = 4.0

# After sending Finalize on close() we give the server a short window to
# flush any trailing partial as a Results frame before we send CloseStream.
# Kept well below 500 ms total close-latency budget.
_FINALIZE_DRAIN_SECONDS = 0.1


class DeepgramSTT(STTProvider):
    def __init__(
        self,
        api_key: str,
        language: str = "en",
        model: str = "nova-3",
        encoding: str = "linear16",
        sample_rate: int = 16000,
        *,
        endpointing_ms: int = 150,
        utterance_end_ms: int | None = 1000,
        smart_format: bool = False,
        interim_results: bool = True,
        vad_events: bool = True,
    ):
        # ``smart_format`` defaults to ``False`` because punctuation and numeral
        # formatting add roughly 50–150 ms to TTFT on each final transcript and
        # are rarely useful for telephony pipelines that immediately feed the
        # text into an LLM. Pass ``smart_format=True`` to opt back in for use
        # cases (e.g. dashboards, transcripts) where the formatted text is
        # surfaced directly to humans.
        self.api_key = api_key
        self.language = language
        self.model = model
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.endpointing_ms = endpointing_ms
        self.utterance_end_ms = utterance_end_ms
        self.smart_format = smart_format
        self.interim_results = interim_results
        self.vad_events = vad_events
        self._ws = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self.request_id: str | None = None
        self._audio_bytes_sent: int = 0

    def _record_transcript_cost(self) -> None:
        from getpatter.observability.attributes import record_patter_attrs

        bytes_per_sample = 1 if self.encoding == "mulaw" else 2
        seconds = self._audio_bytes_sent / float(self.sample_rate * bytes_per_sample)
        record_patter_attrs(
            {
                "patter.cost.stt_seconds": seconds,
                "patter.stt.provider": "deepgram",
            }
        )
        self._audio_bytes_sent = 0

    def __repr__(self) -> str:
        return f"DeepgramSTT(model={self.model!r}, language={self.language!r}, encoding={self.encoding!r})"

    @classmethod
    def for_twilio(
        cls,
        api_key: str,
        language: str = "en",
        model: str = "nova-3",
        **kwargs,
    ):
        """Create a Deepgram adapter configured for Twilio mulaw 8kHz."""
        return cls(
            api_key=api_key,
            language=language,
            model=model,
            encoding="mulaw",
            sample_rate=8000,
            **kwargs,
        )

    async def connect(self) -> None:
        params = {
            "model": self.model,
            "language": self.language,
            "encoding": self.encoding,
            "sample_rate": str(self.sample_rate),
            "channels": "1",
            "interim_results": "true" if self.interim_results else "false",
            "endpointing": str(self.endpointing_ms),
            "smart_format": "true" if self.smart_format else "false",
            "vad_events": "true" if self.vad_events else "false",
            "no_delay": "true",
        }
        if self.utterance_end_ms is not None:
            # utterance_end_ms has a hard minimum of 1000 on Deepgram's API.
            params["utterance_end_ms"] = str(max(int(self.utterance_end_ms), 1000))
        url = f"{DEEPGRAM_WS_URL}?{urlencode(params)}"
        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {self.api_key}"},
            )
        except InvalidStatus as exc:
            # websockets>=14 exposes the HTTP status via exc.response.status_code.
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (401, 403):
                raise AuthenticationError(
                    f"Deepgram rejected the API key (HTTP {status_code})."
                ) from exc
            if status_code == 429:
                raise RateLimitError(
                    "Deepgram rate limit exceeded (HTTP 429)."
                ) from exc
            raise PatterConnectionError(
                f"Deepgram WebSocket upgrade failed (HTTP {status_code})."
            ) from exc
        except OSError as exc:
            # Transient network / DNS / TLS issues — wrap so callers can
            # distinguish from programmer errors.
            raise PatterConnectionError(
                f"Failed to connect to Deepgram: {exc}"
            ) from exc

        # Start the KeepAlive pump. Deepgram closes the socket after ~10 s of
        # silence; a 4 s cadence keeps us comfortably inside that window.
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        try:
            while self._ws is not None and self._ws.state.name == "OPEN":
                await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)
                if self._ws is None or self._ws.state.name != "OPEN":
                    return
                try:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
                except Exception:
                    # Socket may have raced to close; the receive loop will
                    # observe the closure and surface it to the caller.
                    return
        except asyncio.CancelledError:
            raise

    async def send_audio(self, audio_chunk: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")
        # Deepgram treats a zero-length binary frame as CloseStream — so
        # silently drop empty chunks to avoid accidentally tearing down
        # the session (e.g. when a VAD gate emits an empty buffer).
        if len(audio_chunk) == 0:
            return
        self._audio_bytes_sent += len(audio_chunk)
        await self._ws.send(audio_chunk)

    def _parse_message(self, raw_message: str) -> Transcript | None:
        data = json.loads(raw_message)
        msg_type = data.get("type", "")

        if msg_type == "Metadata":
            self.request_id = data.get("request_id")
            return None

        if msg_type == "SpeechStarted":
            return Transcript(
                text="",
                is_final=False,
                confidence=0.0,
                event_type="SpeechStarted",
                request_id=self.request_id,
            )

        if msg_type == "UtteranceEnd":
            return Transcript(
                text="",
                is_final=True,
                confidence=0.0,
                event_type="UtteranceEnd",
                request_id=self.request_id,
            )

        if msg_type != "Results":
            return None

        alternatives = data.get("channel", {}).get("alternatives", [])
        if not alternatives:
            return None

        best = alternatives[0]
        text = best.get("transcript", "").strip()
        if not text:
            return None

        # is_final alone marks a stable utterance; speech_final is a faster
        # end-of-utterance hint from Deepgram's VAD. Accept either so the
        # pipeline doesn't wait up to utterance_end_ms on every turn.
        speech_final = bool(data.get("speech_final", False))
        is_final = bool(data.get("is_final", False) or speech_final)
        from_finalize = bool(data.get("from_finalize", False))
        words = best.get("words", []) or []
        return Transcript(
            text=text,
            is_final=is_final,
            confidence=best.get("confidence", 0.0),
            speech_final=speech_final,
            from_finalize=from_finalize,
            request_id=self.request_id,
            words=list(words),
            event_type="Results",
        )

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
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
        # Cancel the KeepAlive pump first so it does not race the close
        # handshake.
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None

        if self._ws is not None:
            ws = self._ws
            # Send Finalize first to flush any trailing partial into a
            # final Results frame. Give the server a short drain window
            # (bounded well below the 500 ms close-latency budget) before
            # sending CloseStream.
            try:
                await ws.send(json.dumps({"type": "Finalize"}))
                await asyncio.sleep(_FINALIZE_DRAIN_SECONDS)
            except Exception:
                pass
            try:
                await ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass
            self._ws = None
