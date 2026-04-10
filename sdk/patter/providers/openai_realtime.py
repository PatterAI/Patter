import asyncio
import base64
import json
import logging

import websockets

logger = logging.getLogger("patter.openai_realtime")


class OpenAIRealtimeAdapter:
    """Bridges Twilio/Telnyx media stream to OpenAI Realtime API.

    Handles the full conversation loop: audio in → AI processing → audio out.
    No separate STT/TTS needed.
    """

    OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini-realtime-preview",
        voice: str = "alloy",
        instructions: str = "",
        language: str = "en",
        tools: list[dict] | None = None,
        audio_format: str = "g711_ulaw",
    ):
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.language = language
        self.tools = tools
        self.audio_format = audio_format
        self._ws = None
        self._running = False

    def __repr__(self) -> str:
        return f"OpenAIRealtimeAdapter(model={self.model!r}, voice={self.voice!r}, audio_format={self.audio_format!r})"

    async def connect(self) -> None:
        """Connect to OpenAI Realtime API."""
        url = f"{self.OPENAI_REALTIME_URL}?model={self.model}"
        self._ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {self.api_key}",
                "OpenAI-Beta": "realtime=v1",
            },
        )
        self._running = True

        # Wait for session.created
        response = await self._ws.recv()
        data = json.loads(response)
        if data.get("type") != "session.created":
            raise RuntimeError(f"Expected session.created, got {data.get('type')}")

        # Configure session audio format (g711_ulaw for Twilio, pcm16 for Telnyx)
        session_config: dict = {
            "input_audio_format": self.audio_format,
            "output_audio_format": self.audio_format,
            "voice": self.voice,
            "instructions": self.instructions or f"You are a helpful voice assistant. Respond in {self.language}. Be concise and natural.",
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
            },
            "input_audio_transcription": {
                "model": "whisper-1",
            },
        }
        if self.tools:
            session_config["tools"] = [
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                }
                for t in self.tools
            ]
        await self._ws.send(json.dumps({
            "type": "session.update",
            "session": session_config,
        }))

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to OpenAI Realtime API (format must match configured audio_format)."""
        if self._ws is None:
            return
        encoded = base64.b64encode(audio).decode("ascii")
        await self._ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": encoded,
        }))

    async def receive_events(self):
        """Yield events from OpenAI Realtime API.

        Yields tuples of (event_type, data):
        - ("audio", bytes) — audio chunk to send to Twilio
        - ("transcript_input", str) — what the user said
        - ("transcript_output", str) — what the AI said
        - ("speech_started", None) — user started speaking (barge-in)
        - ("response_done", None) — AI finished responding
        """
        if self._ws is None:
            return

        try:
            async for raw in self._ws:
                data = json.loads(raw)
                event_type = data.get("type", "")

                if event_type == "response.audio.delta":
                    # Audio chunk from AI — in the configured audio_format
                    audio_bytes = base64.b64decode(data.get("delta", ""))
                    yield ("audio", audio_bytes)

                elif event_type == "response.audio_transcript.delta":
                    # What the AI is saying (text)
                    yield ("transcript_output", data.get("delta", ""))

                elif event_type == "input_audio_buffer.speech_started":
                    # User started speaking — barge-in
                    yield ("speech_started", None)

                elif event_type == "input_audio_buffer.speech_stopped":
                    yield ("speech_stopped", None)

                elif event_type == "conversation.item.input_audio_transcription.completed":
                    # What the user said
                    yield ("transcript_input", data.get("transcript", ""))

                elif event_type == "response.function_call_arguments.done":
                    yield ("function_call", {
                        "call_id": data.get("call_id", ""),
                        "name": data.get("name", ""),
                        "arguments": data.get("arguments", "{}"),
                    })

                elif event_type == "response.done":
                    yield ("response_done", data.get("response", {}))

                elif event_type == "error":
                    logger.error("OpenAI Realtime error: %s", data.get("error", {}))

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._running = False

    async def cancel_response(self) -> None:
        """Cancel current AI response (for barge-in)."""
        if self._ws:
            await self._ws.send(json.dumps({"type": "response.cancel"}))

    async def send_text(self, text: str) -> None:
        """Send a text message to the AI (triggers a spoken response)."""
        if self._ws is None:
            return
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def send_function_result(self, call_id: str, result: str) -> None:
        """Send a function call result back to OpenAI and trigger a new response."""
        if self._ws is None:
            return
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def close(self) -> None:
        """Close the connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
