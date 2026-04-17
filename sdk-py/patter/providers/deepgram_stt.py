import json
from typing import AsyncIterator

import websockets

from patter.providers.base import STTProvider, Transcript

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"


class DeepgramSTT(STTProvider):
    def __init__(
        self,
        api_key: str,
        language: str = "en",
        model: str = "nova-3",
        encoding: str = "linear16",
        sample_rate: int = 16000,
    ):
        self.api_key = api_key
        self.language = language
        self.model = model
        self.encoding = encoding
        self.sample_rate = sample_rate
        self._ws = None
        self.request_id: str | None = None

    def __repr__(self) -> str:
        return f"DeepgramSTT(model={self.model!r}, language={self.language!r}, encoding={self.encoding!r})"

    @classmethod
    def for_twilio(cls, api_key: str, language: str = "en", model: str = "nova-3"):
        """Create a Deepgram adapter configured for Twilio mulaw 8kHz."""
        return cls(
            api_key=api_key,
            language=language,
            model=model,
            encoding="mulaw",
            sample_rate=8000,
        )

    async def connect(self) -> None:
        url = (
            f"{DEEPGRAM_WS_URL}"
            f"?model={self.model}"
            f"&language={self.language}"
            f"&encoding={self.encoding}"
            f"&sample_rate={self.sample_rate}"
            f"&channels=1"
            f"&interim_results=true"
            f"&endpointing=300"
            f"&smart_format=true"
            f"&vad_events=true"
            f"&no_delay=true"
        )
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

        return Transcript(
            text=text,
            is_final=data.get("is_final", False) and data.get("speech_final", False),
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
