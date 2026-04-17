"""Unit tests for the Patter evals framework.

These tests MOCK the OpenAI judge backend — integration tests against the
real OpenAI API live in ``tests/integration/`` and skip if ``OPENAI_API_KEY``
is not set.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from patter.evals import (
    EvalCase,
    EvalResult,
    EvalRunner,
    EvalSuite,
    EvalTurn,
    JudgeResult,
    LLMJudge,
)
from patter.evals.runner import load_suite


class FakeBackend:
    """Test double for the judge backend — returns a canned JSON string."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def judge(self, prompt: str) -> str:
        self.calls += 1
        return json.dumps(self.payload)


@pytest.mark.asyncio
async def test_llm_judge_parses_score_and_reasoning():
    judge = LLMJudge(
        backend=FakeBackend({"score": 0.9, "passed": True, "reasoning": "great"})
    )
    case = EvalCase(
        name="test",
        turns=[EvalTurn(user="hi")],
        expected_behavior="reply politely",
        rubric="pass if polite",
    )
    result = await judge.judge_case(case, [{"role": "user", "text": "hi"}])
    assert isinstance(result, JudgeResult)
    assert result.score == pytest.approx(0.9)
    assert result.passed is True
    assert result.reasoning == "great"


@pytest.mark.asyncio
async def test_llm_judge_tolerates_code_fences():
    judge = LLMJudge(backend=FakeBackend({"score": 0.5, "passed": False, "reasoning": "meh"}))
    # Replace backend to return a fenced string instead of the raw dict.
    fenced = '```json\n{"score": 0.5, "passed": false, "reasoning": "meh"}\n```'

    class Fenced:
        async def judge(self, _):
            return fenced

    judge._backend = Fenced()
    case = EvalCase(name="t", turns=[], expected_behavior="", rubric="")
    result = await judge.judge_case(case, [])
    assert result.score == pytest.approx(0.5)
    assert result.passed is False


@pytest.mark.asyncio
async def test_llm_judge_fails_safely_on_invalid_json():
    class BadBackend:
        async def judge(self, _):
            return "not json at all"

    judge = LLMJudge(backend=BadBackend())
    case = EvalCase(name="t", turns=[], expected_behavior="", rubric="")
    result = await judge.judge_case(case, [])
    assert result.score == 0.0
    assert result.passed is False


@pytest.mark.asyncio
async def test_llm_judge_clamps_score_to_unit_range():
    judge = LLMJudge(backend=FakeBackend({"score": 1.5, "passed": True, "reasoning": ""}))
    case = EvalCase(name="t", turns=[], expected_behavior="", rubric="")
    result = await judge.judge_case(case, [])
    assert result.score == 1.0


@pytest.mark.asyncio
async def test_runner_end_to_end_produces_transcript_and_report():
    case = EvalCase(
        name="greeting",
        turns=[EvalTurn(user="hello"), EvalTurn(user="how are you?")],
        expected_behavior="greet and respond",
        rubric="pass if reply is non-empty",
        first_message="Hi, how can I help?",
    )
    suite = EvalSuite(name="demo", cases=[case])
    judge = LLMJudge(
        backend=FakeBackend({"score": 1.0, "passed": True, "reasoning": "looks good"})
    )
    runner = EvalRunner(judge=judge)

    async def reply(text: str) -> str:
        return f"echo:{text}"

    def factory():
        return reply

    results = await runner.run(suite, factory)

    assert len(results) == 1
    result = results[0]
    assert result.case_name == "greeting"
    assert result.judge.passed is True
    # transcript has: first_message (agent) + user + agent + user + agent = 5 entries
    assert len(result.transcript) == 5
    assert result.transcript[0] == {"role": "agent", "text": "Hi, how can I help?"}
    assert result.transcript[1] == {"role": "user", "text": "hello"}
    assert result.transcript[2]["role"] == "agent"
    assert result.transcript[2]["text"] == "echo:hello"

    # Report should be valid JSON and contain pass-rate.
    report = json.loads(runner.report(suite, results))
    assert report["suite"] == "demo"
    assert report["total"] == 1
    assert report["passed"] == 1
    assert report["pass_rate"] == 1.0


@pytest.mark.asyncio
async def test_runner_handles_agent_exception():
    case = EvalCase(
        name="boom",
        turns=[EvalTurn(user="hi")],
        expected_behavior="",
        rubric="",
    )
    suite = EvalSuite(name="s", cases=[case])
    judge = LLMJudge(backend=FakeBackend({"score": 0, "passed": False, "reasoning": ""}))
    runner = EvalRunner(judge=judge)

    async def broken(_: str) -> str:
        raise RuntimeError("agent died")

    def factory():
        return broken

    results = await runner.run(suite, factory)
    assert len(results) == 1
    assert results[0].error is not None
    assert "agent died" in results[0].error
    assert results[0].judge.passed is False


def test_load_suite_yaml(tmp_path: Path):
    pytest.importorskip("yaml")
    src = tmp_path / "suite.yaml"
    src.write_text(
        """
name: test-suite
cases:
  - name: greeting
    expected_behavior: greet
    rubric: pass if greeting
    turns:
      - user: hi
        expected_contains: [hello, hi]
    tags: [smoke]
""",
        encoding="utf-8",
    )
    suite = load_suite(src)
    assert suite.name == "test-suite"
    assert len(suite.cases) == 1
    case = suite.cases[0]
    assert case.name == "greeting"
    assert case.turns[0].user == "hi"
    assert case.tags == ["smoke"]


def test_load_suite_json(tmp_path: Path):
    src = tmp_path / "suite.json"
    src.write_text(
        json.dumps(
            {
                "name": "json-suite",
                "cases": [
                    {
                        "name": "t1",
                        "expected_behavior": "b",
                        "rubric": "r",
                        "turns": [{"user": "hey"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    suite = load_suite(src)
    assert suite.name == "json-suite"
    assert suite.cases[0].turns[0].user == "hey"
