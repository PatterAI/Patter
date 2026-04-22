"""Shared pytest fixtures and helpers for the Patter SDK test suite."""

from __future__ import annotations

import struct
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter.models import Agent


# ---------------------------------------------------------------------------
# Helper functions (plain, not fixtures — import where needed)
# ---------------------------------------------------------------------------


def fake_pcm_frame(duration_ms: int = 20, sample_rate: int = 16000) -> bytes:
    """Return PCM silence bytes (16-bit LE, mono) for *duration_ms* ms."""
    num_samples = int(sample_rate * duration_ms / 1000)
    return b"\x00\x00" * num_samples


def fake_mulaw_frame(duration_ms: int = 20) -> bytes:
    """Return mulaw-encoded silence bytes (8 kHz, 8-bit mono).

    mulaw silence is encoded as 0xFF.
    """
    num_samples = int(8000 * duration_ms / 1000)
    return b"\xff" * num_samples


def make_agent(**overrides: Any) -> Agent:
    """Return an ``Agent`` dataclass with sensible defaults.

    Any keyword argument overrides the corresponding default field.
    """
    defaults: dict[str, Any] = {
        "system_prompt": "You are a helpful test agent.",
        "voice": "alloy",
        "model": "gpt-4o-mini-realtime-preview",
        "language": "en",
        "first_message": "Hello, how can I help?",
        "provider": "pipeline",
        "tools": None,
        "stt": None,
        "tts": None,
        "variables": None,
        "guardrails": None,
    }
    merged = {**defaults, **overrides}
    return Agent(**merged)


def make_config(**overrides: Any) -> dict[str, Any]:
    """Return a config dict suitable for ``Patter()`` init in local mode."""
    defaults: dict[str, Any] = {
        "mode": "local",
        "phone_number": "+15551234567",
        "twilio_sid": "ACtest000000000000000000000000000",
        "twilio_token": "test_auth_token_000000000000000000",
        "openai_key": "sk-test-key-0000000000000000000000",
        "webhook_url": "test.ngrok.io",
    }
    return {**defaults, **overrides}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ws_server():
    """Async context-manager fixture yielding a mock WebSocket.

    The mock records sent messages in ``ws.sent`` and received messages
    in ``ws.received``.  Call ``ws.recv.return_value`` or
    ``ws.recv.side_effect`` to configure what the consumer reads.
    """

    class _WSContext:
        def __init__(self) -> None:
            self.ws = AsyncMock()
            self.ws.sent: list[Any] = []
            self.ws.received: list[Any] = []

            # Track sent messages
            original_send = self.ws.send

            async def _tracking_send(data: Any) -> None:
                self.ws.sent.append(data)
                return await original_send(data)

            self.ws.send = AsyncMock(side_effect=_tracking_send)

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *exc: Any):
            pass

    return _WSContext()


@pytest.fixture
def mock_http_client():
    """Return a mocked ``httpx.AsyncClient`` with configurable responses.

    Configure responses via ``client.return_value`` or by setting
    side effects on individual methods (``client.get``, ``client.post``, etc.).
    """
    client = AsyncMock()
    # Provide a default 200 response for any method
    default_response = MagicMock()
    default_response.status_code = 200
    default_response.json.return_value = {}
    default_response.text = ""
    default_response.raise_for_status = MagicMock()

    client.get.return_value = default_response
    client.post.return_value = default_response
    client.put.return_value = default_response
    client.patch.return_value = default_response
    client.delete.return_value = default_response

    return client


@pytest.fixture
def mock_twilio_webhook():
    """Return a callable that builds a Twilio-style webhook POST request.

    Usage::

        req = mock_twilio_webhook(
            url="/webhook/voice",
            body={"CallSid": "CA123", "From": "+15551234567"},
        )
    """

    def _build(
        url: str = "/webhook/voice",
        body: dict[str, str] | None = None,
        signature: str = "mock-twilio-signature",
    ) -> dict[str, Any]:
        return {
            "method": "POST",
            "url": url,
            "headers": {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": signature,
            },
            "body": body or {
                "CallSid": "CA00000000000000000000000000000000",
                "AccountSid": "ACtest000000000000000000000000000",
                "From": "+15551234567",
                "To": "+15559876543",
                "CallStatus": "ringing",
            },
        }

    return _build


@pytest.fixture
def mock_telnyx_webhook():
    """Return a callable that builds a Telnyx-style webhook POST request.

    Usage::

        req = mock_telnyx_webhook(
            url="/webhook/telnyx",
            event_type="call.initiated",
        )
    """

    def _build(
        url: str = "/webhook/telnyx",
        event_type: str = "call.initiated",
        call_control_id: str = "v3:test-call-control-id",
        call_leg_id: str = "test-call-leg-id",
        connection_id: str = "test-connection-id",
        from_number: str = "+15551234567",
        to_number: str = "+15559876543",
    ) -> dict[str, Any]:
        return {
            "method": "POST",
            "url": url,
            "headers": {
                "Content-Type": "application/json",
            },
            "body": {
                "data": {
                    "event_type": event_type,
                    "id": "evt-test-id",
                    "occurred_at": "2025-01-01T00:00:00.000000Z",
                    "payload": {
                        "call_control_id": call_control_id,
                        "call_leg_id": call_leg_id,
                        "call_session_id": "test-session-id",
                        "connection_id": connection_id,
                        "from": from_number,
                        "to": to_number,
                        "state": "parked" if event_type == "call.initiated" else "active",
                    },
                    "record_type": "event",
                },
                "meta": {
                    "attempt": 1,
                    "delivered_to": url,
                },
            },
        }

    return _build
