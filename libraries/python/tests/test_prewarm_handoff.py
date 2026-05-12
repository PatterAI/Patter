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


# ---------------------------------------------------------------------------
# OpenAI Realtime parking + adoption — Patter._park_provider_connections
# must open and stash a primed ``session.updated`` WS for ``openai_realtime``
# agents so ``OpenAIRealtimeStreamHandler`` can adopt it on ``start``.
# ---------------------------------------------------------------------------


async def test_park_provider_connections_opens_realtime_session_ws() -> None:
    """Agent in ``openai_realtime`` mode → ``open_parked_connection`` runs
    on a transient Realtime adapter and the resulting WS lands in the slot."""
    from unittest.mock import AsyncMock
    import dataclasses

    phone = _make_patter()
    phone._local_config = dataclasses.replace(phone._local_config, openai_key="sk-test")
    agent = Agent(system_prompt="p", provider="openai_realtime", voice="alloy")

    parked_ws = FakeWS()
    captured: dict[str, object] = {}

    class _RecordingAdapter:
        def __init__(self, **kwargs: object) -> None:
            captured["init_kwargs"] = kwargs
            self.open_parked_connection = AsyncMock(return_value=parked_ws)

    import getpatter.providers.openai_realtime as realtime_mod

    original_adapter = realtime_mod.OpenAIRealtimeAdapter
    realtime_mod.OpenAIRealtimeAdapter = _RecordingAdapter  # type: ignore[misc]
    try:
        phone._park_provider_connections(agent, "CAtest_rt1")
        await _drain(phone)
    finally:
        realtime_mod.OpenAIRealtimeAdapter = original_adapter  # type: ignore[misc]

    slot = phone.pop_prewarmed_connections("CAtest_rt1")
    assert slot is not None
    assert slot.get("openai_realtime") is parked_ws
    # And the adapter received the right config.
    kwargs = captured["init_kwargs"]
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["audio_format"] == "g711_ulaw"


async def test_park_provider_connections_skips_realtime_without_openai_key() -> None:
    """No OpenAI key → no Realtime adapter built → no slot allocated."""
    phone = _make_patter()  # openai_key=""
    agent = Agent(system_prompt="p", provider="openai_realtime")

    constructed = 0

    class _RecordingAdapter:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructed
            constructed += 1

    import getpatter.providers.openai_realtime as realtime_mod

    original_adapter = realtime_mod.OpenAIRealtimeAdapter
    realtime_mod.OpenAIRealtimeAdapter = _RecordingAdapter  # type: ignore[misc]
    try:
        phone._park_provider_connections(agent, "CAtest_rt2")
        await _drain(phone)
    finally:
        realtime_mod.OpenAIRealtimeAdapter = original_adapter  # type: ignore[misc]

    assert constructed == 0
    assert phone.pop_prewarmed_connections("CAtest_rt2") is None


async def test_park_realtime_failure_does_not_propagate() -> None:
    """A failing ``open_parked_connection`` is best-effort — slot stays empty
    on the ``openai_realtime`` key but the call still proceeds."""
    from unittest.mock import AsyncMock
    import dataclasses

    phone = _make_patter()
    phone._local_config = dataclasses.replace(phone._local_config, openai_key="sk-test")
    agent = Agent(system_prompt="p", provider="openai_realtime")

    class _BoomAdapter:
        def __init__(self, **_kwargs: object) -> None:
            self.open_parked_connection = AsyncMock(
                side_effect=RuntimeError("network down")
            )

    import getpatter.providers.openai_realtime as realtime_mod

    original_adapter = realtime_mod.OpenAIRealtimeAdapter
    realtime_mod.OpenAIRealtimeAdapter = _BoomAdapter  # type: ignore[misc]
    try:
        phone._park_provider_connections(agent, "CAtest_rt3")
        await _drain(phone)
    finally:
        realtime_mod.OpenAIRealtimeAdapter = original_adapter  # type: ignore[misc]

    slot = phone.pop_prewarmed_connections("CAtest_rt3")
    # Slot is allocated (the helper sets it before scheduling tasks) but the
    # ``openai_realtime`` key was never populated. Falling back to a cold
    # ``connect()`` is the correct behaviour.
    if slot is not None:
        assert "openai_realtime" not in slot


async def test_realtime_stream_handler_adopts_parked_ws() -> None:
    """``OpenAIRealtimeStreamHandler.start()`` adopts a parked WS via
    ``adopt_websocket`` instead of calling ``connect()``."""
    from unittest.mock import AsyncMock, MagicMock

    from getpatter.stream_handler import OpenAIRealtimeStreamHandler

    parked_ws = FakeWS()
    pop_calls: list[str] = []

    def _pop(call_id: str) -> dict | None:
        pop_calls.append(call_id)
        return {"openai_realtime": parked_ws}

    agent = Agent(
        system_prompt="hi",
        first_message="",
        provider="openai_realtime",
        model="gpt-4o-mini-realtime-preview",
        voice="alloy",
    )
    audio_sender = MagicMock()
    audio_sender.send_audio = AsyncMock()

    handler = OpenAIRealtimeStreamHandler(
        agent=agent,
        audio_sender=audio_sender,
        call_id="CAtest_adopt",
        caller="+15550000001",
        callee="+15550000002",
        resolved_prompt="hi",
        metrics=None,
        openai_key="sk-test",
        audio_format="g711_ulaw",
        pop_prewarmed_connections=_pop,
    )

    # Patch the adapter so we can verify adopt vs connect without opening
    # a real WS.
    import getpatter.providers.openai_realtime as realtime_mod

    adapter_instance: dict[str, object] = {}

    class _StubAdapter:
        def __init__(self, **kwargs: object) -> None:
            self.connect = AsyncMock()
            self.adopt_websocket = MagicMock()
            self.send_first_message = AsyncMock()
            self.send_text = AsyncMock()
            self.receive_events = AsyncMock()
            adapter_instance["instance"] = self

    original_adapter = realtime_mod.OpenAIRealtimeAdapter
    realtime_mod.OpenAIRealtimeAdapter = _StubAdapter  # type: ignore[misc]
    try:
        await handler.start()
    finally:
        realtime_mod.OpenAIRealtimeAdapter = original_adapter  # type: ignore[misc]
        # Background _forward_events task — cancel to keep the test loop clean.
        bg = getattr(handler, "_background_task", None)
        if bg is not None:
            bg.cancel()

    assert pop_calls == ["CAtest_adopt"]
    inst = adapter_instance["instance"]
    inst.adopt_websocket.assert_called_once_with(parked_ws)  # type: ignore[attr-defined]
    inst.connect.assert_not_called()  # type: ignore[attr-defined]


async def test_realtime_stream_handler_falls_back_when_no_parked_slot() -> None:
    """No parked WS → handler calls ``connect()`` as normal."""
    from unittest.mock import AsyncMock, MagicMock

    from getpatter.stream_handler import OpenAIRealtimeStreamHandler

    agent = Agent(
        system_prompt="hi",
        first_message="",
        provider="openai_realtime",
        voice="alloy",
    )
    audio_sender = MagicMock()
    audio_sender.send_audio = AsyncMock()

    handler = OpenAIRealtimeStreamHandler(
        agent=agent,
        audio_sender=audio_sender,
        call_id="CAtest_cold",
        caller="+15550000001",
        callee="+15550000002",
        resolved_prompt="hi",
        metrics=None,
        openai_key="sk-test",
        audio_format="g711_ulaw",
        pop_prewarmed_connections=lambda _cid: None,
    )

    import getpatter.providers.openai_realtime as realtime_mod

    adapter_instance: dict[str, object] = {}

    class _StubAdapter:
        def __init__(self, **kwargs: object) -> None:
            self.connect = AsyncMock()
            self.adopt_websocket = MagicMock()
            self.send_first_message = AsyncMock()
            self.send_text = AsyncMock()
            self.receive_events = AsyncMock()
            adapter_instance["instance"] = self

    original_adapter = realtime_mod.OpenAIRealtimeAdapter
    realtime_mod.OpenAIRealtimeAdapter = _StubAdapter  # type: ignore[misc]
    try:
        await handler.start()
    finally:
        realtime_mod.OpenAIRealtimeAdapter = original_adapter  # type: ignore[misc]
        bg = getattr(handler, "_background_task", None)
        if bg is not None:
            bg.cancel()

    inst = adapter_instance["instance"]
    inst.connect.assert_awaited_once()  # type: ignore[attr-defined]
    inst.adopt_websocket.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Built-in tools (transfer_call / end_call) must be present in the primed
# session — without them, hit-prewarm calls cannot transfer or end gracefully
# because OpenAI server-side state has no record that those tools exist.
# ---------------------------------------------------------------------------


async def test_warmup_adapter_includes_builtin_and_user_tools() -> None:
    """``Patter._build_realtime_warmup_adapter`` must pass the canonical
    tools list (user tools + ``transfer_call`` + ``end_call``) to the
    transient adapter so the primed ``session.update`` matches the live
    one. Without this, adopted parked sessions silently refuse to call
    the built-ins."""
    import dataclasses

    from getpatter.stream_handler import END_CALL_TOOL, TRANSFER_CALL_TOOL

    phone = _make_patter()
    phone._local_config = dataclasses.replace(phone._local_config, openai_key="sk-test")

    custom_tool = {
        "name": "lookup_order",
        "description": "Look up an order by id",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    }
    agent = Agent(
        system_prompt="p",
        provider="openai_realtime",
        voice="alloy",
        tools=(custom_tool,),
    )

    adapter = phone._build_realtime_warmup_adapter(agent)
    assert adapter is not None
    # Real ``OpenAIRealtimeAdapter`` was instantiated with ``tools=[...]``.
    tool_names = [t["name"] for t in (adapter.tools or [])]
    assert "lookup_order" in tool_names, (
        f"user-defined tool missing from warmup adapter: {tool_names}"
    )
    assert TRANSFER_CALL_TOOL["name"] in tool_names, (
        f"transfer_call missing from warmup adapter: {tool_names}"
    )
    assert END_CALL_TOOL["name"] in tool_names, (
        f"end_call missing from warmup adapter: {tool_names}"
    )


async def test_warmup_adapter_includes_builtins_when_agent_has_no_tools() -> None:
    """Even with no user tools, the warmup adapter must carry the two
    Patter-injected built-ins so adopted sessions can still transfer /
    end calls."""
    import dataclasses

    from getpatter.stream_handler import END_CALL_TOOL, TRANSFER_CALL_TOOL

    phone = _make_patter()
    phone._local_config = dataclasses.replace(phone._local_config, openai_key="sk-test")
    agent = Agent(system_prompt="p", provider="openai_realtime")

    adapter = phone._build_realtime_warmup_adapter(agent)
    assert adapter is not None
    tool_names = [t["name"] for t in (adapter.tools or [])]
    assert tool_names == [TRANSFER_CALL_TOOL["name"], END_CALL_TOOL["name"]]


async def test_realtime_stream_handler_falls_back_when_parked_ws_died() -> None:
    """A parked WS whose underlying socket closed between park and adopt
    is detected via ``closed`` and the handler falls through to ``connect()``."""
    from unittest.mock import AsyncMock, MagicMock

    from getpatter.stream_handler import OpenAIRealtimeStreamHandler

    dead_ws = FakeWS()
    dead_ws.closed = True  # WS died during the ringing window

    agent = Agent(
        system_prompt="hi",
        first_message="",
        provider="openai_realtime",
        voice="alloy",
    )
    audio_sender = MagicMock()
    audio_sender.send_audio = AsyncMock()

    handler = OpenAIRealtimeStreamHandler(
        agent=agent,
        audio_sender=audio_sender,
        call_id="CAtest_dead",
        caller="+15550000001",
        callee="+15550000002",
        resolved_prompt="hi",
        metrics=None,
        openai_key="sk-test",
        audio_format="g711_ulaw",
        pop_prewarmed_connections=lambda _cid: {"openai_realtime": dead_ws},
    )

    import getpatter.providers.openai_realtime as realtime_mod

    adapter_instance: dict[str, object] = {}

    class _StubAdapter:
        def __init__(self, **kwargs: object) -> None:
            self.connect = AsyncMock()
            self.adopt_websocket = MagicMock()
            self.send_first_message = AsyncMock()
            self.send_text = AsyncMock()
            self.receive_events = AsyncMock()
            adapter_instance["instance"] = self

    original_adapter = realtime_mod.OpenAIRealtimeAdapter
    realtime_mod.OpenAIRealtimeAdapter = _StubAdapter  # type: ignore[misc]
    try:
        await handler.start()
    finally:
        realtime_mod.OpenAIRealtimeAdapter = original_adapter  # type: ignore[misc]
        bg = getattr(handler, "_background_task", None)
        if bg is not None:
            bg.cancel()

    inst = adapter_instance["instance"]
    inst.connect.assert_awaited_once()  # type: ignore[attr-defined]
    inst.adopt_websocket.assert_not_called()  # type: ignore[attr-defined]
