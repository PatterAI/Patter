"""Fluent assertions for :class:`~getpatter.evals.session.TurnResult`.

Chainable expectations against the outcome of one real
pipeline turn driven by :class:`~getpatter.evals.session.EvalSession`::

    from getpatter.evals.assertions import expect

    result = await session.user_says("book me a table for two")
    expect(result).tool_called(
        "book_table", args_subset={"party_size": 2}
    ).agent_text_contains("booked")

    # Semantic check via the existing LLMJudge (async, ends the chain):
    await expect(result).judge(
        LLMJudge(), intent="The agent confirms the booking and offers help."
    )

Every failed expectation raises ``AssertionError`` with the observed
values, so plain ``pytest`` reports are actionable without extra plumbing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from getpatter.evals.case import EvalCase, JudgeResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from getpatter.evals.llm_judge import LLMJudge
    from getpatter.evals.session import TurnResult

logger = logging.getLogger("getpatter.evals")

__all__ = ["expect", "TurnExpectation"]


def expect(result: "TurnResult") -> "TurnExpectation":
    """Wrap a :class:`TurnResult` in a chainable expectation object."""
    return TurnExpectation(result)


def _is_subset(subset: Any, actual: Any) -> bool:
    """True when ``subset`` is recursively contained in ``actual``.

    Dicts match when every key in ``subset`` exists in ``actual`` and its
    value matches recursively; every other type matches by equality.
    """
    if isinstance(subset, dict):
        if not isinstance(actual, dict):
            return False
        return all(k in actual and _is_subset(v, actual[k]) for k, v in subset.items())
    return bool(subset == actual)


class TurnExpectation:
    """Chainable assertions over one turn. See :func:`expect`."""

    def __init__(self, result: "TurnResult") -> None:
        self._result = result

    @property
    def result(self) -> "TurnResult":
        """The wrapped :class:`TurnResult` (escape hatch for ad-hoc asserts)."""
        return self._result

    # -- tools ---------------------------------------------------------------

    def tool_called(
        self, name: str, args_subset: dict | None = None
    ) -> "TurnExpectation":
        """Assert that tool ``name`` ran this turn.

        ``args_subset`` (optional) must be recursively contained in the
        arguments of at least one matching invocation — extra argument keys
        are allowed, listed keys must match exactly.
        """
        matches = [tc for tc in self._result.tool_calls if tc.name == name]
        if not matches:
            called = [tc.name for tc in self._result.tool_calls]
            raise AssertionError(
                f"expected tool {name!r} to be called this turn; "
                f"tools called: {called or 'none'}"
            )
        if args_subset is not None and not any(
            _is_subset(args_subset, tc.arguments) for tc in matches
        ):
            raise AssertionError(
                f"tool {name!r} was called, but no invocation matched "
                f"args_subset={args_subset!r}; observed arguments: "
                f"{[tc.arguments for tc in matches]!r}"
            )
        return self

    def no_tool_called(self, name: str | None = None) -> "TurnExpectation":
        """Assert that no tool ran this turn (or that ``name`` did not)."""
        if name is None:
            if self._result.tool_calls:
                raise AssertionError(
                    "expected no tool calls this turn; tools called: "
                    f"{[tc.name for tc in self._result.tool_calls]!r}"
                )
            return self
        offenders = [tc for tc in self._result.tool_calls if tc.name == name]
        if offenders:
            raise AssertionError(
                f"expected tool {name!r} NOT to be called this turn; it ran "
                f"{len(offenders)} time(s) with arguments "
                f"{[tc.arguments for tc in offenders]!r}"
            )
        return self

    # -- text -----------------------------------------------------------------

    def agent_text_contains(
        self, *needles: str, case_sensitive: bool = False
    ) -> "TurnExpectation":
        """Assert that every ``needle`` appears in the spoken agent text."""
        haystack = self._result.agent_text
        cmp_haystack = haystack if case_sensitive else haystack.lower()
        missing = [
            n
            for n in needles
            if (n if case_sensitive else n.lower()) not in cmp_haystack
        ]
        if missing:
            raise AssertionError(
                f"agent text is missing {missing!r}; agent said: {haystack!r}"
            )
        return self

    # -- semantic judge ---------------------------------------------------------

    async def judge(
        self,
        llm_judge: "LLMJudge",
        intent: str,
        *,
        rubric: str | None = None,
    ) -> JudgeResult:
        """Score this turn against ``intent`` with the existing LLM judge.

        Builds a synthetic :class:`EvalCase` whose ``expected_behavior`` is
        ``intent`` and judges the turn's full history snapshot. Raises
        ``AssertionError`` when the judge fails the turn; returns the
        :class:`JudgeResult` otherwise (chain-ending, async).
        """
        case = EvalCase(
            name="inline-judge",
            turns=(),
            expected_behavior=intent,
            rubric=rubric
            or f"Pass when the agent's behavior matches: {intent}",
        )
        transcript = self._result.transcript()
        verdict = await llm_judge.judge_case(case, transcript)
        logger.info(
            "judge intent=%r score=%.2f passed=%s",
            intent,
            verdict.score,
            verdict.passed,
        )
        if not verdict.passed:
            raise AssertionError(
                f"LLM judge failed the turn (score={verdict.score:.2f}): "
                f"{verdict.reasoning} — intent was {intent!r}; agent said "
                f"{self._result.agent_text!r}"
            )
        return verdict
