"""Unit tests for patter.client — the main Patter SDK client."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from patter.client import Patter, DEFAULT_BACKEND_URL, DEFAULT_REST_URL
from patter.exceptions import PatterConnectionError, ProvisionError
from patter.models import Agent, IncomingMessage


# ---------------------------------------------------------------------------
# Construction / mode detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPatterInit:
    """Patter() constructor — mode detection and validation."""

    def test_cloud_mode_default(self) -> None:
        client = Patter(api_key="pt_test")
        assert client._mode == "cloud"
        assert client.api_key == "pt_test"

    def test_cloud_mode_custom_urls(self) -> None:
        client = Patter(
            api_key="pt_test",
            backend_url="wss://custom.host",
            rest_url="https://custom.host",
        )
        assert client._backend_url == "wss://custom.host"
        assert client._rest_url == "https://custom.host"

    def test_local_mode_explicit(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        assert client._mode == "local"

    def test_local_mode_auto_detected_twilio(self) -> None:
        """twilio_sid without api_key auto-detects local mode."""
        client = Patter(
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        assert client._mode == "local"

    def test_local_mode_auto_detected_telnyx(self) -> None:
        """telnyx_key without api_key auto-detects local mode."""
        client = Patter(
            telnyx_key="KEY_test",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        assert client._mode == "local"

    def test_local_mode_requires_phone_number(self) -> None:
        with pytest.raises(ValueError, match="phone_number"):
            Patter(
                twilio_sid="ACtest000000000000000000000000000",
                twilio_token="tok",
                webhook_url="abc.ngrok.io",
            )

    def test_local_mode_requires_webhook_url(self) -> None:
        with pytest.raises(ValueError, match="webhook_url"):
            Patter(
                twilio_sid="ACtest000000000000000000000000000",
                twilio_token="tok",
                phone_number="+15550001234",
            )

    def test_local_mode_requires_twilio_token(self) -> None:
        with pytest.raises(ValueError, match="twilio_token"):
            Patter(
                twilio_sid="ACtest000000000000000000000000000",
                phone_number="+15550001234",
                webhook_url="abc.ngrok.io",
            )

    def test_api_key_property_cloud(self) -> None:
        client = Patter(api_key="pt_abc")
        assert client.api_key == "pt_abc"

    def test_api_key_property_local(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        assert client.api_key == ""


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConnect:
    """Patter.connect() — cloud mode WebSocket connection."""

    async def test_connect_calls_connection(self) -> None:
        client = Patter(api_key="pt_test")
        handler = AsyncMock(return_value="reply")
        client._connection = AsyncMock()
        client._connection.connect = AsyncMock()

        await client.connect(on_message=handler)
        client._connection.connect.assert_awaited_once()

    async def test_connect_raises_in_local_mode(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(PatterConnectionError, match="local mode"):
            await client.connect(on_message=AsyncMock())

    async def test_connect_with_provider_registers_number(self) -> None:
        client = Patter(api_key="pt_test")
        client._connection = AsyncMock()
        client._connection.connect = AsyncMock()
        client._http = AsyncMock()
        response = MagicMock()
        response.status_code = 201
        response.json.return_value = {}
        client._http.post = AsyncMock(return_value=response)

        await client.connect(
            on_message=AsyncMock(),
            provider="twilio",
            provider_key="AC_key",
            number="+15550001234",
        )
        client._http.post.assert_awaited_once()
        client._connection.connect.assert_awaited_once()


# ---------------------------------------------------------------------------
# call()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCall:
    """Patter.call() — outbound calling in cloud and local modes."""

    async def test_call_cloud_mode_connected(self) -> None:
        client = Patter(api_key="pt_test")
        client._connection = AsyncMock()
        client._connection.is_connected = True
        client._connection.request_call = AsyncMock()

        await client.call(to="+39123456789", first_message="Ciao!")
        client._connection.request_call.assert_awaited_once_with(
            from_number="",
            to_number="+39123456789",
            first_message="Ciao!",
        )

    async def test_call_cloud_mode_not_connected_with_handler(self) -> None:
        client = Patter(api_key="pt_test")
        client._connection = AsyncMock()
        client._connection.is_connected = False
        client._connection.connect = AsyncMock()
        client._connection.request_call = AsyncMock()
        handler = AsyncMock(return_value="reply")

        await client.call(to="+39123456789", on_message=handler)
        client._connection.connect.assert_awaited_once()

    async def test_call_cloud_mode_not_connected_no_handler_raises(self) -> None:
        client = Patter(api_key="pt_test")
        client._connection = AsyncMock()
        client._connection.is_connected = False

        with pytest.raises(PatterConnectionError, match="Not connected"):
            await client.call(to="+39123456789")

    async def test_call_local_mode_requires_agent(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(PatterConnectionError, match="agent parameter"):
            await client.call(to="+15550009999")

    async def test_call_local_mode_validates_e164(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        agent = Agent(system_prompt="Test")
        with pytest.raises(ValueError, match="E.164"):
            await client.call(to="notanumber", agent=agent)

    @patch("patter.client.TwilioAdapter", create=True)
    async def test_call_local_twilio(self, mock_adapter_cls) -> None:
        """Local twilio call initiates via TwilioAdapter."""
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        agent = Agent(system_prompt="Test")
        mock_adapter = AsyncMock()
        mock_adapter.initiate_call = AsyncMock(return_value="CA_call_id")

        with patch(
            "patter.providers.twilio_adapter.TwilioAdapter",
            return_value=mock_adapter,
        ):
            await client.call(to="+15550009999", agent=agent)
            mock_adapter.initiate_call.assert_awaited_once()


# ---------------------------------------------------------------------------
# agent() — local mode agent creation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAgentFactory:
    """Patter.agent() — agent configuration for local mode."""

    def test_agent_basic(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        agent = client.agent(system_prompt="You are helpful.")
        assert isinstance(agent, Agent)
        assert agent.system_prompt == "You are helpful."
        assert agent.voice == "alloy"
        assert agent.provider == "openai_realtime"

    def test_agent_invalid_provider(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(ValueError, match="provider must be one of"):
            client.agent(system_prompt="Test", provider="invalid")

    def test_agent_openai_realtime_requires_key(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(ValueError, match="openai_key"):
            client.agent(system_prompt="Test", provider="openai_realtime")

    def test_agent_tools_validation(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(TypeError, match="tools must be a list"):
            client.agent(system_prompt="Test", tools="bad")

    def test_agent_tool_missing_name(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(ValueError, match="missing required 'name'"):
            client.agent(system_prompt="Test", tools=[{"description": "x"}])

    def test_agent_tool_requires_handler_or_webhook(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(ValueError, match="webhook_url.*handler"):
            client.agent(system_prompt="Test", tools=[{"name": "t"}])

    def test_agent_variables_must_be_dict(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(TypeError, match="variables must be a dict"):
            client.agent(system_prompt="Test", variables="bad")

    def test_agent_guardrails_must_be_list(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            openai_key="sk-x",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(TypeError, match="guardrails must be a list"):
            client.agent(system_prompt="Test", guardrails="bad")


# ---------------------------------------------------------------------------
# serve() — local mode embedded server
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServe:
    """Patter.serve() — local mode server validation."""

    async def test_serve_raises_in_cloud_mode(self) -> None:
        client = Patter(api_key="pt_test")
        agent = Agent(system_prompt="Test")
        with pytest.raises(PatterConnectionError, match="local mode"):
            await client.serve(agent)

    async def test_serve_rejects_non_agent(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(TypeError, match="Agent instance"):
            await client.serve("not an agent")

    async def test_serve_rejects_invalid_port(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        agent = Agent(system_prompt="Test")
        with pytest.raises(ValueError, match="port"):
            await client.serve(agent, port=0)

    async def test_serve_rejects_bool_port(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        agent = Agent(system_prompt="Test")
        with pytest.raises(ValueError, match="port"):
            await client.serve(agent, port=True)


# ---------------------------------------------------------------------------
# test() — local mode terminal test session
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTestMode:
    """Patter.test() — terminal test session validation."""

    async def test_test_raises_in_cloud_mode(self) -> None:
        client = Patter(api_key="pt_test")
        agent = Agent(system_prompt="Test")
        with pytest.raises(PatterConnectionError, match="local mode"):
            await client.test(agent)

    async def test_test_rejects_non_agent(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(TypeError, match="Agent instance"):
            await client.test("not an agent")


# ---------------------------------------------------------------------------
# Cloud REST API methods
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCloudAPIMethods:
    """Cloud-mode REST API methods — agent, number, and call management."""

    def _cloud_client(self) -> Patter:
        client = Patter(api_key="pt_test")
        client._http = AsyncMock()
        return client

    # -- create_agent --

    async def test_create_agent_success(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=201)
        resp.json.return_value = {"id": "agent_1"}
        client._http.post = AsyncMock(return_value=resp)

        result = await client.create_agent(name="My Agent", system_prompt="Help")
        assert result == {"id": "agent_1"}

    async def test_create_agent_failure(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=400, text="Bad request")
        client._http.post = AsyncMock(return_value=resp)

        with pytest.raises(ProvisionError, match="Failed to create agent"):
            await client.create_agent(name="Bad", system_prompt="x")

    async def test_create_agent_local_mode_raises(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        with pytest.raises(PatterConnectionError, match="cloud mode"):
            await client.create_agent(name="x", system_prompt="x")

    # -- list_agents --

    async def test_list_agents(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"id": "a1"}, {"id": "a2"}]
        client._http.get = AsyncMock(return_value=resp)

        result = await client.list_agents()
        assert len(result) == 2

    # -- update_agent --

    async def test_update_agent_success(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": "a1", "name": "Updated"}
        client._http.patch = AsyncMock(return_value=resp)

        result = await client.update_agent("a1", name="Updated")
        assert result["name"] == "Updated"

    async def test_update_agent_failure(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=404, text="Not found")
        client._http.patch = AsyncMock(return_value=resp)

        with pytest.raises(ProvisionError, match="Failed to update"):
            await client.update_agent("bad_id")

    # -- delete_agent --

    async def test_delete_agent_success(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=204)
        client._http.delete = AsyncMock(return_value=resp)

        await client.delete_agent("a1")  # should not raise

    async def test_delete_agent_failure(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=404, text="Not found")
        client._http.delete = AsyncMock(return_value=resp)

        with pytest.raises(ProvisionError, match="Failed to delete"):
            await client.delete_agent("bad_id")

    # -- buy_number --

    async def test_buy_number_success(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=201)
        resp.json.return_value = {"number": "+15551112222"}
        client._http.post = AsyncMock(return_value=resp)

        result = await client.buy_number()
        assert result["number"] == "+15551112222"

    async def test_buy_number_failure(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=500, text="Internal error")
        client._http.post = AsyncMock(return_value=resp)

        with pytest.raises(ProvisionError, match="Failed to buy number"):
            await client.buy_number()

    # -- list_numbers --

    async def test_list_numbers(self) -> None:
        client = self._cloud_client()
        resp = MagicMock()
        resp.json.return_value = [{"id": "n1"}]
        client._http.get = AsyncMock(return_value=resp)

        result = await client.list_numbers()
        assert len(result) == 1

    # -- assign_agent --

    async def test_assign_agent_success(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"status": "ok"}
        client._http.post = AsyncMock(return_value=resp)

        result = await client.assign_agent("n1", "a1")
        assert result["status"] == "ok"

    async def test_assign_agent_failure(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=400, text="Bad")
        client._http.post = AsyncMock(return_value=resp)

        with pytest.raises(ProvisionError, match="Failed to assign"):
            await client.assign_agent("n1", "bad")

    # -- list_calls --

    async def test_list_calls(self) -> None:
        client = self._cloud_client()
        resp = MagicMock()
        resp.json.return_value = [{"id": "c1"}]
        client._http.get = AsyncMock(return_value=resp)

        result = await client.list_calls(limit=10)
        assert len(result) == 1

    # -- get_call --

    async def test_get_call_success(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"id": "c1", "transcript": []}
        client._http.get = AsyncMock(return_value=resp)

        result = await client.get_call("c1")
        assert result["id"] == "c1"

    async def test_get_call_not_found(self) -> None:
        client = self._cloud_client()
        resp = MagicMock(status_code=404, text="Not found")
        client._http.get = AsyncMock(return_value=resp)

        with pytest.raises(ProvisionError, match="Call not found"):
            await client.get_call("bad")


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDisconnect:
    """Patter.disconnect() — resource cleanup."""

    async def test_disconnect_cloud_mode(self) -> None:
        client = Patter(api_key="pt_test")
        client._connection = AsyncMock()
        client._connection.disconnect = AsyncMock()
        client._http = AsyncMock()
        client._http.aclose = AsyncMock()

        await client.disconnect()
        client._connection.disconnect.assert_awaited_once()
        client._http.aclose.assert_awaited_once()

    async def test_disconnect_local_mode_no_server(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        await client.disconnect()  # should not raise

    async def test_disconnect_local_mode_with_server(self) -> None:
        client = Patter(
            mode="local",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok",
            phone_number="+15550001234",
            webhook_url="abc.ngrok.io",
        )
        mock_server = AsyncMock()
        mock_server.stop = AsyncMock()
        client._server = mock_server

        await client.disconnect()
        mock_server.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStaticHelpers:
    """Static helper methods: deepgram, elevenlabs, guardrail, tool."""

    def test_deepgram(self) -> None:
        cfg = Patter.deepgram(api_key="dg_key", language="it")
        assert cfg.provider == "deepgram"
        assert cfg.api_key == "dg_key"
        assert cfg.language == "it"

    def test_elevenlabs(self) -> None:
        cfg = Patter.elevenlabs(api_key="el_key", voice="emma")
        assert cfg.provider == "elevenlabs"
        assert cfg.voice == "emma"

    def test_whisper(self) -> None:
        cfg = Patter.whisper(api_key="w_key")
        assert cfg.provider == "whisper"

    def test_openai_tts(self) -> None:
        cfg = Patter.openai_tts(api_key="oai_key", voice="nova")
        assert cfg.provider == "openai"
        assert cfg.voice == "nova"

    def test_guardrail(self) -> None:
        g = Patter.guardrail(
            name="No medical",
            blocked_terms=["diagnosis"],
            replacement="See a doctor.",
        )
        assert g["name"] == "No medical"
        assert g["blocked_terms"] == ["diagnosis"]
        assert g["replacement"] == "See a doctor."

    def test_tool_with_handler(self) -> None:
        handler = lambda args, ctx: "ok"
        t = Patter.tool(name="my_tool", handler=handler)
        assert t["name"] == "my_tool"
        assert t["handler"] is handler

    def test_tool_with_webhook(self) -> None:
        t = Patter.tool(name="my_tool", webhook_url="https://example.com/hook")
        assert t["webhook_url"] == "https://example.com/hook"

    def test_tool_requires_handler_or_webhook(self) -> None:
        with pytest.raises(ValueError, match="handler or webhook_url"):
            Patter.tool(name="my_tool")
