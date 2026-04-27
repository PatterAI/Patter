"""Ultravox realtime adapter.

Partially adapted (~70% port) from LiveKit Agents
(livekit-plugins/livekit-plugins-ultravox/livekit/plugins/ultravox/realtime/realtime_model.py,
Apache 2.0). Ultravox uses a pure WebSocket + aiohttp protocol — no vendor
SDK — so the port is substantially lighter than the Gemini one.

Reframed to Patter's ``connect``/``send_audio``/``receive_events``/``close``
surface, matching :class:`~getpatter.providers.openai_realtime.OpenAIRealtimeAdapter`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

logger = logging.getLogger("getpatter.ultravox_realtime")

# Ultravox v1 REST endpoint used to create an ephemeral call. The call
# response includes a ``joinUrl`` WebSocket URL that the client connects to
# for the audio/event stream.
DEFAULT_API_BASE = "https://api.ultravox.ai/api"
DEFAULT_SAMPLE_RATE_HZ = 16000


class UltravoxRealtimeAdapter:
    """Bridges a bidirectional audio stream to an Ultravox realtime call.

    Flow (matches LiveKit plugin):
        1. POST ``/calls`` to create a call and receive a ``joinUrl``.
        2. Open the ``joinUrl`` WebSocket.
        3. Binary frames on the socket are PCM16 audio (both directions).
        4. Text frames are JSON control events (transcripts, tool calls,
           state updates).

    Requires ``aiohttp>=3.10`` (``pip install getpatter[ultravox]``).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "fixie-ai/ultravox",
        voice: str = "",
        instructions: str = "",
        language: str = "en",
        tools: list[dict] | None = None,
        api_base: str = DEFAULT_API_BASE,
        sample_rate: int = DEFAULT_SAMPLE_RATE_HZ,
        first_message: str = "",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.language = language
        self.tools = tools
        self.api_base = api_base.rstrip("/")
        self.sample_rate = sample_rate
        self.first_message = first_message
        self._session: Any = None  # aiohttp.ClientSession
        self._ws: Any = None  # aiohttp.ClientWebSocketResponse
        self._running = False

    def __repr__(self) -> str:
        return (
            f"UltravoxRealtimeAdapter(model={self.model!r}, voice={self.voice!r}, "
            f"sample_rate={self.sample_rate})"
        )

    async def connect(self) -> None:
        """Create a call, then open the join WebSocket."""
        try:
            import aiohttp  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Ultravox requires the 'aiohttp' package. "
                "Install with: pip install getpatter[ultravox]"
            ) from exc

        self._session = aiohttp.ClientSession(
            headers={"X-API-Key": self.api_key},
        )

        create_payload: dict[str, Any] = {
            "model": self.model,
            "languageHint": self.language,
            # PCM16 mono at ``self.sample_rate``
            "medium": {
                "serverWebSocket": {
                    "inputSampleRate": self.sample_rate,
                    "outputSampleRate": self.sample_rate,
                }
            },
            "recordingEnabled": False,
        }
        if self.voice:
            create_payload["voice"] = self.voice
        if self.instructions:
            create_payload["systemPrompt"] = self.instructions
        # ``firstSpeaker`` and ``initialMessages`` are mutually exclusive on the
        # Ultravox API: setting both causes the server to reject the call.
        # Prefer ``initialMessages`` when a ``first_message`` is configured;
        # otherwise default to FIRST_SPEAKER_USER (user speaks first).
        if self.first_message:
            create_payload["initialOutputMedium"] = "MESSAGE_MEDIUM_VOICE"
            create_payload["initialMessages"] = [
                {"role": "MESSAGE_ROLE_AGENT", "text": self.first_message},
            ]
        else:
            create_payload["firstSpeaker"] = "FIRST_SPEAKER_USER"
        if self.tools:
            create_payload["selectedTools"] = [
                {
                    "temporaryTool": {
                        "modelToolName": t["name"],
                        "description": t.get("description", ""),
                        "dynamicParameters": _tool_params_to_ultravox(t.get("parameters", {})),
                    }
                }
                for t in self.tools
            ]

        try:
            async with self._session.post(
                f"{self.api_base}/calls",
                json=create_payload,
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Ultravox create call failed: {resp.status} {body}"
                    )
                call = await resp.json()
        except Exception:
            await self._session.close()
            self._session = None
            raise

        join_url = call.get("joinUrl")
        if not join_url:
            await self._session.close()
            self._session = None
            raise RuntimeError("Ultravox response missing joinUrl")

        self._ws = await self._session.ws_connect(join_url, heartbeat=20.0)
        self._running = True

    async def send_audio(self, audio: bytes) -> None:
        """Send a PCM16 mono binary frame at ``self.sample_rate``."""
        if self._ws is None:
            return
        await self._ws.send_bytes(audio)

    async def send_text(self, text: str) -> None:
        """Send a text turn. Ultravox treats this as ``input_text_message``."""
        if self._ws is None:
            return
        await self._ws.send_str(json.dumps({
            "type": "input_text_message",
            "text": text,
        }))

    async def send_function_result(self, call_id: str, result: str) -> None:
        """Return a client tool result."""
        if self._ws is None:
            return
        await self._ws.send_str(json.dumps({
            "type": "client_tool_result",
            "invocationId": call_id,
            "result": result,
            "responseType": "tool-response",
        }))

    async def cancel_response(self) -> None:
        """Ask the agent to stop speaking immediately (barge-in)."""
        if self._ws is None:
            return
        await self._ws.send_str(json.dumps({"type": "playback_clear_buffer"}))

    async def receive_events(self) -> AsyncIterator[tuple[str, Any]]:
        """Yield ``(event_type, payload)`` tuples from the Ultravox WebSocket.

        Yields:
            ``("audio", bytes)`` — PCM16 chunks from the agent
            ``("transcript_input", str)`` — user transcript (final)
            ``("transcript_output", str)`` — agent transcript delta
            ``("function_call", {"call_id", "name", "arguments"})``
            ``("speech_started", None)`` — user began speaking
            ``("response_done", None)`` — agent finished current turn
        """
        if self._ws is None:
            return

        try:
            import aiohttp  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover
            return

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    yield ("audio", msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        event = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning("Ultravox: non-JSON text frame")
                        continue
                    async for item in self._translate_event(event):
                        yield item
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            logger.error("Ultravox receive error: %s", exc)
        finally:
            self._running = False

    async def _translate_event(
        self, event: dict[str, Any]
    ) -> AsyncIterator[tuple[str, Any]]:
        """Map an Ultravox JSON event to Patter adapter event tuples."""
        etype = event.get("type", "")
        if etype == "transcript":
            role = event.get("role", "")
            text = event.get("text", "") or event.get("delta", "")
            is_final = bool(event.get("final", False))
            if role == "user" and is_final and text:
                yield ("transcript_input", text)
            elif role == "agent" and text:
                yield ("transcript_output", text)
        elif etype == "client_tool_invocation":
            yield (
                "function_call",
                {
                    "call_id": event.get("invocationId", ""),
                    "name": event.get("toolName", ""),
                    "arguments": json.dumps(event.get("parameters", {})),
                },
            )
        elif etype == "state":
            state = event.get("state", "")
            if state == "listening":
                yield ("speech_started", None)
            elif state == "idle":
                yield ("response_done", None)
        elif etype == "playback_clear_buffer":
            yield ("speech_started", None)

    async def close(self) -> None:
        """Close the WebSocket and underlying HTTP session."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover
                pass
            self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover
                pass
            self._session = None


def _tool_params_to_ultravox(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate OpenAI-style JSON Schema to Ultravox ``dynamicParameters``.

    Ultravox expects a flat list of parameter descriptors. This converts the
    typical ``{"type": "object", "properties": {...}, "required": [...]}``
    shape passed through Patter into Ultravox's format. Kept deliberately
    permissive — unknown shapes pass through as a single blob.
    """
    props = parameters.get("properties", {}) if isinstance(parameters, dict) else {}
    required = set(parameters.get("required", []) or [])
    out: list[dict[str, Any]] = []
    for name, schema in props.items():
        out.append({
            "name": name,
            "location": "PARAMETER_LOCATION_BODY",
            "schema": schema,
            "required": name in required,
        })
    return out
