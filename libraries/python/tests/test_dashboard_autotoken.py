"""Auto-token dashboard protection — authentic real-app route tests.

These tests build the REAL FastAPI application via ``EmbeddedServer._create_app``
and drive it through the REAL Starlette ``TestClient`` (real ASGI routing, real
route mounting, real auth dependency). Nothing under test is mocked — replacing
the auto-token branch with a no-op (leaving ``effective_token=""``) makes the
exposed-default case answer 200 unauthenticated, which flips the protection
assertion to a failure. That is the litmus per ``.claude/rules/authentic-tests.md``.

Behaviour: the dashboard + call-data API (call transcripts + metadata = PII) are
ALWAYS mounted. When the server is reachable beyond loopback (a tunnel / public
``webhook_url`` / explicit non-loopback ``PATTER_BIND_HOST``) without a configured
``dashboard_token``, the SDK auto-generates a one-time token so the dashboard is
available but protected with zero config. ``allow_insecure_dashboard=True`` is the
explicit opt-out that serves it fully open. Loopback-only local dev stays open.
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


def _make_server(
    webhook_url: str,
    *,
    dashboard: bool = True,
    dashboard_token: str = "",
    allow_insecure_dashboard: bool = False,
):
    """Construct a real EmbeddedServer (not yet built into an app)."""
    from getpatter.server import EmbeddedServer

    return EmbeddedServer(
        config=_config(webhook_url),
        agent=_agent(),
        dashboard=dashboard,
        dashboard_token=dashboard_token,
        allow_insecure_dashboard=allow_insecure_dashboard,
    )


def _route_paths(app) -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "path")}


@pytest.fixture(autouse=True)
def _clear_bind_host(monkeypatch):
    """Isolate signal (c): ensure no inherited PATTER_BIND_HOST leaks in."""
    monkeypatch.delenv("PATTER_BIND_HOST", raising=False)


@pytest.mark.skipif(not _has_fastapi, reason="fastapi not installed")
@pytest.mark.integration
class TestDashboardAutoToken:
    """Auto-token protection behaviour across the four spec'd scenarios."""

    # --- Case 1: exposed + dashboard on + token "" + default flag ---
    #
    # The dashboard + call-data API ARE mounted, but protected by an
    # auto-generated token: unauthenticated => 401, with the token => 200.

    def test_exposed_default_mounts_dashboard_and_api(self):
        server = _make_server(_PUBLIC_WEBHOOK)
        app = server._create_app()
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT in paths
        assert _DASHBOARD_API in paths
        assert _CALLDATA_API in paths

    def test_exposed_default_still_mounts_carrier_webhook_and_health(self):
        # Calls MUST keep working: webhook + media + health always mount.
        server = _make_server(_PUBLIC_WEBHOOK)
        app = server._create_app()
        paths = _route_paths(app)
        assert _WEBHOOK_ROUTE in paths
        assert _HEALTH_ROUTE in paths

    def test_exposed_default_resolves_nonempty_uuid_token(self):
        import uuid

        server = _make_server(_PUBLIC_WEBHOOK)
        server._create_app()
        token = server.effective_dashboard_token
        assert token  # non-empty
        # RFC 4122 v4 UUID with dashes — byte-for-byte the same shape the
        # TypeScript SDK's crypto.randomUUID() emits (parity, blocking #1).
        # 36 chars, version nibble 4; parse to prove it's a real UUID.
        assert len(token) == 36
        parsed = uuid.UUID(token)  # raises ValueError if not a valid UUID
        assert parsed.version == 4
        assert str(parsed) == token

    def test_exposed_default_unauthenticated_request_is_401(self):
        from starlette.testclient import TestClient

        client = TestClient(_make_server(_PUBLIC_WEBHOOK)._create_app())
        # LITMUS: with no auto-token these would be 200 — protection proof.
        assert client.get(_DASHBOARD_ROOT).status_code == 401
        assert client.get(_CALLDATA_API).status_code == 401
        # Health still answers so liveness probes / calls are unaffected.
        assert client.get(_HEALTH_ROUTE).status_code == 200

    def test_exposed_default_authorized_with_query_token_succeeds(self):
        from starlette.testclient import TestClient

        server = _make_server(_PUBLIC_WEBHOOK)
        client = TestClient(server._create_app())
        token = server.effective_dashboard_token
        assert client.get(f"{_DASHBOARD_ROOT}?token={token}").status_code == 200
        assert client.get(f"{_CALLDATA_API}?token={token}").status_code == 200

    def test_exposed_default_authorized_with_bearer_header_succeeds(self):
        from starlette.testclient import TestClient

        server = _make_server(_PUBLIC_WEBHOOK)
        client = TestClient(server._create_app())
        token = server.effective_dashboard_token
        ok = client.get(_CALLDATA_API, headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200

    # --- Case 2: exposed + explicit dashboard_token => mounted, 401/200 ---

    def test_exposed_with_explicit_token_uses_it(self):
        server = _make_server(_PUBLIC_WEBHOOK, dashboard_token="secret")
        server._create_app()
        assert server.effective_dashboard_token == "secret"

    def test_exposed_with_explicit_token_unauthenticated_is_401(self):
        from starlette.testclient import TestClient

        client = TestClient(
            _make_server(_PUBLIC_WEBHOOK, dashboard_token="secret")._create_app()
        )
        assert client.get(_DASHBOARD_ROOT).status_code == 401
        assert client.get(_CALLDATA_API).status_code == 401

    def test_exposed_with_explicit_token_authorized_is_200(self):
        from starlette.testclient import TestClient

        client = TestClient(
            _make_server(_PUBLIC_WEBHOOK, dashboard_token="secret")._create_app()
        )
        ok = client.get(_CALLDATA_API, headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200

    # --- Case 3: loopback-only + no token => mounted and OPEN (local dev) ---

    def test_loopback_only_mounts_dashboard_and_api(self):
        server = _make_server(_LOOPBACK_WEBHOOK)
        app = server._create_app()
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT in paths
        assert _DASHBOARD_API in paths
        assert _CALLDATA_API in paths

    def test_loopback_only_is_open_no_autotoken(self):
        # Zero-friction local dev: no token generated, served open.
        server = _make_server(_LOOPBACK_WEBHOOK)
        server._create_app()
        assert server.effective_dashboard_token == ""

    def test_loopback_only_reachable_unauthenticated_over_http(self):
        from starlette.testclient import TestClient

        client = TestClient(_make_server(_LOOPBACK_WEBHOOK)._create_app())
        assert client.get(_DASHBOARD_ROOT).status_code == 200
        assert client.get(_CALLDATA_API).status_code == 200

    def test_empty_webhook_url_local_dev_is_open(self):
        # No tunnel, no webhook_url at all (pure local dev) — open.
        from starlette.testclient import TestClient

        server = _make_server("")
        client = TestClient(server._create_app())
        assert server.effective_dashboard_token == ""
        assert client.get(_DASHBOARD_ROOT).status_code == 200
        assert client.get(_CALLDATA_API).status_code == 200

    # --- Case 4: exposed + no token + allow_insecure_dashboard=True => OPEN ---

    def test_exposed_insecure_optout_is_open(self):
        server = _make_server(_PUBLIC_WEBHOOK, allow_insecure_dashboard=True)
        server._create_app()
        assert server.effective_dashboard_token == ""

    def test_exposed_insecure_optout_reachable_unauthenticated_over_http(self):
        from starlette.testclient import TestClient

        client = TestClient(
            _make_server(_PUBLIC_WEBHOOK, allow_insecure_dashboard=True)._create_app()
        )
        assert client.get(_DASHBOARD_ROOT).status_code == 200
        assert client.get(_CALLDATA_API).status_code == 200

    def test_exposed_insecure_optout_logs_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="getpatter"):
            _make_server(_PUBLIC_WEBHOOK, allow_insecure_dashboard=True)._create_app()
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "WITHOUT authentication" in joined
        assert "allow_insecure_dashboard=True" in joined

    def test_exposed_default_logs_autotoken_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="getpatter"):
            _make_server(_PUBLIC_WEBHOOK)._create_app()
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "auto-generated" in joined

    # --- dashboard=False disables everything regardless of exposure ---

    def test_dashboard_disabled_mounts_neither_but_keeps_webhook(self):
        app = _make_server(_PUBLIC_WEBHOOK, dashboard=False)._create_app()
        paths = _route_paths(app)
        assert _DASHBOARD_ROOT not in paths
        assert _CALLDATA_API not in paths
        assert _WEBHOOK_ROUTE in paths
        assert _HEALTH_ROUTE in paths


@pytest.mark.skipif(not _has_fastapi, reason="fastapi not installed")
@pytest.mark.integration
class TestDashboardExposureSignalBindHost:
    """Signal (c): explicit non-loopback PATTER_BIND_HOST triggers exposure."""

    def test_explicit_nonloopback_bind_host_protects_dashboard(self, monkeypatch):
        # Even with a loopback webhook_url, an explicit 0.0.0.0 bind exposes
        # the port; the dashboard mounts but is auto-token protected (401).
        from starlette.testclient import TestClient

        monkeypatch.setenv("PATTER_BIND_HOST", "0.0.0.0")
        server = _make_server(_LOOPBACK_WEBHOOK)
        client = TestClient(server._create_app())
        assert server.effective_dashboard_token  # auto-generated
        assert client.get(_DASHBOARD_ROOT).status_code == 401
        assert _WEBHOOK_ROUTE in _route_paths(server._create_app())

    def test_explicit_loopback_bind_host_stays_open(self, monkeypatch):
        # Explicitly setting the loopback default must NOT trip exposure.
        from starlette.testclient import TestClient

        monkeypatch.setenv("PATTER_BIND_HOST", "127.0.0.1")
        server = _make_server(_LOOPBACK_WEBHOOK)
        client = TestClient(server._create_app())
        assert server.effective_dashboard_token == ""
        assert client.get(_DASHBOARD_ROOT).status_code == 200
        assert client.get(_CALLDATA_API).status_code == 200


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
