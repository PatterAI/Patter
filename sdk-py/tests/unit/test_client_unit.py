"""Unit tests for getpatter.client — the main Patter SDK client (local mode)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter import (
    OpenAIRealtime,
    Telnyx,
    Tool,
    Twilio,
    guardrail,
    tool,
)
from getpatter.client import Patter
from getpatter.exceptions import PatterConnectionError
from getpatter.models import Agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_phone(**kwargs) -> Patter:
    """Build a default local-mode Patter instance for tests."""
    defaults = dict(
        carrier=Twilio(account_sid="ACtest000000000000000000000000000", auth_token="tok"),
        phone_number="+15550001234",
        webhook_url="abc.ngrok.io",
    )
    defaults.update(kwargs)
    return Patter(**defaults)


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPatterInit:
    """Patter() constructor — validation and cloud-mode rejection."""

    def test_api_key_raises_not_implemented(self) -> None:
        """Cloud mode is not yet implemented — api_key= must raise."""
        with pytest.raises(NotImplementedError, match="Patter Cloud is not yet available"):
            Patter(api_key="pt_xxx")

    def test_backend_url_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="Patter Cloud is not yet available"):
            Patter(backend_url="wss://custom.host")

    def test_rest_url_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="Patter Cloud is not yet available"):
            Patter(rest_url="https://custom.host")

    def test_unknown_mode_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="Patter Cloud is not yet available"):
            Patter(mode="cloud")

    def test_unexpected_kwarg_raises_typeerror(self) -> None:
        with pytest.raises(TypeError, match="unexpected keyword"):
            Patter(carrier=Twilio(account_sid="AC1", auth_token="t"), phone_number="+1", bogus="x")

    def test_local_mode_explicit(self) -> None:
        client = Patter(
            mode="local",
            carrier=Twilio(
                account_sid="ACtest000000000000000000000000000",
                auth_token="tok",
            ),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        assert client._local_config is not None

    def test_local_mode_auto_detected_twilio(self) -> None:
        """Twilio carrier auto-detects local mode."""
        client = _local_phone()
        assert client._local_config.telephony_provider == "twilio"

    def test_local_mode_auto_detected_telnyx(self) -> None:
        """Telnyx carrier auto-detects local mode."""
        client = Patter(
            carrier=Telnyx(api_key="KEY_test", connection_id="200"),
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        assert client._local_config.telephony_provider == "telnyx"

    def test_local_mode_requires_phone_number(self) -> None:
        with pytest.raises(ValueError, match="phone_number"):
            Patter(
                carrier=Twilio(
                    account_sid="ACtest000000000000000000000000000",
                    auth_token="tok",
                ),
                webhook_url="abc.ngrok.io",
            )

    def test_local_mode_accepts_missing_webhook_url(self) -> None:
        phone = Patter(
            carrier=Twilio(
                account_sid="ACtest000000000000000000000000000",
                auth_token="tok",
            ),
            phone_number="+15550001234",
        )
        assert phone is not None


# ---------------------------------------------------------------------------
# call() — local mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCall:
    """Patter.call() — outbound calling in local mode."""

    async def test_call_requires_agent(self) -> None:
        client = _local_phone()
        with pytest.raises(PatterConnectionError, match="agent parameter"):
            await client.call(to="+15550009999")

    async def test_call_validates_e164(self) -> None:
        client = _local_phone()
        agent = Agent(system_prompt="Test")
        with pytest.raises(ValueError, match="E.164"):
            await client.call(to="notanumber", agent=agent)

    @patch("getpatter.client.TwilioAdapter", create=True)
    async def test_call_local_twilio(self, mock_adapter_cls) -> None:
        """Local twilio call initiates via TwilioAdapter."""
        client = _local_phone()
        agent = Agent(system_prompt="Test")
        mock_adapter = AsyncMock()
        mock_adapter.initiate_call = AsyncMock(return_value="CA_call_id")

        with patch(
            "getpatter.providers.twilio_adapter.TwilioAdapter",
            return_value=mock_adapter,
        ):
            await client.call(to="+15550009999", agent=agent)
            mock_adapter.initiate_call.assert_awaited_once()


# ---------------------------------------------------------------------------
# agent() — local mode agent creation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAgentFactory:
    """Patter.agent() — agent configuration."""

    def test_agent_basic(self) -> None:
        client = _local_phone()
        agent = client.agent(
            engine=OpenAIRealtime(api_key="sk-test"),
            system_prompt="You are helpful.",
        )
        assert isinstance(agent, Agent)
        assert agent.system_prompt == "You are helpful."
        assert agent.voice == "alloy"
        assert agent.provider == "openai_realtime"

    def test_agent_openai_realtime_requires_key(self) -> None:
        client = _local_phone()
        # No engine and no openai key anywhere → should raise.
        with pytest.raises(ValueError, match="OpenAI"):
            client.agent(system_prompt="Test")

    def test_agent_tools_validation(self) -> None:
        client = _local_phone()
        with pytest.raises(TypeError, match="tools must be a list"):
            client.agent(
                engine=OpenAIRealtime(api_key="sk-test"),
                system_prompt="Test",
                tools="bad",
            )

    def test_agent_tool_rejects_dict(self) -> None:
        client = _local_phone()
        with pytest.raises(TypeError, match="Tool instance"):
            client.agent(
                engine=OpenAIRealtime(api_key="sk-test"),
                system_prompt="Test",
                tools=[{"name": "x"}],
            )

    def test_agent_variables_must_be_dict(self) -> None:
        client = _local_phone()
        with pytest.raises(TypeError, match="variables must be a dict"):
            client.agent(
                engine=OpenAIRealtime(api_key="sk-test"),
                system_prompt="Test",
                variables="bad",
            )

    def test_agent_guardrails_must_be_list(self) -> None:
        client = _local_phone()
        with pytest.raises(TypeError, match="guardrails must be a list"):
            client.agent(
                engine=OpenAIRealtime(api_key="sk-test"),
                system_prompt="Test",
                guardrails="bad",
            )


# ---------------------------------------------------------------------------
# serve() — embedded server validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServe:
    """Patter.serve() — server validation."""

    async def test_serve_rejects_non_agent(self) -> None:
        client = _local_phone()
        with pytest.raises(TypeError, match="Agent instance"):
            await client.serve("not an agent")

    async def test_serve_rejects_invalid_port(self) -> None:
        client = _local_phone()
        agent = Agent(system_prompt="Test")
        with pytest.raises(ValueError, match="port"):
            await client.serve(agent, port=0)

    async def test_serve_rejects_bool_port(self) -> None:
        client = _local_phone()
        agent = Agent(system_prompt="Test")
        with pytest.raises(ValueError, match="port"):
            await client.serve(agent, port=True)


# ---------------------------------------------------------------------------
# test() — terminal test session
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTestMode:
    """Patter.test() — terminal test session validation."""

    async def test_test_rejects_non_agent(self) -> None:
        client = _local_phone()
        with pytest.raises(TypeError, match="Agent instance"):
            await client.test("not an agent")


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDisconnect:
    """Patter.disconnect() — resource cleanup."""

    async def test_disconnect_no_server(self) -> None:
        client = _local_phone()
        await client.disconnect()  # should not raise

    async def test_disconnect_with_server(self) -> None:
        client = _local_phone()
        mock_server = AsyncMock()
        mock_server.stop = AsyncMock()
        client._server = mock_server

        await client.disconnect()
        mock_server.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# Module-level factories (guardrail, tool)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFactories:
    """Module-level ``guardrail()`` and ``tool()`` factories."""

    def test_guardrail_factory(self) -> None:
        g = guardrail(
            name="No medical",
            blocked_terms=["diagnosis"],
            replacement="See a doctor.",
        )
        assert g.name == "No medical"
        assert g.blocked_terms == ["diagnosis"]
        assert g.replacement == "See a doctor."

    def test_tool_factory_with_handler(self) -> None:
        def handler(args: dict, ctx: dict) -> str:
            return "ok"

        t = tool(name="my_tool", handler=handler)
        assert isinstance(t, Tool)
        assert t.name == "my_tool"
        assert t.handler is handler

    def test_tool_factory_with_webhook(self) -> None:
        t = tool(name="my_tool", webhook_url="https://example.com/hook")
        assert t.webhook_url == "https://example.com/hook"

    def test_tool_factory_requires_handler_or_webhook(self) -> None:
        with pytest.raises(ValueError, match="handler.*webhook_url|webhook_url.*handler"):
            tool(name="my_tool")
