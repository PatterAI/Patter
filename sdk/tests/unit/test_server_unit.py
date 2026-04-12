"""Unit tests for patter.server — EmbeddedServer construction & HTTP routes.

Tests the FastAPI app created by ``_create_app()``. Route handler tests
call the endpoint functions directly with mock Request objects (avoiding
FastAPI's annotation-resolution issues with PEP 563 + inner functions).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.local_config import LocalConfig
from patter.server import EmbeddedServer

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(**overrides) -> EmbeddedServer:
    """Build an EmbeddedServer with sensible defaults for testing."""
    cfg = LocalConfig(
        telephony_provider="twilio",
        twilio_sid="ACtest000000000000000000000000000",
        twilio_token="tok_test_00000000000000000000000000",
        openai_key="sk-test",
        webhook_url="test.ngrok.io",
        phone_number="+15551234567",
    )
    agent = make_agent()
    defaults = dict(config=cfg, agent=agent, dashboard=False)
    defaults.update(overrides)
    return EmbeddedServer(**defaults)


def _get_endpoint(app, path: str):
    """Find and return the endpoint function for a given route path."""
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise ValueError(f"No route found for {path}")


class _MockRequest:
    """Minimal mock of a Starlette Request for direct handler testing."""

    def __init__(
        self,
        form_data: dict | None = None,
        json_data: dict | None = None,
        headers: dict | None = None,
        url: str = "https://test.ngrok.io/webhooks/twilio/voice",
        query_params: dict | None = None,
    ):
        self._form_data = form_data or {}
        self._json_data = json_data
        self.headers = headers or {}
        self.url = url
        self.query_params = query_params or {}

    async def form(self):
        return self._form_data

    async def json(self):
        return self._json_data


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestEmbeddedServerInit:
    """EmbeddedServer.__init__ stores config and defaults."""

    def test_stores_config_and_agent(self) -> None:
        srv = _make_server()
        assert srv.config.twilio_sid == "ACtest000000000000000000000000000"
        assert srv.agent.voice == "alloy"

    def test_defaults_recording_false(self) -> None:
        srv = _make_server()
        assert srv.recording is False

    def test_defaults_dashboard_true_when_not_overridden(self) -> None:
        srv = EmbeddedServer(
            config=LocalConfig(),
            agent=make_agent(),
        )
        assert srv.dashboard is True

    def test_callbacks_initially_none(self) -> None:
        srv = _make_server()
        assert srv.on_call_start is None
        assert srv.on_call_end is None
        assert srv.on_transcript is None
        assert srv.on_message is None
        assert srv.on_metrics is None

    def test_no_active_connections_initially(self) -> None:
        srv = _make_server()
        assert len(srv._active_connections) == 0

    def test_voicemail_message_stored(self) -> None:
        srv = _make_server(voicemail_message="Leave a message")
        assert srv.voicemail_message == "Leave a message"

    def test_pricing_stored(self) -> None:
        pricing = {"stt": 0.01}
        srv = _make_server(pricing=pricing)
        assert srv.pricing == pricing

    def test_dashboard_token_stored(self) -> None:
        srv = _make_server(dashboard=True, dashboard_token="secret123")
        assert srv.dashboard_token == "secret123"


# ---------------------------------------------------------------------------
# _wrap_callbacks
# ---------------------------------------------------------------------------


class TestWrapCallbacks:
    """_wrap_callbacks returns wrappers that feed the store then call user fns."""

    @pytest.mark.asyncio
    async def test_calls_store_and_user_callback_on_start(self) -> None:
        srv = _make_server()
        store = MagicMock()
        srv._metrics_store = store
        user_cb = AsyncMock()
        srv.on_call_start = user_cb

        on_start, _, _ = srv._wrap_callbacks()
        await on_start({"call_id": "c1"})

        store.record_call_start.assert_called_once_with({"call_id": "c1"})
        user_cb.assert_awaited_once_with({"call_id": "c1"})

    @pytest.mark.asyncio
    async def test_calls_store_and_user_callback_on_end(self) -> None:
        srv = _make_server()
        store = MagicMock()
        srv._metrics_store = store
        user_cb = AsyncMock()
        srv.on_call_end = user_cb

        _, on_end, _ = srv._wrap_callbacks()
        data = {"call_id": "c1", "metrics": {"duration": 10}}
        await on_end(data)

        store.record_call_end.assert_called_once_with(data, metrics=data.get("metrics"))
        user_cb.assert_awaited_once_with(data)

    @pytest.mark.asyncio
    async def test_calls_store_and_user_callback_on_metrics(self) -> None:
        srv = _make_server()
        store = MagicMock()
        srv._metrics_store = store
        user_cb = AsyncMock()
        srv.on_metrics = user_cb

        _, _, on_metrics = srv._wrap_callbacks()
        await on_metrics({"turn": 1})

        store.record_turn.assert_called_once_with({"turn": 1})
        user_cb.assert_awaited_once_with({"turn": 1})

    @pytest.mark.asyncio
    async def test_no_store_still_calls_user_callback(self) -> None:
        srv = _make_server()
        srv._metrics_store = None
        user_cb = AsyncMock()
        srv.on_call_start = user_cb

        on_start, _, _ = srv._wrap_callbacks()
        await on_start({"call_id": "c1"})

        user_cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_user_callback_still_calls_store(self) -> None:
        srv = _make_server()
        store = MagicMock()
        srv._metrics_store = store
        srv.on_call_start = None

        on_start, _, _ = srv._wrap_callbacks()
        await on_start({"call_id": "c1"})

        store.record_call_start.assert_called_once()


# ---------------------------------------------------------------------------
# _create_app — route registration
# ---------------------------------------------------------------------------


class TestCreateApp:
    """_create_app builds a FastAPI app with the correct routes."""

    def test_returns_fastapi_app(self) -> None:
        from fastapi import FastAPI

        srv = _make_server()
        app = srv._create_app()
        assert isinstance(app, FastAPI)

    def test_has_health_route(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/health" in paths

    def test_has_twilio_voice_route(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/webhooks/twilio/voice" in paths

    def test_has_twilio_recording_route(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/webhooks/twilio/recording" in paths

    def test_has_twilio_amd_route(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/webhooks/twilio/amd" in paths

    def test_has_telnyx_voice_route(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/webhooks/telnyx/voice" in paths

    def test_has_twilio_ws_stream_route(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/ws/stream/{call_id}" in paths

    def test_has_telnyx_ws_stream_route(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/ws/telnyx/stream/{call_id}" in paths

    def test_dashboard_enabled_mounts_ui_and_api_routes(self) -> None:
        srv = _make_server(dashboard=True)
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/" in paths
        assert "/api/dashboard/calls" in paths
        assert "/api/v1/calls" in paths
        assert srv._metrics_store is not None

    def test_dashboard_disabled_skips_ui_routes(self) -> None:
        srv = _make_server(dashboard=False)
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/api/dashboard/calls" not in paths
        assert "/api/v1/calls" not in paths
        assert srv._metrics_store is None


# ---------------------------------------------------------------------------
# HTTP route handlers — called directly with mock request
# ---------------------------------------------------------------------------


class TestHealthRoute:
    """GET /health returns status ok."""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/health")
        result = await endpoint()
        assert result == {"status": "ok", "mode": "local"}


class TestTwilioVoiceRoute:
    """POST /webhooks/twilio/voice returns TwiML."""

    @pytest.mark.asyncio
    @patch("patter.providers.twilio_adapter.TwilioAdapter")
    async def test_twilio_voice_returns_xml_no_sig_validation(self, mock_adapter_cls) -> None:
        mock_adapter_cls.generate_stream_twiml.return_value = "<Response><Connect/></Response>"
        srv = _make_server()
        srv.config = LocalConfig(
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="",  # empty = no validation
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
        )
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/voice")

        request = _MockRequest(
            form_data={
                "CallSid": "CA00000000000000000000000000000000",
                "From": "+15551234567",
                "To": "+15559876543",
            },
        )
        response = await endpoint(request)
        assert response.status_code == 200
        assert "text/xml" in response.media_type

    @pytest.mark.asyncio
    async def test_twilio_voice_with_invalid_signature_returns_403(self) -> None:
        srv = _make_server()
        srv.config = LocalConfig(
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="real_token_value",
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
        )
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/voice")

        request = _MockRequest(
            form_data={"CallSid": "CA00000000000000000000000000000000"},
            headers={"X-Twilio-Signature": "badsig"},
            url="https://test.ngrok.io/webhooks/twilio/voice",
        )
        response = await endpoint(request)
        assert response.status_code == 403


class TestTwilioRecordingRoute:
    """POST /webhooks/twilio/recording returns 204."""

    @pytest.mark.asyncio
    async def test_recording_callback_returns_204(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/recording")

        request = _MockRequest(
            form_data={
                "RecordingSid": "RE000",
                "RecordingUrl": "https://api.twilio.com/rec",
                "CallSid": "CA00000000000000000000000000000000",
            },
        )
        response = await endpoint(request)
        assert response.status_code == 204


class TestTwilioAMDRoute:
    """POST /webhooks/twilio/amd returns 204 with voicemail handling."""

    @pytest.mark.asyncio
    async def test_amd_human_returns_204(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/amd")

        request = _MockRequest(
            form_data={
                "AnsweredBy": "human",
                "CallSid": "CA00000000000000000000000000000000",
            },
        )
        response = await endpoint(request)
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_amd_machine_no_voicemail_message_returns_204(self) -> None:
        srv = _make_server(voicemail_message="")
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/amd")

        request = _MockRequest(
            form_data={
                "AnsweredBy": "machine_end_beep",
                "CallSid": "CA00000000000000000000000000000000",
            },
        )
        response = await endpoint(request)
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_amd_machine_invalid_sid_skips_voicemail(self) -> None:
        srv = _make_server(voicemail_message="Leave a message")
        srv.config = LocalConfig(
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok_test",
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
        )
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/amd")

        request = _MockRequest(
            form_data={
                "AnsweredBy": "machine_end_beep",
                "CallSid": "INVALID",
            },
        )
        response = await endpoint(request)
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_amd_machine_drops_voicemail(self) -> None:
        srv = _make_server(voicemail_message="Please leave a message")
        srv.config = LocalConfig(
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok_test",
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
        )
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/amd")

        call_sid = "CA" + "a" * 32
        request = _MockRequest(
            form_data={
                "AnsweredBy": "machine_end_beep",
                "CallSid": call_sid,
            },
        )

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_http):
            response = await endpoint(request)

        assert response.status_code == 204
        mock_http.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_amd_voicemail_http_error_still_returns_204(self) -> None:
        srv = _make_server(voicemail_message="Leave a message")
        srv.config = LocalConfig(
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok_test",
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
        )
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/amd")

        call_sid = "CA" + "a" * 32
        request = _MockRequest(
            form_data={
                "AnsweredBy": "machine_end_silence",
                "CallSid": call_sid,
            },
        )

        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post.side_effect = Exception("Network error")

        with patch("httpx.AsyncClient", return_value=mock_http):
            response = await endpoint(request)

        assert response.status_code == 204


class TestTelnyxVoiceRoute:
    """POST /webhooks/telnyx/voice returns JSON commands."""

    @pytest.mark.asyncio
    async def test_valid_telnyx_webhook_returns_commands(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/telnyx/voice")

        request = _MockRequest(
            json_data={
                "data": {
                    "payload": {
                        "call_control_id": "v3:abc123",
                        "from": "+15551234567",
                        "to": "+15559876543",
                    }
                }
            },
        )
        response = await endpoint(request)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_telnyx_structure_returns_400(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/telnyx/voice")

        request = _MockRequest(json_data={"data": "not_a_dict"})
        response = await endpoint(request)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_telnyx_missing_required_fields_returns_400(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/telnyx/voice")

        request = _MockRequest(
            json_data={
                "data": {
                    "payload": {
                        "call_control_id": "",
                        "from": "+15551234567",
                        "to": "+15559876543",
                    }
                }
            },
        )
        response = await endpoint(request)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_telnyx_sig_warning_logged_once(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/telnyx/voice")

        request = _MockRequest(
            json_data={
                "data": {
                    "payload": {
                        "call_control_id": "v3:abc123",
                        "from": "+1555",
                        "to": "+1666",
                    }
                }
            },
        )
        # First call
        await endpoint(request)
        assert srv._telnyx_sig_warning_logged is True

        # Second call shouldn't change the flag
        await endpoint(request)
        assert srv._telnyx_sig_warning_logged is True


# ---------------------------------------------------------------------------
# stop() — graceful shutdown
# ---------------------------------------------------------------------------


class TestStop:
    """EmbeddedServer.stop() closes connections and shuts down."""

    @pytest.mark.asyncio
    async def test_stop_idempotent(self) -> None:
        srv = _make_server()
        await srv.stop()
        await srv.stop()  # second call should be a no-op
        assert srv._shutting_down is True

    @pytest.mark.asyncio
    async def test_stop_closes_active_connections(self) -> None:
        srv = _make_server()
        ws = AsyncMock()
        srv._active_connections.add(ws)

        await srv.stop()

        ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_sets_server_should_exit(self) -> None:
        srv = _make_server()
        srv._server = MagicMock()

        await srv.stop()

        assert srv._server.should_exit is True

    @pytest.mark.asyncio
    async def test_stop_handles_ws_close_exception(self) -> None:
        srv = _make_server()
        ws = AsyncMock()
        ws.close.side_effect = RuntimeError("already closed")
        srv._active_connections.add(ws)

        # Should not raise
        await srv.stop()
        assert srv._shutting_down is True


# ---------------------------------------------------------------------------
# Dashboard routes via httpx (these DON'T have the PEP 563 issue)
# ---------------------------------------------------------------------------


class TestDashboardRoutes:
    """Dashboard and API routes work through httpx ASGI transport."""

    @pytest.mark.asyncio
    async def test_dashboard_ui_returns_html(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_dashboard_calls_returns_json(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/calls")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_dashboard_aggregates_returns_json(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/aggregates")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_dashboard_call_detail_not_found(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/calls/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_dashboard_active_returns_json(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/active")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_dashboard_export_json(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/export/calls?format=json")
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_dashboard_export_csv(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/export/calls?format=csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_health_via_httpx(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# B2B API routes via httpx
# ---------------------------------------------------------------------------


class TestAPIRoutes:
    """B2B API routes work through httpx ASGI transport."""

    @pytest.mark.asyncio
    async def test_api_list_calls(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/calls")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "pagination" in body

    @pytest.mark.asyncio
    async def test_api_active_calls(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/calls/active")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_call_detail_not_found(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/calls/nope")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_api_analytics_overview(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/analytics/overview")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_analytics_costs(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/analytics/costs")
        assert resp.status_code == 200
        body = resp.json()
        assert "data" in body
        assert "total_cost" in body["data"]

    @pytest.mark.asyncio
    async def test_api_analytics_costs_invalid_from_date(self) -> None:
        import httpx

        srv = _make_server(dashboard=True)
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/analytics/costs?from=bad-date")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Dashboard auth integration
# ---------------------------------------------------------------------------


class TestDashboardAuth:
    """Dashboard routes respect the dashboard_token setting."""

    @pytest.mark.asyncio
    async def test_token_required_returns_401(self) -> None:
        import httpx

        srv = _make_server(dashboard=True, dashboard_token="mysecret")
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/calls")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_token_via_header_succeeds(self) -> None:
        import httpx

        srv = _make_server(dashboard=True, dashboard_token="mysecret")
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/dashboard/calls",
                headers={"Authorization": "Bearer mysecret"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_token_via_query_param_succeeds(self) -> None:
        import httpx

        srv = _make_server(dashboard=True, dashboard_token="mysecret")
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/calls?token=mysecret")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


class TestBanner:
    """show_banner() prints the ASCII art banner."""

    def test_show_banner_runs(self, capsys) -> None:
        from patter.banner import show_banner

        show_banner()
        captured = capsys.readouterr()
        assert "██" in captured.out


# ---------------------------------------------------------------------------
# Common handler utilities
# ---------------------------------------------------------------------------


class TestCommonHandlers:
    """Tests for patter.handlers.common functions."""

    def test_validate_e164_valid(self) -> None:
        from patter.handlers.common import _validate_e164

        assert _validate_e164("+15551234567") is True

    def test_validate_e164_invalid_no_plus(self) -> None:
        from patter.handlers.common import _validate_e164

        assert _validate_e164("15551234567") is False

    def test_validate_e164_invalid_too_short(self) -> None:
        from patter.handlers.common import _validate_e164

        assert _validate_e164("+123") is False

    def test_sanitize_variable_value_strips_control_chars(self) -> None:
        from patter.handlers.common import _sanitize_variable_value

        result = _sanitize_variable_value("hello\x00world\x0a")
        assert "\x00" not in result

    def test_sanitize_variable_value_truncates_at_500(self) -> None:
        from patter.handlers.common import _sanitize_variable_value

        result = _sanitize_variable_value("x" * 1000)
        assert len(result) == 500

    def test_resolve_variables_replaces_placeholders(self) -> None:
        from patter.handlers.common import _resolve_variables

        result = _resolve_variables("Hello {name}, age {age}", {"name": "Alice", "age": "30"})
        assert result == "Hello Alice, age 30"

    def test_resolve_variables_no_match_unchanged(self) -> None:
        from patter.handlers.common import _resolve_variables

        result = _resolve_variables("Hello {name}", {"other": "value"})
        assert result == "Hello {name}"

    def test_create_stt_from_config_none(self) -> None:
        from patter.handlers.common import _create_stt_from_config

        assert _create_stt_from_config(None) is None

    def test_create_tts_from_config_none(self) -> None:
        from patter.handlers.common import _create_tts_from_config

        assert _create_tts_from_config(None) is None
