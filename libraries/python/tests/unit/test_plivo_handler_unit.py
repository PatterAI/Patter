"""Unit tests for the Plivo carrier: AudioSender envelopes, V3 signature,
AMD classification, extra-header parsing, the REST adapter, the credential
dataclass, and the ``plivo_stream_bridge`` lifecycle.

Mirrors ``tests/unit/test_twilio_bridge_unit.py`` and the Twilio handler tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_agent


class FakeWS:
    """Minimal async WebSocket capturing outbound text frames."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


# ---------------------------------------------------------------------------
# PlivoAudioSender — wire envelopes
# ---------------------------------------------------------------------------


async def test_send_audio_emits_play_audio_envelope():
    from getpatter.telephony.plivo import PlivoAudioSender

    ws = FakeWS()
    sender = PlivoAudioSender(ws, "stream1", input_is_mulaw_8k=True)
    await sender.send_audio(b"\xff" * 160)
    msg = ws.sent[-1]
    assert msg["event"] == "playAudio"
    assert msg["media"]["contentType"] == "audio/x-mulaw"
    assert msg["media"]["sampleRate"] == 8000
    assert msg["media"]["payload"] == base64.b64encode(b"\xff" * 160).decode()


async def test_send_clear_emits_clear_audio_with_stream_id():
    from getpatter.telephony.plivo import PlivoAudioSender

    ws = FakeWS()
    sender = PlivoAudioSender(ws, "stream-xyz", input_is_mulaw_8k=True)
    await sender.send_clear()
    assert ws.sent[-1] == {"event": "clearAudio", "streamId": "stream-xyz"}


async def test_send_mark_emits_checkpoint():
    from getpatter.telephony.plivo import PlivoAudioSender

    ws = FakeWS()
    sender = PlivoAudioSender(ws, "stream-xyz", input_is_mulaw_8k=True)
    await sender.send_mark("ignored")
    msg = ws.sent[-1]
    assert msg["event"] == "checkpoint"
    assert msg["streamId"] == "stream-xyz"
    assert msg["name"] == "audio_1"


async def test_send_dtmf_filters_invalid_and_emits_send_dtmf():
    from getpatter.telephony.plivo import PlivoAudioSender

    ws = FakeWS()
    sender = PlivoAudioSender(ws, "s", input_is_mulaw_8k=True)
    await sender.send_dtmf("12ab#xZ")  # x and Z are not valid DTMF
    assert ws.sent[-1] == {"event": "sendDTMF", "dtmf": "12ab#"}


async def test_send_dtmf_no_valid_digits_sends_nothing():
    from getpatter.telephony.plivo import PlivoAudioSender

    ws = FakeWS()
    sender = PlivoAudioSender(ws, "s", input_is_mulaw_8k=True)
    await sender.send_dtmf("xyz")
    assert ws.sent == []


async def test_on_mark_confirmed_records_name():
    from getpatter.telephony.plivo import PlivoAudioSender

    sender = PlivoAudioSender(FakeWS(), "s", input_is_mulaw_8k=True)
    sender.on_mark_confirmed("audio_3")
    assert sender.last_confirmed_mark == "audio_3"


async def test_send_audio_transcodes_pcm16_to_mulaw():
    """Pipeline path (input_is_mulaw_8k=False) resamples + mulaw-encodes."""
    from getpatter.telephony.plivo import PlivoAudioSender

    ws = FakeWS()
    sender = PlivoAudioSender(ws, "s", input_is_mulaw_8k=False)
    # 320 bytes PCM16 @ 16 kHz (10 ms) → resampled to 8 kHz → mulaw.
    await sender.send_audio(b"\x00\x10" * 160)
    assert ws.sent[-1]["event"] == "playAudio"
    assert ws.sent[-1]["media"]["payload"]  # non-empty mulaw payload


# ---------------------------------------------------------------------------
# V3 signature
# ---------------------------------------------------------------------------


def _v3_sig(url: str, nonce: str, token: str, params: dict | None = None) -> str:
    """Mirror of plivo-python ``signature_v3``: HMAC-SHA256 of
    ``url + sorted_post_params + "." + nonce``, base64-encoded."""
    base = url
    if params:
        base += "".join(f"{k}{params[k]}" for k in sorted(params))
    signed = f"{base}.{nonce}"
    return base64.b64encode(
        hmac.new(token.encode(), signed.encode(), hashlib.sha256).digest()
    ).decode()


def test_validate_plivo_signature_accepts_valid_get():
    from getpatter.server import _validate_plivo_signature

    url, nonce, token = "https://h/webhooks/plivo/voice", "nonce123", "tok"
    sig = _v3_sig(url, nonce, token)
    assert _validate_plivo_signature(url, nonce, sig, token, method="GET")


def test_validate_plivo_signature_accepts_valid_post_with_params():
    """POST signs url + sorted(k+v) + "." + nonce — must match Plivo's SDK."""
    from getpatter.server import _validate_plivo_signature

    url, nonce, token = "https://h/webhooks/plivo/voice", "n", "tok"
    params = {"CallUUID": "CU1", "From": "+15551112222", "To": "+15553334444"}
    sig = _v3_sig(url, nonce, token, params)
    assert _validate_plivo_signature(url, nonce, sig, token, params=params, method="POST")


def test_validate_plivo_signature_post_without_params_falls_back_to_url_nonce():
    """Empty POST params behaves like GET — just url + "." + nonce."""
    from getpatter.server import _validate_plivo_signature

    url, nonce, token = "https://h/webhooks/plivo/voice", "n", "tok"
    sig = _v3_sig(url, nonce, token)  # no params
    assert _validate_plivo_signature(url, nonce, sig, token, params={}, method="POST")


def test_validate_plivo_signature_rejects_tampered():
    from getpatter.server import _validate_plivo_signature

    url, nonce, token = "https://h/webhooks/plivo/voice", "nonce123", "tok"
    assert not _validate_plivo_signature(url, nonce, "deadbeef", token, method="GET")


def test_validate_plivo_signature_rejects_param_mismatch():
    """Tampering with any POST param value must invalidate the signature."""
    from getpatter.server import _validate_plivo_signature

    url, nonce, token = "https://h/webhooks/plivo/voice", "n", "tok"
    original = {"CallUUID": "CU1", "From": "+1"}
    sig = _v3_sig(url, nonce, token, original)
    tampered = {"CallUUID": "CU1", "From": "+9"}
    assert not _validate_plivo_signature(
        url, nonce, sig, token, params=tampered, method="POST"
    )


def test_validate_plivo_signature_supports_rotation():
    from getpatter.server import _validate_plivo_signature

    url, nonce, token = "https://h/webhooks/plivo/voice", "n", "tok"
    good = _v3_sig(url, nonce, token)
    assert _validate_plivo_signature(
        url, nonce, f"oldsig, {good}", token, method="GET"
    )


def test_validate_plivo_signature_requires_all_inputs():
    from getpatter.server import _validate_plivo_signature

    assert not _validate_plivo_signature("u", "", "sig", "tok")
    assert not _validate_plivo_signature("u", "n", "", "tok")
    assert not _validate_plivo_signature("u", "n", "sig", "")


# ---------------------------------------------------------------------------
# AMD classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("human", "human"),
        ("person", "human"),
        ("machine", "machine"),
        ("machine_end_beep", "machine"),
        ("answering_machine", "machine"),
        ("true", "machine"),
        ("fax", "fax"),
        ("", "unknown"),
        ("weird", "unknown"),
    ],
)
def test_classify_plivo_amd(raw, expected):
    from getpatter.server import _classify_plivo_amd

    assert _classify_plivo_amd(raw) == expected


# ---------------------------------------------------------------------------
# extra_headers parsing (port of plivo.rs parse_extra_headers)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"userId": "12345", "sessionId": "abc"}', {"userId": "12345", "sessionId": "abc"}),
        ("{X-PH-name: Amal, X-PH-k: true}", {"X-PH-name": "Amal", "X-PH-k": "true"}),
        ("userId=12345;sessionId=abc", {"userId": "12345", "sessionId": "abc"}),
        ("agentUuid=xxx,name=Amal", {"agentUuid": "xxx", "name": "Amal"}),
        ("", {}),
        ("{}", {}),
    ],
)
def test_parse_plivo_extra_headers(raw, expected):
    from getpatter.telephony.plivo import _parse_plivo_extra_headers

    assert _parse_plivo_extra_headers(raw) == expected


# ---------------------------------------------------------------------------
# PlivoAdapter REST shape
# ---------------------------------------------------------------------------


async def test_initiate_call_builds_answer_url_payload():
    from getpatter.providers.plivo_adapter import PlivoAdapter

    adapter = PlivoAdapter("MA-test-only", "tok")
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"request_uuid": "req-1"}
    adapter._client.post = AsyncMock(return_value=resp)
    try:
        rid = await adapter.initiate_call(
            "+15550001111",
            "+15550002222",
            "wss://unused",
            answer_url="https://h/webhooks/plivo/voice",
            hangup_url="https://h/webhooks/plivo/status",
            ring_timeout=25,
            machine_detection=True,
            machine_detection_url="https://h/webhooks/plivo/amd",
        )
    finally:
        await adapter.close()
    assert rid == "req-1"
    path, kwargs = adapter._client.post.call_args[0][0], adapter._client.post.call_args.kwargs
    assert path == "/Call/"
    body = kwargs["json"]
    assert body["answer_url"] == "https://h/webhooks/plivo/voice"
    assert body["hangup_url"] == "https://h/webhooks/plivo/status"
    assert body["hangup_method"] == "POST"
    assert body["ring_timeout"] == 25
    assert body["machine_detection"] == "true"
    assert body["machine_detection_url"] == "https://h/webhooks/plivo/amd"


async def test_end_call_treats_404_as_success():
    from getpatter.providers.plivo_adapter import PlivoAdapter

    adapter = PlivoAdapter("MA-test-only", "tok")
    resp = MagicMock(status_code=404)
    resp.raise_for_status.side_effect = AssertionError("must not raise on 404")
    adapter._client.delete = AsyncMock(return_value=resp)
    try:
        await adapter.end_call("CALLUUID")  # should not raise
    finally:
        await adapter.close()
    assert "CALLUUID" in adapter._client.delete.call_args[0][0]


# ---------------------------------------------------------------------------
# Carrier credential dataclass
# ---------------------------------------------------------------------------


def test_carrier_kind_is_plivo():
    from getpatter.carriers.plivo import Carrier

    assert Carrier(auth_id="MA1", auth_token="t").kind == "plivo"


def test_carrier_reads_env(monkeypatch):
    monkeypatch.setenv("PLIVO_AUTH_ID", "MAENV")
    monkeypatch.setenv("PLIVO_AUTH_TOKEN", "tokenv")
    from getpatter.carriers.plivo import Carrier

    c = Carrier()
    assert c.auth_id == "MAENV" and c.auth_token == "tokenv"


def test_carrier_missing_credentials_raises(monkeypatch):
    monkeypatch.delenv("PLIVO_AUTH_ID", raising=False)
    monkeypatch.delenv("PLIVO_AUTH_TOKEN", raising=False)
    from getpatter.carriers.plivo import Carrier

    with pytest.raises(ValueError, match="auth_id and auth_token"):
        Carrier()


def test_unpack_carrier_dispatches_plivo():
    from getpatter.carriers.plivo import Carrier
    from getpatter.client import Patter

    fake = Carrier(auth_id="MA1", auth_token="t")  # gitleaks:allow
    kind, unpacked = Patter._unpack_carrier(fake)
    assert kind == "plivo"
    assert unpacked == {"auth_id": "MA1", "auth_token": "t"}  # gitleaks:allow


# ---------------------------------------------------------------------------
# plivo_stream_bridge lifecycle
# ---------------------------------------------------------------------------


def _start_message(call_id: str = "CU" + "a" * 30, stream_id: str = "ST1") -> str:
    return json.dumps(
        {
            "event": "start",
            "start": {
                "callId": call_id,
                "streamId": stream_id,
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000},
            },
        }
    )


def _make_mock_ws(messages: list[str]) -> AsyncMock:
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.query_params = {"caller": "+15551234567", "callee": "+15559876543"}
    ws.receive_text = AsyncMock(side_effect=messages + [Exception("stop")])
    ws.send_text = AsyncMock()
    return ws


@patch("getpatter.telephony.plivo.OpenAIRealtimeStreamHandler")
@patch("getpatter.telephony.plivo.create_metrics_accumulator")
@patch("getpatter.telephony.plivo.resolve_agent_prompt", return_value="prompt")
@patch("getpatter.telephony.plivo.fetch_deepgram_cost", new_callable=AsyncMock)
async def test_bridge_start_then_stop_fires_callbacks(
    mock_fetch_dg, mock_resolve, mock_create_metrics, mock_handler_cls
):
    from getpatter.telephony.plivo import plivo_stream_bridge

    ws = _make_mock_ws([_start_message(), json.dumps({"event": "stop"})])
    mock_handler = AsyncMock()
    mock_handler.audio_sender = None
    mock_handler_cls.return_value = mock_handler
    mock_create_metrics.return_value = MagicMock()
    on_call_start = AsyncMock(return_value=None)
    on_call_end = AsyncMock()

    await plivo_stream_bridge(
        websocket=ws,
        agent=make_agent(provider="openai_realtime"),
        openai_key="sk-test",
        on_call_start=on_call_start,
        on_call_end=on_call_end,
    )

    ws.accept.assert_awaited_once()
    mock_handler.start.assert_awaited_once()
    mock_handler.cleanup.assert_awaited_once()
    on_call_start.assert_awaited_once()
    # The on_call_start payload tags the carrier as plivo.
    assert on_call_start.call_args[0][0]["telephony_provider"] == "plivo"
    on_call_end.assert_awaited_once()


@patch("getpatter.telephony.plivo.OpenAIRealtimeStreamHandler")
@patch("getpatter.telephony.plivo.create_metrics_accumulator")
@patch("getpatter.telephony.plivo.resolve_agent_prompt", return_value="prompt")
@patch("getpatter.telephony.plivo.fetch_deepgram_cost", new_callable=AsyncMock)
async def test_bridge_media_and_playedstream_and_dtmf(
    mock_fetch_dg, mock_resolve, mock_create_metrics, mock_handler_cls
):
    from getpatter.telephony.plivo import plivo_stream_bridge

    audio = b"\xff" * 160
    messages = [
        _start_message(),
        json.dumps({"event": "media", "media": {"payload": base64.b64encode(audio).decode()}}),
        json.dumps({"event": "playedStream", "name": "audio_1"}),
        json.dumps({"event": "dtmf", "dtmf": {"digit": "7"}}),
        json.dumps({"event": "stop"}),
    ]
    ws = _make_mock_ws(messages)
    mock_handler = AsyncMock()
    mock_handler.audio_sender = None
    mock_handler_cls.return_value = mock_handler
    mock_create_metrics.return_value = MagicMock()
    on_transcript = AsyncMock()

    await plivo_stream_bridge(
        websocket=ws,
        agent=make_agent(provider="openai_realtime"),
        openai_key="sk-test",
        on_transcript=on_transcript,
    )

    mock_handler.on_audio_received.assert_awaited_once_with(audio)
    mock_handler.on_mark.assert_awaited_once_with("audio_1")
    mock_handler.on_dtmf.assert_awaited_once_with("7")
    assert "[DTMF: 7]" in on_transcript.call_args[0][0]["text"]
