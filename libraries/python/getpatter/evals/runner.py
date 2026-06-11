"""Eval runner — executes an :class:`EvalSuite` against a scripted agent.

Two execution paths per case:

* **Legacy** (default): the caller supplies an ``agent_factory`` returning an
  async ``reply(text) -> str`` callable — no SDK machinery involved.
* **Real pipeline**: when :class:`~getpatter.evals.case.EvalCase` carries an
  ``agent=`` (a :class:`getpatter.models.Agent`, optionally with
  ``llm_provider=``), the runner drives the case through
  :class:`~getpatter.evals.session.EvalSession` — the real
  ``PipelineStreamHandler`` call loop with tools, hooks, guardrails, and
  history handling. Example::

      case = EvalCase(
          name="books a table",
          turns=(EvalTurn(user="table for two at eight"),),
          expected_behavior="Agent books and confirms the table.",
          rubric="Pass if a booking is confirmed.",
          agent=my_agent,                      # real Agent under test
          llm_provider=ScriptedLLMProvider([...]),  # or a real provider
      )
      results = await EvalRunner(judge=judge).run(
          EvalSuite(name="s", cases=(case,))
      )
"""

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
    cases: tuple[EvalCase, ...]
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
        self, suite: EvalSuite, agent_factory: AgentFactory | None = None
    ) -> list[EvalResult]:
        """Run every case in ``suite`` sequentially.

        ``agent_factory`` is required only for cases that do NOT carry their
        own ``agent=`` (the legacy ``reply()`` path).
        """
        results: list[EvalResult] = []
        for case in suite.cases:
            result = await self.run_case(case, agent_factory)
            results.append(result)
        return results

    async def run_case(
        self, case: EvalCase, agent_factory: AgentFactory | None = None
    ) -> EvalResult:
        """Run a single case and return its :class:`EvalResult`.

        Routes through the real-pipeline :class:`EvalSession` when
        ``case.agent`` is set; otherwise uses the legacy ``reply()``-callable
        ``agent_factory`` (unchanged behaviour).
        """
        start = time.monotonic()
        transcript: list[dict[str, str]] = []
        error: str | None = None

        try:
            if case.agent is not None:
                await self._run_turns_with_session(case, transcript)
            else:
                if agent_factory is None:
                    raise ValueError(
                        f"case {case.name!r} has no agent= and no "
                        "agent_factory was supplied"
                    )
                await self._run_turns_with_reply(case, agent_factory, transcript)
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

        try:
            judge_result = await self.judge.judge_case(case, transcript)
        except Exception as exc:  # noqa: BLE001 - network call to the judge LLM
            # One transient judge failure (429, timeout, missing key) used to
            # abort the WHOLE suite, discarding every completed case.
            duration = time.monotonic() - start
            from getpatter.evals.case import JudgeResult

            return EvalResult(
                case_name=case.name,
                transcript=transcript,
                judge=JudgeResult(
                    score=0.0, passed=False, reasoning=f"judge error: {exc}"
                ),
                duration_s=duration,
                error=f"judge error: {exc}",
            )
        duration = time.monotonic() - start
        return EvalResult(
            case_name=case.name,
            transcript=transcript,
            judge=judge_result,
            duration_s=duration,
            error=error,
        )

    async def _run_turns_with_reply(
        self,
        case: EvalCase,
        agent_factory: AgentFactory,
        transcript: list[dict[str, str]],
    ) -> None:
        """Legacy path — drives the case against a ``reply()`` callable.

        Appends into ``transcript`` in place so a mid-case exception still
        leaves the partial transcript for the judge (existing semantics).
        """
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
            self._log_missing_expected(case, turn, reply or "")

    async def _run_turns_with_session(
        self, case: EvalCase, transcript: list[dict[str, str]]
    ) -> None:
        """Real-pipeline path — drives the case through ``EvalSession``.

        The agent's REAL handler emits its own ``first_message`` (a
        ``case.first_message`` overrides the agent's), tools/hooks/guardrails
        run for real, and the transcript mirrors what the pipeline actually
        said. Appends into ``transcript`` in place (partial-on-error, same as
        the legacy path).
        """
        import dataclasses

        # Local import keeps `getpatter.evals.runner` light for the CLI —
        # the session module pulls in the stream handler.
        from getpatter.evals.session import EvalSession

        agent = case.agent
        if case.first_message:
            agent = dataclasses.replace(agent, first_message=case.first_message)

        async with EvalSession(agent=agent, llm_provider=case.llm_provider) as s:
            first_message = getattr(agent, "first_message", "")
            if first_message:
                transcript.append({"role": "agent", "text": first_message})
            for turn in case.turns:
                transcript.append({"role": "user", "text": turn.user})
                result = await s.user_says(turn.user)
                transcript.append({"role": "agent", "text": result.agent_text})
                self._log_missing_expected(case, turn, result.agent_text)

    @staticmethod
    def _log_missing_expected(case: EvalCase, turn: EvalTurn, reply: str) -> None:
        """Cheap pre-filter — if a required substring is missing we still let
        the judge decide, but log for easier debugging."""
        for needle in turn.expected_contains:
            if needle.lower() not in reply.lower():
                logger.info(
                    "case=%r expected_contains=%r missing in reply",
                    case.name,
                    needle,
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
        raise ValueError(
            f"Eval suite {path} must be a mapping, got {type(data).__name__}"
        )

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
        cases=tuple(cases),
        metadata=dict(data.get("metadata", {}) or {}),
    )
