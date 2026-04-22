"""Eval runner — executes an :class:`EvalSuite` against a scripted agent."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from getpatter.evals.case import EvalCase, EvalResult, EvalTurn
from getpatter.evals.llm_judge import LLMJudge

logger = logging.getLogger("getpatter.evals")

# An agent factory takes no arguments and returns an object with an async
# ``reply(text: str) -> str`` method. This decouples the runner from the real
# Patter Agent wiring (which is owned by handlers) and lets callers plug in
# any chat-completions client or mock. ``reply`` receives one user turn and
# returns the agent's final text response.
AgentCallable = Callable[[str], Awaitable[str]]
AgentFactory = Callable[[], AgentCallable]


@dataclass(frozen=True)
class EvalSuite:
    """A named collection of :class:`EvalCase` to run together."""

    name: str
    cases: list[EvalCase]
    metadata: dict[str, Any] = field(default_factory=dict)


class EvalRunner:
    """Drives one or more cases against an agent and produces a JSON report.

    Usage::

        async def my_agent_factory() -> Callable[[str], Awaitable[str]]:
            bot = MyBot()
            return bot.reply

        suite = load_suite(Path("eval/suite.yaml"))
        runner = EvalRunner(judge=LLMJudge())
        results = await runner.run(suite, my_agent_factory)
        print(runner.report(suite, results))
    """

    def __init__(self, judge: LLMJudge | None = None) -> None:
        self.judge = judge or LLMJudge()

    async def run(
        self, suite: EvalSuite, agent_factory: AgentFactory
    ) -> list[EvalResult]:
        """Run every case in ``suite`` sequentially."""
        results: list[EvalResult] = []
        for case in suite.cases:
            result = await self.run_case(case, agent_factory)
            results.append(result)
        return results

    async def run_case(
        self, case: EvalCase, agent_factory: AgentFactory
    ) -> EvalResult:
        """Run a single case and return its :class:`EvalResult`."""
        start = time.monotonic()
        transcript: list[dict[str, str]] = []
        error: str | None = None

        try:
            agent = agent_factory()
            if not callable(agent):
                # Factories that return async may need to be awaited.
                if hasattr(agent, "__await__"):
                    agent = await agent  # type: ignore[assignment]

            if case.first_message:
                transcript.append({"role": "agent", "text": case.first_message})

            for turn in case.turns:
                transcript.append({"role": "user", "text": turn.user})
                reply = await agent(turn.user) if callable(agent) else ""
                transcript.append({"role": "agent", "text": reply or ""})

                # Cheap pre-filter — if a required substring is missing we
                # still let the judge decide, but log for easier debugging.
                for needle in turn.expected_contains:
                    if needle.lower() not in (reply or "").lower():
                        logger.info(
                            "case=%r expected_contains=%r missing in reply", case.name, needle
                        )
        except Exception as exc:  # noqa: BLE001 - we need catch-all here
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("case=%r raised", case.name)

        # If we failed to produce any transcript, skip the judge.
        if error and not transcript:
            duration = time.monotonic() - start
            from getpatter.evals.case import JudgeResult

            return EvalResult(
                case_name=case.name,
                transcript=transcript,
                judge=JudgeResult(score=0.0, passed=False, reasoning=error),
                duration_s=duration,
                error=error,
            )

        judge_result = await self.judge.judge_case(case, transcript)
        duration = time.monotonic() - start
        return EvalResult(
            case_name=case.name,
            transcript=transcript,
            judge=judge_result,
            duration_s=duration,
            error=error,
        )

    def report(self, suite: EvalSuite, results: list[EvalResult]) -> str:
        """Render a JSON report suitable for CI artefacts."""
        total = len(results)
        passed = sum(1 for r in results if r.judge.passed)
        payload = {
            "suite": suite.name,
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": (passed / total) if total else 0.0,
            "cases": [r.to_dict() for r in results],
        }
        return json.dumps(payload, indent=2)


def load_suite(path: Path) -> EvalSuite:
    """Load a suite from YAML or JSON.

    Schema (YAML)::

        name: "customer support v1"
        cases:
          - name: "greeting is warm"
            expected_behavior: "Agent greets the caller warmly and asks how it can help."
            rubric: "Pass if reply contains a greeting and an open-ended question."
            turns:
              - user: "hi"
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Loading YAML suites requires 'pyyaml'. "
                "Install with: pip install getpatter[evals]"
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)

    if not isinstance(data, dict):
        raise ValueError(f"Eval suite {path} must be a mapping, got {type(data).__name__}")

    cases_raw = data.get("cases", [])
    if not isinstance(cases_raw, list):
        raise ValueError(f"Eval suite {path}: 'cases' must be a list")

    cases: list[EvalCase] = []
    for i, c in enumerate(cases_raw):
        if not isinstance(c, dict):
            raise ValueError(f"Eval suite {path}: case {i} must be a mapping")
        turns_raw = c.get("turns", [])
        turns = [
            EvalTurn(
                user=str(t.get("user", "")),
                expected_contains=list(t.get("expected_contains", []) or []),
            )
            for t in turns_raw
            if isinstance(t, dict)
        ]
        cases.append(
            EvalCase(
                name=str(c.get("name", f"case_{i}")),
                turns=turns,
                expected_behavior=str(c.get("expected_behavior", "")),
                rubric=str(c.get("rubric", "")),
                tags=list(c.get("tags", []) or []),
                first_message=str(c.get("first_message", "")),
            )
        )

    return EvalSuite(
        name=str(data.get("name", path.stem)),
        cases=cases,
        metadata=dict(data.get("metadata", {}) or {}),
    )
