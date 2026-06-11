"""EvalRunner ↔ EvalSession integration (real pipeline, scripted LLM).

Verifies that an :class:`EvalCase` carrying ``agent=`` / ``llm_provider=``
is routed through the real ``PipelineStreamHandler`` harness, while the
legacy ``reply()``-factory path keeps working unchanged — including both
flavours mixed in one suite.
"""

from __future__ import annotations

import json

import pytest

from getpatter.evals import EvalCase, EvalRunner, EvalSuite, EvalTurn, LLMJudge
from getpatter.evals.session import (
    ScriptedLLMProvider,
    text_turn,
    tool_call_turn,
)
from getpatter.models import Agent


class _FakeJudgeBackend:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    async def judge(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.payload)


def _passing_judge() -> tuple[LLMJudge, _FakeJudgeBackend]:
    backend = _FakeJudgeBackend(
        {"score": 1.0, "passed": True, "reasoning": "ok"}
    )
    return LLMJudge(backend=backend), backend


@pytest.mark.unit
async def test_runner_drives_real_pipeline_for_agent_case():
    tool_ran: list[dict] = []

    async def cancel_order(arguments: dict, call_context: dict) -> str:
        tool_ran.append(arguments)
        return json.dumps({"cancelled": True})

    agent = Agent(
        system_prompt="You are a support agent.",
        provider="pipeline",
        tools=(
            {
                "name": "cancel_order",
                "description": "Cancel an order",
                "parameters": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                },
                "handler": cancel_order,
            },
        ),
    )
    provider = ScriptedLLMProvider(
        [
            tool_call_turn("cancel_order", {"order_id": "A-1"}),
            text_turn("Done — order A-1 is cancelled."),
        ]
    )
    case = EvalCase(
        name="cancels the order",
        turns=(EvalTurn(user="please cancel order A-1"),),
        expected_behavior="Agent cancels the order and confirms.",
        rubric="Pass when the cancellation is confirmed.",
        first_message="Hi, you have reached support.",
        agent=agent,
        llm_provider=provider,
    )
    judge, backend = _passing_judge()
    runner = EvalRunner(judge=judge)

    # No agent_factory needed — the case carries its own real agent.
    results = await runner.run(EvalSuite(name="s", cases=(case,)))

    assert len(results) == 1
    result = results[0]
    assert result.error is None
    assert result.judge.passed is True
    # The REAL pipeline executed the tool through the real ToolExecutor.
    assert tool_ran == [{"order_id": "A-1"}]
    # Transcript: case.first_message (spoken by the REAL start() path) +
    # the user turn + what the agent actually said.
    assert result.transcript[0] == {
        "role": "agent",
        "text": "Hi, you have reached support.",
    }
    assert result.transcript[1] == {
        "role": "user",
        "text": "please cancel order A-1",
    }
    assert result.transcript[2]["role"] == "agent"
    assert "cancelled" in result.transcript[2]["text"]
    # The judge saw the real transcript.
    assert "order A-1 is cancelled" in backend.prompts[0]

    # Report shape is unchanged.
    report = json.loads(runner.report(EvalSuite(name="s", cases=(case,)), results))
    assert report["passed"] == 1


@pytest.mark.unit
async def test_runner_mixes_legacy_and_session_cases_in_one_suite():
    agent = Agent(system_prompt="hi", provider="pipeline")
    session_case = EvalCase(
        name="real-pipeline",
        turns=(EvalTurn(user="ping"),),
        expected_behavior="",
        rubric="",
        agent=agent,
        llm_provider=ScriptedLLMProvider([text_turn("pong from pipeline")]),
    )
    legacy_case = EvalCase(
        name="legacy-reply",
        turns=(EvalTurn(user="ping"),),
        expected_behavior="",
        rubric="",
    )

    async def reply(text: str) -> str:
        return f"echo:{text}"

    judge, _ = _passing_judge()
    runner = EvalRunner(judge=judge)
    results = await runner.run(
        EvalSuite(name="mixed", cases=(session_case, legacy_case)),
        agent_factory=lambda: reply,
    )

    assert [r.case_name for r in results] == ["real-pipeline", "legacy-reply"]
    assert results[0].transcript[-1]["text"] == "pong from pipeline"
    assert results[1].transcript[-1]["text"] == "echo:ping"
    assert all(r.error is None for r in results)


@pytest.mark.unit
async def test_runner_requires_factory_for_legacy_cases():
    legacy_case = EvalCase(
        name="needs-factory",
        turns=(EvalTurn(user="hi"),),
        expected_behavior="",
        rubric="",
    )
    judge, _ = _passing_judge()
    runner = EvalRunner(judge=judge)
    results = await runner.run(EvalSuite(name="s", cases=(legacy_case,)))
    assert results[0].error is not None
    assert "agent_factory" in results[0].error


@pytest.mark.unit
async def test_runner_session_case_records_error_but_judges_partial():
    """A failure mid-case keeps the partial transcript and reports the error
    — same contract as the legacy path."""
    agent = Agent(system_prompt="hi", provider="pipeline")
    case = EvalCase(
        name="dup-turn",
        # The second identical turn is dropped by the REAL commit filter,
        # which the session surfaces as an error.
        turns=(EvalTurn(user="same text"), EvalTurn(user="same text")),
        expected_behavior="",
        rubric="",
        agent=agent,
        llm_provider=ScriptedLLMProvider([text_turn("first"), text_turn("second")]),
    )
    judge, _ = _passing_judge()
    runner = EvalRunner(judge=judge)
    results = await runner.run(EvalSuite(name="s", cases=(case,)))
    result = results[0]
    assert result.error is not None
    assert "commit filter" in result.error
    # Partial transcript from before the failure is preserved for the judge.
    assert {"role": "user", "text": "same text"} in list(result.transcript)
    assert any(
        t == {"role": "agent", "text": "first"} for t in result.transcript
    )
