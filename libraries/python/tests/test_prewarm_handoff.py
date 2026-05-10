"""Tests for the prewarm-handoff (FIX A) — keep parked WSs OPEN and adopt
them at call connect, instead of close-and-reopen which doesn't warm
TLS on Node ``ws`` (Python ``websockets`` has the same issue at the
TCP / TLS level).

Coverage:
  1. ``Patter._park_provider_connections`` invokes
     ``open_parked_connection`` on the configured STT / TTS adapters.
  2. The parked WS stays OPEN past the historic 250 ms idle window.
  3. ``pop_prewarmed_connections`` returns the parked handles and
     removes them from the cache (consume-once semantics).
  4. ``close_prewarmed_connections`` (and ``_record_prewarm_waste``)
     drains parked sockets cleanly.
  5. A handle whose underlying WS died between park and adopt is
     dropped silently.

Tests use authentic real code paths — only the carrier HTTP boundary
and provider WS open are mocked. See
``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from getpatter.client import Patter
from getpatter.models import Agent
from getpatter.providers.base import STTProvider, TTSProvider, Transcript


class FakeWS:
    """Minimal stand-in for the per-provider WS handles used in
    parking tests. Mirrors the public surface the SDK reads —
    ``closed`` and ``close()``."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class StubSession:
    """aiohttp.ClientSession-shaped stub used as the first half of
    Cartesia STT's ``(session, ws)`` parked-handle tuple."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class StubSTTWithPark(STTProvider):
    def __init__(self) -> None:
        self.park_calls = 0
        self.adopt_calls = 0
        self.parked_session: StubSession | None = None
        self.parked_ws: FakeWS | None = None

    async def connect(self) -> None:  # pragma: no cover - unused in handoff tests
        return None

    async def send_audio(self, audio_chunk: bytes) -> None:  # pragma: no cover
        return None

    async def receive_transcripts(
        self,
    ) -> AsyncIterator[Transcript]:  # pragma: no cover
        if False:
            yield  # pragma: no cover

    async def close(self) -> None:
        return None

    async def open_parked_connection(self) -> tuple[StubSession, FakeWS]:
        self.park_calls += 1
        self.parked_session = StubSession()
        self.parked_ws = FakeWS()
        return self.parked_session, self.parked_ws

    def adopt_websocket(
        self, session: StubSession, ws: FakeWS
    ) -> None:  # pragma: no cover - drained via pop in tests
        self.adopt_calls += 1


class StubParkedTTS:
    """Mimic of ``ElevenLabsParkedWS``: object with ``.ws`` attribute."""

    def __init__(self) -> None:
        self.ws = FakeWS()
        self.bos_sent = True


class StubTTSWithPark(TTSProvider):
    def __init__(self) -> None:
        self.park_calls = 0
        self.adopt_calls = 0
        self.parked_handle: StubParkedTTS | None = None

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:  # pragma: no cover
        if False:
            yield b""

    async def close(self) -> None:
        return None

    async def open_parked_connection(self) -> StubParkedTTS:
        self.park_calls += 1
        self.parked_handle = StubParkedTTS()
        return self.parked_handle

    def adopt_websocket(
        self, parked: StubParkedTTS
    ) -> None:  # pragma: no cover - drained via pop in tests
        self.adopt_calls += 1


def _make_patter() -> Patter:
    from getpatter.carriers.twilio import Carrier as Twilio

    return Patter(
        carrier=Twilio(
            account_sid="ACtest000000000000000000000000000",
            auth_token="test_auth_token_000000000000000000",
        ),
        phone_number="+15551234567",
        webhook_url="example.test",
    )


async def _drain(phone: Patter, timeout: float = 1.0) -> None:
    if phone._prewarm_tasks:
        await asyncio.wait_for(
            asyncio.gather(*phone._prewarm_tasks, return_exceptions=True),
            timeout=timeout,
        )


async def test_park_provider_connections_calls_open_on_stt_and_tts() -> None:
    phone = _make_patter()
    stt = StubSTTWithPark()
    tts = StubTTSWithPark()
    agent = Agent(system_prompt="p", provider="pipeline", stt=stt, tts=tts)
    phone._park_provider_connections(agent, "CAtest1")
    await _drain(phone)
    assert stt.park_calls == 1
    assert tts.park_calls == 1


async def test_parked_ws_stays_open_past_historic_idle_window() -> None:
    phone = _make_patter()
    stt = StubSTTWithPark()
    tts = StubTTSWithPark()
    agent = Agent(system_prompt="p", provider="pipeline", stt=stt, tts=tts)
    phone._park_provider_connections(agent, "CAtest2")
    await _drain(phone)
    # Sleep well past the historic 250 ms warmup-then-close window.
    await asyncio.sleep(0.4)
    assert stt.parked_ws is not None and not stt.parked_ws.closed
    assert tts.parked_handle is not None and not tts.parked_handle.ws.closed


async def test_pop_prewarmed_connections_consume_once() -> None:
    phone = _make_patter()
    stt = StubSTTWithPark()
    tts = StubTTSWithPark()
    agent = Agent(system_prompt="p", provider="pipeline", stt=stt, tts=tts)
    phone._park_provider_connections(agent, "CAtest3")
    await _drain(phone)
    slot = phone.pop_prewarmed_connections("CAtest3")
    assert slot is not None
    assert slot["stt"] == (stt.parked_session, stt.parked_ws)
    assert slot["tts"] is tts.parked_handle
    # Second pop returns None — slot already drained.
    assert phone.pop_prewarmed_connections("CAtest3") is None


async def test_close_prewarmed_connections_drains_sockets() -> None:
    phone = _make_patter()
    stt = StubSTTWithPark()
    tts = StubTTSWithPark()
    agent = Agent(system_prompt="p", provider="pipeline", stt=stt, tts=tts)
    phone._park_provider_connections(agent, "CAtest4")
    await _drain(phone)
    assert stt.parked_ws is not None and not stt.parked_ws.closed
    phone.close_prewarmed_connections("CAtest4")
    # Closes are scheduled asynchronously via create_task — drain them.
    for _ in range(5):
        await asyncio.sleep(0)
    assert stt.parked_ws.closed is True
    assert tts.parked_handle is not None and tts.parked_handle.ws.closed is True
    # Slot drained.
    assert phone.pop_prewarmed_connections("CAtest4") is None


async def test_record_prewarm_waste_drains_parked_sockets() -> None:
    phone = _make_patter()
    stt = StubSTTWithPark()
    tts = StubTTSWithPark()
    agent = Agent(system_prompt="p", provider="pipeline", stt=stt, tts=tts)
    phone._park_provider_connections(agent, "CAtest5")
    await _drain(phone)
    phone._record_prewarm_waste("CAtest5")
    for _ in range(5):
        await asyncio.sleep(0)
    assert stt.parked_ws is not None and stt.parked_ws.closed is True
    assert tts.parked_handle is not None and tts.parked_handle.ws.closed is True


async def test_park_skipped_when_neither_provider_supports_parking() -> None:
    phone = _make_patter()

    # Adapters without ``open_parked_connection`` must not allocate a slot.
    class MinimalSTT(STTProvider):
        async def connect(self) -> None:
            return None

        async def send_audio(self, _ac: bytes) -> None:
            return None

        async def receive_transcripts(
            self,
        ) -> AsyncIterator[Transcript]:  # pragma: no cover
            if False:
                yield  # pragma: no cover

        async def close(self) -> None:
            return None

    agent = Agent(system_prompt="p", provider="pipeline", stt=MinimalSTT())
    phone._park_provider_connections(agent, "CAtest6")
    # No slot was created — pop returns None.
    assert phone.pop_prewarmed_connections("CAtest6") is None
