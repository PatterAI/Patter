import json
from typing import AsyncIterator
from urllib.parse import urlencode

import websockets

from getpatter.providers.base import STTProvider, Transcript

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"


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
        smart_format: bool = True,
        interim_results: bool = True,
        vad_events: bool = True,
    ):
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
        self.request_id: str | None = None

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
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {self.api_key}"},
        )

    async def send_audio(self, audio_chunk: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")
        await self._ws.send(audio_chunk)

    def _parse_message(self, raw_message: str) -> Transcript | None:
        data = json.loads(raw_message)
        msg_type = data.get("type", "")

        if msg_type == "Metadata":
            self.request_id = data.get("request_id")
            return None

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
        is_final = bool(data.get("is_final", False) or data.get("speech_final", False))
        return Transcript(
            text=text,
            is_final=is_final,
            confidence=best.get("confidence", 0.0),
        )

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        if self._ws is None:
            raise RuntimeError("Not connected. Call connect() first.")

        async for raw_message in self._ws:
            if isinstance(raw_message, bytes):
                continue  # Skip binary frames
            transcript = self._parse_message(raw_message)
            if transcript is not None:
                yield transcript

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
            await self._ws.close()
            self._ws = None
