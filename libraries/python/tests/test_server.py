"""Tests for EmbeddedServer (local mode)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from getpatter.server import EmbeddedServer
from getpatter.local_config import LocalConfig
from getpatter.models import Agent


def make_config(**kwargs) -> LocalConfig:
    defaults = dict(
        telephony_provider="twilio",
        twilio_sid="AC_test",
        twilio_token="tok_test",
        openai_key="sk-test",
        phone_number="+15550000000",
        webhook_url="abc.ngrok.io",
    )
    defaults.update(kwargs)
    return LocalConfig(**defaults)


def make_agent(**kwargs) -> Agent:
    defaults = dict(system_prompt="You are helpful.")
    defaults.update(kwargs)
    return Agent(**defaults)


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_embedded_server_init():
    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    assert server.config.twilio_sid == "AC_test"
    assert server.agent.system_prompt == "You are helpful."
    assert server.on_call_start is None
    assert server.on_call_end is None
    assert server.on_transcript is None


def test_embedded_server_stores_agent():
    config = make_config()
    agent = make_agent(system_prompt="custom prompt", voice="nova")
    server = EmbeddedServer(config=config, agent=agent)
    assert server.agent.voice == "nova"
    assert server.agent.system_prompt == "custom prompt"


def test_embedded_server_callbacks_settable():
    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    cb = AsyncMock()
    server.on_call_start = cb
    server.on_call_end = cb
    server.on_transcript = cb
    assert server.on_call_start is cb


# ---------------------------------------------------------------------------
# _create_app
# ---------------------------------------------------------------------------


def test_embedded_server_creates_app():
    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    app = server._create_app()
    assert app is not None


def test_create_app_returns_fastapi():
    from fastapi import FastAPI

    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    app = server._create_app()
    assert isinstance(app, FastAPI)


def test_create_app_has_health_route():
    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    app = server._create_app()
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/health" in routes


def test_create_app_has_twilio_webhook_route():
    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    app = server._create_app()
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/webhooks/twilio/voice" in routes


def test_create_app_has_telnyx_webhook_route():
    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    app = server._create_app()
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/webhooks/telnyx/voice" in routes


def test_create_app_has_websocket_route():
    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    app = server._create_app()
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/ws/stream/{call_id}" in routes


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_does_nothing_when_no_server():
    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    # Should not raise
    await server.stop()


@pytest.mark.asyncio
async def test_stop_sets_should_exit():
    config = make_config()
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    mock_srv = MagicMock()
    server._server = mock_srv
    await server.stop()
    assert mock_srv.should_exit is True


# ---------------------------------------------------------------------------
# Telnyx config
# ---------------------------------------------------------------------------


def test_embedded_server_telnyx_config():
    config = make_config(
        telephony_provider="telnyx",
        telnyx_key="KEY_test",
        telnyx_connection_id="conn_123",
    )
    agent = make_agent()
    server = EmbeddedServer(config=config, agent=agent)
    assert server.config.telephony_provider == "telnyx"
    assert server.config.telnyx_connection_id == "conn_123"
