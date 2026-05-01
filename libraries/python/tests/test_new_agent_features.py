"""Tests for new agent features: local tools, streaming on_message, CallControl, per-call config."""

import asyncio
import inspect
import json

import pytest

from getpatter import (
    CallControl,
    OpenAIRealtime,
    Patter,
    Tool,
    Twilio,
    tool,
)
from getpatter.models import Agent, CallControl, STTConfig, TTSConfig
from getpatter.services.tool_executor import ToolExecutor


def _local_phone():
    return Patter(
        carrier=Twilio(account_sid="AC" + "a" * 32, auth_token="test"),
        phone_number="+15550001234",
        webhook_url="test.ngrok.io",
    )


# ── Local Function Tools ──


class TestLocalFunctionTools:
    """Test that tools accept handler (Python callable) instead of webhook_url."""

    def test_tool_factory_with_handler(self):
        def my_handler(args, ctx):
            return {"result": "ok"}

        t = tool(
            name="lookup",
            description="Look something up",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
            handler=my_handler,
        )
        assert isinstance(t, Tool)
        assert t.name == "lookup"
        assert t.handler is my_handler
        assert t.webhook_url == ""

    def test_tool_factory_with_webhook(self):
        t = tool(
            name="lookup",
            description="Look something up",
            webhook_url="https://example.com/hook",
        )
        assert t.webhook_url == "https://example.com/hook"
        assert t.handler is None

    def test_tool_factory_requires_handler_or_webhook(self):
        with pytest.raises(ValueError, match="handler.*webhook_url|webhook_url.*handler"):
            tool(name="noop", description="nothing")

    def test_agent_accepts_handler_tool(self):
        phone = _local_phone()
        agent = phone.agent(
            engine=OpenAIRealtime(api_key="sk-test"),
            system_prompt="Test",
            tools=[
                tool(name="fn", description="d", handler=lambda a, c: "ok"),
            ],
        )
        assert len(agent.tools) == 1
        assert "handler" in agent.tools[0]

    def test_agent_rejects_raw_dict_tool(self):
        phone = _local_phone()
        with pytest.raises(TypeError, match="Tool instance"):
            phone.agent(
                engine=OpenAIRealtime(api_key="sk-test"),
                system_prompt="Test",
                tools=[{"name": "legacy", "handler": lambda a, c: "ok"}],
            )


class TestToolExecutorHandler:
    """Test that ToolExecutor can call local Python functions."""

    @pytest.mark.asyncio
    async def test_sync_handler(self):
        def handler(args, ctx):
            return {"sum": args["a"] + args["b"]}

        executor = ToolExecutor()
        result = await executor.execute(
            tool_name="add",
            arguments={"a": 1, "b": 2},
            call_context={"call_id": "c1"},
            handler=handler,
        )
        parsed = json.loads(result)
        assert parsed["sum"] == 3

    @pytest.mark.asyncio
    async def test_async_handler(self):
        async def handler(args, ctx):
            return {"value": args["x"] * 2}

        executor = ToolExecutor()
        result = await executor.execute(
            tool_name="double",
            arguments={"x": 5},
            call_context={"call_id": "c1"},
            handler=handler,
        )
        parsed = json.loads(result)
        assert parsed["value"] == 10

    @pytest.mark.asyncio
    async def test_handler_returns_string(self):
        def handler(args, ctx):
            return "plain text result"

        executor = ToolExecutor()
        result = await executor.execute(
            tool_name="text",
            arguments={},
            call_context={"call_id": "c1"},
            handler=handler,
        )
        assert result == "plain text result"

    @pytest.mark.asyncio
    async def test_handler_receives_call_context(self):
        captured = {}

        def handler(args, ctx):
            captured.update(ctx)
            return "ok"

        executor = ToolExecutor()
        await executor.execute(
            tool_name="ctx_check",
            arguments={},
            call_context={"call_id": "c99", "caller": "+1"},
            handler=handler,
        )
        assert captured["call_id"] == "c99"
        assert captured["caller"] == "+1"

    @pytest.mark.asyncio
    async def test_handler_error_returns_fallback(self):
        def handler(args, ctx):
            raise RuntimeError("boom")

        executor = ToolExecutor()
        result = await executor.execute(
            tool_name="fail",
            arguments={},
            call_context={"call_id": "c1"},
            handler=handler,
        )
        parsed = json.loads(result)
        assert parsed["fallback"] is True
        assert "boom" in parsed["error"]

    @pytest.mark.asyncio
    async def test_no_handler_no_webhook_returns_error(self):
        executor = ToolExecutor()
        result = await executor.execute(
            tool_name="orphan",
            arguments={},
            call_context={"call_id": "c1"},
        )
        parsed = json.loads(result)
        assert parsed["fallback"] is True
        assert "no handler" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_handler_takes_precedence_over_webhook(self):
        def handler(args, ctx):
            return {"source": "handler"}

        executor = ToolExecutor()
        result = await executor.execute(
            tool_name="both",
            arguments={},
            call_context={"call_id": "c1"},
            handler=handler,
            webhook_url="https://example.com/should-not-be-called",
        )
        parsed = json.loads(result)
        assert parsed["source"] == "handler"


# ── CallControl ──


class TestCallControl:
    """Test CallControl model and its methods."""

    def test_initial_state(self):
        cc = CallControl(
            call_id="c1",
            caller="+1",
            callee="+2",
            telephony_provider="twilio",
        )
        assert cc.call_id == "c1"
        assert cc.caller == "+1"
        assert not cc.ended

    @pytest.mark.asyncio
    async def test_transfer_calls_fn(self):
        called_with = []

        async def mock_transfer(number):
            called_with.append(number)

        cc = CallControl(
            call_id="c1", caller="+1", callee="+2",
            telephony_provider="twilio",
            _transfer_fn=mock_transfer,
        )
        await cc.transfer("+15559999999")
        assert called_with == ["+15559999999"]
        assert cc._transferred
        assert cc.ended

    @pytest.mark.asyncio
    async def test_hangup_calls_fn(self):
        hung_up = []

        async def mock_hangup():
            hung_up.append(True)

        cc = CallControl(
            call_id="c1", caller="+1", callee="+2",
            telephony_provider="twilio",
            _hangup_fn=mock_hangup,
        )
        await cc.hangup()
        assert hung_up == [True]
        assert cc._hung_up
        assert cc.ended

    @pytest.mark.asyncio
    async def test_transfer_without_fn_warns(self):
        cc = CallControl(
            call_id="c1", caller="+1", callee="+2",
            telephony_provider="twilio",
        )
        await cc.transfer("+1555")  # Should not raise
        assert not cc.ended

    def test_call_control_importable(self):
        from getpatter import CallControl
        assert CallControl is not None

    def test_ended_property_false_initially(self):
        cc = CallControl(
            call_id="c1", caller="+1", callee="+2",
            telephony_provider="twilio",
        )
        assert cc.ended is False


# ── Streaming on_message ──


class TestStreamingOnMessage:
    """Test that on_message can be an async generator for streaming responses."""

    def test_serve_signature_accepts_on_message(self):
        sig = inspect.signature(Patter.serve)
        assert "on_message" in sig.parameters

    def test_async_generator_detection(self):
        """Verify that inspect.isasyncgen works on async generators."""
        async def gen_handler(data):
            yield "Hello "
            yield "world"

        result = gen_handler({"text": "hi"})
        assert inspect.isasyncgen(result)

    def test_regular_async_function_not_generator(self):
        """Verify that regular async functions are not detected as generators."""
        async def regular_handler(data):
            return "Hello"

        import asyncio
        result = regular_handler({"text": "hi"})
        assert asyncio.iscoroutine(result)
        assert not inspect.isasyncgen(result)
        # Clean up the coroutine
        result.close()


# ── Dynamic Per-Call Config ──


class TestDynamicPerCallConfig:
    """Test that on_call_start can return config overrides."""

    def test_agent_dataclass_supports_replace(self):
        """Verify Agent can be recreated with overrides via dataclass."""
        from dataclasses import asdict

        agent = Agent(system_prompt="Original", voice="alloy", model="gpt-4o-mini-realtime-preview")
        overrides = {"system_prompt": "Override", "voice": "echo"}
        base = {k: v for k, v in asdict(agent).items() if k not in overrides}
        base.update(overrides)
        new_agent = Agent(**base)

        assert new_agent.system_prompt == "Override"
        assert new_agent.voice == "echo"
        assert new_agent.model == "gpt-4o-mini-realtime-preview"  # Unchanged

    def test_stt_config_from_dict(self):
        cfg = STTConfig(provider="deepgram", api_key="dg-test", language="it")
        assert cfg.provider == "deepgram"
        assert cfg.language == "it"

    def test_tts_config_from_dict(self):
        cfg = TTSConfig(provider="elevenlabs", api_key="el-test", voice="rachel")
        assert cfg.provider == "elevenlabs"
        assert cfg.voice == "rachel"

    def test_override_preserves_unset_fields(self):
        """Overrides should only change specified fields, preserving defaults."""
        from dataclasses import asdict

        agent = Agent(
            system_prompt="Original",
            voice="alloy",
            model="gpt-4o-mini-realtime-preview",
            language="en",
            first_message="Hi",
            provider="pipeline",
        )
        overrides = {"voice": "shimmer", "language": "it"}
        base = {k: v for k, v in asdict(agent).items() if k not in overrides}
        base.update(overrides)
        new_agent = Agent(**base)

        assert new_agent.system_prompt == "Original"
        assert new_agent.voice == "shimmer"
        assert new_agent.language == "it"
        assert new_agent.first_message == "Hi"
        assert new_agent.provider == "pipeline"


# ── Integration: serve() signature ──


class TestServeSignature:
    """Verify serve() accepts all the new callback shapes."""

    def test_serve_has_on_message(self):
        sig = inspect.signature(Patter.serve)
        assert "on_message" in sig.parameters

    def test_serve_has_on_call_start(self):
        sig = inspect.signature(Patter.serve)
        assert "on_call_start" in sig.parameters

    def test_serve_has_dashboard(self):
        sig = inspect.signature(Patter.serve)
        assert "dashboard" in sig.parameters
