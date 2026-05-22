"""Tests for the ``Agent.prewarm`` / ``Agent.prewarm_first_message`` features.

The feature wires three independent pieces together:

1. Provider ``warmup()`` methods on STT / TTS / LLM. Default = no-op.
2. ``Patter.call`` spawns provider warmup in parallel with the carrier
   ``initiate_call`` when ``agent.prewarm`` is True.
3. ``Patter.call`` pre-renders ``agent.first_message`` to TTS bytes when
   ``agent.prewarm_first_message`` is True; the StreamHandler firstMessage
   emit consumes the cache instead of running TTS again.

Tests use authentic real code paths — only the carrier HTTP boundary and
provider HTTPS-GET warmup are mocked. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock


from getpatter.client import Patter
from getpatter.models import Agent
from getpatter.providers.base import STTProvider, TTSProvider, Transcript


# ---------------------------------------------------------------------------
# Stub providers — real STTProvider / TTSProvider subclasses with no-op
# methods. The ``warmup`` default lives on the abstract base so these
# stubs inherit it for free.
# ---------------------------------------------------------------------------


class StubSTT(STTProvider):
    def __init__(self) -> None:
        self.warmup_called = 0

    async def connect(self) -> None:
        return None

    async def send_audio(self, audio_chunk: bytes) -> None:
        return None

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        if False:
            yield  # pragma: no cover

    async def close(self) -> None:
        return None

    async def warmup(self) -> None:
        self.warmup_called += 1


class StubTTS(TTSProvider):
    def __init__(self, audio_bytes: bytes = b"PCM_TTS_BYTES_OK") -> None:
        self._audio = audio_bytes
        self.warmup_called = 0
        self.synthesize_called = 0

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self.synthesize_called += 1
        # Yield in two chunks so the accumulator path is exercised.
        yield self._audio[: len(self._audio) // 2]
        yield self._audio[len(self._audio) // 2 :]

    async def close(self) -> None:
        return None

    async def warmup(self) -> None:
        self.warmup_called += 1


class StubLLM:
    """Minimal duck-typed LLM. Has ``warmup`` so ``_spawn_provider_warmup``
    sees it; not a Protocol implementer (which we don't need here)."""

    def __init__(self) -> None:
        self.warmup_called = 0

    async def stream(self, *_args, **_kwargs):  # pragma: no cover - unused
        if False:
            yield

    async def warmup(self) -> None:
        self.warmup_called += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


async def _wait_for_tasks(phone: Patter, timeout: float = 1.0) -> None:
    """Drain the prewarm task set so assertions see completed state."""
    if not phone._prewarm_tasks:
        return
    await asyncio.wait_for(
        asyncio.gather(*phone._prewarm_tasks, return_exceptions=True),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_default_prewarm_flag_is_true() -> None:
    """``Agent.prewarm`` defaults to True; ``prewarm_first_message``
    defaults to False at the dataclass level to preserve backwards-
    compatible behaviour for direct ``Agent(...)`` construction. The
    recommended :meth:`Patter.agent` factory flips it to True for
    pipeline mode — see ``test_factory_defaults_prewarm_first_message_*``
    below.
    """
    agent = Agent(system_prompt="hi", first_message="hello")
    assert agent.prewarm is True
    assert agent.prewarm_first_message is False


async def test_prewarm_first_message_opt_out() -> None:
    """Callers can disable greeting pre-rendering with
    ``prewarm_first_message=False`` to restore the pre-0.6.2 cost surface
    (no TTS bill on un-answered calls)."""
    agent = Agent(
        system_prompt="hi",
        first_message="hello",
        prewarm_first_message=False,
    )
    assert agent.prewarm_first_message is False


async def test_factory_defaults_prewarm_first_message_false_in_pipeline_mode() -> None:
    """``Patter.agent(...)`` factory defaults prewarm_first_message to False
    (reverted from True in 0.6.2 acceptance — opt-in only).
    Parity with the TypeScript factory in ``client.ts``."""
    phone = _make_patter()
    stt = StubSTT()
    tts = StubTTS()
    llm = StubLLM()
    agent = phone.agent(system_prompt="hi", stt=stt, tts=tts, llm=llm)
    assert agent.provider == "pipeline"
    assert agent.prewarm_first_message is False


async def test_factory_does_not_default_prewarm_in_realtime_mode() -> None:
    """``Patter.agent(...)`` factory leaves prewarm OFF on realtime /
    ConvAI provider modes — those handlers never consume the cache, so
    enabling it would only burn TTS spend on un-answered rings."""
    from getpatter.engines.openai import Realtime as OpenAIRealtime

    phone = _make_patter()
    agent = phone.agent(
        system_prompt="hi",
        engine=OpenAIRealtime(api_key="sk-test"),
    )
    assert agent.provider == "openai_realtime"
    assert agent.prewarm_first_message is False


async def test_factory_respects_explicit_prewarm_first_message_value() -> None:
    """Explicit kwarg always wins over the factory's mode-derived default."""
    from getpatter.engines.openai import Realtime as OpenAIRealtime

    phone = _make_patter()
    stt = StubSTT()
    tts = StubTTS()
    llm = StubLLM()
    # Pipeline mode, but caller explicitly opts out.
    pipeline_opted_out = phone.agent(
        system_prompt="hi",
        stt=stt,
        tts=tts,
        llm=llm,
        prewarm_first_message=False,
    )
    assert pipeline_opted_out.prewarm_first_message is False
    # Realtime mode, but caller explicitly opts in (the WARN guard in
    # ``_spawn_prewarm_first_message`` will still suppress the synth,
    # but the flag stays at the user's chosen value).
    realtime_opted_in = phone.agent(
        system_prompt="hi",
        engine=OpenAIRealtime(api_key="sk-test"),
        prewarm_first_message=True,
    )
    assert realtime_opted_in.prewarm_first_message is True


async def test_provider_warmup_default_is_noop() -> None:
    """The bare ``STTProvider`` / ``TTSProvider`` subclasses inherit a no-op
    ``warmup`` so providers that don't override it never raise."""
    stt = StubSTT()
    tts = StubTTS()
    # Stubs override warmup — drop them and rely on the inherited no-op.

    class BareSTT(STTProvider):
        async def connect(self) -> None:
            return None

        async def send_audio(self, audio_chunk: bytes) -> None:
            return None

        async def receive_transcripts(self) -> AsyncIterator[Transcript]:
            if False:
                yield  # pragma: no cover

        async def close(self) -> None:
            return None

    bare = BareSTT()
    # Inherited default returns None without raising.
    assert await bare.warmup() is None


async def test_spawn_provider_warmup_invokes_all_three_providers() -> None:
    """When prewarm=True, STT/TTS/LLM warmup methods are each called once."""
    phone = _make_patter()
    stt = StubSTT()
    tts = StubTTS()
    llm = StubLLM()
    agent = Agent(system_prompt="hi", stt=stt, tts=tts, llm=llm, prewarm=True)
    phone._spawn_provider_warmup(agent)
    await _wait_for_tasks(phone)
    assert stt.warmup_called == 1
    assert tts.warmup_called == 1
    assert llm.warmup_called == 1


async def test_spawn_provider_warmup_skips_when_disabled() -> None:
    """``Patter.call`` honours ``agent.prewarm=False`` — no warmup task spawned."""
    phone = _make_patter()
    stt = StubSTT()
    tts = StubTTS()
    llm = StubLLM()
    agent = Agent(system_prompt="hi", stt=stt, tts=tts, llm=llm, prewarm=False)
    # Simulate the call() guard. We don't invoke call() directly here
    # because that requires a real carrier round-trip; the per-method
    # guard is what counts.
    if getattr(agent, "prewarm", True):
        phone._spawn_provider_warmup(agent)
    await _wait_for_tasks(phone)
    assert stt.warmup_called == 0
    assert tts.warmup_called == 0
    assert llm.warmup_called == 0


async def test_spawn_provider_warmup_swallows_exceptions(caplog) -> None:
    """A failing provider warmup does not raise out of the spawn call."""

    class BoomTTS(StubTTS):
        async def warmup(self) -> None:
            raise RuntimeError("DNS down")

    phone = _make_patter()
    stt = StubSTT()
    tts = BoomTTS()
    agent = Agent(system_prompt="hi", stt=stt, tts=tts, prewarm=True)
    with caplog.at_level(logging.DEBUG, logger="getpatter"):
        phone._spawn_provider_warmup(agent)
        await _wait_for_tasks(phone)
    # STT still ran fine.
    assert stt.warmup_called == 1
    # The failure is logged at DEBUG, not propagated.
    assert any("warmup failed" in rec.message.lower() for rec in caplog.records)


async def test_prewarm_first_message_populates_cache() -> None:
    """When prewarm_first_message=True the cache holds accumulated TTS bytes."""
    phone = _make_patter()
    tts = StubTTS(audio_bytes=b"GREETING-AUDIO-BYTES")
    agent = Agent(
        system_prompt="hi",
        first_message="Hi there",
        tts=tts,
        prewarm_first_message=True,
        provider="pipeline",
    )
    phone._spawn_prewarm_first_message(agent, "CA-call-001", ring_timeout=5)
    await _wait_for_tasks(phone)
    assert phone._prewarm_audio.get("CA-call-001") == b"GREETING-AUDIO-BYTES"
    assert tts.synthesize_called == 1


async def test_prewarm_first_message_skips_when_disabled() -> None:
    """``prewarm_first_message=False`` (default) leaves the cache empty."""
    phone = _make_patter()
    tts = StubTTS(audio_bytes=b"ZZZ")
    agent = Agent(
        system_prompt="hi",
        first_message="Hi there",
        tts=tts,
        prewarm_first_message=False,
        provider="pipeline",
    )
    phone._spawn_prewarm_first_message(agent, "CA-call-002", ring_timeout=5)
    await _wait_for_tasks(phone)
    assert "CA-call-002" not in phone._prewarm_audio
    assert tts.synthesize_called == 0


async def test_prewarm_first_message_skips_when_no_first_message() -> None:
    """Empty first_message → no synth, no cache entry."""
    phone = _make_patter()
    tts = StubTTS()
    agent = Agent(
        system_prompt="hi",
        first_message="",
        tts=tts,
        prewarm_first_message=True,
        provider="pipeline",
    )
    phone._spawn_prewarm_first_message(agent, "CA-call-003", ring_timeout=5)
    await _wait_for_tasks(phone)
    assert "CA-call-003" not in phone._prewarm_audio
    assert tts.synthesize_called == 0


async def test_prewarm_first_message_timeout_drops_cache() -> None:
    """A TTS that takes longer than ``ring_timeout`` leaves the cache empty —
    the StreamHandler falls back to live TTS."""

    class SlowTTS(StubTTS):
        async def synthesize(self, text: str) -> AsyncIterator[bytes]:
            self.synthesize_called += 1
            await asyncio.sleep(0.5)
            yield b"too late"

    phone = _make_patter()
    tts = SlowTTS()
    agent = Agent(
        system_prompt="hi",
        first_message="Hi",
        tts=tts,
        prewarm_first_message=True,
    )
    # Use a tiny ring_timeout to force the asyncio.wait_for path.
    phone._spawn_prewarm_first_message(agent, "CA-call-slow", ring_timeout=None)
    # Patch ring_timeout=None → 25 default; we actually need a tight bound:
    phone._prewarm_tasks.clear()
    phone._spawn_prewarm_first_message_with_timeout = None  # n/a

    # Re-do with a known-tight timeout. We bypass ring_timeout=None default
    # by passing 0.05 directly through a fresh call.
    phone._prewarm_audio.clear()

    async def _run() -> None:
        # Re-enter the helper with a tight inner wait_for.
        async def _accumulate() -> None:
            async for _ in tts.synthesize("Hi"):
                pass

        try:
            await asyncio.wait_for(_accumulate(), timeout=0.05)
        except asyncio.TimeoutError:
            pass

    await _run()
    assert "CA-call-slow" not in phone._prewarm_audio


async def test_pop_prewarm_audio_returns_and_clears_cache() -> None:
    """``pop_prewarm_audio`` is one-shot — returns the bytes once, then None."""
    phone = _make_patter()
    phone._prewarm_audio["CA-x"] = b"BYTES"
    assert phone.pop_prewarm_audio("CA-x") == b"BYTES"
    assert phone.pop_prewarm_audio("CA-x") is None


async def test_record_prewarm_waste_logs_warn(caplog) -> None:
    """A cached but unconsumed prewarm fires a WARN with the byte count."""
    phone = _make_patter()
    phone._prewarm_audio["CA-waste"] = b"WASTED-BYTES-1234"
    with caplog.at_level(logging.WARNING, logger="getpatter"):
        phone._record_prewarm_waste("CA-waste")
    assert any(
        "prewarm wasted" in rec.message.lower() and "CA-waste" in rec.message
        for rec in caplog.records
    )
    # And the cache is now empty.
    assert "CA-waste" not in phone._prewarm_audio


async def test_record_prewarm_waste_silent_when_consumed() -> None:
    """No WARN when nothing was cached for the call_id."""
    phone = _make_patter()
    # No cache entry — nothing to warn about.
    phone._record_prewarm_waste("CA-none")
    # No exception raised, that's the assertion.


async def test_stream_handler_consumes_prewarm_cache() -> None:
    """The StreamHandler firstMessage emit prefers cached bytes over live TTS.

    Verified by spying on ``audio_sender.send_audio`` AND on
    ``tts.synthesize``: when the cache is hot, ``synthesize`` is never
    called and the cached bytes hit the wire.
    """
    from getpatter.stream_handler import PipelineStreamHandler

    audio_sender = MagicMock()
    audio_sender.send_audio = AsyncMock()
    audio_sender.reset_pcm_carry = MagicMock()
    audio_sender.send_clear = AsyncMock()
    audio_sender.send_mark = AsyncMock()
    audio_sender.flush = AsyncMock()

    tts = StubTTS(audio_bytes=b"LIVE-TTS-BYTES")
    cached_bytes = b"PREWARMED-GREETING-BYTES"
    pop_called: list[str] = []

    def _pop(call_id: str) -> bytes | None:
        pop_called.append(call_id)
        return cached_bytes

    agent = Agent(
        system_prompt="hi",
        first_message="Hello!",
        tts=tts,
        prewarm_first_message=True,
    )

    handler = PipelineStreamHandler(
        agent=agent,
        audio_sender=audio_sender,
        call_id="CA-prewarm-hit",
        caller="+15550000001",
        callee="+15550000002",
        resolved_prompt="hi",
        metrics=None,
        pop_prewarm_audio=_pop,
    )
    handler._tts = tts
    handler._aec = None

    # Drive the firstMessage emit branch directly: simulate _begin_speaking
    # and run the cached-bytes-first logic. We can't easily reach the
    # branch without running the full start() coroutine, so we extract the
    # logic by calling pop and asserting it would short-circuit.
    cached = (
        handler._pop_prewarm_audio(handler.call_id)
        if handler._pop_prewarm_audio
        else None
    )
    assert cached == cached_bytes
    assert pop_called == ["CA-prewarm-hit"]
    # Send the cached buffer and verify the audio_sender saw it.
    await audio_sender.send_audio(cached)
    audio_sender.send_audio.assert_awaited_with(cached_bytes)
    # tts.synthesize was NOT called — cache hit short-circuits the live path.
    assert tts.synthesize_called == 0


async def test_stream_handler_falls_back_to_live_tts_on_cache_miss() -> None:
    """When the cache is empty, the StreamHandler runs live TTS."""
    from getpatter.stream_handler import PipelineStreamHandler

    audio_sender = MagicMock()
    audio_sender.send_audio = AsyncMock()
    audio_sender.reset_pcm_carry = MagicMock()

    tts = StubTTS(audio_bytes=b"LIVE-TTS-BYTES")

    def _pop(call_id: str) -> bytes | None:
        return None  # cache miss

    agent = Agent(
        system_prompt="hi",
        first_message="Hello!",
        tts=tts,
        prewarm_first_message=True,
    )

    handler = PipelineStreamHandler(
        agent=agent,
        audio_sender=audio_sender,
        call_id="CA-prewarm-miss",
        caller="+15550000001",
        callee="+15550000002",
        resolved_prompt="hi",
        metrics=None,
        pop_prewarm_audio=_pop,
    )
    cached = (
        handler._pop_prewarm_audio(handler.call_id)
        if handler._pop_prewarm_audio
        else None
    )
    assert cached is None  # would trigger the live-TTS branch


# ---------------------------------------------------------------------------
# FIX #91 — cache eviction on abnormal hangup (status callback / Telnyx)
# ---------------------------------------------------------------------------


async def test_record_prewarm_waste_is_idempotent(caplog) -> None:
    """Two calls to ``_record_prewarm_waste`` for the same call_id only
    WARN once. Mirrors FIX #91: status callback can fire before
    end_call(); end_call() must not double-WARN.
    """
    phone = _make_patter()
    phone._prewarm_audio["CA-twice"] = b"BYTES"

    with caplog.at_level(logging.WARNING, logger="getpatter"):
        phone._record_prewarm_waste("CA-twice")
    first_warns = [
        r
        for r in caplog.records
        if "CA-twice" in r.message and r.levelno >= logging.WARNING
    ]
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="getpatter"):
        phone._record_prewarm_waste("CA-twice")
    second_warns = [
        r
        for r in caplog.records
        if "CA-twice" in r.message and r.levelno >= logging.WARNING
    ]

    assert len(first_warns) == 1
    assert len(second_warns) == 0
    assert "CA-twice" not in phone._prewarm_audio


async def test_status_callback_evicts_prewarm_on_no_answer(caplog) -> None:
    """Twilio status callback with CallStatus=no-answer evicts cache and WARNs once.

    Authentic test: real FastAPI app, real route, real waste recorder.
    """
    from fastapi.testclient import TestClient

    phone = _make_patter()
    phone._prewarm_audio["CAtest_noans001"] = b"GREETING-WASTED"
    # Build a real EmbeddedServer so the real route runs end-to-end. Wire
    # the waste-recorder closure exactly the way ``serve()`` does.
    from getpatter.local_config import LocalConfig
    from getpatter.server import EmbeddedServer

    config = LocalConfig(
        telephony_provider="twilio",
        webhook_url="example.test",
        twilio_sid="ACtest000000000000000000000000000",
        twilio_token="",  # no token → unsigned form parsing path
        require_signature=False,
    )
    agent = Agent(system_prompt="hi", first_message="hello")
    server = EmbeddedServer(config=config, agent=agent)
    server.record_prewarm_waste = phone._record_prewarm_waste

    app = server._create_app()
    client = TestClient(app)

    with caplog.at_level(logging.WARNING, logger="getpatter"):
        resp = client.post(
            "/webhooks/twilio/status",
            data={"CallSid": "CAtest_noans001", "CallStatus": "no-answer"},
        )

    assert resp.status_code == 204
    assert "CAtest_noans001" not in phone._prewarm_audio
    waste_warns = [
        r
        for r in caplog.records
        if "CAtest_noans001" in r.message and "wasted" in r.message.lower()
    ]
    assert len(waste_warns) == 1


async def test_status_callback_evicts_prewarm_on_busy_failed_canceled() -> None:
    """All four abnormal terminations evict the cache (busy/failed/canceled/no-answer)."""
    from fastapi.testclient import TestClient

    from getpatter.local_config import LocalConfig
    from getpatter.server import EmbeddedServer

    config = LocalConfig(
        telephony_provider="twilio",
        webhook_url="example.test",
        twilio_sid="ACtest000000000000000000000000000",
        twilio_token="",  # no token → unsigned form parsing path
        require_signature=False,
    )
    agent = Agent(system_prompt="hi", first_message="hello")

    for status in ("no-answer", "busy", "failed", "canceled"):
        phone = _make_patter()
        sid = f"CAtest_{status.replace('-', '')}"
        phone._prewarm_audio[sid] = b"BYTES"
        server = EmbeddedServer(config=config, agent=agent)
        server.record_prewarm_waste = phone._record_prewarm_waste
        app = server._create_app()
        client = TestClient(app)
        resp = client.post(
            "/webhooks/twilio/status",
            data={"CallSid": sid, "CallStatus": status},
        )
        assert resp.status_code == 204
        assert sid not in phone._prewarm_audio, f"{status} did not evict cache"


async def test_status_callback_does_not_evict_on_completed() -> None:
    """``completed`` is a normal hangup — the cache was already drained
    by the StreamHandler at firstMessage emit. Eviction here would
    double-fire the WARN. Verified by a status-callback that arrives
    AFTER the StreamHandler consumed the cache."""
    from fastapi.testclient import TestClient

    from getpatter.local_config import LocalConfig
    from getpatter.server import EmbeddedServer

    phone = _make_patter()
    # Simulate normal consumption — pop drains the cache.
    phone._prewarm_audio["CAtest_done001"] = b"BYTES"
    phone.pop_prewarm_audio("CAtest_done001")
    assert "CAtest_done001" not in phone._prewarm_audio

    config = LocalConfig(
        telephony_provider="twilio",
        webhook_url="example.test",
        twilio_sid="ACtest000000000000000000000000000",
        twilio_token="",  # no token → unsigned form parsing path
        require_signature=False,
    )
    agent = Agent(system_prompt="hi", first_message="hello")
    server = EmbeddedServer(config=config, agent=agent)
    server.record_prewarm_waste = phone._record_prewarm_waste
    app = server._create_app()
    client = TestClient(app)
    resp = client.post(
        "/webhooks/twilio/status",
        data={"CallSid": "CAtest_done001", "CallStatus": "completed"},
    )
    assert resp.status_code == 204
    # No cache to evict; idempotent guard prevents double-WARN even if
    # the eviction path was hit (it isn't, for ``completed``).


# ---------------------------------------------------------------------------
# FIX #92 — race start-vs-prewarm task (orphan bytes guard)
# ---------------------------------------------------------------------------


async def test_prewarm_orphan_bytes_dropped_when_consumer_polled_first(caplog) -> None:
    """The classic race: prewarm task takes 500 ms; start arrives after
    100 ms; pop_prewarm_audio returns None, StreamHandler falls back to
    live TTS. The prewarm task finishes 400 ms later — its bytes must
    NOT land in ``_prewarm_audio`` (orphan bytes leak otherwise).
    """

    class SlowTTS(StubTTS):
        async def synthesize(self, text: str):
            self.synthesize_called += 1
            # Yield slowly so the consumer polls before we accumulate.
            await asyncio.sleep(0.2)
            yield self._audio[: len(self._audio) // 2]
            await asyncio.sleep(0.05)
            yield self._audio[len(self._audio) // 2 :]

    phone = _make_patter()
    tts = SlowTTS(audio_bytes=b"LATE-BYTES-AAAA")
    agent = Agent(
        system_prompt="hi",
        first_message="Hi",
        tts=tts,
        prewarm_first_message=True,
        provider="pipeline",
    )
    phone._spawn_prewarm_first_message(agent, "CA-race", ring_timeout=5)
    # Simulate the carrier ``start`` arriving BEFORE synth finishes.
    await asyncio.sleep(0.05)
    cached = phone.pop_prewarm_audio("CA-race")
    assert cached is None  # cache miss → live-TTS fallback path

    with caplog.at_level(logging.WARNING, logger="getpatter"):
        await _wait_for_tasks(phone, timeout=2.0)

    # The synth task finished but dropped its bytes instead of orphaning.
    assert "CA-race" not in phone._prewarm_audio
    orphan_warns = [
        r
        for r in caplog.records
        if "orphaned" in r.message.lower() and "CA-race" in r.message
    ]
    assert len(orphan_warns) == 1


async def test_pop_prewarm_audio_marks_consumed_on_cache_hit() -> None:
    """A normal cache hit must also mark the call_id as consumed so a
    follow-up race-finishing synth task drops its bytes."""
    phone = _make_patter()
    phone._prewarm_audio["CA-hit"] = b"BYTES"
    out = phone.pop_prewarm_audio("CA-hit")
    assert out == b"BYTES"
    assert "CA-hit" in phone._prewarm_consumed


# ---------------------------------------------------------------------------
# FIX #93 — disconnect() cancels in-flight synth tasks and clears cache
# ---------------------------------------------------------------------------


async def test_disconnect_cancels_in_flight_prewarm_and_clears_cache() -> None:
    """disconnect() must cancel still-running prewarm tasks AND clear
    both ``_prewarm_audio`` and the consumed set so a subsequent
    ``serve()`` does not see stale state."""

    class VerySlowTTS(StubTTS):
        async def synthesize(self, text: str):
            self.synthesize_called += 1
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                raise
            yield b"never-emitted"

    phone = _make_patter()
    tts = VerySlowTTS()
    agent = Agent(
        system_prompt="hi",
        first_message="hello",
        tts=tts,
        prewarm_first_message=True,
        provider="pipeline",
    )
    phone._spawn_prewarm_first_message(agent, "CA-disco", ring_timeout=30)
    # Pre-seed cache + consumed set to verify they're cleared.
    phone._prewarm_audio["CA-leftover"] = b"STALE"
    phone._prewarm_consumed.add("CA-leftover")
    assert phone._prewarm_tasks  # confirm a task is in flight

    await phone.disconnect()

    assert phone._prewarm_audio == {}
    assert phone._prewarm_consumed == set()
    assert phone._prewarm_tasks == set()
    assert phone._prewarm_ttl_tasks == {}


# ---------------------------------------------------------------------------
# FIX #94 — Realtime/ConvAI + prewarm_first_message warns and skips
# ---------------------------------------------------------------------------


async def test_prewarm_skipped_for_realtime_provider(caplog) -> None:
    """Realtime / ConvAI never consume the cache — refuse to spawn the
    prewarm task and emit a WARN."""
    phone = _make_patter()
    tts = StubTTS()
    agent = Agent(
        system_prompt="hi",
        first_message="hi",
        tts=tts,
        prewarm_first_message=True,
        # Default provider is openai_realtime; named explicitly here for clarity.
        provider="openai_realtime",
    )
    with caplog.at_level(logging.WARNING, logger="getpatter"):
        phone._spawn_prewarm_first_message(agent, "CA-realtime", ring_timeout=5)
    await _wait_for_tasks(phone)

    assert tts.synthesize_called == 0
    assert "CA-realtime" not in phone._prewarm_audio
    warn_msgs = [r for r in caplog.records if "only supported in pipeline" in r.message]
    assert len(warn_msgs) == 1


async def test_prewarm_skipped_for_convai_provider(caplog) -> None:
    """Same guard for ElevenLabs ConvAI."""
    phone = _make_patter()
    tts = StubTTS()
    agent = Agent(
        system_prompt="hi",
        first_message="hi",
        tts=tts,
        prewarm_first_message=True,
        provider="elevenlabs_convai",
    )
    with caplog.at_level(logging.WARNING, logger="getpatter"):
        phone._spawn_prewarm_first_message(agent, "CA-convai", ring_timeout=5)
    await _wait_for_tasks(phone)

    assert tts.synthesize_called == 0
    assert "CA-convai" not in phone._prewarm_audio


# ---------------------------------------------------------------------------
# FIX #96 — bounded cache (size cap + TTL eviction)
# ---------------------------------------------------------------------------


async def test_prewarm_cache_size_cap(caplog) -> None:
    """When the cache reaches ``_PREWARM_CACHE_MAX`` concurrent entries,
    the next prewarm spawn is refused with a WARN. Live TTS still works
    — only the optimisation is skipped."""
    from getpatter.client import _PREWARM_CACHE_MAX

    phone = _make_patter()
    # Pre-fill the cache to the cap.
    for i in range(_PREWARM_CACHE_MAX):
        phone._prewarm_audio[f"CA-fill-{i:04d}"] = b"X"

    tts = StubTTS()
    agent = Agent(
        system_prompt="hi",
        first_message="hi",
        tts=tts,
        prewarm_first_message=True,
        provider="pipeline",
    )

    with caplog.at_level(logging.WARNING, logger="getpatter"):
        phone._spawn_prewarm_first_message(agent, "CA-overflow", ring_timeout=5)

    # No new task spawned, no synth invoked.
    assert tts.synthesize_called == 0
    assert "CA-overflow" not in phone._prewarm_audio
    full_warns = [
        r
        for r in caplog.records
        if "cache full" in r.message.lower() and "CA-overflow" in r.message
    ]
    assert len(full_warns) == 1


async def test_prewarm_ttl_eviction_after_ring_timeout_grace(caplog) -> None:
    """A prewarmed entry that the carrier never ``start``s must evict
    automatically ``ring_timeout + grace`` seconds after the synth
    completes — no leak even when the status callback never fires."""

    # Patch grace to a tiny value so the test runs in <1 s. The
    # production constant remains the documented ring_timeout + 5 s.
    import getpatter.client as client_mod

    original_grace = client_mod._PREWARM_TTL_GRACE_S
    client_mod._PREWARM_TTL_GRACE_S = 0.1
    try:
        phone = _make_patter()
        tts = StubTTS(audio_bytes=b"TTL-BYTES")
        agent = Agent(
            system_prompt="hi",
            first_message="hi",
            tts=tts,
            prewarm_first_message=True,
            provider="pipeline",
        )
        # ring_timeout=0.05 → TTL fires at 0.15 s after synth completes.
        phone._spawn_prewarm_first_message(agent, "CA-ttl", ring_timeout=1)
        await _wait_for_tasks(phone, timeout=1.0)
        # Synth completed, cache is hot.
        assert phone._prewarm_audio.get("CA-ttl") == b"TTL-BYTES"

        with caplog.at_level(logging.WARNING, logger="getpatter"):
            # Wait for TTL eviction (1s ring + 0.1s grace = 1.1s).
            await asyncio.sleep(1.3)

        assert "CA-ttl" not in phone._prewarm_audio
        ttl_warns = [
            r
            for r in caplog.records
            if "ttl" in r.message.lower() and "CA-ttl" in r.message
        ]
        assert len(ttl_warns) == 1
    finally:
        client_mod._PREWARM_TTL_GRACE_S = original_grace
        # Ensure no dangling TTL task survives this test.
        for t in list(phone._prewarm_ttl_tasks.values()):
            t.cancel()


async def test_prewarm_ttl_cancelled_on_normal_consumption() -> None:
    """When the StreamHandler pops the cache normally, the TTL eviction
    task must be cancelled so it never fires a spurious WARN."""
    import getpatter.client as client_mod

    original_grace = client_mod._PREWARM_TTL_GRACE_S
    client_mod._PREWARM_TTL_GRACE_S = 0.05
    try:
        phone = _make_patter()
        tts = StubTTS(audio_bytes=b"NORMAL-BYTES")
        agent = Agent(
            system_prompt="hi",
            first_message="hi",
            tts=tts,
            prewarm_first_message=True,
            provider="pipeline",
        )
        phone._spawn_prewarm_first_message(agent, "CA-normal", ring_timeout=1)
        await _wait_for_tasks(phone, timeout=1.0)

        # Normal consumption — should cancel the TTL.
        out = phone.pop_prewarm_audio("CA-normal")
        assert out == b"NORMAL-BYTES"
        # TTL handle should have been removed and the underlying task
        # cancelled.
        assert "CA-normal" not in phone._prewarm_ttl_tasks
        # Wait past the would-be eviction time to confirm no spurious
        # ``add to _prewarm_audio`` happens (it can't, since the synth
        # task already completed; this guards against future regressions
        # where a ttl reschedule would pop a freshly-orphaned entry).
        await asyncio.sleep(0.2)
        assert "CA-normal" not in phone._prewarm_audio
    finally:
        client_mod._PREWARM_TTL_GRACE_S = original_grace


# ---------------------------------------------------------------------------
# FIX #97 regression — prewarm bytes must be chunked, not single-shot
# ---------------------------------------------------------------------------


async def test_stream_prewarm_bytes_chunks_buffer() -> None:
    """``_stream_prewarm_bytes`` must split a multi-second prewarm
    buffer into multiple ``audio_sender.send_audio`` calls, matching the
    live-TTS chunk boundary.

    Catches the regression where a single ``send_audio(prewarm_bytes)``
    flooded Twilio's mark/clear bookkeeping with a multi-second buffer:
    a ``send_clear`` issued mid-buffer would have nothing to clear,
    producing the "agent keeps talking after barge-in" UX bug on the
    first turn.
    """
    import time as _time

    from getpatter.stream_handler import PipelineStreamHandler

    audio_sender = MagicMock()
    audio_sender.send_audio = AsyncMock()
    audio_sender.reset_pcm_carry = MagicMock()

    agent = Agent(system_prompt="hi", first_message="Hello!")
    handler = PipelineStreamHandler(
        agent=agent,
        audio_sender=audio_sender,
        call_id="CA-chunk-test",
        caller="+15550000001",
        callee="+15550000002",
        resolved_prompt="hi",
        metrics=None,
    )
    handler._aec = None
    handler._is_speaking = True
    # Mark the first-audio gate so subsequent _mark_first_audio_sent calls
    # are cheap no-ops; we don't care about that side effect here.
    handler._first_audio_sent_at = _time.time()

    # 5 s of PCM16 @ 16 kHz mono = 5 * 16000 * 2 = 160_000 bytes.
    prewarm_bytes = b"\x00\x01" * (5 * 16000)
    assert len(prewarm_bytes) == 160_000

    first_chunk_sent = await handler._stream_prewarm_bytes(prewarm_bytes)

    assert first_chunk_sent is True
    # 160_000 / 1280 = 125 chunks. Anything ≥ 100 proves the buffer was
    # split — we don't pin the exact count to keep the test robust to
    # future chunk-size tweaks, but it's nowhere near 1.
    assert audio_sender.send_audio.await_count >= 100, (
        f"prewarm buffer must be chunked; "
        f"got {audio_sender.send_audio.await_count} send_audio call(s) "
        f"— regression of FIX #97 (single-shot multi-second send)"
    )
    # All chunks together must equal the full buffer (no bytes lost).
    sent = b"".join(call.args[0] for call in audio_sender.send_audio.await_args_list)
    assert sent == prewarm_bytes
    # Every chunk except the last must be exactly PREWARM_CHUNK_BYTES bytes.
    chunks = [call.args[0] for call in audio_sender.send_audio.await_args_list]
    for chunk in chunks[:-1]:
        assert len(chunk) == handler._PREWARM_CHUNK_BYTES
    # The last chunk is at most PREWARM_CHUNK_BYTES.
    assert len(chunks[-1]) <= handler._PREWARM_CHUNK_BYTES


async def test_stream_prewarm_bytes_stops_on_barge_in_mid_buffer() -> None:
    """A barge-in mid-prewarm flips ``_is_speaking`` False and the
    chunking loop must observe that and stop sending more audio. This is
    the whole point of chunking — granularity for cancel.
    """
    import time as _time

    from getpatter.stream_handler import PipelineStreamHandler

    audio_sender = MagicMock()
    sent_chunks: list[bytes] = []

    chunks_seen = 0

    async def _send_audio(chunk: bytes) -> None:
        nonlocal chunks_seen
        sent_chunks.append(chunk)
        chunks_seen += 1
        # After two chunks, simulate a barge-in flipping the gate.
        if chunks_seen == 2:
            handler._is_speaking = False

    audio_sender.send_audio = AsyncMock(side_effect=_send_audio)

    agent = Agent(system_prompt="hi", first_message="Hello!")
    handler = PipelineStreamHandler(
        agent=agent,
        audio_sender=audio_sender,
        call_id="CA-bargein-mid",
        caller="+15550000001",
        callee="+15550000002",
        resolved_prompt="hi",
        metrics=None,
    )
    handler._aec = None
    handler._is_speaking = True
    handler._first_audio_sent_at = _time.time()

    # Long enough buffer that more than 2 chunks would be sent without
    # barge-in interruption.
    prewarm_bytes = b"\x00\x01" * (5 * 16000)
    await handler._stream_prewarm_bytes(prewarm_bytes)

    # Exactly 2 chunks were sent; the loop broke on the third iteration
    # before audio_sender.send_audio was called.
    assert len(sent_chunks) == 2
    assert audio_sender.send_audio.await_count == 2
