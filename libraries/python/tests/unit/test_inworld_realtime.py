"""Unit tests for the Inworld Realtime speech-to-speech engine.

These assert the adapter's endpoint / provider key / defaults, the v1
``session.update`` body, the full mocked WebSocket round trip (connect
handshake, audio in, audio out, tool call), the engine-marker -> provider
unpack, and the pricing key the adapter meters against.

Only the network boundary (``websockets.connect``) is mocked — the adapter,
the handshake, and the event loop are the real code paths. See
``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import patch

import pytest

from getpatter.client import Patter
from getpatter.engines.inworld import InworldRealtime
from getpatter.pricing import calculate_tts_cost, merge_pricing
from getpatter.providers.inworld_realtime import (
    INWORLD_REALTIME_DEFAULT_MODEL,
    INWORLD_REALTIME_DEFAULT_VOICE,
    INWORLD_REALTIME_WS_URL,
    InworldRealtimeAdapter,
)
from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter
from getpatter.services.metrics import CallMetricsAccumulator

TEST_KEY = "inworld-test-key"
# One mu-law frame's worth of recognisable bytes — asserted byte-for-byte on
# both legs so a silent transcode regression cannot pass.
AUDIO_BYTES = bytes([0xFF, 0x7F, 0x00, 0x10])


def _adapter(**kwargs) -> InworldRealtimeAdapter:
    return InworldRealtimeAdapter(api_key=TEST_KEY, **kwargs)


class _FakeRealtimeWS:
    """Stand-in for a ``websockets`` client connection driven by a script.

    ``recv()`` pops the next queued server frame; async iteration (used by
    ``receive_events``) drains the same queue and then blocks forever so the
    consumer task stays alive until the test cancels it.
    """

    def __init__(self, frames: list[str]) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._frames = list(frames)

    def queue(self, frame: dict) -> None:
        self._frames.append(json.dumps(frame))

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if not self._frames:
            raise TimeoutError
        return self._frames.pop(0)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while not self._frames:
            await asyncio.sleep(0.001)
        return self._frames.pop(0)

    def sent_json(self) -> list[dict]:
        return [json.loads(p) for p in self.sent]


def _patch_connect(ws: _FakeRealtimeWS):
    """Patch the boundary the INHERITED ``connect()`` uses, recording kwargs."""
    calls: list[tuple[tuple, dict]] = []

    async def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return ws

    return (
        patch(
            "getpatter.providers.openai_realtime.websockets.connect",
            side_effect=fake_connect,
        ),
        calls,
    )


# ---------------------------------------------------------------------------
# Endpoint / provider key / defaults
# ---------------------------------------------------------------------------


def test_endpoint_is_inworld_and_provider_key_matches() -> None:
    a = _adapter()
    assert a.OPENAI_REALTIME_URL == INWORLD_REALTIME_WS_URL == "wss://api.inworld.ai/v1/realtime"
    # Inworld publishes no separate Realtime rate, so the adapter meters
    # against the same pricing key as Inworld TTS (parity with TypeScript).
    assert a.provider_key == "inworld"


def test_subclasses_the_v1_openai_realtime_adapter() -> None:
    # Every ``isinstance(OpenAIRealtimeAdapter)`` feature gate in the stream
    # handler must fire for Inworld too — no per-provider branches.
    assert isinstance(_adapter(), OpenAIRealtimeAdapter)


def test_defaults_are_inworld_realtime_and_ashley() -> None:
    a = _adapter()
    assert a.model == INWORLD_REALTIME_DEFAULT_MODEL == "inworld-realtime"
    assert a.voice == INWORLD_REALTIME_DEFAULT_VOICE == "Ashley"
    # v1 g711_ulaw pass-through — the carrier-native wire format.
    assert a.audio_format == "g711_ulaw"


def test_explicit_model_and_voice_win_over_defaults() -> None:
    a = _adapter(model="inworld-realtime-pro", voice="Olivia")
    assert a.model == "inworld-realtime-pro"
    assert a.voice == "Olivia"


def test_base_url_override_trims_trailing_slashes() -> None:
    a = _adapter(base_url="wss://example.test/rt/")
    assert a.OPENAI_REALTIME_URL == "wss://example.test/rt"
    # The class default is untouched for every other instance.
    assert _adapter().OPENAI_REALTIME_URL == INWORLD_REALTIME_WS_URL


# ---------------------------------------------------------------------------
# session.update body (v1 shape)
# ---------------------------------------------------------------------------


def test_session_config_is_the_v1_shape_with_the_inworld_voice() -> None:
    cfg = _adapter(instructions="Be brief.")._build_session_config()
    assert cfg["voice"] == "Ashley"
    assert cfg["input_audio_format"] == "g711_ulaw"
    assert cfg["output_audio_format"] == "g711_ulaw"
    assert cfg["instructions"] == "Be brief."
    # v1 omits the GA-only response-gating keys.
    assert "create_response" not in cfg["turn_detection"]


def test_transcription_language_is_pinned_when_set_and_omitted_otherwise() -> None:
    assert (
        _adapter(transcription_language="it")._build_session_config()[
            "input_audio_transcription"
        ]["language"]
        == "it"
    )
    assert "language" not in _adapter()._build_session_config()[
        "input_audio_transcription"
    ]


# ---------------------------------------------------------------------------
# Mocked WebSocket: connect handshake
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_connect_uses_the_inworld_endpoint_and_bearer_key() -> None:
    ws = _FakeRealtimeWS(
        [
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "session.updated"}),
        ]
    )
    ctx, calls = _patch_connect(ws)
    a = _adapter(voice="Olivia")
    with ctx:
        await a.connect()

    url, kwargs = calls[0][0][0], calls[0][1]
    assert url == f"{INWORLD_REALTIME_WS_URL}?model={INWORLD_REALTIME_DEFAULT_MODEL}"
    assert kwargs["additional_headers"]["Authorization"] == f"Bearer {TEST_KEY}"

    update = ws.sent_json()[0]
    assert update["type"] == "session.update"
    assert update["session"]["voice"] == "Olivia"


@pytest.mark.mocked
async def test_connect_targets_a_base_url_override() -> None:
    ws = _FakeRealtimeWS(
        [
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "session.updated"}),
        ]
    )
    ctx, calls = _patch_connect(ws)
    with ctx:
        await _adapter(base_url="wss://example.test/rt/", model="m1").connect()
    assert calls[0][0][0] == "wss://example.test/rt?model=m1"


@pytest.mark.mocked
async def test_connect_surfaces_a_setup_error_frame() -> None:
    ws = _FakeRealtimeWS(
        [json.dumps({"type": "error", "error": {"message": "model not found"}})]
    )
    ctx, _calls = _patch_connect(ws)
    with ctx, pytest.raises(RuntimeError, match="model not found"):
        await _adapter(model="bad-model").connect()
    # The socket is closed rather than leaked on a failed handshake.
    assert ws.closed


# ---------------------------------------------------------------------------
# Mocked WebSocket: audio in / audio out / tool call
# ---------------------------------------------------------------------------


async def _connected(ws: _FakeRealtimeWS, **kwargs) -> InworldRealtimeAdapter:
    ws.queue({"type": "session.created"})
    ws.queue({"type": "session.updated"})
    ctx, _calls = _patch_connect(ws)
    a = _adapter(**kwargs)
    with ctx:
        await a.connect()
    return a


@pytest.mark.mocked
async def test_send_audio_appends_base64_to_the_input_buffer() -> None:
    ws = _FakeRealtimeWS([])
    a = await _connected(ws)
    await a.send_audio(AUDIO_BYTES)

    append = ws.sent_json()[-1]
    assert append["type"] == "input_audio_buffer.append"
    assert base64.b64decode(append["audio"]) == AUDIO_BYTES


@pytest.mark.mocked
async def test_audio_delta_is_decoded_and_yielded_so_a_call_can_hear_the_model() -> None:
    ws = _FakeRealtimeWS([])
    a = await _connected(ws)
    ws.queue(
        {
            "type": "response.audio.delta",
            "delta": base64.b64encode(AUDIO_BYTES).decode("ascii"),
        }
    )
    events = a.receive_events()
    assert await asyncio.wait_for(events.__anext__(), timeout=1.0) == (
        "audio",
        AUDIO_BYTES,
    )
    await events.aclose()


@pytest.mark.mocked
async def test_tool_call_round_trip() -> None:
    ws = _FakeRealtimeWS([])
    a = await _connected(
        ws,
        tools=[
            {
                "name": "lookup",
                "description": "look up",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    )
    # The tool reaches the session in the OpenAI wire format.
    session_tools = ws.sent_json()[0]["session"]["tools"]
    assert [t["name"] for t in session_tools] == ["lookup"]

    ws.queue(
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"q":"hours"}',
        }
    )
    events = a.receive_events()
    kind, payload = await asyncio.wait_for(events.__anext__(), timeout=1.0)
    assert kind == "function_call"
    assert payload == {
        "call_id": "call_1",
        "name": "lookup",
        "arguments": '{"q":"hours"}',
    }

    await a.send_function_result("call_1", '{"open": true}')
    item = ws.sent_json()[-2]
    assert item["type"] == "conversation.item.create"
    assert item["item"]["call_id"] == "call_1"
    assert item["item"]["output"] == '{"open": true}'
    assert ws.sent_json()[-1]["type"] == "response.create"
    await events.aclose()


# ---------------------------------------------------------------------------
# Prewarm opt-out
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_warmup_is_a_noop_and_parking_is_unsupported() -> None:
    ws = _FakeRealtimeWS([])
    ctx, calls = _patch_connect(ws)
    a = _adapter()
    with ctx:
        assert await a.warmup() is None
        with pytest.raises(RuntimeError, match="not supported"):
            await a.open_parked_connection()
    # Neither call may open a socket — the base implementations target OpenAI.
    assert calls == []


# ---------------------------------------------------------------------------
# Engine marker -> client dispatch
# ---------------------------------------------------------------------------


def test_engine_marker_unpacks_to_inworld_realtime() -> None:
    marker = InworldRealtime(
        api_key=TEST_KEY, voice="Olivia", transcription_language="it"
    )
    kind, fields = Patter._unpack_engine(marker)
    assert kind == "inworld_realtime"
    assert fields["api_key"] == TEST_KEY
    assert fields["voice"] == "Olivia"
    assert fields["model"] == INWORLD_REALTIME_DEFAULT_MODEL
    assert fields["transcription_language"] == "it"


def test_engine_marker_reads_the_env_key(monkeypatch) -> None:
    monkeypatch.setenv("INWORLD_API_KEY", "from-env")
    assert InworldRealtime().api_key == "from-env"


def test_engine_marker_requires_an_api_key(monkeypatch) -> None:
    monkeypatch.delenv("INWORLD_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key"):
        InworldRealtime()


def test_agent_dispatch_selects_the_inworld_provider(monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok_test")
    from getpatter import Twilio

    phone = Patter(
        carrier=Twilio(),
        phone_number="+15550000000",
        webhook_url="https://abc.ngrok.io",
    )
    agent = phone.agent(
        engine=InworldRealtime(api_key=TEST_KEY, voice="Olivia", base_url="wss://x/rt"),
        system_prompt="You are helpful.",
    )
    assert agent.provider == "inworld_realtime"
    assert agent.voice == "Olivia"
    assert agent.inworld_realtime == {"base_url": "wss://x/rt"}
    # The engine's key is backfilled into LocalConfig for the stream-handler.
    assert phone._local_config.inworld_key == TEST_KEY


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_adapter_meters_against_the_existing_inworld_pricing_entry() -> None:
    pricing = merge_pricing(None)
    key = InworldRealtimeAdapter.provider_key
    assert key in pricing
    # $25 / 1M chars = $0.025 / 1k chars (On-Demand list rate).
    assert calculate_tts_cost(key, 1000, pricing) == pytest.approx(0.025)


# ---------------------------------------------------------------------------
# Cost accumulation
# ---------------------------------------------------------------------------


def _metrics_accumulator(provider_mode: str) -> CallMetricsAccumulator:
    return CallMetricsAccumulator(
        call_id=f"test-cost-{provider_mode}",
        provider_mode=provider_mode,
        telephony_provider="twilio",
    )


# A realistic OpenAI-Realtime ``response.done`` usage payload. Inworld's API is
# OpenAI-Realtime-compatible, so its responses can carry the same shape.
_USAGE = {
    "input_token_details": {
        "text_tokens": 1000,
        "audio_tokens": 2000,
        "cached_tokens_details": {"text_tokens": 0, "audio_tokens": 0},
    },
    "output_token_details": {"text_tokens": 500, "audio_tokens": 1500},
}


def test_inworld_realtime_does_not_price_usage_against_the_openai_rate_table() -> None:
    """Inworld usage must not be metered with OpenAI Realtime token prices.

    ``calculate_realtime_cost`` defaults to ``provider="openai_realtime"``, so
    an unguarded ``record_realtime_usage`` would attribute OpenAI's per-token
    rates to an Inworld call. Guard parity with ``xai_realtime``.
    """
    inworld = _metrics_accumulator("inworld_realtime")
    inworld.record_realtime_usage(_USAGE)
    assert inworld._total_realtime_cost == 0.0
    assert inworld._total_realtime_cached_savings == 0.0

    # Control: the same payload IS priced for the token-billed OpenAI mode, so
    # the assertion above proves the guard fires, not that the payload is inert.
    openai = _metrics_accumulator("openai_realtime")
    openai.record_realtime_usage(_USAGE)
    assert openai._total_realtime_cost > 0.0


def test_inworld_realtime_reports_zero_ai_cost_deliberately() -> None:
    """Inworld publishes no Realtime rate, so AI cost is an explicit zero.

    This asserts the dedicated branch exists: telephony is still billed, and
    stt/tts/llm are zero because there is no rate to meter -- not because the
    provider fell through to the pipeline else-branch.
    """
    acc = _metrics_accumulator("inworld_realtime")
    cost = acc._compute_cost(600.0)  # 10-minute call

    assert cost.stt == 0.0
    assert cost.tts == 0.0
    assert cost.llm == 0.0
    assert cost.telephony > 0.0
    assert cost.total == pytest.approx(cost.telephony)
