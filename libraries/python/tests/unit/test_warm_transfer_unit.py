"""Unit tests for WARM TRANSFER (``transfer_call(mode="warm")``).

Covers, mocking ONLY the carrier REST boundary (httpx):

- The exact Twilio REST sequence for warm mode: (1) redirect the caller leg
  into a named conference with hold semantics, (2) create the target leg with
  a ``<Say>`` announcement + conference join.
- Cold mode stays byte-identical to the historical single-POST ``<Dial>``
  redirect.
- Telnyx / Plivo return the documented "not yet supported" error envelope
  for warm mode and never touch the network.
- The extended ``transfer_call`` tool schema (``mode`` / ``summary``).
- The pipeline built-in transfer handler envelope behaviour.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_agent
from tests.unit.test_twilio_bridge_unit import (
    _make_mock_ws,
    _start_message,
    _stop_message,
)

CALL_SID = "CA" + "a" * 32
ACCOUNT_SID = "AC" + "0" * 32


def _mock_http(status_code: int = 200, status_codes: list[int] | None = None):
    """Async-context-manager httpx.AsyncClient mock with a stubbed ``post``."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if status_codes is not None:
        responses = [MagicMock(status_code=c) for c in status_codes]
        client.post = AsyncMock(side_effect=responses)
    else:
        client.post = AsyncMock(return_value=MagicMock(status_code=status_code))
    return client


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTransferToolSchema:
    def test_mode_and_summary_added(self) -> None:
        from getpatter.stream_handler import TRANSFER_CALL_TOOL

        props = TRANSFER_CALL_TOOL["parameters"]["properties"]
        assert set(props) == {"number", "mode", "summary"}
        assert props["mode"]["enum"] == ["cold", "warm"]
        # ``number`` stays the only required field — mode defaults to cold.
        assert TRANSFER_CALL_TOOL["parameters"]["required"] == ["number"]

    def test_number_field_unchanged(self) -> None:
        from getpatter.stream_handler import TRANSFER_CALL_TOOL

        assert TRANSFER_CALL_TOOL["parameters"]["properties"]["number"] == {
            "type": "string",
            "description": "Phone number to transfer to (E.164 format)",
        }


# ---------------------------------------------------------------------------
# twilio_warm_transfer — exact REST sequence
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.mocked
class TestTwilioWarmTransferRestSequence:
    @pytest.mark.asyncio
    async def test_warm_sequence_two_posts(self) -> None:
        from getpatter.telephony.twilio import twilio_warm_transfer

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await twilio_warm_transfer(
                call_sid=CALL_SID,
                to_number="+15550001111",
                from_number="+15552223333",
                twilio_sid=ACCOUNT_SID,
                twilio_token="token",
                summary="Mario needs help with order 12.",
                webhook_host="example.ngrok.io",
            )

        assert result["status"] == "transferring"
        assert result["mode"] == "warm"
        assert result["to"] == "+15550001111"
        assert result["conference"] == f"patter-warm-{CALL_SID}"
        assert http.post.await_count == 2

        # POST 1 — caller leg redirected into the conference (on hold).
        url_1 = http.post.await_args_list[0].args[0]
        data_1 = http.post.await_args_list[0].kwargs["data"]
        assert url_1 == (
            f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}"
            f"/Calls/{CALL_SID}.json"
        )
        twiml_1 = data_1["Twiml"]
        assert f"patter-warm-{CALL_SID}" in twiml_1
        assert 'startConferenceOnEnter="false"' in twiml_1
        assert 'endConferenceOnExit="true"' in twiml_1
        assert (
            "https://example.ngrok.io/webhooks/twilio/conference" in twiml_1
        )  # conference status callback registered

        # POST 2 — target (human agent) leg created with Say announcement.
        url_2 = http.post.await_args_list[1].args[0]
        data_2 = http.post.await_args_list[1].kwargs["data"]
        assert url_2 == (
            f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Calls.json"
        )
        assert data_2["To"] == "+15550001111"
        assert data_2["From"] == "+15552223333"
        twiml_2 = data_2["Twiml"]
        assert "<Say>Mario needs help with order 12.</Say>" in twiml_2
        assert f"patter-warm-{CALL_SID}" in twiml_2
        assert 'startConferenceOnEnter="true"' in twiml_2
        assert 'endConferenceOnExit="true"' in twiml_2
        # Target-leg terminal status callback carries the caller's CallSid.
        assert data_2["StatusCallback"] == (
            "https://example.ngrok.io/webhooks/twilio/warm-status"
            f"?caller_call_sid={CALL_SID}"
        )
        assert data_2["StatusCallbackEvent"] == "completed"

    @pytest.mark.asyncio
    async def test_summary_omitted_skips_say(self) -> None:
        from getpatter.telephony.twilio import twilio_warm_transfer

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            await twilio_warm_transfer(
                call_sid=CALL_SID,
                to_number="+15550001111",
                from_number="+15552223333",
                twilio_sid=ACCOUNT_SID,
                twilio_token="token",
            )
        twiml_2 = http.post.await_args_list[1].kwargs["data"]["Twiml"]
        assert "<Say>" not in twiml_2

    @pytest.mark.asyncio
    async def test_summary_is_xml_escaped(self) -> None:
        from getpatter.telephony.twilio import twilio_warm_transfer

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            await twilio_warm_transfer(
                call_sid=CALL_SID,
                to_number="+15550001111",
                from_number="+15552223333",
                twilio_sid=ACCOUNT_SID,
                twilio_token="token",
                summary='<script>"a" & b</script>',
            )
        twiml_2 = http.post.await_args_list[1].kwargs["data"]["Twiml"]
        assert "<script>" not in twiml_2
        assert "&lt;script&gt;" in twiml_2

    @pytest.mark.asyncio
    async def test_no_webhook_host_omits_callbacks(self) -> None:
        from getpatter.telephony.twilio import twilio_warm_transfer

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await twilio_warm_transfer(
                call_sid=CALL_SID,
                to_number="+15550001111",
                from_number="+15552223333",
                twilio_sid=ACCOUNT_SID,
                twilio_token="token",
            )
        assert result["status"] == "transferring"
        twiml_1 = http.post.await_args_list[0].kwargs["data"]["Twiml"]
        assert "statusCallback" not in twiml_1
        data_2 = http.post.await_args_list[1].kwargs["data"]
        assert "StatusCallback" not in data_2

    @pytest.mark.asyncio
    async def test_invalid_number_rejected_without_rest_calls(self) -> None:
        from getpatter.telephony.twilio import twilio_warm_transfer

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await twilio_warm_transfer(
                call_sid=CALL_SID,
                to_number="not-a-number",
                from_number="+15552223333",
                twilio_sid=ACCOUNT_SID,
                twilio_token="token",
            )
        assert result["error"] == "Invalid phone number format"
        http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_call_sid_rejected(self) -> None:
        from getpatter.telephony.twilio import twilio_warm_transfer

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await twilio_warm_transfer(
                call_sid="../../evil",
                to_number="+15550001111",
                from_number="+15552223333",
                twilio_sid=ACCOUNT_SID,
                twilio_token="token",
            )
        assert "error" in result
        http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_from_number_rejected(self) -> None:
        from getpatter.telephony.twilio import twilio_warm_transfer

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await twilio_warm_transfer(
                call_sid=CALL_SID,
                to_number="+15550001111",
                from_number="",
                twilio_sid=ACCOUNT_SID,
                twilio_token="token",
            )
        assert "error" in result
        http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_conference_redirect_failure_returns_envelope(self) -> None:
        from getpatter.telephony.twilio import twilio_warm_transfer

        http = _mock_http(status_code=400)
        with patch("httpx.AsyncClient", return_value=http):
            result = await twilio_warm_transfer(
                call_sid=CALL_SID,
                to_number="+15550001111",
                from_number="+15552223333",
                twilio_sid=ACCOUNT_SID,
                twilio_token="token",
            )
        assert "error" in result
        # Only the failed redirect was attempted — no target dial.
        assert http.post.await_count == 1

    @pytest.mark.asyncio
    async def test_target_dial_failure_releases_caller(self) -> None:
        from getpatter.telephony.twilio import twilio_warm_transfer

        # Redirect OK (200), dial fails (400), recovery POST (200).
        http = _mock_http(status_codes=[200, 400, 200])
        with patch("httpx.AsyncClient", return_value=http):
            result = await twilio_warm_transfer(
                call_sid=CALL_SID,
                to_number="+15550001111",
                from_number="+15552223333",
                twilio_sid=ACCOUNT_SID,
                twilio_token="token",
            )
        assert "error" in result
        assert http.post.await_count == 3
        recovery = http.post.await_args_list[2]
        assert f"/Calls/{CALL_SID}.json" in recovery.args[0]
        assert "<Hangup/>" in recovery.kwargs["data"]["Twiml"]


# ---------------------------------------------------------------------------
# Bridge-level transfer fns (captured from the live bridges, REST mocked)
# ---------------------------------------------------------------------------


async def _capture_twilio_transfer_fn(**bridge_kwargs):
    """Drive twilio_stream_bridge through start→stop and capture the
    ``transfer_fn`` closure it hands to the stream handler."""
    from getpatter.telephony.twilio import twilio_stream_bridge

    ws = _make_mock_ws([_start_message(call_sid=CALL_SID), _stop_message()])
    with (
        patch("getpatter.telephony.twilio.OpenAIRealtimeStreamHandler") as handler_cls,
        patch("getpatter.telephony.twilio.create_metrics_accumulator") as metrics,
        patch("getpatter.telephony.twilio.resolve_agent_prompt", return_value="p"),
        patch("getpatter.telephony.twilio.fetch_deepgram_cost", new_callable=AsyncMock),
    ):
        handler = AsyncMock()
        handler.audio_sender = None
        handler.stt = None
        handler_cls.return_value = handler
        metrics.return_value = MagicMock()
        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            twilio_sid=ACCOUNT_SID,
            twilio_token="token",
            **bridge_kwargs,
        )
        return handler_cls.call_args.kwargs["transfer_fn"]


@pytest.mark.unit
@pytest.mark.mocked
class TestTwilioBridgeTransferFn:
    @pytest.mark.asyncio
    async def test_cold_mode_byte_identical_single_post(self) -> None:
        """Default (cold) transfer issues EXACTLY the historical single POST
        with the bare ``<Dial>`` TwiML body — no conference, no extra params."""
        transfer_fn = await _capture_twilio_transfer_fn()
        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await transfer_fn("+15550001111")
        assert result is None  # legacy contract: cold returns None
        assert http.post.await_count == 1
        call = http.post.await_args_list[0]
        assert call.args[0] == (
            f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}"
            f"/Calls/{CALL_SID}.json"
        )
        assert call.kwargs["data"] == {
            "Twiml": "<Response><Dial>+15550001111</Dial></Response>"
        }

    @pytest.mark.asyncio
    async def test_warm_mode_runs_conference_sequence(self) -> None:
        transfer_fn = await _capture_twilio_transfer_fn(
            webhook_host="example.ngrok.io", agent_number="+15559998888"
        )
        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await transfer_fn(
                "+15550001111", mode="warm", summary="Caller needs billing."
            )
        assert result["status"] == "transferring"
        assert result["mode"] == "warm"
        assert http.post.await_count == 2
        # From = the configured agent number.
        assert http.post.await_args_list[1].kwargs["data"]["From"] == "+15559998888"

    @pytest.mark.asyncio
    async def test_warm_mode_from_falls_back_to_callee(self) -> None:
        transfer_fn = await _capture_twilio_transfer_fn()
        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await transfer_fn("+15550001111", mode="warm")
        assert result["status"] == "transferring"
        # _make_mock_ws sets callee=+15559876543 via query params.
        assert http.post.await_args_list[1].kwargs["data"]["From"] == "+15559876543"


@pytest.mark.unit
@pytest.mark.mocked
class TestTelnyxPlivoWarmUnsupported:
    @pytest.mark.asyncio
    async def test_telnyx_warm_returns_error_envelope_no_rest(self) -> None:
        from getpatter.telephony.telnyx import telnyx_stream_bridge

        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.query_params = {"caller": "+15551234567", "callee": "+15559876543"}
        start = json.dumps(
            {
                "event": "start",
                "stream_id": "s1",
                "start": {"call_control_id": "cc-123", "media_format": {}},
            }
        )
        stop = json.dumps({"event": "stop"})
        ws.receive_text = AsyncMock(side_effect=[start, stop, Exception("stop")])
        ws.send_text = AsyncMock()

        with (
            patch(
                "getpatter.telephony.telnyx.OpenAIRealtimeStreamHandler"
            ) as handler_cls,
            patch("getpatter.telephony.telnyx.create_metrics_accumulator") as metrics,
            patch("getpatter.telephony.telnyx.resolve_agent_prompt", return_value="p"),
            patch(
                "getpatter.telephony.telnyx.fetch_deepgram_cost",
                new_callable=AsyncMock,
            ),
        ):
            handler = AsyncMock()
            handler.audio_sender = None
            handler.stt = None
            handler_cls.return_value = handler
            metrics.return_value = MagicMock()
            await telnyx_stream_bridge(
                websocket=ws,
                agent=make_agent(provider="openai_realtime"),
                openai_key="sk-test",
                telnyx_key="key-test",
            )
            transfer_fn = handler_cls.call_args.kwargs["transfer_fn"]

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await transfer_fn("+15550001111", mode="warm", summary="s")
        assert result == {"error": "warm transfer not yet supported on telnyx"}
        http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plivo_warm_returns_error_envelope_no_rest(self) -> None:
        from getpatter.telephony.plivo import plivo_stream_bridge

        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.query_params = {"caller": "+15551234567", "callee": "+15559876543"}
        start = json.dumps(
            {
                "event": "start",
                "start": {"callId": "uuid-1", "streamId": "s1", "mediaFormat": {}},
            }
        )
        stop = json.dumps({"event": "stop"})
        ws.receive_text = AsyncMock(side_effect=[start, stop, Exception("stop")])
        ws.send_text = AsyncMock()

        with (
            patch(
                "getpatter.telephony.plivo.OpenAIRealtimeStreamHandler"
            ) as handler_cls,
            patch("getpatter.telephony.plivo.create_metrics_accumulator") as metrics,
            patch("getpatter.telephony.plivo.resolve_agent_prompt", return_value="p"),
            patch(
                "getpatter.telephony.plivo.fetch_deepgram_cost",
                new_callable=AsyncMock,
            ),
        ):
            handler = AsyncMock()
            handler.audio_sender = None
            handler.stt = None
            handler_cls.return_value = handler
            metrics.return_value = MagicMock()
            await plivo_stream_bridge(
                websocket=ws,
                agent=make_agent(provider="openai_realtime"),
                openai_key="sk-test",
                plivo_auth_id="MA" + "0" * 18,
                plivo_auth_token="token",
                webhook_host="example.ngrok.io",
            )
            transfer_fn = handler_cls.call_args.kwargs["transfer_fn"]

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            result = await transfer_fn("+15550001111", mode="warm", summary="s")
        assert result == {"error": "warm transfer not yet supported on plivo"}
        http.post.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.mocked
class TestTelnyxClientStateTransfer:
    """Telnyx cold transfer can carry an opt-in ``client_state`` that Telnyx
    base64-encodes and echoes on the transferred leg's webhooks. Mocks ONLY
    the carrier REST boundary (``httpx``)."""

    async def _capture_telnyx_transfer_fn(self):
        from getpatter.telephony.telnyx import telnyx_stream_bridge

        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.query_params = {"caller": "+15551234567", "callee": "+15559876543"}
        start = json.dumps(
            {
                "event": "start",
                "stream_id": "s1",
                "start": {"call_control_id": "cc-123", "media_format": {}},
            }
        )
        stop = json.dumps({"event": "stop"})
        ws.receive_text = AsyncMock(side_effect=[start, stop, Exception("stop")])
        ws.send_text = AsyncMock()

        with (
            patch(
                "getpatter.telephony.telnyx.OpenAIRealtimeStreamHandler"
            ) as handler_cls,
            patch("getpatter.telephony.telnyx.create_metrics_accumulator") as metrics,
            patch("getpatter.telephony.telnyx.resolve_agent_prompt", return_value="p"),
            patch(
                "getpatter.telephony.telnyx.fetch_deepgram_cost",
                new_callable=AsyncMock,
            ),
        ):
            handler = AsyncMock()
            handler.audio_sender = None
            handler.stt = None
            handler_cls.return_value = handler
            metrics.return_value = MagicMock()
            await telnyx_stream_bridge(
                websocket=ws,
                agent=make_agent(provider="openai_realtime"),
                openai_key="sk-test",
                telnyx_key="key-test",
            )
            return handler_cls.call_args.kwargs["transfer_fn"]

    @pytest.mark.asyncio
    async def test_cold_without_client_state_posts_bare_to(self) -> None:
        transfer_fn = await self._capture_telnyx_transfer_fn()
        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            await transfer_fn("+15550001111")
        assert http.post.await_count == 1
        assert http.post.await_args_list[0].kwargs["json"] == {"to": "+15550001111"}

    @pytest.mark.asyncio
    async def test_cold_with_client_state_base64_encodes_into_body(self) -> None:
        import base64

        transfer_fn = await self._capture_telnyx_transfer_fn()
        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            await transfer_fn("+15550001111", client_state="customer_id=VIP42")
        body = http.post.await_args_list[0].kwargs["json"]
        assert body["to"] == "+15550001111"
        assert base64.b64decode(body["client_state"]).decode() == "customer_id=VIP42"


# ---------------------------------------------------------------------------
# Pipeline built-in transfer handler — mode plumbing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPipelineTransferHandlerModes:
    @pytest.mark.asyncio
    async def test_cold_result_strings_byte_identical(self) -> None:
        from getpatter.stream_handler import _augment_with_builtin_handoff_tools

        captured: list = []

        async def fake_transfer(number: str) -> None:
            captured.append(number)

        tools = _augment_with_builtin_handoff_tools(
            None, transfer_fn=fake_transfer, hangup_fn=None
        )
        result = await tools[0]["handler"]({"number": "+14155551234"}, {})
        assert result == "Transferring to +14155551234"
        assert captured == ["+14155551234"]

    @pytest.mark.asyncio
    async def test_warm_dispatches_kwargs_and_wraps_result(self) -> None:
        from getpatter.stream_handler import _augment_with_builtin_handoff_tools

        seen: list = []

        async def warm_capable(number: str, *, mode: str = "cold", summary: str = ""):
            seen.append((number, mode, summary))
            return {"status": "transferring", "mode": mode, "to": number}

        tools = _augment_with_builtin_handoff_tools(
            None, transfer_fn=warm_capable, hangup_fn=None
        )
        result = json.loads(
            await tools[0]["handler"](
                {"number": "+14155551234", "mode": "warm", "summary": "ctx"}, {}
            )
        )
        assert seen == [("+14155551234", "warm", "ctx")]
        assert result == {
            "status": "transferring",
            "mode": "warm",
            "to": "+14155551234",
        }

    @pytest.mark.asyncio
    async def test_warm_against_legacy_fn_returns_envelope(self) -> None:
        from getpatter.stream_handler import _augment_with_builtin_handoff_tools

        called: list = []

        async def legacy(number: str) -> None:
            called.append(number)

        tools = _augment_with_builtin_handoff_tools(
            None, transfer_fn=legacy, hangup_fn=None
        )
        result = json.loads(
            await tools[0]["handler"]({"number": "+14155551234", "mode": "warm"}, {})
        )
        assert "error" in result
        assert called == []  # never silently degrades to a cold redirect

    @pytest.mark.asyncio
    async def test_invalid_mode_rejected(self) -> None:
        from getpatter.stream_handler import _augment_with_builtin_handoff_tools

        called: list = []

        async def fake_transfer(number: str) -> None:
            called.append(number)

        tools = _augment_with_builtin_handoff_tools(
            None, transfer_fn=fake_transfer, hangup_fn=None
        )
        result = json.loads(
            await tools[0]["handler"]({"number": "+14155551234", "mode": "hot"}, {})
        )
        assert result["status"] == "rejected"
        assert "error" in result
        assert called == []


# ---------------------------------------------------------------------------
# CallControl.transfer — programmatic warm-transfer surface
# ---------------------------------------------------------------------------


def _make_call_control(transfer_fn):
    from getpatter.models import CallControl

    return CallControl(
        call_id=CALL_SID,
        caller="+15551234567",
        callee="+15559876543",
        telephony_provider="twilio",
        _transfer_fn=transfer_fn,
    )


@pytest.mark.unit
class TestCallControlTransferModes:
    @pytest.mark.asyncio
    async def test_invalid_mode_rejected_without_dispatch(self) -> None:
        """A typo'd mode must NEVER silently degrade to a blind redirect."""
        called: list = []

        async def warm_capable(number, *, mode: str = "cold", summary: str = ""):
            called.append((number, mode))

        control = _make_call_control(warm_capable)
        result = await control.transfer("+14155551234", mode="hot")  # type: ignore[arg-type]
        assert result["status"] == "rejected"
        assert "Invalid transfer mode" in result["error"]
        assert called == []
        assert control.is_transferred is False

    @pytest.mark.asyncio
    async def test_warm_error_envelope_keeps_call_running(self) -> None:
        async def unsupported(number, *, mode: str = "cold", summary: str = ""):
            return {"error": "warm transfer not yet supported on telnyx"}

        control = _make_call_control(unsupported)
        result = await control.transfer("+14155551234", mode="warm")
        assert result["error"]
        # The call is NOT marked transferred — the AI keeps the line.
        assert control.is_transferred is False
        assert control.ended is False

    @pytest.mark.asyncio
    async def test_warm_success_marks_transferred(self) -> None:
        async def warm_ok(number, *, mode: str = "cold", summary: str = ""):
            return {"status": "transferring", "mode": mode, "to": number}

        control = _make_call_control(warm_ok)
        result = await control.transfer(
            "+14155551234", mode="warm", summary="Order 12."
        )
        assert result["status"] == "transferring"
        assert control.is_transferred is True

    @pytest.mark.asyncio
    async def test_cold_legacy_contract_unchanged(self) -> None:
        called: list = []

        async def legacy(number):
            called.append(number)

        control = _make_call_control(legacy)
        result = await control.transfer("+14155551234")
        assert result is None  # legacy contract
        assert called == ["+14155551234"]
        assert control.is_transferred is True

    @pytest.mark.asyncio
    async def test_cold_client_state_threaded_to_capable_fn(self) -> None:
        """``client_state`` is forwarded on a cold transfer to a carrier fn
        whose signature accepts it (Telnyx)."""
        seen: list = []

        async def capable(number, *, mode="cold", summary="", client_state=None):
            seen.append((number, client_state))

        control = _make_call_control(capable)
        result = await control.transfer(
            "+14155551234", client_state="customer_id=VIP42"
        )
        assert result is None  # cold contract
        assert seen == [("+14155551234", "customer_id=VIP42")]
        assert control.is_transferred is True

    @pytest.mark.asyncio
    async def test_cold_client_state_ignored_by_legacy_fn(self) -> None:
        """A legacy ``(number)``-only carrier fn (Twilio/Plivo) must still be
        called positionally when ``client_state`` is supplied — byte-identical
        to the historical contract, no crash."""
        called: list = []

        async def legacy(number):
            called.append(number)

        control = _make_call_control(legacy)
        result = await control.transfer(
            "+14155551234", client_state="customer_id=VIP42"
        )
        assert result is None
        assert called == ["+14155551234"]
        assert control.is_transferred is True

    @pytest.mark.asyncio
    async def test_warm_against_legacy_fn_returns_envelope(self) -> None:
        called: list = []

        async def legacy(number):
            called.append(number)

        control = _make_call_control(legacy)
        result = await control.transfer("+14155551234", mode="warm")
        assert "error" in result
        assert called == []
        assert control.is_transferred is False


# ---------------------------------------------------------------------------
# Server webhook routes — conference + warm-status callbacks
# ---------------------------------------------------------------------------


def _make_warm_server(**config_overrides):
    from getpatter.local_config import LocalConfig
    from getpatter.server import EmbeddedServer

    cfg_kwargs = dict(
        telephony_provider="twilio",
        twilio_sid=ACCOUNT_SID,
        twilio_token="",
        openai_key="sk-test",
        webhook_url="test.ngrok.io",
        phone_number="+15551234567",
        require_signature=False,
    )
    cfg_kwargs.update(config_overrides)
    return EmbeddedServer(
        config=LocalConfig(**cfg_kwargs),
        agent=make_agent(),
        dashboard=False,
        allow_insecure_dashboard=True,
    )


def _get_endpoint(app, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise ValueError(f"No route found for {path}")


class _MockRequest:
    def __init__(
        self,
        form_data: dict | None = None,
        headers: dict | None = None,
        url: str = "https://test.ngrok.io/webhooks/twilio/conference",
        query_params: dict | None = None,
    ):
        self._form_data = form_data or {}
        self.headers = headers or {}
        self.url = url
        self.query_params = query_params or {}

    async def form(self):
        return self._form_data

    async def body(self):
        return b""


@pytest.mark.unit
class TestWarmTransferWebhookRoutes:
    @pytest.mark.asyncio
    async def test_conference_route_returns_204(self) -> None:
        srv = _make_warm_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/conference")
        response = await endpoint(
            _MockRequest(
                form_data={
                    "StatusCallbackEvent": "participant-join",
                    "FriendlyName": f"patter-warm-{CALL_SID}",
                    "ConferenceSid": "CF" + "0" * 32,
                    "CallSid": CALL_SID,
                }
            )
        )
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_conference_route_fails_closed_without_token(self) -> None:
        # require_signature=True (default posture) + no twilio_token → 503,
        # exactly like every other Twilio webhook.
        srv = _make_warm_server(require_signature=True, twilio_token="")
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/conference")
        response = await endpoint(_MockRequest())
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_conference_route_rejects_invalid_signature(self) -> None:
        srv = _make_warm_server(require_signature=True, twilio_token="real_token")
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/conference")
        response = await endpoint(
            _MockRequest(headers={"X-Twilio-Signature": "badsig"})
        )
        assert response.status_code == 403

    def _signed_request(
        self, token: str, form_data: dict, query_params: dict
    ) -> _MockRequest:
        """Build a request with a VALID X-Twilio-Signature — the warm-status
        action path needs a configured ``twilio_token`` for the recovery REST
        call, which also makes signature validation mandatory (fail-closed)."""
        from twilio.request_validator import RequestValidator

        url = "https://test.ngrok.io/webhooks/twilio/warm-status"
        signature = RequestValidator(token).compute_signature(url, form_data)
        return _MockRequest(
            form_data=form_data,
            headers={"X-Twilio-Signature": signature},
            url=url,
            query_params=query_params,
        )

    @pytest.mark.asyncio
    async def test_warm_status_failure_releases_parked_caller(self) -> None:
        srv = _make_warm_server(twilio_token="tok_test")
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/warm-status")

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            response = await endpoint(
                self._signed_request(
                    "tok_test",
                    {"CallSid": "CA" + "b" * 32, "CallStatus": "no-answer"},
                    {"caller_call_sid": CALL_SID},
                )
            )
        assert response.status_code == 204
        http.post.assert_awaited_once()
        call = http.post.await_args_list[0]
        assert f"/Calls/{CALL_SID}.json" in call.args[0]
        assert "<Hangup/>" in call.kwargs["data"]["Twiml"]

    @pytest.mark.asyncio
    async def test_warm_status_completed_is_noop(self) -> None:
        srv = _make_warm_server(twilio_token="tok_test")
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/warm-status")

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            response = await endpoint(
                self._signed_request(
                    "tok_test",
                    {"CallSid": "CA" + "b" * 32, "CallStatus": "completed"},
                    {"caller_call_sid": CALL_SID},
                )
            )
        assert response.status_code == 204
        http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_warm_status_rejects_invalid_caller_sid(self) -> None:
        # Path-injection guard: a forged caller_call_sid must never be
        # interpolated into the Twilio REST URL.
        srv = _make_warm_server(twilio_token="tok_test")
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/warm-status")

        http = _mock_http()
        with patch("httpx.AsyncClient", return_value=http):
            response = await endpoint(
                self._signed_request(
                    "tok_test",
                    {"CallSid": "CA" + "b" * 32, "CallStatus": "failed"},
                    {"caller_call_sid": "../../evil"},
                )
            )
        assert response.status_code == 204
        http.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_warm_status_fails_closed_without_token(self) -> None:
        srv = _make_warm_server(require_signature=True, twilio_token="")
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/warm-status")
        response = await endpoint(_MockRequest())
        assert response.status_code == 503
