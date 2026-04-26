"""Unit tests for the ``/webhooks/twilio/status`` endpoint (BUG #06) and for
``ring_timeout`` propagation into Twilio/Telnyx dial parameters (IMP2).

Two regressions are locked in here:

  1. **BUG #06** — outbound calls that never reach the media channel
     (no-answer, busy, carrier-rejected) used to disappear from the
     dashboard entirely. The fix wires Twilio's ``StatusCallback`` to
     ``/webhooks/twilio/status`` and forwards every transition to the
     in-memory metrics store.
  2. **IMP2** — callers may set ``ring_timeout`` on ``Patter.call()`` to
     control how long the phone rings before the carrier gives up. It
     must land as ``Timeout`` on Twilio's REST payload and ``timeout_secs``
     on Telnyx's — the old code silently dropped it.

The tests exercise the endpoint directly via the ASGI layer, and exercise
``ring_timeout`` by patching the adapter's ``initiate_call`` and inspecting
the kwargs it was invoked with (no network traffic).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.local_config import LocalConfig
from getpatter.server import EmbeddedServer

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(**overrides) -> EmbeddedServer:
    cfg = LocalConfig(
        telephony_provider="twilio",
        twilio_sid="ACtest000000000000000000000000000",
        twilio_token="",  # empty = no signature validation
        openai_key="sk-test",
        webhook_url="test.ngrok.io",
        phone_number="+15551234567",
        require_signature=False,
    )
    defaults = dict(config=cfg, agent=make_agent(), dashboard=True)
    defaults.update(overrides)
    return EmbeddedServer(**defaults)


def _get_endpoint(app, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise ValueError(f"No route found for {path}")


class _MockRequest:
    def __init__(self, form_data: dict, headers: dict | None = None) -> None:
        self._form = form_data
        self.headers = headers or {}
        self.url = "https://test.ngrok.io/webhooks/twilio/status"
        self.query_params = {}

    async def form(self):
        return self._form


# ---------------------------------------------------------------------------
# /webhooks/twilio/status — endpoint routing + status propagation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStatusRouteRegistration:
    """The status callback route must be mounted by ``_create_app``."""

    def test_route_exists(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        paths = [r.path for r in app.routes]
        assert "/webhooks/twilio/status" in paths


@pytest.mark.unit
class TestStatusCallback:
    """The status endpoint forwards each transition to the metrics store."""

    @pytest.mark.asyncio
    async def test_status_returns_204(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/status")
        request = _MockRequest(
            form_data={
                "CallSid": "CA" + "1" * 32,
                "CallStatus": "ringing",
            }
        )
        response = await endpoint(request)
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_status_updates_metrics_store(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/status")
        # Stub the store so we can inspect the call.
        srv._metrics_store = MagicMock()

        request = _MockRequest(
            form_data={
                "CallSid": "CA" + "2" * 32,
                "CallStatus": "no-answer",
                "CallDuration": "0",
            }
        )
        response = await endpoint(request)

        assert response.status_code == 204
        srv._metrics_store.update_call_status.assert_called_once()
        call_args = srv._metrics_store.update_call_status.call_args
        assert call_args.args[0] == "CA" + "2" * 32
        assert call_args.args[1] == "no-answer"
        assert call_args.kwargs.get("duration_seconds") == 0.0

    @pytest.mark.asyncio
    async def test_status_without_duration_omits_extra(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/status")
        srv._metrics_store = MagicMock()

        request = _MockRequest(
            form_data={
                "CallSid": "CA" + "3" * 32,
                "CallStatus": "ringing",
            }
        )
        await endpoint(request)

        # No duration -> no ``duration_seconds`` kwarg (keeps the active
        # call record's ``duration_seconds`` untouched).
        kwargs = srv._metrics_store.update_call_status.call_args.kwargs
        assert "duration_seconds" not in kwargs

    @pytest.mark.asyncio
    async def test_status_invalid_duration_swallowed(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/status")
        srv._metrics_store = MagicMock()

        request = _MockRequest(
            form_data={
                "CallSid": "CA" + "4" * 32,
                "CallStatus": "completed",
                "CallDuration": "not-a-number",
            }
        )
        response = await endpoint(request)
        assert response.status_code == 204
        # Status was still recorded — just without a duration.
        srv._metrics_store.update_call_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_status_missing_sid_skips_store(self) -> None:
        srv = _make_server()
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/status")
        srv._metrics_store = MagicMock()

        request = _MockRequest(
            form_data={"CallStatus": "ringing"}  # no CallSid
        )
        response = await endpoint(request)

        assert response.status_code == 204
        srv._metrics_store.update_call_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_with_no_metrics_store_returns_204(self) -> None:
        """Dashboard disabled -> no store; endpoint must still accept the POST."""
        srv = _make_server(dashboard=False)
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/status")

        request = _MockRequest(
            form_data={
                "CallSid": "CA" + "5" * 32,
                "CallStatus": "completed",
            }
        )
        response = await endpoint(request)
        assert response.status_code == 204  # no raise, no store


@pytest.mark.unit
class TestStatusSignature:
    """Signature enforcement on the status endpoint mirrors the other routes."""

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_403(self) -> None:
        srv = _make_server()
        srv.config = LocalConfig(
            telephony_provider="twilio",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="real_token_value",  # -> validator active
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
        )
        app = srv._create_app()
        endpoint = _get_endpoint(app, "/webhooks/twilio/status")

        request = _MockRequest(
            form_data={
                "CallSid": "CA" + "a" * 32,
                "CallStatus": "ringing",
            },
            headers={"X-Twilio-Signature": "wrong"},
        )
        response = await endpoint(request)
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# ring_timeout propagation through Patter.call()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRingTimeoutPropagation:
    """``ring_timeout`` must end up in the dial parameters for each provider."""

    @pytest.mark.asyncio
    async def test_twilio_ring_timeout_becomes_timeout_param(self) -> None:
        from getpatter.client import Patter

        cfg = LocalConfig(
            telephony_provider="twilio",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok_test",
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
            phone_number="+15551234567",
        )
        phone = Patter.__new__(Patter)
        phone._mode = "local"
        phone._local_config = cfg
        phone._server = None

        with patch(
            "getpatter.providers.twilio_adapter.TwilioAdapter"
        ) as mock_adapter_cls:
            mock_adapter = mock_adapter_cls.return_value
            mock_adapter.initiate_call = AsyncMock(
                return_value="CA" + "9" * 32
            )

            await phone.call(
                to="+15559876543",
                agent=make_agent(),
                ring_timeout=45,
            )

            mock_adapter.initiate_call.assert_awaited_once()
            extra_params = mock_adapter.initiate_call.await_args.kwargs[
                "extra_params"
            ]
            assert extra_params["Timeout"] == 45

    @pytest.mark.asyncio
    async def test_twilio_ring_timeout_omitted_when_none(self) -> None:
        """Passing no ring_timeout must not inject Timeout into the payload."""
        from getpatter.client import Patter

        cfg = LocalConfig(
            telephony_provider="twilio",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok_test",
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
            phone_number="+15551234567",
        )
        phone = Patter.__new__(Patter)
        phone._mode = "local"
        phone._local_config = cfg
        phone._server = None

        with patch(
            "getpatter.providers.twilio_adapter.TwilioAdapter"
        ) as mock_adapter_cls:
            mock_adapter = mock_adapter_cls.return_value
            mock_adapter.initiate_call = AsyncMock(
                return_value="CA" + "8" * 32
            )

            await phone.call(to="+15559876543", agent=make_agent())

            extra_params = mock_adapter.initiate_call.await_args.kwargs[
                "extra_params"
            ]
            assert "Timeout" not in extra_params

    @pytest.mark.asyncio
    async def test_twilio_statuscallback_always_registered(self) -> None:
        """BUG #06 — every outbound Twilio call must set StatusCallback so
        the dashboard receives ringing / no-answer / busy transitions."""
        from getpatter.client import Patter

        cfg = LocalConfig(
            telephony_provider="twilio",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok_test",
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
            phone_number="+15551234567",
        )
        phone = Patter.__new__(Patter)
        phone._mode = "local"
        phone._local_config = cfg
        phone._server = None

        with patch(
            "getpatter.providers.twilio_adapter.TwilioAdapter"
        ) as mock_adapter_cls:
            mock_adapter = mock_adapter_cls.return_value
            mock_adapter.initiate_call = AsyncMock(
                return_value="CA" + "7" * 32
            )

            await phone.call(to="+15559876543", agent=make_agent())

            extra = mock_adapter.initiate_call.await_args.kwargs[
                "extra_params"
            ]
            assert (
                extra["StatusCallback"]
                == "https://test.ngrok.io/webhooks/twilio/status"
            )
            assert extra["StatusCallbackMethod"] == "POST"
            # Events we care about for BUG #06.
            assert "ringing" in extra["StatusCallbackEvent"]
            assert "completed" in extra["StatusCallbackEvent"]

    @pytest.mark.asyncio
    async def test_telnyx_ring_timeout_becomes_timeout_secs(self) -> None:
        """Telnyx uses ``timeout_secs``, not ``Timeout`` — confirm the adapter
        receives the kwarg verbatim."""
        from getpatter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(
            api_key="tk_test",
            connection_id="conn-1",
        )

        captured: dict = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"data": {"call_control_id": "v3:xyz"}}

        async def _fake_post(path: str, **kwargs):
            captured["path"] = path
            captured["json"] = kwargs.get("json", {})
            return _Resp()

        adapter._client = MagicMock()
        adapter._client.post = _fake_post

        await adapter.initiate_call(
            "+15551234567",
            "+15559876543",
            "wss://test.ngrok.io/ws/telnyx/stream/outbound",
            ring_timeout=30,
        )

        assert captured["path"] == "/calls"
        assert captured["json"]["timeout_secs"] == 30

    @pytest.mark.asyncio
    async def test_telnyx_ring_timeout_omitted_when_none(self) -> None:
        from getpatter.providers.telnyx_adapter import TelnyxAdapter

        adapter = TelnyxAdapter(api_key="tk_test", connection_id="conn-1")
        captured: dict = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"data": {"call_control_id": "v3:xyz"}}

        async def _fake_post(path: str, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return _Resp()

        adapter._client = MagicMock()
        adapter._client.post = _fake_post

        await adapter.initiate_call(
            "+15551234567",
            "+15559876543",
            "wss://test.ngrok.io/ws/telnyx/stream/outbound",
        )

        assert "timeout_secs" not in captured["json"]
