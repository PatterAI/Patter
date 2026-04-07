import asyncio
import json
import logging
from typing import Callable, Awaitable
import websockets
from patter.exceptions import PatterConnectionError
from patter.models import IncomingMessage


class PatterConnection:
    def __init__(self, api_key: str, backend_url: str = "wss://api.patter.dev") -> None:
        self._api_key = api_key
        self.backend_url = backend_url.rstrip("/")
        self._ws_url = f"{self.backend_url}/ws/sdk"
        self._ws = None
        self._running = False
        self._on_message: Callable[[IncomingMessage], Awaitable[str]] | None = None
        self._on_call_start: Callable[[dict], Awaitable[None]] | None = None
        self._on_call_end: Callable[[dict], Awaitable[None]] | None = None
        self._listen_task: asyncio.Task | None = None

    def __repr__(self) -> str:
        return f"PatterConnection(backend_url={self.backend_url!r})"

    def _parse_message(self, raw: str) -> IncomingMessage | None:
        data = json.loads(raw)
        if data.get("type") != "message":
            return None
        return IncomingMessage(text=data["text"], call_id=data["call_id"], caller=data.get("caller", ""))

    async def send_response(self, call_id: str, text: str) -> None:
        if self._ws is None:
            raise PatterConnectionError("Not connected")
        await self._ws.send(json.dumps({"type": "response", "call_id": call_id, "text": text}))

    async def connect(
        self,
        on_message: Callable[[IncomingMessage], Awaitable[str]],
        on_call_start: Callable[[dict], Awaitable[None]] | None = None,
        on_call_end: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self._on_message = on_message
        self._on_call_start = on_call_start
        self._on_call_end = on_call_end
        self._running = True
        try:
            self._ws = await websockets.connect(
                self._ws_url,
                additional_headers={"X-API-Key": self._api_key},
            )
        except Exception as e:
            raise PatterConnectionError(f"Failed to connect to Patter: {e}") from e

        # Start listen loop as background task (non-blocking)
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "message" and self._on_message is not None:
                    message = self._parse_message(raw)
                    if message is not None:
                        try:
                            response_text = await self._on_message(message)
                            if response_text is not None:
                                await self.send_response(message.call_id, response_text)
                        except Exception:
                            import logging
                            logging.getLogger("patter").exception(
                                "on_message handler error for call_id=%s", message.call_id
                            )

                elif msg_type == "call_start" and self._on_call_start is not None:
                    try:
                        await self._on_call_start(data)
                    except Exception:
                        logging.getLogger("patter").exception(
                            "on_call_start handler error"
                        )

                elif msg_type == "call_end" and self._on_call_end is not None:
                    try:
                        await self._on_call_end(data)
                    except Exception:
                        logging.getLogger("patter").exception(
                            "on_call_end handler error"
                        )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._running = False

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            return self._ws.state.name == "OPEN"
        except AttributeError:
            return self._ws is not None

    async def request_call(self, from_number: str, to_number: str, first_message: str = "") -> None:
        if self._ws is None:
            raise PatterConnectionError("Not connected")
        await self._ws.send(json.dumps({"type": "call", "from": from_number, "to": to_number, "first_message": first_message}))

    async def disconnect(self) -> None:
        self._running = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
