"""Patter evaluation framework.

Primitives:
- :class:`~getpatter.evals.case.EvalCase` — declarative description of a test case
- :class:`~getpatter.evals.runner.EvalRunner` — drives one or more cases
- :class:`~getpatter.evals.llm_judge.LLMJudge` — uses an LLM to score a transcript
  against a rubric
"""

from getpatter.evals.case import EvalCase, EvalTurn, JudgeResult, EvalResult
from getpatter.evals.llm_judge import LLMJudge
from getpatter.evals.runner import EvalRunner, EvalSuite

__all__ = [
    "EvalCase",
    "EvalTurn",
    "JudgeResult",
    "EvalResult",
    "LLMJudge",
    "EvalRunner",
    "EvalSuite",
]
