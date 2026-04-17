"""Integration test for the evals framework against real OpenAI.

Skipped when ``OPENAI_API_KEY`` is not set. This is the only place the judge
is exercised against the real API — all other tests use a mock backend.
"""

from __future__ import annotations

import os

import pytest

from patter.evals import EvalCase, EvalRunner, EvalSuite, EvalTurn, LLMJudge

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
@pytest.mark.asyncio
async def test_llm_judge_scores_polite_reply_higher_than_rude():
    """End-to-end smoke: judge must prefer a polite reply over a rude one."""
    case = EvalCase(
        name="polite-reply",
        turns=[EvalTurn(user="Hi, is this customer support?")],
        expected_behavior=(
            "Agent greets the caller warmly and offers to help with a "
            "support issue."
        ),
        rubric=(
            "Pass if the agent's reply is polite, acknowledges the caller, "
            "and offers to help. Fail if the agent is rude, dismissive, or "
            "does not acknowledge the caller."
        ),
    )

    judge = LLMJudge(model="gpt-4o-mini", pass_threshold=0.7)

    polite_transcript = [
        {"role": "user", "text": "Hi, is this customer support?"},
        {"role": "agent", "text": "Hi there! Yes, you've reached support. How can I help you today?"},
    ]
    rude_transcript = [
        {"role": "user", "text": "Hi, is this customer support?"},
        {"role": "agent", "text": "What do you want."},
    ]

    polite = await judge.judge_case(case, polite_transcript)
    rude = await judge.judge_case(case, rude_transcript)

    assert polite.score > rude.score
    assert polite.passed is True


@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
@pytest.mark.asyncio
async def test_runner_produces_valid_json_report():
    case = EvalCase(
        name="echo",
        turns=[EvalTurn(user="hello")],
        expected_behavior="Agent responds with any non-empty text.",
        rubric="Pass if reply is non-empty and grammatically sensible.",
    )
    suite = EvalSuite(name="smoke", cases=[case])
    runner = EvalRunner()

    async def reply(text: str) -> str:
        return "Hello! How can I help?"

    results = await runner.run(suite, lambda: reply)
    report_json = runner.report(suite, results)

    import json

    payload = json.loads(report_json)
    assert payload["suite"] == "smoke"
    assert payload["total"] == 1
    assert "cases" in payload
