"""Unit tests for the eval assertion helpers (``getpatter.evals.assertions``).

These exercise :func:`expect` against hand-built ``TurnResult`` objects —
the end-to-end pairing with a real session lives in
``test_eval_session.py``. The ``judge`` helper reuses the existing
``LLMJudge`` with an injected fake backend (no network).
"""

from __future__ import annotations

import json

import pytest

from getpatter.evals.assertions import expect
from getpatter.evals.llm_judge import LLMJudge
from getpatter.evals.session import ToolCallRecord, TurnResult


def _result(
    *,
    agent_text: str = "Booked your table for two.",
    tool_calls: tuple[ToolCallRecord, ...] = (),
    history: tuple[dict, ...] = (),
) -> TurnResult:
    return TurnResult(
        user_text="book a table",
        agent_text=agent_text,
        tool_calls=tool_calls,
        history_snapshot=history,
        interrupted=False,
        metrics_turn=None,
    )


class _FakeJudgeBackend:
    """Canned judge backend — records prompts, returns a fixed verdict."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    async def judge(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(self.payload)


@pytest.mark.unit
def test_tool_called_passes_and_chains():
    record = ToolCallRecord(
        name="book_table",
        arguments={"party_size": 2, "time": "20:00"},
        result='{"ok": true}',
    )
    result = _result(tool_calls=(record,))
    chained = (
        expect(result)
        .tool_called("book_table")
        .tool_called("book_table", args_subset={"party_size": 2})
        .agent_text_contains("booked", "table")
    )
    assert chained.result is result


@pytest.mark.unit
def test_tool_called_fails_with_observed_tools_in_message():
    result = _result(
        tool_calls=(ToolCallRecord(name="other_tool", arguments={}),)
    )
    with pytest.raises(AssertionError, match="other_tool"):
        expect(result).tool_called("book_table")


@pytest.mark.unit
def test_tool_called_args_subset_mismatch_fails():
    record = ToolCallRecord(name="book_table", arguments={"party_size": 4})
    with pytest.raises(AssertionError, match="party_size"):
        expect(_result(tool_calls=(record,))).tool_called(
            "book_table", args_subset={"party_size": 2}
        )


@pytest.mark.unit
def test_tool_called_args_subset_is_recursive():
    record = ToolCallRecord(
        name="update",
        arguments={"customer": {"id": "c1", "tier": "gold"}, "notify": True},
    )
    expect(_result(tool_calls=(record,))).tool_called(
        "update", args_subset={"customer": {"id": "c1"}}
    )
    with pytest.raises(AssertionError):
        expect(_result(tool_calls=(record,))).tool_called(
            "update", args_subset={"customer": {"id": "nope"}}
        )


@pytest.mark.unit
def test_no_tool_called_passes_and_fails():
    expect(_result()).no_tool_called()
    busy = _result(tool_calls=(ToolCallRecord(name="t", arguments={}),))
    with pytest.raises(AssertionError, match="no tool calls"):
        expect(busy).no_tool_called()
    # Named variant: only the named tool is forbidden.
    expect(busy).no_tool_called("other")
    with pytest.raises(AssertionError):
        expect(busy).no_tool_called("t")


@pytest.mark.unit
def test_agent_text_contains_case_handling():
    result = _result(agent_text="Your Table is Booked.")
    expect(result).agent_text_contains("table is booked")
    with pytest.raises(AssertionError, match="missing"):
        expect(result).agent_text_contains("table is booked", case_sensitive=True)


@pytest.mark.unit
async def test_judge_passes_and_returns_verdict():
    backend = _FakeJudgeBackend(
        {"score": 0.9, "passed": True, "reasoning": "confirms the booking"}
    )
    judge = LLMJudge(backend=backend)
    history = (
        {"role": "user", "text": "book a table"},
        {"role": "assistant", "text": "Booked your table for two."},
    )
    verdict = await expect(_result(history=history)).judge(
        judge, intent="The agent confirms the booking."
    )
    assert verdict.passed is True
    assert verdict.score == pytest.approx(0.9)
    # The judge saw the turn transcript with assistant mapped to "agent".
    assert "agent: Booked your table for two." in backend.prompts[0]
    assert "The agent confirms the booking." in backend.prompts[0]


@pytest.mark.unit
async def test_judge_failure_raises_assertion_error_with_reasoning():
    backend = _FakeJudgeBackend(
        {"score": 0.2, "passed": False, "reasoning": "never confirmed"}
    )
    judge = LLMJudge(backend=backend)
    with pytest.raises(AssertionError, match="never confirmed"):
        await expect(_result()).judge(judge, intent="Booking is confirmed.")
