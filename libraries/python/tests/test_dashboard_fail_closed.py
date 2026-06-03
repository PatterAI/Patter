"""Fail-closed dashboard gate — authentic real-app route-mounting tests.

These tests build the REAL FastAPI application via ``EmbeddedServer._create_app``
and drive it through the REAL Starlette ``TestClient`` (real ASGI routing, real
route mounting, real auth dependency). Nothing under test is mocked — replacing
the gate with a no-op (always mount) makes these tests fail, which is the
litmus per ``.claude/rules/authentic-tests.md``.

The gate's purpose: when the embedded metrics dashboard + call-data API would be
reachable beyond loopback (a tunnel / public ``webhook_url`` / explicit
non-loopback ``PATTER_BIND_HOST``) without a ``dashboard_token``, the dashboard
and ``/api/*`` call-data routes are NOT mounted (they expose call transcripts and
metadata — PII). The carrier webhook + ``/health`` routes always mount so calls
keep working. ``allow_insecure_dashboard=True`` is the explicit escape hatch.
"""

import pytest

from getpatter.local_config import LocalConfig
from getpatter.models import Agent

_has_fastapi = False
try:
    import fastapi  # noqa: F401
    from starlette.testclient import TestClient  # noqa: F401

    _has_fastapi = True
except ImportError:
    pass

# A public (non-loopback) webhook hostname — what a tunnel would assign.
# Not real PII / infra: trycloudflare.com is the public quick-tunnel domain.
_PUBLIC_WEBHOOK = "patter-test.trycloudflare.com"
_LOOPBACK_WEBHOOK = "127.0.0.1"

# Representative routes.
_DASHBOARD_ROOT = "/"
_DASHBOARD_API = "/api/dashboard/calls"
_CALLDATA_API = "/api/v1/calls"
_WEBHOOK_ROUTE = "/webhooks/twilio/voice"
_HEALTH_ROUTE = "/health"


def _agent() -> Agent:
    return Agent(
        system_prompt="Test", voice="alloy", model="gpt-4o-mini-realtime-preview"
    )


def _config(webhook_url: str) -> LocalConfig:
    return LocalConfig(
        telephony_provider="twilio",
        twilio_sid="AC" + "a" * 32,
        twilio_token="test-token",
        openai_key="sk-test",
        phone_number="+15550001234",
        webhook_url=webhook_url,
    )


def _build_app(
    webhook_url: str,
    *,
    dashboard: bool = True,
    dashboard_token: str = "",
    allow_insecure_dashboard: bool = False,
):
    """Construct a real EmbeddedServer and return its real FastAPI app."""
    from getpatter.server import EmbeddedServer

    server = EmbeddedServer(
        config=_config(webhook_url),
        agent=_agent(),
        dashboard=dashboard,
        dashboard_token=dashboard_token,
        allow_insecure_dashboard=allow_insecure_dashboard,
    )
    return server._create_app()


def _route_paths(app) -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "path")}


@pytest.fixture(autouse=True)
def _clear_bind_host(monkeypatch):
    """Isolate signal (c): ensure no inherited PATTER_BIND_HOST leaks in."""
    monkeypatch.delenv("PATTER_BIND_HOST", raising=False)


@pytest.mark.skipif(not _has_fastapi, reason="fastapi not installed")
@pytest.mark.integration
class TestDashboardFailClosed:
    """Real-app gate behaviour across the four spec'd scenarios."""

    # --- Case 1: exposed + dashboard on + token "" + default flag => gated ---

    def test_exposed_unauthenticated_does_not_mount_dashboard_or_api(self):
        app = _build_app(_PUBLIC_WEBHOOK)
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT not in paths
        assert _DASHBOARD_API not in paths
        assert _CALLDATA_API not in paths

    def test_exposed_unauthenticated_still_mounts_carrier_webhook_and_health(self):
        # Calls MUST keep working: webhook + media + health always mount.
        app = _build_app(_PUBLIC_WEBHOOK)
        paths = _route_paths(app)
        assert _WEBHOOK_ROUTE in paths
        assert _HEALTH_ROUTE in paths

    def test_exposed_unauthenticated_dashboard_and_api_return_404_over_http(self):
        from starlette.testclient import TestClient

        client = TestClient(_build_app(_PUBLIC_WEBHOOK))
        assert client.get(_DASHBOARD_ROOT).status_code == 404
        assert client.get(_DASHBOARD_API).status_code == 404
        assert client.get(_CALLDATA_API).status_code == 404
        # Health still answers so liveness probes / calls are unaffected.
        assert client.get(_HEALTH_ROUTE).status_code == 200

    # --- Case 2: exposed + dashboard on + token SET => mounted, 401 ---

    def test_exposed_with_token_mounts_dashboard_and_api(self):
        app = _build_app(_PUBLIC_WEBHOOK, dashboard_token="s3cret")
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT in paths
        assert _DASHBOARD_API in paths
        assert _CALLDATA_API in paths

    def test_exposed_with_token_unauthenticated_request_is_401(self):
        from starlette.testclient import TestClient

        client = TestClient(_build_app(_PUBLIC_WEBHOOK, dashboard_token="s3cret"))
        assert client.get(_DASHBOARD_ROOT).status_code == 401
        assert client.get(_CALLDATA_API).status_code == 401

    def test_exposed_with_token_authorized_request_succeeds(self):
        from starlette.testclient import TestClient

        client = TestClient(_build_app(_PUBLIC_WEBHOOK, dashboard_token="s3cret"))
        ok = client.get(_CALLDATA_API, headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200

    # --- Case 3: loopback-only + dashboard on + token "" => mounted ---

    def test_loopback_only_unauthenticated_mounts_dashboard_and_api(self):
        # Local-dev path: backward compatible, still served unauthenticated.
        app = _build_app(_LOOPBACK_WEBHOOK)
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT in paths
        assert _DASHBOARD_API in paths
        assert _CALLDATA_API in paths

    def test_loopback_only_dashboard_and_api_reachable_over_http(self):
        from starlette.testclient import TestClient

        client = TestClient(_build_app(_LOOPBACK_WEBHOOK))
        assert client.get(_DASHBOARD_ROOT).status_code == 200
        assert client.get(_CALLDATA_API).status_code == 200

    def test_empty_webhook_url_local_dev_mounts_dashboard_and_api(self):
        # No tunnel, no webhook_url at all (pure local dev) — still served.
        app = _build_app("")
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT in paths
        assert _CALLDATA_API in paths

    # --- Case 4: exposed + token "" + allow_insecure_dashboard=True => mounted ---

    def test_exposed_with_escape_hatch_mounts_dashboard_and_api(self):
        app = _build_app(_PUBLIC_WEBHOOK, allow_insecure_dashboard=True)
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT in paths
        assert _DASHBOARD_API in paths
        assert _CALLDATA_API in paths

    def test_exposed_with_escape_hatch_dashboard_reachable_over_http(self):
        from starlette.testclient import TestClient

        client = TestClient(_build_app(_PUBLIC_WEBHOOK, allow_insecure_dashboard=True))
        assert client.get(_DASHBOARD_ROOT).status_code == 200
        assert client.get(_CALLDATA_API).status_code == 200

    # --- dashboard=False disables everything regardless of exposure ---

    def test_dashboard_disabled_mounts_neither_but_keeps_webhook(self):
        app = _build_app(_PUBLIC_WEBHOOK, dashboard=False)
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT not in paths
        assert _CALLDATA_API not in paths
        assert _WEBHOOK_ROUTE in paths
        assert _HEALTH_ROUTE in paths


@pytest.mark.skipif(not _has_fastapi, reason="fastapi not installed")
@pytest.mark.integration
class TestDashboardExposureSignalBindHost:
    """Signal (c): explicit non-loopback PATTER_BIND_HOST triggers exposure."""

    def test_explicit_nonloopback_bind_host_gates_unauthenticated_dashboard(
        self, monkeypatch
    ):
        # Even with a loopback webhook_url, an explicit 0.0.0.0 bind exposes
        # the port; the gate must fire.
        monkeypatch.setenv("PATTER_BIND_HOST", "0.0.0.0")
        app = _build_app(_LOOPBACK_WEBHOOK)
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT not in paths
        assert _CALLDATA_API not in paths
        assert _WEBHOOK_ROUTE in paths

    def test_explicit_loopback_bind_host_does_not_gate(self, monkeypatch):
        # Explicitly setting the loopback default must NOT trip exposure.
        monkeypatch.setenv("PATTER_BIND_HOST", "127.0.0.1")
        app = _build_app(_LOOPBACK_WEBHOOK)
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT in paths
        assert _CALLDATA_API in paths


@pytest.mark.unit
class TestAllowInsecureDashboardConfigThreading:
    """The opt-in flag exists with a safe default and threads to the server.

    ``allow_insecure_dashboard`` lives on ``Patter.serve()`` — alongside
    ``dashboard`` and ``dashboard_token`` — mirroring the TypeScript SDK where
    ``allowInsecureDashboard`` is a ``ServeOptions`` field passed to ``serve()``.
    """

    def test_serve_has_safe_default(self):
        import inspect

        from getpatter.client import Patter

        sig = inspect.signature(Patter.serve)
        assert "allow_insecure_dashboard" in sig.parameters
        assert sig.parameters["allow_insecure_dashboard"].default is False

    def test_serve_sits_next_to_other_dashboard_params(self):
        # Parity: the flag travels the same path as dashboard / dashboard_token.
        import inspect

        from getpatter.client import Patter

        sig = inspect.signature(Patter.serve)
        for name in ("dashboard", "dashboard_token", "allow_insecure_dashboard"):
            assert name in sig.parameters

    def test_embedded_server_has_safe_default(self):
        import inspect

        from getpatter.server import EmbeddedServer

        sig = inspect.signature(EmbeddedServer.__init__)
        assert "allow_insecure_dashboard" in sig.parameters
        assert sig.parameters["allow_insecure_dashboard"].default is False

    @staticmethod
    def _serve_phone_and_agent():
        from getpatter import OpenAIRealtime
        from getpatter.carriers.twilio import Carrier as Twilio
        from getpatter.client import Patter

        phone = Patter(
            carrier=Twilio(account_sid="AC" + "a" * 32, auth_token="tok_test"),
            phone_number="+15550001234",
            webhook_url=_PUBLIC_WEBHOOK,
        )
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-test"), system_prompt="hi"
        )
        return phone, agent

    async def test_serve_threads_flag_to_embedded_server(self):
        # serve(allow_insecure_dashboard=True) must reach EmbeddedServer with
        # the same value (the path dashboard / dashboard_token already travel).
        from unittest.mock import AsyncMock, MagicMock, patch

        phone, agent = self._serve_phone_and_agent()

        mock_server = MagicMock()
        mock_server.start = AsyncMock()
        with patch(
            "getpatter.server.EmbeddedServer", return_value=mock_server
        ) as MockServer:
            await phone.serve(agent, port=9123, allow_insecure_dashboard=True)
        _, kwargs = MockServer.call_args
        assert kwargs["allow_insecure_dashboard"] is True

    async def test_serve_default_threads_false_to_embedded_server(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        phone, agent = self._serve_phone_and_agent()

        mock_server = MagicMock()
        mock_server.start = AsyncMock()
        with patch(
            "getpatter.server.EmbeddedServer", return_value=mock_server
        ) as MockServer:
            await phone.serve(agent, port=9124)
        _, kwargs = MockServer.call_args
        assert kwargs["allow_insecure_dashboard"] is False

    def test_embedded_server_stores_flag_on_instance(self):
        from getpatter.server import EmbeddedServer

        server = EmbeddedServer(
            config=_config(_PUBLIC_WEBHOOK),
            agent=_agent(),
            allow_insecure_dashboard=True,
        )
        assert server.allow_insecure_dashboard is True
