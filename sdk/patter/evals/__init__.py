"""Patter evaluation framework.

Primitives:
- :class:`~patter.evals.case.EvalCase` — declarative description of a test case
- :class:`~patter.evals.runner.EvalRunner` — drives one or more cases
- :class:`~patter.evals.llm_judge.LLMJudge` — uses an LLM to score a transcript
  against a rubric

Inspired by LiveKit Agents' evals module (Apache 2.0) but not a direct port —
LiveKit's primitives depend on LiveKit-specific room/session abstractions that
do not apply to Patter's handler model.
"""

from patter.evals.case import EvalCase, EvalTurn, JudgeResult, EvalResult
from patter.evals.llm_judge import LLMJudge
from patter.evals.runner import EvalRunner, EvalSuite

__all__ = [
    "EvalCase",
    "EvalTurn",
    "JudgeResult",
    "EvalResult",
    "LLMJudge",
    "EvalRunner",
    "EvalSuite",
]
