"""Tests for the SKILLS primitive (built-in ``use_skill`` tool, progressive
disclosure).

Covers:
- ``build_use_skill_tool`` schema — DISCOVERY layer surfaces only skill names +
  descriptions as an enum, NOT the full instructions.
- ``apply_skill_activation`` — ACTIVATION layers instructions into the prompt
  and merges the skill's tools (additive, deduped).
- ``assert_no_skill_conflicts`` — reserved / cross-skill / agent-tool collisions.
- ``Patter.agent(skills=[...])`` validation + Agent field + Tool normalisation.
- Pipeline mode: ``use_skill`` is advertised; activating a skill layers the
  instructions, makes the skill's tool invocable, and a skill tool is NOT in
  the combined tool list before activation.
- Realtime mode: ``use_skill`` activation sends the expected ``session.update``
  (now carrying the skill's tools) and ALWAYS a function result; unknown /
  already-active skills return envelopes; malformed args never wedge the model.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from getpatter import Skill
from getpatter.stream_handler import (
    OpenAIRealtimeStreamHandler,
    PipelineStreamHandler,
)
from getpatter.tools.skills import (
    USE_SKILL_TOOL_NAME,
    apply_skill_activation,
    assert_no_skill_conflicts,
    build_use_skill_tool,
    skill_activation_block,
)
from tests.conftest import make_agent

# Dict-form tools (the internal normalised shape — what ``Agent`` carries).
REFUND_TOOL = {
    "name": "process_refund",
    "description": "Issue a refund for an order.",
    "parameters": {"type": "object", "properties": {}},
    "handler": AsyncMock(return_value="refunded"),
}
SKILL_INSTRUCTIONS = (
    "STEP 1: verify the order id. STEP 2: confirm the amount. "
    "STEP 3: call process_refund. Never refund more than $500 without escalation."
)


def _refund_skill() -> Skill:
    return Skill(
        name="refunds",
        description="Process customer refunds end to end.",
        instructions=SKILL_INSTRUCTIONS,
        tools=(REFUND_TOOL,),
    )


# ---------------------------------------------------------------------------
# build_use_skill_tool — DISCOVERY layer
# ---------------------------------------------------------------------------


def test_use_skill_tool_surfaces_names_and_descriptions_not_instructions():
    skill = _refund_skill()
    tool = build_use_skill_tool((skill,))

    assert tool["name"] == USE_SKILL_TOOL_NAME == "use_skill"
    enum = tool["parameters"]["properties"]["skill_name"]["enum"]
    assert enum == ["refunds"]
    assert tool["parameters"]["required"] == ["skill_name"]
    # Discovery surfaces the description (WHEN to use it)...
    assert "Process customer refunds end to end." in tool["description"]
    # ...but NEVER the full instructions (progressive disclosure — loaded only
    # on activation, keeping the call-start prompt cheap).
    assert SKILL_INSTRUCTIONS not in tool["description"]
    assert "process_refund" not in tool["description"]


def test_use_skill_enum_sorted_deterministic():
    a = Skill(name="zeta", description="z", instructions="iz")
    b = Skill(name="alpha", description="a", instructions="ia")
    t1 = build_use_skill_tool((a, b))
    t2 = build_use_skill_tool((b, a))
    assert t1 == t2
    assert t1["parameters"]["properties"]["skill_name"]["enum"] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# apply_skill_activation — ACTIVATION layer
# ---------------------------------------------------------------------------


def test_apply_skill_activation_layers_instructions_and_tools():
    agent = make_agent(
        system_prompt="You are a support agent.",
        tools=({"name": "lookup_order", "description": "", "parameters": {}},),
    )
    skill = _refund_skill()

    activated = apply_skill_activation(agent, skill)

    # Instructions layered into the system prompt as a # Skill: block...
    assert "You are a support agent." in activated.system_prompt
    assert "# Skill: refunds" in activated.system_prompt
    assert SKILL_INSTRUCTIONS in activated.system_prompt
    # ...base prompt preserved (additive, not a one-way replace).
    assert activated.system_prompt.startswith("You are a support agent.")
    # Skill tool merged in; original tool retained.
    names = [t["name"] for t in activated.tools]
    assert names == ["lookup_order", "process_refund"]
    # Frozen dataclass — original untouched.
    assert agent.system_prompt == "You are a support agent."
    assert [t["name"] for t in (agent.tools or ())] == ["lookup_order"]


def test_apply_skill_activation_dedupes_existing_tool_names():
    # A skill tool whose name already exists on the agent is not added twice.
    agent = make_agent(
        system_prompt="x",
        tools=(
            {"name": "process_refund", "description": "existing", "parameters": {}},
        ),
    )
    activated = apply_skill_activation(agent, _refund_skill())
    names = [t["name"] for t in activated.tools]
    assert names == ["process_refund"]  # not duplicated


def test_skill_activation_block_format():
    block = skill_activation_block(
        Skill(name="refunds", description="d", instructions="DO X")
    )
    assert block == "# Skill: refunds\n\nDO X"


# ---------------------------------------------------------------------------
# assert_no_skill_conflicts
# ---------------------------------------------------------------------------


def test_conflict_reserved_builtin_name_rejected():
    bad = Skill(
        name="x",
        description="d",
        instructions="i",
        tools=({"name": "end_call", "description": "", "parameters": {}},),
    )
    with pytest.raises(ValueError, match="reserved built-in tool name"):
        assert_no_skill_conflicts((bad,))


def test_conflict_use_skill_name_rejected():
    bad = Skill(
        name="x",
        description="d",
        instructions="i",
        tools=({"name": "use_skill", "description": "", "parameters": {}},),
    )
    with pytest.raises(ValueError, match="reserved built-in tool name"):
        assert_no_skill_conflicts((bad,))


def test_conflict_with_agent_tool_rejected():
    agent_tools = [{"name": "process_refund", "description": "", "parameters": {}}]
    with pytest.raises(ValueError, match="top-level"):
        assert_no_skill_conflicts((_refund_skill(),), agent_tools)


def test_conflict_duplicate_skill_name_rejected():
    a = Skill(name="dup", description="d", instructions="i")
    b = Skill(name="dup", description="d2", instructions="i2")
    with pytest.raises(ValueError, match="Duplicate skill name"):
        assert_no_skill_conflicts((a, b))


def test_conflict_cross_skill_tool_name_rejected():
    a = Skill(
        name="a",
        description="d",
        instructions="i",
        tools=({"name": "shared", "description": "", "parameters": {}},),
    )
    b = Skill(
        name="b",
        description="d",
        instructions="i",
        tools=({"name": "shared", "description": "", "parameters": {}},),
    )
    with pytest.raises(ValueError, match="more than one skill"):
        assert_no_skill_conflicts((a, b))


def test_no_conflict_clean_passes():
    assert_no_skill_conflicts((_refund_skill(),), [{"name": "lookup_order"}]) is None


# ---------------------------------------------------------------------------
# Patter.agent(skills=...) — validation + Tool normalisation
# ---------------------------------------------------------------------------


class TestAgentFactorySkills:
    def _phone(self):
        from getpatter import Patter, Twilio

        return Patter(
            carrier=Twilio(account_sid="AC" + "0" * 32, auth_token="token"),
            phone_number="+15550000000",
            webhook_url="example.ngrok.io",
        )

    def _engine(self):
        from getpatter import OpenAIRealtime

        return OpenAIRealtime(api_key="sk-test")

    def test_skills_stored_and_tools_normalised(self):
        from getpatter import tool

        phone = self._phone()

        async def _do(args, ctx):
            return "ok"

        skill = Skill(
            name="refunds",
            description="Handle refunds.",
            instructions=SKILL_INSTRUCTIONS,
            tools=(tool(name="process_refund", description="Refund", handler=_do),),
        )
        agent = phone.agent(
            system_prompt="Support.", engine=self._engine(), skills=[skill]
        )
        assert len(agent.skills) == 1
        stored = agent.skills[0]
        assert stored.name == "refunds"
        # Public Tool instance normalised to the internal dict shape.
        assert isinstance(stored.tools[0], dict)
        assert stored.tools[0]["name"] == "process_refund"
        # Default stays empty — zero behaviour change for existing agents.
        plain = phone.agent(system_prompt="x", engine=self._engine())
        assert plain.skills == ()

    def test_skills_must_be_list_of_skill(self):
        phone = self._phone()
        with pytest.raises(TypeError, match="must be a Skill"):
            phone.agent(
                system_prompt="x",
                engine=self._engine(),
                skills=[{"name": "nope"}],  # type: ignore[list-item]
            )

    def test_skill_tool_colliding_with_reserved_rejected_at_build(self):
        from getpatter import tool

        phone = self._phone()

        async def _do(args, ctx):
            return "ok"

        skill = Skill(
            name="x",
            description="d",
            instructions="i",
            tools=(tool(name="transfer_call", description="bad", handler=_do),),
        )
        with pytest.raises(ValueError, match="reserved built-in tool name"):
            phone.agent(system_prompt="x", engine=self._engine(), skills=[skill])


# ---------------------------------------------------------------------------
# Pipeline handler — _perform_skill_activation
# ---------------------------------------------------------------------------


def _make_pipeline_handler(agent):
    return PipelineStreamHandler(
        agent=agent,
        audio_sender=MagicMock(),
        call_id="CA" + "a" * 32,
        caller="+15551234567",
        callee="+15559876543",
        resolved_prompt=agent.system_prompt,
        metrics=None,
    )


def test_pipeline_use_skill_tool_injected_but_skill_tool_not_yet_available():
    agent = make_agent(
        system_prompt="Support.", skills=(_refund_skill(),), first_message=""
    )
    handler = _make_pipeline_handler(agent)
    handler._transfer_fn = AsyncMock()
    handler._hangup_fn = AsyncMock()

    tools = handler._build_combined_pipeline_tools()
    names = [t["name"] for t in tools]
    # use_skill is advertised (discovery) alongside the built-ins...
    assert USE_SKILL_TOOL_NAME in names
    # ...but the SKILL'S OWN tool is NOT invocable before activation.
    assert "process_refund" not in names


@pytest.mark.asyncio
async def test_pipeline_skill_activation_layers_prompt_and_unlocks_tool():
    agent = make_agent(
        system_prompt="You are a support agent.",
        skills=(_refund_skill(),),
        first_message="",
    )
    handler = _make_pipeline_handler(agent)
    handler._transfer_fn = AsyncMock()
    handler._hangup_fn = AsyncMock()
    llm_loop = MagicMock()
    handler._llm_loop = llm_loop

    result = json.loads(await handler._perform_skill_activation("refunds"))

    assert result == {"status": "skill_activated", "skill": "refunds"}
    # Instructions now layered into the live agent prompt.
    assert "# Skill: refunds" in handler.agent.system_prompt
    assert SKILL_INSTRUCTIONS in handler.agent.system_prompt
    # The LLM loop was re-armed for the NEXT turn with the skill's tool now
    # present (ACTIVATION exposure).
    llm_loop.update_agent.assert_called_once()
    kwargs = llm_loop.update_agent.call_args.kwargs
    assert kwargs["system_prompt"] == handler.agent.system_prompt
    tool_names = [t["name"] for t in kwargs["tools"]]
    assert "process_refund" in tool_names
    assert USE_SKILL_TOOL_NAME in tool_names
    # History records the activation.
    assert any(
        e["role"] == "system" and "Activated skill 'refunds'" in e["text"]
        for e in handler.conversation_history
    )


@pytest.mark.asyncio
async def test_pipeline_skill_activation_idempotent():
    agent = make_agent(
        system_prompt="Support.", skills=(_refund_skill(),), first_message=""
    )
    handler = _make_pipeline_handler(agent)
    handler._transfer_fn = AsyncMock()
    handler._hangup_fn = AsyncMock()
    handler._llm_loop = MagicMock()

    await handler._perform_skill_activation("refunds")
    prompt_after_first = handler.agent.system_prompt
    second = json.loads(await handler._perform_skill_activation("refunds"))

    assert second == {"status": "already_active", "skill": "refunds"}
    # No double-layering of the instructions block.
    assert handler.agent.system_prompt == prompt_after_first
    assert handler.agent.system_prompt.count("# Skill: refunds") == 1


@pytest.mark.asyncio
async def test_pipeline_skill_unknown_name_returns_envelope():
    agent = make_agent(
        system_prompt="Support.", skills=(_refund_skill(),), first_message=""
    )
    handler = _make_pipeline_handler(agent)
    handler._llm_loop = MagicMock()

    result = json.loads(await handler._perform_skill_activation("nope"))
    assert "Unknown skill 'nope'" in result["error"]
    assert result["available"] == ["refunds"]
    handler._llm_loop.update_agent.assert_not_called()
    # No activation, no history.
    assert "# Skill:" not in handler.agent.system_prompt


@pytest.mark.asyncio
async def test_pipeline_use_skill_handler_round_trip():
    agent = make_agent(
        system_prompt="Support.", skills=(_refund_skill(),), first_message=""
    )
    handler = _make_pipeline_handler(agent)
    handler._transfer_fn = AsyncMock()
    handler._hangup_fn = AsyncMock()
    handler._llm_loop = MagicMock()

    tools = handler._build_combined_pipeline_tools()
    use_skill_tool = next(t for t in tools if t["name"] == USE_SKILL_TOOL_NAME)
    assert callable(use_skill_tool["handler"])
    result = json.loads(await use_skill_tool["handler"]({"skill_name": "refunds"}, {}))
    assert result == {"status": "skill_activated", "skill": "refunds"}


# ---------------------------------------------------------------------------
# Realtime handler — _handle_use_skill_function_call
# ---------------------------------------------------------------------------


def _make_realtime_handler(agent):
    handler = OpenAIRealtimeStreamHandler(
        agent=agent,
        audio_sender=MagicMock(),
        call_id="CA" + "a" * 32,
        caller="+15551234567",
        callee="+15559876543",
        resolved_prompt=agent.system_prompt,
        metrics=None,
        openai_key="sk-test",
    )
    handler._adapter = AsyncMock()
    return handler


@pytest.mark.asyncio
async def test_realtime_skill_activation_sends_session_update_with_skill_tools():
    agent = make_agent(
        system_prompt="You are a support agent.",
        provider="openai_realtime",
        skills=(_refund_skill(),),
        first_message="",
    )
    handler = _make_realtime_handler(agent)
    adapter = handler._adapter

    await handler._handle_use_skill_function_call(
        {
            "call_id": "fc-1",
            "name": USE_SKILL_TOOL_NAME,
            "arguments": json.dumps({"skill_name": "refunds"}),
        }
    )

    # session.update carries the skill-layered prompt + the skill's tool.
    adapter.update_session.assert_awaited_once()
    kwargs = adapter.update_session.await_args.kwargs
    assert "# Skill: refunds" in kwargs["instructions"]
    assert SKILL_INSTRUCTIONS in kwargs["instructions"]
    tool_names = [t["name"] for t in kwargs["tools"]]
    assert "process_refund" in tool_names
    # use_skill re-advertised so further skills can still activate.
    assert USE_SKILL_TOOL_NAME in tool_names
    assert "transfer_call" in tool_names and "end_call" in tool_names

    # A function result is ALWAYS sent.
    adapter.send_function_result.assert_awaited_once()
    fc_id, result_str = adapter.send_function_result.await_args.args
    assert fc_id == "fc-1"
    assert json.loads(result_str) == {"status": "skill_activated", "skill": "refunds"}


@pytest.mark.asyncio
async def test_realtime_skill_session_update_precedes_function_result():
    agent = make_agent(
        system_prompt="Support.",
        provider="openai_realtime",
        skills=(_refund_skill(),),
        first_message="",
    )
    handler = _make_realtime_handler(agent)
    order: list[str] = []
    handler._adapter.update_session.side_effect = lambda **kw: order.append("update")
    handler._adapter.send_function_result.side_effect = lambda *a: order.append(
        "result"
    )

    await handler._handle_use_skill_function_call(
        {
            "call_id": "fc-1",
            "name": USE_SKILL_TOOL_NAME,
            "arguments": json.dumps({"skill_name": "refunds"}),
        }
    )
    assert order == ["update", "result"]


@pytest.mark.asyncio
async def test_realtime_skill_unknown_name_sends_envelope_no_update():
    agent = make_agent(
        system_prompt="Support.",
        provider="openai_realtime",
        skills=(_refund_skill(),),
        first_message="",
    )
    handler = _make_realtime_handler(agent)
    adapter = handler._adapter

    await handler._handle_use_skill_function_call(
        {
            "call_id": "fc-2",
            "name": USE_SKILL_TOOL_NAME,
            "arguments": json.dumps({"skill_name": "sales"}),
        }
    )
    adapter.update_session.assert_not_awaited()
    adapter.send_function_result.assert_awaited_once()
    _, result_str = adapter.send_function_result.await_args.args
    result = json.loads(result_str)
    assert "Unknown skill 'sales'" in result["error"]
    assert result["available"] == ["refunds"]
    assert "# Skill:" not in handler.agent.system_prompt


@pytest.mark.asyncio
async def test_realtime_skill_already_active_no_double_update():
    agent = make_agent(
        system_prompt="Support.",
        provider="openai_realtime",
        skills=(_refund_skill(),),
        first_message="",
    )
    handler = _make_realtime_handler(agent)

    fc = {
        "call_id": "fc-1",
        "name": USE_SKILL_TOOL_NAME,
        "arguments": json.dumps({"skill_name": "refunds"}),
    }
    await handler._handle_use_skill_function_call(fc)
    handler._adapter.update_session.reset_mock()
    handler._adapter.send_function_result.reset_mock()

    await handler._handle_use_skill_function_call(fc)
    handler._adapter.update_session.assert_not_awaited()
    _, result_str = handler._adapter.send_function_result.await_args.args
    assert json.loads(result_str) == {"status": "already_active", "skill": "refunds"}


@pytest.mark.asyncio
async def test_realtime_skill_malformed_args_sends_envelope():
    agent = make_agent(
        system_prompt="Support.",
        provider="openai_realtime",
        skills=(_refund_skill(),),
        first_message="",
    )
    handler = _make_realtime_handler(agent)

    await handler._handle_use_skill_function_call(
        {"call_id": "fc-3", "name": USE_SKILL_TOOL_NAME, "arguments": "{not json"}
    )
    handler._adapter.send_function_result.assert_awaited_once()
    _, result_str = handler._adapter.send_function_result.await_args.args
    assert "error" in json.loads(result_str)
    handler._adapter.update_session.assert_not_awaited()
