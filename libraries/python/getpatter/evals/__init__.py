"""Patter evaluation framework.

Primitives:
- :class:`~getpatter.evals.case.EvalCase` — declarative description of a test case
- :class:`~getpatter.evals.runner.EvalRunner` — drives one or more cases
- :class:`~getpatter.evals.llm_judge.LLMJudge` — uses an LLM to score a transcript
  against a rubric

Real-pipeline harness (drives an actual ``PipelineStreamHandler`` — tools,
hooks, guardrails, barge-in state machine, history — with fake telephony /
STT / TTS at the boundary):

- :class:`~getpatter.evals.session.EvalSession` — ``async with
  EvalSession(agent=..., llm_provider=...) as s: await s.user_says("hi")``
- :class:`~getpatter.evals.session.ScriptedLLMProvider` (+
  :func:`~getpatter.evals.session.text_turn` /
  :func:`~getpatter.evals.session.tool_call_turn`) — deterministic LLM for CI
- :func:`~getpatter.evals.assertions.expect` — chainable assertions over a
  :class:`~getpatter.evals.session.TurnResult` (``tool_called``,
  ``no_tool_called``, ``agent_text_contains``, async ``judge``)

The session-layer names are loaded lazily (PEP 562) so importing
``getpatter.evals`` for the CLI / legacy runner stays light.
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
    # Real-pipeline harness (lazy):
    "EvalSession",
    "TurnResult",
    "ToolCallRecord",
    "FakeAudioSender",
    "FakeSTT",
    "FakeTTS",
    "ScriptedLLMProvider",
    "text_turn",
    "tool_call_turn",
    "expect",
    "TurnExpectation",
]

_SESSION_EXPORTS = frozenset(
    {
        "EvalSession",
        "TurnResult",
        "ToolCallRecord",
        "FakeAudioSender",
        "FakeSTT",
        "FakeTTS",
        "ScriptedLLMProvider",
        "text_turn",
        "tool_call_turn",
    }
)
_ASSERTION_EXPORTS = frozenset({"expect", "TurnExpectation"})


def __getattr__(name: str):
    """Lazy-load the heavy session/assertions modules on first access."""
    if name in _SESSION_EXPORTS:
        from getpatter.evals import session

        return getattr(session, name)
    if name in _ASSERTION_EXPORTS:
        from getpatter.evals import assertions

        return getattr(assertions, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
