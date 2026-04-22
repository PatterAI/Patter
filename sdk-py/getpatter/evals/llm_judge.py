"""LLM-as-judge scoring for eval cases."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from getpatter.evals.case import EvalCase, JudgeResult

logger = logging.getLogger("getpatter.evals")


_JUDGE_SYSTEM = (
    "You are a strict but fair evaluator of voice-AI agents. "
    "You will be given: (1) the expected behavior for the agent, (2) a rubric, "
    "(3) a transcript of the conversation. "
    "Return a JSON object with exactly three keys:\n"
    "  - \"score\": float between 0.0 and 1.0\n"
    "  - \"passed\": boolean (true when score >= threshold)\n"
    "  - \"reasoning\": short string explaining the score\n"
    "Do not return any text outside the JSON object."
)


class LLMJudge:
    """Scores conversation transcripts against a rubric via an OpenAI model.

    The judge is intentionally provider-specific (OpenAI chat completions)
    because reliability of structured JSON output matters more than provider
    flexibility for evals. Callers who need a different backend can implement
    a ``JudgeBackend`` and inject it via the ``backend`` argument.
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        pass_threshold: float = 0.7,
        backend: Any = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.pass_threshold = pass_threshold
        self._backend = backend  # for tests — any object exposing ``judge(prompt)``.

    async def judge_case(
        self, case: EvalCase, transcript: list[dict[str, str]]
    ) -> JudgeResult:
        """Return a :class:`JudgeResult` for the given transcript."""
        prompt = self._build_prompt(case, transcript)
        if self._backend is not None:
            raw = await self._backend.judge(prompt)
        else:
            raw = await self._call_openai(prompt)
        return self._parse(raw)

    def _build_prompt(
        self, case: EvalCase, transcript: list[dict[str, str]]
    ) -> str:
        lines = [
            f"EXPECTED BEHAVIOR: {case.expected_behavior}",
            f"RUBRIC: {case.rubric}",
            f"PASS THRESHOLD: {self.pass_threshold}",
            "TRANSCRIPT:",
        ]
        for turn in transcript:
            lines.append(f"  {turn.get('role', '?')}: {turn.get('text', '')}")
        return "\n".join(lines)

    async def _call_openai(self, prompt: str) -> str:
        """Call OpenAI chat completions — lazy import to keep extras optional."""
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "LLMJudge requires the 'openai' package. "
                "Install with: pip install getpatter[evals]"
            ) from exc

        client = AsyncOpenAI(api_key=self.api_key) if self.api_key else AsyncOpenAI()
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        return response.choices[0].message.content or "{}"

    def _parse(self, raw: str) -> JudgeResult:
        """Parse the judge's JSON — tolerant of extra whitespace / code fences."""
        text = raw.strip()
        # Strip a leading fence if the model added one despite json_object mode.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("LLMJudge: invalid JSON, defaulting to fail: %r", raw)
            return JudgeResult(
                score=0.0, passed=False, reasoning=f"Judge returned invalid JSON: {raw[:200]}"
            )
        score_raw = data.get("score", 0.0)
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        passed = bool(data.get("passed", score >= self.pass_threshold))
        reasoning = str(data.get("reasoning", ""))
        return JudgeResult(score=score, passed=passed, reasoning=reasoning)
