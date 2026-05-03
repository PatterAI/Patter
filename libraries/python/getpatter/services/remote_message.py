"""Remote message handler for B2B webhook and WebSocket integration.

Allows ``on_message`` to be a URL string instead of a callable:

- HTTP webhook: ``on_message="https://api.customer.com/patter/message"``
- WebSocket: ``on_message="ws://localhost:9000/stream"``
"""

from __future__ import annotations

__all__ = ["RemoteMessageHandler", "is_remote_url", "is_websocket_url"]

import hashlib
import hmac
import json
import logging
from typing import AsyncGenerator

import httpx

logger = logging.getLogger("getpatter")

# Maximum response size from webhook (64 KB)
_MAX_RESPONSE_BYTES = 64 * 1024


class RemoteMessageHandler:
    """Bridges on_message to an external HTTP webhook or WebSocket.

    Args:
        webhook_secret: Optional HMAC secret. When provided, outgoing webhook
            requests include an ``X-Patter-Signature`` header so the receiver
            can verify the payload originated from Patter.
    """

    def __init__(self, webhook_secret: str | None = None) -> None:
        self._webhook_secret = webhook_secret
        self._client = httpx.AsyncClient(timeout=30.0)

    def _sign_payload(self, body: bytes) -> str:
        """Compute HMAC-SHA256 hex digest for *body*."""
        if self._webhook_secret is None:
            raise ValueError("Cannot sign without a webhook_secret")
        return hmac.new(
            self._webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

    async def close(self) -> None:
        """Close the underlying HTTP client and release connections."""
        await self._client.aclose()

    async def call_webhook(self, url: str, data: dict) -> str:
        """POST transcript to HTTP webhook, return response text.

        The webhook receives a JSON payload::

            {"text": "...", "call_id": "...", "caller": "...",
             "callee": "...", "history": [...]}

        The response can be plain text or JSON ``{"text": "..."}``.

        When ``webhook_secret`` was provided at construction time, the request
        includes an ``X-Patter-Signature`` header with the HMAC-SHA256 hex
        digest of the JSON body.

        Args:
            url: The webhook URL.
            data: Message data dict.

        Returns:
            The agent's response text.
        """
        if url.startswith("http://"):
            logger.warning(
                "Webhook URL uses unencrypted http:// — call transcripts "
                "and phone numbers will be sent in plaintext. "
                "Use https:// in production."
            )
        from getpatter.tools.tool_executor import _validate_webhook_url

        try:
            _validate_webhook_url(url)
        except ValueError as exc:
            logger.warning("on_message webhook URL rejected: %s", exc)
            return ""
        body = json.dumps(data).encode("utf-8")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._webhook_secret is not None:
            headers["X-Patter-Signature"] = self._sign_payload(body)
        response = await self._client.post(url, content=body, headers=headers)
        response.raise_for_status()

        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ValueError(
                f"Webhook response too large: {len(response.content)} bytes "
                f"(max {_MAX_RESPONSE_BYTES})"
            )

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            body = response.json()
            if isinstance(body, dict):
                return body.get("text", "")
            return str(body)
        return response.text

    async def call_websocket(self, url: str, data: dict) -> AsyncGenerator[str, None]:
        """Send transcript via WebSocket, yield response chunks.

        Sends the message data as JSON. Receives one or more JSON frames
        with ``{"text": "..."}`` — multiple frames enable streaming.

        A frame with ``{"done": true}`` signals end of response.

        Args:
            url: The WebSocket URL (``ws://`` or ``wss://``).
            data: Message data dict.

        Yields:
            Text chunks from the remote server.
        """
        if url.startswith("ws://"):
            logger.warning(
                "WebSocket URL uses unencrypted ws:// — call transcripts "
                "and phone numbers will be sent in plaintext. "
                "Use wss:// in production."
            )
        import ipaddress
        from urllib.parse import urlparse
        from getpatter.tools.tool_executor import _BLOCKED_HOSTNAMES

        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            logger.warning(
                "on_message WebSocket URL rejected: invalid scheme %r", parsed.scheme
            )
            return
        hostname = parsed.hostname or ""
        if not hostname:
            logger.warning("on_message WebSocket URL rejected: missing hostname")
            return
        if hostname.lower() in _BLOCKED_HOSTNAMES:
            logger.warning(
                "on_message WebSocket URL rejected: blocked hostname %r", hostname
            )
            return
        try:
            addr = ipaddress.ip_address(hostname)
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_reserved
            ):
                logger.warning(
                    "on_message WebSocket URL rejected: points to private/reserved address %r",
                    hostname,
                )
                return
        except ValueError:
            pass
        try:
            import websockets
        except ImportError:
            raise ImportError(
                "The 'websockets' package is required for WebSocket on_message. "
                "Install it with: pip install websockets"
            )

        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(data))

            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    # Plain text frame
                    yield str(raw)
                    continue

                if isinstance(frame, dict):
                    if frame.get("done"):
                        return
                    text = frame.get("text", "")
                    if text:
                        yield text
                else:
                    yield str(frame)


def is_remote_url(on_message) -> bool:
    """Check if on_message is a remote URL string."""
    if not isinstance(on_message, str):
        return False
    return on_message.startswith(("http://", "https://", "ws://", "wss://"))


def is_websocket_url(url: str) -> bool:
    """Check if a URL is a WebSocket URL."""
    return url.startswith(("ws://", "wss://"))
