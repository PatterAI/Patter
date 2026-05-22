"""Unit tests for getpatter.client — the main Patter SDK client (local mode)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
        carrier=Twilio(
            account_sid="ACtest000000000000000000000000000", auth_token="tok"
        ),
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
        with pytest.raises(
            NotImplementedError, match="Patter Cloud is not yet available"
        ):
            Patter(api_key="pt_xxx")

    def test_backend_url_raises_not_implemented(self) -> None:
        with pytest.raises(
            NotImplementedError, match="Patter Cloud is not yet available"
        ):
            Patter(backend_url="wss://custom.host")

    def test_rest_url_raises_not_implemented(self) -> None:
        with pytest.raises(
            NotImplementedError, match="Patter Cloud is not yet available"
        ):
            Patter(rest_url="https://custom.host")

    def test_unknown_mode_raises_not_implemented(self) -> None:
        with pytest.raises(
            NotImplementedError, match="Patter Cloud is not yet available"
        ):
            Patter(mode="cloud")

    def test_unexpected_kwarg_raises_typeerror(self) -> None:
        with pytest.raises(TypeError, match="unexpected keyword"):
            Patter(
                carrier=Twilio(account_sid="AC1", auth_token="t"),
                phone_number="+1",
                bogus="x",
            )

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

    def test_agent_default_flags_off(self) -> None:
        """Builder defaults match the Agent dataclass defaults."""
        client = _local_phone()
        agent = client.agent(
            engine=OpenAIRealtime(api_key="sk-test"),
            system_prompt="Hi",
        )
        assert agent.aggressive_first_flush is False
        assert agent.disable_phone_preamble is False
        assert agent.echo_cancellation is False

    def test_agent_builder_passes_through_pipeline_flags(self) -> None:
        """The 3 boolean flags reach the Agent dataclass via the builder.

        Pre-fix the builder enumerated kwargs explicitly and silently
        dropped any flag it didn't list — TS parity was broken because
        the TS side spreads the whole options object.
        """
        client = _local_phone()
        agent = client.agent(
            engine=OpenAIRealtime(api_key="sk-test"),
            system_prompt="Hi",
            aggressive_first_flush=True,
            disable_phone_preamble=True,
            echo_cancellation=True,
        )
        assert agent.aggressive_first_flush is True
        assert agent.disable_phone_preamble is True
        assert agent.echo_cancellation is True

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
        # Server reference should be cleared so a follow-up serve() builds a
        # fresh one rather than reusing the stopped instance.
        assert client._server is None

    async def test_disconnect_clears_tunnel_owned_webhook_url(self) -> None:
        """An auto-tunnel webhook URL must be cleared on disconnect.

        Plugins call ``ensureServing`` as ``disconnect() → serve()`` whenever
        the agent identity changes. Without this clear the second ``serve()``
        sees ``localConfig.webhookUrl`` already populated and trips the
        ``Cannot use both tunnel=True and webhook_url`` guard.
        """
        from dataclasses import replace

        client = _local_phone(webhook_url="")
        # Simulate what serve() does after starting a cloudflared tunnel.
        client._local_config = replace(
            client._local_config, webhook_url="auto.trycloudflare.com"
        )
        client._tunnel_owns_webhook_url = True

        await client.disconnect()

        assert client._local_config.webhook_url == ""
        assert client._tunnel_owns_webhook_url is False
        # Deferreds dropped so a follow-up serve() recreates them.
        assert client._ready is None
        assert client._tunnel_ready is None

    async def test_disconnect_preserves_explicit_webhook_url(self) -> None:
        """An explicit webhook URL passed at construction must NOT be cleared."""
        client = _local_phone(webhook_url="static.example.com")
        # No tunnel — the URL belongs to the caller.
        assert client._tunnel_owns_webhook_url is False

        await client.disconnect()

        assert client._local_config.webhook_url == "static.example.com"
        assert client._tunnel_owns_webhook_url is False

    async def test_disconnect_is_idempotent(self) -> None:
        """Calling disconnect() twice in a row must not raise."""
        client = _local_phone()
        await client.disconnect()
        await client.disconnect()  # should not raise


# ---------------------------------------------------------------------------
# ready / tunnel_ready — serve-ready futures (parity with TS phone.ready)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReadyFuture:
    """phone.ready and phone.tunnel_ready — lazy futures that track serve() state."""

    # --- lazy creation -------------------------------------------------------

    async def test_ready_creates_future_on_first_access(self) -> None:
        """Accessing phone.ready before serve() returns a pending Future."""
        import asyncio

        client = _local_phone()
        fut = client.ready
        assert isinstance(fut, asyncio.Future)
        assert not fut.done()

    async def test_tunnel_ready_creates_future_on_first_access(self) -> None:
        """Accessing phone.tunnel_ready before serve() returns a pending Future.

        Use webhook_url="" so there is no static pre-resolution — the tunnel
        must call _resolve_tunnel_ready() before the future is done.
        """
        import asyncio

        client = _local_phone(webhook_url="")
        fut = client.tunnel_ready
        assert isinstance(fut, asyncio.Future)
        assert not fut.done()

    async def test_ready_returns_same_future_on_repeated_access(self) -> None:
        """Repeated accesses return the same Future object (idempotent)."""
        client = _local_phone()
        assert client.ready is client.ready

    async def test_tunnel_ready_returns_same_future_on_repeated_access(self) -> None:
        client = _local_phone(webhook_url="")
        assert client.tunnel_ready is client.tunnel_ready

    # --- resolution ----------------------------------------------------------

    async def test_ready_resolves_when_server_listening(self) -> None:
        """_resolve_ready() fulfils phone.ready with the webhook hostname."""
        client = _local_phone()
        fut = client.ready
        assert not fut.done()

        client._resolve_ready("my-tunnel.trycloudflare.com")

        assert fut.done()
        assert await fut == "my-tunnel.trycloudflare.com"

    async def test_tunnel_ready_resolves_when_tunnel_known(self) -> None:
        client = _local_phone(webhook_url="")
        fut = client.tunnel_ready
        client._resolve_tunnel_ready("tunnel.example.com")

        assert fut.done()
        assert await fut == "tunnel.example.com"

    async def test_ready_rejects_on_serve_failure(self) -> None:
        """_reject_ready() causes phone.ready to raise the propagated error."""
        import asyncio

        client = _local_phone()
        fut = client.ready

        err = RuntimeError("port already in use")
        client._reject_ready(err)

        assert fut.done()
        with pytest.raises(RuntimeError, match="port already in use"):
            await asyncio.shield(fut)

    # --- idempotence (safe to call resolve/reject multiple times) -----------

    async def test_resolve_ready_is_idempotent(self) -> None:
        """Calling _resolve_ready() twice does not raise FutureDoneError."""
        client = _local_phone()
        client._resolve_ready("host-a.com")
        client._resolve_ready("host-b.com")  # should be a no-op, not raise
        assert await client.ready == "host-a.com"

    async def test_reject_ready_after_resolve_is_noop(self) -> None:
        client = _local_phone()
        client._resolve_ready("host.com")
        client._reject_ready(RuntimeError("ignored"))  # must not raise
        assert await client.ready == "host.com"

    # --- static-webhook pre-resolution (no tunnel) --------------------------

    async def test_tunnel_ready_pre_resolved_for_static_webhook(self) -> None:
        """With an explicit webhookUrl, tunnel_ready is resolved immediately."""
        client = _local_phone(webhook_url="static.example.com")
        fut = client.tunnel_ready
        assert fut.done()
        assert fut.result() == "static.example.com"

    # --- disconnect recreates the futures -----------------------------------

    async def test_disconnect_recreates_ready_future(self) -> None:
        """After disconnect(), phone.ready is a fresh pending Future.

        Mirrors the TS test:
          'recreates ready / tunnelReady so a follow-up serve() can resolve them'
        """
        client = _local_phone()
        before = client.ready
        client._resolve_ready("host.com")

        await client.disconnect()

        after = client.ready
        assert after is not before
        assert not after.done()

    async def test_disconnect_recreates_tunnel_ready_future(self) -> None:
        client = _local_phone(webhook_url="")
        before = client.tunnel_ready
        client._resolve_tunnel_ready("t.example.com")

        await client.disconnect()

        after = client.tunnel_ready
        assert after is not before
        assert not after.done()


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
        with pytest.raises(
            ValueError, match="handler.*webhook_url|webhook_url.*handler"
        ):
            tool(name="my_tool")
