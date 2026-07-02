"""Security tests for the per-call media-stream authentication token (#204).

The media WebSocket endpoints (/ws/stream, /ws/telnyx/stream, /ws/plivo/stream)
used to accept ANY peer with an attacker-chosen call_id and drive a full
STT/LLM/TTS session on the operator's provider keys (toll fraud) or extract the
agent prompt. These tests prove the fix:

  * a token is minted only by the SIGNATURE-VALIDATED webhook / outbound path
    and delivered on each carrier's custom-param channel;
  * the media WS validates it BEFORE any provider session opens, closing with
    WS 1008 on missing/invalid/expired token (the #204 repro must fail to start
    a session);
  * ``require_stream_auth=False`` opts out (allow + loud one-time warning);
  * the token value never appears in logs.

Only the WS/transport is mocked — no real carrier or provider keys are used.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.local_config import LocalConfig
from getpatter.server import EmbeddedServer
from getpatter.telephony.common import STREAM_TOKEN_HEADER, STREAM_TOKEN_PARAM
from tests.conftest import make_agent

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(require_stream_auth: bool = True) -> EmbeddedServer:
    """An EmbeddedServer with the dashboard off (light, no I/O)."""
    return EmbeddedServer(
        config=LocalConfig(require_stream_auth=require_stream_auth),
        agent=make_agent(),
        dashboard=False,
    )


def _twilio_ws(messages: list[str]) -> AsyncMock:
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.query_params = {}
    ws.receive_text = AsyncMock(side_effect=messages + [Exception("stop")])
    ws.send_text = AsyncMock()
    return ws


def _twilio_start(call_sid: str, token: str | None = None) -> str:
    params: dict = {"caller": "+15551112222", "callee": "+15553334444"}
    if token is not None:
        params[STREAM_TOKEN_PARAM] = token
    return json.dumps(
        {
            "event": "start",
            "streamSid": "MZ_test",
            "start": {"callSid": call_sid, "customParameters": params},
        }
    )


def _stop() -> str:
    return json.dumps({"event": "stop"})


def _plivo_ws(messages: list[str]) -> AsyncMock:
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.query_params = {}
    ws.receive_text = AsyncMock(side_effect=messages + [Exception("stop")])
    ws.send_text = AsyncMock()
    return ws


def _plivo_start(call_id: str, token: str | None = None) -> str:
    headers: dict = {"X-PH-caller": "+15551112222", "X-PH-callee": "+15553334444"}
    if token is not None:
        headers[STREAM_TOKEN_HEADER] = token
    return json.dumps(
        {
            "event": "start",
            "start": {
                "callId": call_id,
                "streamId": "ST1",
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000},
            },
            "extra_headers": json.dumps(headers),
        }
    )


# ---------------------------------------------------------------------------
# 1. Token map primitives (mint / validate / check)
# ---------------------------------------------------------------------------


class TestStreamTokenMap:
    def test_mint_returns_high_entropy_token_and_stores(self):
        server = _make_server()
        token = server._mint_stream_token("CALL_1")
        assert isinstance(token, str)
        assert len(token) >= 32  # token_urlsafe(32) -> ~43 chars
        assert "CALL_1" in server._stream_tokens

    def test_two_mints_differ(self):
        server = _make_server()
        assert server._mint_stream_token("A") != server._mint_stream_token("B")

    def test_validate_accepts_minted_token(self):
        server = _make_server()
        token = server._mint_stream_token("CALL_1")
        assert server._validate_stream_token("CALL_1", token) is True

    def test_validate_rejects_wrong_token(self):
        server = _make_server()
        server._mint_stream_token("CALL_1")
        assert server._validate_stream_token("CALL_1", "not-the-token") is False

    def test_validate_rejects_empty_token(self):
        server = _make_server()
        server._mint_stream_token("CALL_1")
        assert server._validate_stream_token("CALL_1", "") is False

    def test_validate_rejects_unknown_call_key(self):
        server = _make_server()
        token = server._mint_stream_token("CALL_1")
        assert server._validate_stream_token("OTHER", token) is False

    def test_validate_rejects_non_ascii_without_raising(self):
        server = _make_server()
        server._mint_stream_token("CALL_1")
        # An attacker-supplied non-ASCII token must not raise (compare as bytes).
        assert server._validate_stream_token("CALL_1", "tökén-ÿ") is False

    def test_validate_rejects_expired_token(self):
        server = _make_server()
        token = server._mint_stream_token("CALL_1")
        # Force the stored entry to be already expired.
        tok, _exp = server._stream_tokens["CALL_1"]
        server._stream_tokens["CALL_1"] = (tok, 0.0)
        assert server._validate_stream_token("CALL_1", token) is False
        # Expired entry is pruned on access.
        assert "CALL_1" not in server._stream_tokens

    def test_token_reusable_within_ttl_not_single_use(self):
        """A legitimate carrier reconnect within the TTL must still validate."""
        server = _make_server()
        token = server._mint_stream_token("CALL_1")
        assert server._validate_stream_token("CALL_1", token) is True
        assert server._validate_stream_token("CALL_1", token) is True

    def test_check_stream_auth_fail_closed_by_default(self):
        server = _make_server(require_stream_auth=True)
        # No token minted -> rejected.
        assert server._check_stream_auth("CALL_1", "") is False
        assert server._check_stream_auth("CALL_1", "guessed") is False

    def test_check_stream_auth_opt_out_allows_and_warns_once(self, caplog):
        server = _make_server(require_stream_auth=False)
        with caplog.at_level(logging.WARNING, logger="getpatter"):
            assert server._check_stream_auth("CALL_1", "") is True
            assert server._check_stream_auth("CALL_2", "") is True
        disabled_warnings = [
            r for r in caplog.records if "authentication is DISABLED" in r.message
        ]
        # Loud, but only once.
        assert len(disabled_warnings) == 1


# ---------------------------------------------------------------------------
# 2. Mint delivery — each carrier's webhook embeds the token on its channel
# ---------------------------------------------------------------------------


class TestMintDelivery:
    def test_twilio_webhook_embeds_token_parameter(self):
        from getpatter.telephony.twilio import twilio_webhook_handler

        twiml = twilio_webhook_handler(
            "CA" + "a" * 32, "+1", "+2", "abc.ngrok.io", stream_token="TOK123"
        )
        assert STREAM_TOKEN_PARAM in twiml
        assert "TOK123" in twiml
        # caller/callee still travel alongside.
        assert "caller" in twiml and "callee" in twiml

    def test_twilio_webhook_no_token_when_empty(self):
        """Backward compatible: no token param when none supplied."""
        from getpatter.telephony.twilio import twilio_webhook_handler

        twiml = twilio_webhook_handler("CA" + "a" * 32, "+1", "+2", "abc.ngrok.io")
        assert STREAM_TOKEN_PARAM not in twiml

    def test_telnyx_webhook_appends_token_query(self):
        from getpatter.telephony.telnyx import telnyx_webhook_handler

        result = telnyx_webhook_handler(
            "ctrl_1", "+1", "+2", "abc.ngrok.io", stream_token="TOK123"
        )
        stream_url = result["commands"][1]["params"]["stream_url"]
        assert f"{STREAM_TOKEN_PARAM}=TOK123" in stream_url

    def test_telnyx_webhook_no_token_when_empty(self):
        from getpatter.telephony.telnyx import telnyx_webhook_handler

        result = telnyx_webhook_handler("ctrl_1", "+1", "+2", "abc.ngrok.io")
        stream_url = result["commands"][1]["params"]["stream_url"]
        assert STREAM_TOKEN_PARAM not in stream_url

    def test_plivo_webhook_adds_token_header(self):
        from getpatter.telephony.plivo import plivo_webhook_handler

        xml = plivo_webhook_handler(
            "CU1", "+1", "+2", "abc.ngrok.io", stream_token="TOK123"
        )
        assert STREAM_TOKEN_HEADER in xml
        assert "TOK123" in xml

    def test_plivo_webhook_no_token_when_empty(self):
        from getpatter.telephony.plivo import plivo_webhook_handler

        xml = plivo_webhook_handler("CU1", "+1", "+2", "abc.ngrok.io")
        assert STREAM_TOKEN_HEADER not in xml


# ---------------------------------------------------------------------------
# 3. Twilio bridge — validates the <Parameter> token before the provider opens
# ---------------------------------------------------------------------------


@patch("getpatter.telephony.twilio.OpenAIRealtimeStreamHandler")
@patch("getpatter.telephony.twilio.create_metrics_accumulator")
@patch("getpatter.telephony.twilio.resolve_agent_prompt", return_value="prompt")
@patch("getpatter.telephony.twilio.fetch_deepgram_cost", new_callable=AsyncMock)
class TestTwilioBridgeStreamAuth:
    async def test_rejects_missing_token(
        self, mock_fetch_dg, mock_resolve, mock_metrics, mock_handler_cls
    ):
        from getpatter.telephony.twilio import twilio_stream_bridge

        server = _make_server()  # fail-closed, no token minted
        call_sid = "CA" + "a" * 32
        ws = _twilio_ws([_twilio_start(call_sid, token=None)])
        on_call_start = AsyncMock()

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            on_call_start=on_call_start,
            validate_stream_token=lambda tok: server._check_stream_auth(call_sid, tok),
        )

        # #204 repro: NO provider session, NO on_call_start, socket closed 1008.
        mock_handler_cls.assert_not_called()
        on_call_start.assert_not_awaited()
        ws.close.assert_awaited_once()
        assert ws.close.call_args.kwargs.get("code") == 1008

    async def test_rejects_wrong_token(
        self, mock_fetch_dg, mock_resolve, mock_metrics, mock_handler_cls
    ):
        from getpatter.telephony.twilio import twilio_stream_bridge

        server = _make_server()
        call_sid = "CA" + "b" * 32
        server._mint_stream_token(call_sid)  # a real token exists
        ws = _twilio_ws([_twilio_start(call_sid, token="attacker-guess")])

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            validate_stream_token=lambda tok: server._check_stream_auth(call_sid, tok),
        )

        mock_handler_cls.assert_not_called()
        ws.close.assert_awaited_once()
        assert ws.close.call_args.kwargs.get("code") == 1008

    async def test_accepts_valid_token(
        self, mock_fetch_dg, mock_resolve, mock_metrics, mock_handler_cls
    ):
        from getpatter.telephony.twilio import twilio_stream_bridge

        server = _make_server()
        call_sid = "CA" + "c" * 32
        token = server._mint_stream_token(call_sid)
        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_metrics.return_value = MagicMock()

        ws = _twilio_ws([_twilio_start(call_sid, token=token), _stop()])

        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            validate_stream_token=lambda tok: server._check_stream_auth(call_sid, tok),
        )

        # Session starts normally; socket not force-closed for auth.
        mock_handler.start.assert_awaited_once()
        for c in ws.close.await_args_list:
            assert c.kwargs.get("code") != 1008

    async def test_opt_out_allows_without_token_and_warns(
        self, mock_fetch_dg, mock_resolve, mock_metrics, mock_handler_cls, caplog
    ):
        from getpatter.telephony.twilio import twilio_stream_bridge

        server = _make_server(require_stream_auth=False)
        call_sid = "CA" + "d" * 32
        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_metrics.return_value = MagicMock()

        ws = _twilio_ws([_twilio_start(call_sid, token=None), _stop()])

        with caplog.at_level(logging.WARNING, logger="getpatter"):
            await twilio_stream_bridge(
                websocket=ws,
                agent=make_agent(provider="openai_realtime"),
                openai_key="sk-test",
                validate_stream_token=lambda tok: server._check_stream_auth(
                    call_sid, tok
                ),
            )

        mock_handler.start.assert_awaited_once()
        assert any(
            "authentication is DISABLED" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# 4. Plivo bridge — validates the X-Patter-Stream-Token extra header
# ---------------------------------------------------------------------------


@patch("getpatter.telephony.plivo.OpenAIRealtimeStreamHandler")
@patch("getpatter.telephony.plivo.create_metrics_accumulator")
@patch("getpatter.telephony.plivo.resolve_agent_prompt", return_value="prompt")
@patch("getpatter.telephony.plivo.fetch_deepgram_cost", new_callable=AsyncMock)
class TestPlivoBridgeStreamAuth:
    async def test_rejects_missing_token(
        self, mock_fetch_dg, mock_resolve, mock_metrics, mock_handler_cls
    ):
        from getpatter.telephony.plivo import plivo_stream_bridge

        server = _make_server()
        call_id = "CU" + "a" * 30
        ws = _plivo_ws([_plivo_start(call_id, token=None)])
        on_call_start = AsyncMock()

        await plivo_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            on_call_start=on_call_start,
            validate_stream_token=lambda tok: server._check_stream_auth(call_id, tok),
        )

        mock_handler_cls.assert_not_called()
        on_call_start.assert_not_awaited()
        ws.close.assert_awaited_once()
        assert ws.close.call_args.kwargs.get("code") == 1008

    async def test_rejects_wrong_token(
        self, mock_fetch_dg, mock_resolve, mock_metrics, mock_handler_cls
    ):
        from getpatter.telephony.plivo import plivo_stream_bridge

        server = _make_server()
        call_id = "CU" + "b" * 30
        server._mint_stream_token(call_id)
        ws = _plivo_ws([_plivo_start(call_id, token="attacker-guess")])

        await plivo_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            validate_stream_token=lambda tok: server._check_stream_auth(call_id, tok),
        )

        mock_handler_cls.assert_not_called()
        ws.close.assert_awaited_once()
        assert ws.close.call_args.kwargs.get("code") == 1008

    async def test_accepts_valid_token(
        self, mock_fetch_dg, mock_resolve, mock_metrics, mock_handler_cls
    ):
        from getpatter.telephony.plivo import plivo_stream_bridge

        server = _make_server()
        call_id = "CU" + "c" * 30
        token = server._mint_stream_token(call_id)
        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_metrics.return_value = MagicMock()

        ws = _plivo_ws([_plivo_start(call_id, token=token), _stop()])

        await plivo_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            validate_stream_token=lambda tok: server._check_stream_auth(call_id, tok),
        )

        mock_handler.start.assert_awaited_once()

    async def test_opt_out_allows_without_token_and_warns(
        self, mock_fetch_dg, mock_resolve, mock_metrics, mock_handler_cls, caplog
    ):
        from getpatter.telephony.plivo import plivo_stream_bridge

        server = _make_server(require_stream_auth=False)
        call_id = "CU" + "d" * 30
        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler.stt = None
        mock_handler_cls.return_value = mock_handler
        mock_metrics.return_value = MagicMock()

        ws = _plivo_ws([_plivo_start(call_id, token=None), _stop()])

        with caplog.at_level(logging.WARNING, logger="getpatter"):
            await plivo_stream_bridge(
                websocket=ws,
                agent=make_agent(provider="openai_realtime"),
                openai_key="sk-test",
                validate_stream_token=lambda tok: server._check_stream_auth(
                    call_id, tok
                ),
            )

        mock_handler.start.assert_awaited_once()
        assert any(
            "authentication is DISABLED" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# 5. Telnyx route — the WS handler validates the query token at accept
#    (before the bridge / provider session). Exercised end-to-end through the
#    real FastAPI route with only the provider bridge mocked.
# ---------------------------------------------------------------------------


class TestTelnyxRouteStreamAuth:
    def _app_with_fake_bridge(self, server: EmbeddedServer, called: dict):
        from starlette.testclient import TestClient

        async def _fake_bridge(websocket, **kwargs):  # noqa: ANN001
            called["hit"] = True
            await websocket.accept()
            try:
                while True:
                    await websocket.receive_text()
            except Exception:
                pass

        # Patch before _create_app so the route closure binds the fake bridge.
        with patch(
            "getpatter.telephony.telnyx.telnyx_stream_bridge", new=_fake_bridge
        ):
            app = server._create_app()
        return TestClient(app)

    def test_rejects_missing_token(self):
        from starlette.testclient import WebSocketDisconnect

        server = _make_server()
        called: dict = {}
        client = self._app_with_fake_bridge(server, called)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/telnyx/stream/ctrl_1"):
                pass
        assert called.get("hit") is not True  # bridge/provider never reached

    def test_rejects_wrong_token(self):
        from starlette.testclient import WebSocketDisconnect

        server = _make_server()
        server._mint_stream_token("ctrl_1")
        called: dict = {}
        client = self._app_with_fake_bridge(server, called)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/telnyx/stream/ctrl_1?patter_stream_token=wrong"
            ):
                pass
        assert called.get("hit") is not True

    def test_accepts_valid_token(self):
        server = _make_server()
        token = server._mint_stream_token("ctrl_1")
        called: dict = {}
        client = self._app_with_fake_bridge(server, called)
        with client.websocket_connect(
            f"/ws/telnyx/stream/ctrl_1?patter_stream_token={token}"
        ):
            pass
        assert called.get("hit") is True


# ---------------------------------------------------------------------------
# 6. Outbound Twilio — initiate_call embeds the token as a <Parameter>
# ---------------------------------------------------------------------------


async def test_twilio_outbound_initiate_call_embeds_token_parameter():
    from getpatter.providers.twilio_adapter import TwilioAdapter

    adapter = TwilioAdapter("AC" + "0" * 32, "auth-token")
    captured: dict = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        result = MagicMock()
        result.sid = "CA" + "e" * 32
        return result

    adapter._twilio_client.calls.create = _fake_create
    await adapter.initiate_call(
        "+15550001111",
        "+15550002222",
        "wss://host/ws/stream/outbound",
        parameters={"patter_stream_token": "OUTBOUND_TOK"},
    )
    twiml = captured["twiml"]
    assert STREAM_TOKEN_PARAM in twiml
    assert "OUTBOUND_TOK" in twiml


# ---------------------------------------------------------------------------
# 7. The token value never appears in logs
# ---------------------------------------------------------------------------


@patch("getpatter.telephony.twilio.OpenAIRealtimeStreamHandler")
@patch("getpatter.telephony.twilio.create_metrics_accumulator")
@patch("getpatter.telephony.twilio.resolve_agent_prompt", return_value="prompt")
@patch("getpatter.telephony.twilio.fetch_deepgram_cost", new_callable=AsyncMock)
async def test_token_never_appears_in_logs(
    mock_fetch_dg, mock_resolve, mock_metrics, mock_handler_cls, caplog
):
    from getpatter.telephony.twilio import twilio_stream_bridge

    server = _make_server()
    call_sid = "CA" + "f" * 32
    token = server._mint_stream_token(call_sid)
    mock_handler = AsyncMock()
    mock_handler.audio_sender = None
    mock_handler.stt = None
    mock_handler_cls.return_value = mock_handler
    mock_metrics.return_value = MagicMock()

    ws = _twilio_ws([_twilio_start(call_sid, token=token), _stop()])

    with caplog.at_level(logging.DEBUG, logger="getpatter"):
        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            validate_stream_token=lambda tok: server._check_stream_auth(call_sid, tok),
        )

    # The token was accepted (session started) yet is absent from every record,
    # including the "Custom params" debug dump (it is stripped before logging).
    mock_handler.start.assert_awaited_once()
    assert token not in caplog.text
    for record in caplog.records:
        assert token not in record.getMessage()
