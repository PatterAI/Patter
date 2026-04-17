"""Call orchestrator — routes audio between STT/TTS and telephony."""

import base64
import json

from patter.providers.base import Transcript
from patter.services.session_manager import CallSession
from patter.services.transcoding import pcm16_to_mulaw, resample_16k_to_8k


class CallOrchestrator:
    def __init__(
        self,
        session: CallSession,
        needs_transcoding: bool = False,
        on_transcript=None,
        on_call_start=None,
        on_call_end=None,
    ):
        self._session = session
        self._needs_transcoding = needs_transcoding
        self._is_speaking = False
        self._on_transcript = on_transcript
        self._on_call_start = on_call_start
        self._on_call_end = on_call_end

    async def handle_transcript(self, transcript: Transcript) -> None:
        if not transcript.is_final:
            if self._is_speaking and transcript.text:
                await self.handle_barge_in()
            return
        if self._on_transcript:
            await self._on_transcript({
                "text": transcript.text,
                "call_id": self._session.call_id,
                "caller": self._session.caller,
                "is_final": True,
            })

    async def handle_sdk_response(self, text: str) -> None:
        if self._session.tts is None:
            return
        self._is_speaking = True
        async for audio_chunk in self._session.tts.synthesize(text):
            if not self._is_speaking:
                break
            if self._needs_transcoding:
                audio_chunk = resample_16k_to_8k(audio_chunk)
                audio_chunk = pcm16_to_mulaw(audio_chunk)
            await self._send_audio_to_telephony(audio_chunk)
        self._is_speaking = False

    async def handle_barge_in(self) -> None:
        self._is_speaking = False
        if self._session.telephony_websocket is not None:
            if self._needs_transcoding:
                msg = json.dumps({"event": "clear", "streamSid": self._session.metadata.get("stream_sid", "")})
            else:
                msg = json.dumps({"event": "clear"})
            await self._session.telephony_websocket.send_text(msg)

    async def _send_audio_to_telephony(self, data: bytes) -> None:
        if not self._session.telephony_websocket:
            return
        encoded = base64.b64encode(data).decode("ascii")
        if self._needs_transcoding:
            payload = json.dumps({"event": "media", "streamSid": self._session.metadata.get("stream_sid", ""), "media": {"payload": encoded}})
        else:
            payload = json.dumps({"event": "media", "media": {"payload": encoded}})
        await self._session.telephony_websocket.send_text(payload)

    async def send_call_start(self) -> None:
        if self._on_call_start:
            await self._on_call_start({
                "call_id": self._session.call_id,
                "caller": self._session.caller,
                "callee": self._session.callee,
                "direction": self._session.direction,
            })

    async def send_call_end(self) -> None:
        if self._on_call_end:
            await self._on_call_end({"call_id": self._session.call_id})
