"""Soak-test specific fixtures."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from getpatter.dashboard.store import MetricsStore
from getpatter.services.metrics import CallMetricsAccumulator


@pytest.fixture
def metrics_store() -> MetricsStore:
    """Return a fresh MetricsStore with default capacity (500)."""
    return MetricsStore(max_calls=500)


@pytest.fixture
def make_accumulator():
    """Factory fixture: returns a new CallMetricsAccumulator with sensible defaults."""

    def _factory(
        call_id: str = "soak-call-0",
        provider_mode: str = "pipeline",
        telephony_provider: str = "twilio",
        stt_provider: str = "deepgram",
        tts_provider: str = "elevenlabs",
        **kwargs: Any,
    ) -> CallMetricsAccumulator:
        return CallMetricsAccumulator(
            call_id=call_id,
            provider_mode=provider_mode,
            telephony_provider=telephony_provider,
            stt_provider=stt_provider,
            tts_provider=tts_provider,
            **kwargs,
        )

    return _factory


@pytest.fixture
def mock_ws_pair():
    """Return a (client, server) pair of mock WebSocket objects.

    The server can be programmatically disconnected/reconnected via
    ``server.disconnect()`` and ``server.reconnect()``.
    """

    class MockWebSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.state: str = "OPEN"
            self._recv_queue: asyncio.Queue[bytes | Exception] = asyncio.Queue()

        async def send(self, data: bytes) -> None:
            if self.state != "OPEN":
                raise ConnectionError("WebSocket is closed")
            self.sent.append(data)

        async def recv(self) -> bytes:
            item = await self._recv_queue.get()
            if isinstance(item, Exception):
                raise item
            return item

        def feed(self, data: bytes) -> None:
            self._recv_queue.put_nowait(data)

        def inject_error(self, exc: Exception) -> None:
            self._recv_queue.put_nowait(exc)

        def disconnect(self) -> None:
            self.state = "CLOSED"
            self._recv_queue.put_nowait(ConnectionError("disconnected"))

        def reconnect(self) -> None:
            self.state = "OPEN"
            # Drain any residual errors
            while not self._recv_queue.empty():
                try:
                    self._recv_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    return MockWebSocket(), MockWebSocket()
