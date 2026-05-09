"""Unit tests for ``getpatter.services.barge_in_strategies``."""

from __future__ import annotations

import pytest

from getpatter.services.barge_in_strategies import (
    BargeInStrategy,
    MinWordsStrategy,
    evaluate_strategies,
    reset_strategies,
)


class TestMinWordsStrategy:
    def test_init_rejects_min_words_below_one(self) -> None:
        with pytest.raises(ValueError):
            MinWordsStrategy(min_words=0)
        with pytest.raises(ValueError):
            MinWordsStrategy(min_words=-3)

    async def test_one_word_confirms_when_agent_silent(self) -> None:
        s = MinWordsStrategy(min_words=3)
        # Agent not speaking → 1 word is enough; we don't want to delay
        # the very first user turn just because the strategy is configured.
        assert (
            await s.evaluate(transcript="hi", is_interim=False, agent_speaking=False)
            is True
        )

    async def test_below_threshold_during_agent_speech_does_not_confirm(self) -> None:
        s = MinWordsStrategy(min_words=3)
        assert (
            await s.evaluate(transcript="okay", is_interim=False, agent_speaking=True)
            is False
        )
        assert (
            await s.evaluate(transcript="uh huh", is_interim=False, agent_speaking=True)
            is False
        )

    async def test_meets_threshold_during_agent_speech_confirms(self) -> None:
        s = MinWordsStrategy(min_words=3)
        assert (
            await s.evaluate(
                transcript="please stop talking",
                is_interim=False,
                agent_speaking=True,
            )
            is True
        )
        assert (
            await s.evaluate(
                transcript="hold on a moment please",
                is_interim=False,
                agent_speaking=True,
            )
            is True
        )

    async def test_use_interim_false_ignores_partials(self) -> None:
        s = MinWordsStrategy(min_words=2, use_interim=False)
        # An interim with enough words is not enough — still wait for final.
        assert (
            await s.evaluate(
                transcript="please stop", is_interim=True, agent_speaking=True
            )
            is False
        )
        # The final partial of the same utterance confirms.
        assert (
            await s.evaluate(
                transcript="please stop", is_interim=False, agent_speaking=True
            )
            is True
        )

    async def test_word_count_uses_whitespace_split(self) -> None:
        s = MinWordsStrategy(min_words=2)
        # Multiple spaces, leading/trailing whitespace, tabs collapse correctly.
        assert (
            await s.evaluate(
                transcript="   hello   world   ",
                is_interim=False,
                agent_speaking=True,
            )
            is True
        )
        assert (
            await s.evaluate(
                transcript="\thello\n",
                is_interim=False,
                agent_speaking=True,
            )
            is False
        )

    async def test_empty_transcript_does_not_confirm_during_agent_speech(self) -> None:
        s = MinWordsStrategy(min_words=2)
        assert (
            await s.evaluate(transcript="", is_interim=False, agent_speaking=True)
            is False
        )

    async def test_protocol_runtime_check(self) -> None:
        # Sanity: MinWordsStrategy structurally satisfies the Protocol.
        assert isinstance(MinWordsStrategy(min_words=2), BargeInStrategy)


class _RecordingStrategy:
    """Test double that records every call and returns a configurable result."""

    def __init__(self, *, returns: bool) -> None:
        self._returns = returns
        self.calls: list[dict] = []
        self.resets = 0

    async def evaluate(
        self, *, transcript: str, is_interim: bool, agent_speaking: bool
    ) -> bool:
        self.calls.append(
            {
                "transcript": transcript,
                "is_interim": is_interim,
                "agent_speaking": agent_speaking,
            }
        )
        return self._returns

    async def reset(self) -> None:
        self.resets += 1


class TestEvaluateStrategies:
    async def test_empty_tuple_returns_false(self) -> None:
        assert (
            await evaluate_strategies(
                (), transcript="anything", is_interim=False, agent_speaking=True
            )
            is False
        )

    async def test_first_true_short_circuits(self) -> None:
        a = _RecordingStrategy(returns=True)
        b = _RecordingStrategy(returns=False)
        result = await evaluate_strategies(
            (a, b),
            transcript="please stop",
            is_interim=False,
            agent_speaking=True,
        )
        assert result is True
        assert len(a.calls) == 1
        # Short-circuit: ``b`` MUST NOT be invoked once ``a`` confirmed.
        assert b.calls == []

    async def test_all_false_returns_false(self) -> None:
        a = _RecordingStrategy(returns=False)
        b = _RecordingStrategy(returns=False)
        result = await evaluate_strategies(
            (a, b), transcript="okay", is_interim=False, agent_speaking=True
        )
        assert result is False
        assert len(a.calls) == 1
        assert len(b.calls) == 1

    async def test_strategy_exception_is_swallowed(self) -> None:
        class _Boom:
            async def evaluate(
                self,
                *,
                transcript: str,
                is_interim: bool,
                agent_speaking: bool,
            ) -> bool:
                raise RuntimeError("boom")

            async def reset(self) -> None:
                return None

        ok = _RecordingStrategy(returns=True)
        # The crashing strategy must not abort the loop — the next strategy
        # should still get its turn and confirm.
        result = await evaluate_strategies(
            (_Boom(), ok),
            transcript="please stop talking",
            is_interim=False,
            agent_speaking=True,
        )
        assert result is True
        assert len(ok.calls) == 1


class TestResetStrategies:
    async def test_resets_each_strategy(self) -> None:
        a = _RecordingStrategy(returns=False)
        b = _RecordingStrategy(returns=False)
        await reset_strategies((a, b))
        assert a.resets == 1
        assert b.resets == 1

    async def test_swallows_per_strategy_errors(self) -> None:
        class _Boom:
            async def evaluate(
                self,
                *,
                transcript: str,
                is_interim: bool,
                agent_speaking: bool,
            ) -> bool:
                return False

            async def reset(self) -> None:
                raise RuntimeError("boom")

        ok = _RecordingStrategy(returns=False)
        # Must not raise even though the first strategy's reset blew up.
        await reset_strategies((_Boom(), ok))
        assert ok.resets == 1
