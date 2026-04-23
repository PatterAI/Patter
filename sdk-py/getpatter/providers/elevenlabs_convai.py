import asyncio
import base64
import json
import logging

import websockets

logger = logging.getLogger("patter")

ELEVENLABS_CONVAI_URL = "wss://api.elevenlabs.io/v1/convai/conversation"


class ElevenLabsConvAIAdapter:
    """Bridges Twilio/Telnyx media stream to ElevenLabs Conversational AI.

    Handles full conversation: STT + LLM + TTS in one WebSocket.
    Uses ElevenLabs premium voices.
    """

    def __init__(
        self,
        api_key: str,
        agent_id: str = "",
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model_id: str = "eleven_turbo_v2_5",
        language: str = "it",
        first_message: str = "",
    ):
        self.api_key = api_key
        self.agent_id = agent_id
        self.voice_id = voice_id
        self.model_id = model_id
        self.language = language
        self.first_message = first_message
        self._ws = None
        self._running = False

    def __repr__(self) -> str:
        return f"ElevenLabsConvAIAdapter(agent_id={self.agent_id!r}, model_id={self.model_id!r})"

    async def connect(self) -> None:
        """Connect to ElevenLabs Conversational AI."""
        url = ELEVENLABS_CONVAI_URL
        if self.agent_id:
            url = f"{url}?agent_id={self.agent_id}"

        self._ws = await websockets.connect(
            url,
            additional_headers={
                "xi-api-key": self.api_key,
            },
        )
        self._running = True

        # Send initial configuration
        config = {
            "type": "conversation_initiation_client_data",
            "conversation_config_override": {
                "tts": {
                    "voice_id": self.voice_id,
                },
            },
        }

        if self.first_message:
            config["conversation_config_override"]["agent"] = {
                "first_message": self.first_message,
            }

        await self._ws.send(json.dumps(config))

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send audio to ElevenLabs. Expects base64-encoded audio."""
        if self._ws is None:
            return
        # ElevenLabs expects base64 audio in a JSON message
        await self._ws.send(json.dumps({
            "type": "audio",
            "audio": base64.b64encode(audio_bytes).decode("ascii"),
        }))

    async def receive_events(self):
        """Yield events from ElevenLabs ConvAI.

        Yields tuples of (event_type, data):
        - ("audio", bytes) — audio chunk to send to caller
        - ("transcript_input", str) — what the user said
        - ("transcript_output", str) — what the AI said
        - ("interruption", None) — user interrupted
        """
        if self._ws is None:
            return

        try:
            async for raw in self._ws:
                data = json.loads(raw)
                msg_type = data.get("type", "")

                if msg_type == "audio":
                    # Audio response from ElevenLabs
                    audio_b64 = data.get("audio", "")
                    if audio_b64:
                        yield ("audio", base64.b64decode(audio_b64))

                elif msg_type == "user_transcript":
                    yield ("transcript_input", data.get("text", ""))

                elif msg_type == "agent_response":
                    yield ("transcript_output", data.get("text", ""))
                    # ElevenLabs agent_response is complete text, signal turn done
                    yield ("response_done", {})

                elif msg_type == "interruption":
                    yield ("interruption", None)

                elif msg_type == "error":
                    logger.error("ElevenLabs ConvAI error: %s", data)

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._running = False

    async def close(self) -> None:
        """Close the connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
