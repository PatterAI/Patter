"""Unit tests for the Gemini Live engine wiring.

Covers the marker -> provider dispatch, the ``ProviderMode`` literal, the
carrier-codec shim's real mu-law/PCM transcoding, and the stream handler's
adapter selection. The only faked thing is the ``google-genai`` Live session
(a paid external boundary that cannot be opened in CI) — every transcoding,
dispatch, and option-forwarding path below runs the real implementation.
See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import typing

import pytest

from getpatter.audio.transcoding import mulaw_to_pcm16
from getpatter.client import Patter
from getpatter.engines.gemini import GeminiLive
from getpatter.models import Agent, ProviderMode
from getpatter.providers.gemini_live import GeminiLiveEventType
from getpatter.providers.gemini_live_bridge import (
    CARRIER_AUDIO_FORMAT,
    GeminiLiveTelephonyAdapter,
    build_gemini_live_adapter,
)
from getpatter.stream_handler import AudioSender, OpenAIRealtimeStreamHandler

TEST_KEY = "test-google-key"
# 20 ms of mu-law 8 kHz — the frame size Twilio/Telnyx/Plivo deliver.
MULAW_FRAME = bytes(range(160))


# ---------------------------------------------------------------------------
# Engine marker -> provider dispatch
# ---------------------------------------------------------------------------


def test_engine_marker_unpacks_to_gemini_live() -> None:
    kind, fields = Patter._unpack_engine(GeminiLive(api_key=TEST_KEY, voice="Kore"))
    assert kind == "gemini_live"
    assert fields["api_key"] == TEST_KEY
    assert fields["voice"] == "Kore"
    assert fields["model"] == "gemini-3.1-flash-live-preview"


def test_marker_defaults_to_gemini_31_flash_live_and_puck() -> None:
    marker = GeminiLive(api_key=TEST_KEY)
    assert marker.model == "gemini-3.1-flash-live-preview"
    assert marker.voice == "Puck"
    assert marker.input_sample_rate == 16000
    assert marker.output_sample_rate == 24000
    assert marker.kind == "gemini_live"


def test_marker_reads_gemini_then_google_env_var(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-fallback")
    assert GeminiLive().api_key == "google-fallback"
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-wins")
    assert GeminiLive().api_key == "gemini-wins"


def test_marker_keeps_api_key_out_of_repr() -> None:
    assert "super-secret" not in repr(GeminiLive(api_key="super-secret"))


def test_marker_constructs_without_any_credential(monkeypatch) -> None:
    # Deliberately does NOT raise (unlike XaiRealtime) — the call path fails
    # fast instead, in ``Patter.agent()``.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert GeminiLive().api_key == ""


def test_unknown_engine_error_names_gemini_live() -> None:
    with pytest.raises(TypeError, match="GeminiLive"):
        Patter._unpack_engine(object())


def test_provider_mode_accepts_gemini_live() -> None:
    assert "gemini_live" in typing.get_args(ProviderMode)


# ---------------------------------------------------------------------------
# Option forwarding through agent() -> Agent -> adapter factory
# ---------------------------------------------------------------------------


def _phone() -> Patter:
    from getpatter.carriers.twilio import Carrier as Twilio

    return Patter(
        carrier=Twilio(account_sid="ACtest", auth_token="token"),
        phone_number="+15555550100",
    )


def test_agent_sets_provider_and_forwards_only_set_knobs() -> None:
    agent = _phone().agent(
        engine=GeminiLive(api_key=TEST_KEY, voice="Kore", temperature=0.4),
        system_prompt="You are terse.",
    )
    assert agent.provider == "gemini_live"
    assert agent.voice == "Kore"
    assert agent.model == "gemini-3.1-flash-live-preview"
    assert agent.gemini_live["temperature"] == 0.4
    # Unset knobs are dropped so the adapter defaults stay authoritative.
    assert "language" not in agent.gemini_live
    assert "api_key" not in agent.gemini_live


def test_agent_rejects_gemini_live_without_a_key(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        _phone().agent(engine=GeminiLive(), system_prompt="hi")


def test_engine_key_backfills_local_config() -> None:
    phone = _phone()
    phone.agent(engine=GeminiLive(api_key=TEST_KEY), system_prompt="hi")
    assert phone._local_config.gemini_key == TEST_KEY


def test_adapter_factory_forwards_agent_and_engine_config() -> None:
    agent = _phone().agent(
        engine=GeminiLive(api_key=TEST_KEY, voice="Aoede", language="es-MX"),
        system_prompt="hola",
    )
    adapter = build_gemini_live_adapter(
        agent=agent,
        api_key=TEST_KEY,
        instructions="hola",
        tools=[],
        audio_format=CARRIER_AUDIO_FORMAT,
    )
    assert adapter.inner.voice == "Aoede"
    assert adapter.inner.model == "gemini-3.1-flash-live-preview"
    assert adapter.inner.language == "es-MX"
    assert adapter.inner.instructions == "hola"


def test_adapter_factory_rejects_an_empty_key() -> None:
    agent = _phone().agent(
        engine=GeminiLive(api_key=TEST_KEY), system_prompt="hi"
    )
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        build_gemini_live_adapter(
            agent=agent,
            api_key="",
            instructions="hi",
            tools=[],
            audio_format=CARRIER_AUDIO_FORMAT,
        )


# ---------------------------------------------------------------------------
# Carrier-codec shim — real transcoding against a fake Live session
# ---------------------------------------------------------------------------


class _FakeGeminiSession:
    """Stands in for the google-genai Live session (paid external boundary)."""

    def __init__(self, events: list[tuple[str, object]]) -> None:
        self.sent: list[bytes] = []
        self._events = events

    async def send_audio(self, audio: bytes) -> None:
        self.sent.append(audio)

    async def receive_events(self):
        for event in self._events:
            yield event


def _shim(events: list[tuple[str, object]]) -> tuple[
    GeminiLiveTelephonyAdapter, _FakeGeminiSession
]:
    adapter = GeminiLiveTelephonyAdapter(TEST_KEY)
    fake = _FakeGeminiSession(events)
    adapter.inner = fake  # type: ignore[assignment]
    return adapter, fake


async def test_shim_decodes_carrier_mulaw_up_to_the_model_input_rate() -> None:
    adapter, fake = _shim([])
    await adapter.send_audio(MULAW_FRAME)
    # 160 mu-law bytes -> 160 PCM16 samples @ 8 kHz -> 320 samples @ 16 kHz.
    assert len(fake.sent) == 1
    assert len(fake.sent[0]) == pytest.approx(len(MULAW_FRAME) * 2 * 2, abs=4)


async def test_shim_encodes_model_pcm_back_down_to_carrier_mulaw() -> None:
    # 480 PCM16 samples (20 ms @ 24 kHz), built from real mu-law decode output.
    model_pcm = mulaw_to_pcm16(MULAW_FRAME) * 3
    adapter, _ = _shim([(GeminiLiveEventType.AUDIO.value, model_pcm)])
    out = [ev async for ev in adapter.receive_events()]
    assert len(out) == 1
    ev_type, payload = out[0]
    assert ev_type == GeminiLiveEventType.AUDIO.value
    # 480 PCM16 samples @ 24 kHz -> 160 samples @ 8 kHz -> 160 mu-law bytes.
    assert len(payload) == pytest.approx(160, abs=4)


async def test_shim_passes_non_audio_events_through_untouched() -> None:
    events = [
        (GeminiLiveEventType.TRANSCRIPT_INPUT.value, "hello"),
        (GeminiLiveEventType.RESPONSE_DONE.value, None),
    ]
    adapter, _ = _shim(list(events))
    assert [ev async for ev in adapter.receive_events()] == events


async def test_shim_passes_audio_through_when_not_on_a_mulaw_carrier() -> None:
    adapter = GeminiLiveTelephonyAdapter(TEST_KEY, audio_format="pcm16")
    fake = _FakeGeminiSession([(GeminiLiveEventType.AUDIO.value, b"\x01\x02")])
    adapter.inner = fake  # type: ignore[assignment]
    await adapter.send_audio(b"\x03\x04")
    assert fake.sent == [b"\x03\x04"]
    assert [ev async for ev in adapter.receive_events()] == [
        (GeminiLiveEventType.AUDIO.value, b"\x01\x02")
    ]


async def test_shim_update_session_warns_instead_of_crashing_the_call(caplog) -> None:
    adapter, _ = _shim([])
    with caplog.at_level("WARNING"):
        await adapter.update_session(instructions="new", tools=[])
    assert "cannot update session config mid-call" in caplog.text


# ---------------------------------------------------------------------------
# Stream-handler adapter selection
# ---------------------------------------------------------------------------


class _NullAudioSender(AudioSender):
    async def send_audio(self, pcm_audio: bytes) -> None:  # pragma: no cover
        return None

    async def send_mark(self, name: str) -> None:  # pragma: no cover
        return None

    async def send_clear(self) -> None:  # pragma: no cover
        return None


def _handler(agent: Agent, **kwargs) -> OpenAIRealtimeStreamHandler:
    return OpenAIRealtimeStreamHandler(
        agent=agent,
        audio_sender=_NullAudioSender(),
        call_id="CA0000000000000000000000000000a001",
        caller="+15555550100",
        callee="+15555550101",
        resolved_prompt=agent.system_prompt,
        metrics=None,
        openai_key="",
        audio_format=CARRIER_AUDIO_FORMAT,
        **kwargs,
    )


async def test_stream_handler_builds_the_gemini_shim(monkeypatch) -> None:
    agent = _phone().agent(
        engine=GeminiLive(api_key=TEST_KEY, voice="Fenrir"),
        system_prompt="You are terse.",
    )
    handler = _handler(agent, gemini_key=TEST_KEY)
    # Stop after construction: connecting would open the real Live socket.
    monkeypatch.setattr(
        GeminiLiveTelephonyAdapter, "connect", lambda self: _noop(), raising=True
    )
    monkeypatch.setattr(
        OpenAIRealtimeStreamHandler, "_forward_events", lambda self: _noop()
    )
    await handler.start()
    try:
        assert isinstance(handler._adapter, GeminiLiveTelephonyAdapter)
        assert handler._adapter.inner.voice == "Fenrir"
        # Built-in call-control tools reach Gemini exactly as they reach OpenAI.
        tool_names = {t["name"] for t in handler._adapter.inner.tools}
        assert {"transfer_call", "end_call"} <= tool_names
    finally:
        await handler.cleanup()


async def test_stream_handler_keeps_openai_adapter_for_other_providers() -> None:
    from getpatter.engines.openai import Realtime

    agent = _phone().agent(
        engine=Realtime(api_key="sk-test"), system_prompt="You are terse."
    )
    handler = _handler(agent)
    assert getattr(agent, "provider", None) == "openai_realtime"
    assert handler._gemini_key == ""


async def _noop() -> None:
    return None
