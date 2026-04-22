"""Eval case data model.

An :class:`EvalCase` is a scripted scenario: a sequence of user turns, an
expected-behavior description, and a rubric used by the judge LLM.

Designed to be loaded from a YAML/JSON suite file — see
:func:`getpatter.evals.runner.load_suite`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalTurn:
    """A single user utterance in a scripted conversation."""

    user: str
    # Optional: a regex the agent's reply must match — used as a cheap
    # pre-filter before invoking the LLM judge.
    expected_contains: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvalCase:
    """A complete evaluation scenario."""

    name: str
    turns: list[EvalTurn]
    expected_behavior: str
    rubric: str
    # Optional metadata for reporting/filtering.
    tags: list[str] = field(default_factory=list)
    # Optional first-message the agent should emit before any user turn.
    first_message: str = ""


@dataclass(frozen=True)
class JudgeResult:
    """The judge's verdict on one case."""

    score: float  # 0.0-1.0
    passed: bool
    reasoning: str


@dataclass(frozen=True)
class EvalResult:
    """The result of running a single :class:`EvalCase`."""

    case_name: str
    transcript: list[dict[str, str]]  # [{"role": "user"|"agent", "text": ...}]
    judge: JudgeResult
    duration_s: float
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "case": self.case_name,
            "score": self.judge.score,
            "passed": self.judge.passed,
            "reasoning": self.judge.reasoning,
            "transcript": list(self.transcript),
            "duration_s": round(self.duration_s, 3),
            "error": self.error,
        }
