"""Unit tests for getpatter.services.metrics — CallMetricsAccumulator."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from getpatter.models import CostBreakdown, LatencyBreakdown, TurnMetrics
from getpatter.services.metrics import CallMetricsAccumulator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_accumulator(**kwargs) -> CallMetricsAccumulator:
    """Create a CallMetricsAccumulator with sensible defaults."""
    defaults = {
        "call_id": "test_call",
        "provider_mode": "pipeline",
        "telephony_provider": "twilio",
        "stt_provider": "deepgram",
        "tts_provider": "elevenlabs",
        "llm_provider": "custom",
        "pricing": None,
    }
    merged = {**defaults, **kwargs}
    return CallMetricsAccumulator(**merged)


def _record_turn(acc: CallMetricsAccumulator, user: str, agent: str) -> TurnMetrics:
    """Simulate a complete turn through the accumulator."""
    acc.start_turn()
    acc.record_stt_complete(user, audio_seconds=1.0)
    acc.record_llm_complete()
    acc.record_tts_first_byte()
    acc.record_tts_complete(agent)
    return acc.record_turn_complete(agent)


# ---------------------------------------------------------------------------
# Construction and initialization
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAccumulatorInit:
    """CallMetricsAccumulator initialization."""

    def test_fields_set(self) -> None:
        acc = _make_accumulator(call_id="c1")
        assert acc.call_id == "c1"
        assert acc.provider_mode == "pipeline"
        assert acc.telephony_provider == "twilio"

    def test_default_stt_format(self) -> None:
        acc = _make_accumulator()
        assert acc._stt_sample_rate == 16000
        assert acc._stt_bytes_per_sample == 2

    def test_configure_stt_format_mulaw(self) -> None:
        acc = _make_accumulator()
        acc.configure_stt_format(sample_rate=8000, bytes_per_sample=1)
        assert acc._stt_sample_rate == 8000
        assert acc._stt_bytes_per_sample == 1


# ---------------------------------------------------------------------------
# Turn lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTurnLifecycle:
    """Turn start/complete/interrupt tracking."""

    def test_complete_turn(self) -> None:
        acc = _make_accumulator()
        turn = _record_turn(acc, "Hello", "Hi there")
        assert turn.turn_index == 0
        assert turn.user_text == "Hello"
        assert turn.agent_text == "Hi there"

    def test_multiple_turns(self) -> None:
        acc = _make_accumulator()
        t1 = _record_turn(acc, "Hello", "Hi")
        t2 = _record_turn(acc, "How are you?", "I'm fine")
        assert t1.turn_index == 0
        assert t2.turn_index == 1

    def test_interrupted_turn(self) -> None:
        acc = _make_accumulator()
        acc.start_turn()
        acc.record_stt_complete("partial", audio_seconds=0.5)
        result = acc.record_turn_interrupted()
        assert result is not None
        assert result.agent_text == "[interrupted]"

    def test_interrupted_no_turn_in_progress(self) -> None:
        acc = _make_accumulator()
        result = acc.record_turn_interrupted()
        assert result is None

    def test_turn_state_reset_after_complete(self) -> None:
        acc = _make_accumulator()
        _record_turn(acc, "Hello", "Hi")
        assert acc._turn_start is None
        assert acc._stt_complete is None

    def test_tts_first_byte_only_recorded_once(self) -> None:
        acc = _make_accumulator()
        acc.start_turn()
        acc.record_stt_complete("text")
        acc.record_llm_complete()
        acc.record_tts_first_byte()
        first = acc._tts_first_byte
        acc.record_tts_first_byte()
        assert acc._tts_first_byte == first  # unchanged


# ---------------------------------------------------------------------------
# Latency calculation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLatencyCalculation:
    """Latency breakdown computation for individual turns."""

    def test_complete_latency_positive(self) -> None:
        acc = _make_accumulator()
        turn = _record_turn(acc, "Test", "Reply")
        # All latency values should be >= 0 (they measure real elapsed time)
        assert turn.latency.stt_ms >= 0
        assert turn.latency.llm_ms >= 0
        assert turn.latency.tts_ms >= 0
        assert turn.latency.total_ms >= 0

    def test_average_latency(self) -> None:
        acc = _make_accumulator()
        _record_turn(acc, "T1", "R1")
        _record_turn(acc, "T2", "R2")
        avg = acc._compute_average_latency()
        assert avg.stt_ms >= 0
        assert avg.total_ms >= 0

    def test_average_latency_no_turns(self) -> None:
        acc = _make_accumulator()
        avg = acc._compute_average_latency()
        assert avg == LatencyBreakdown()

    def test_p95_latency(self) -> None:
        acc = _make_accumulator()
        for i in range(20):
            _record_turn(acc, f"T{i}", f"R{i}")
        p95 = acc._compute_p95_latency()
        assert p95.stt_ms >= 0
        assert p95.total_ms >= 0

    def test_p95_latency_no_turns(self) -> None:
        acc = _make_accumulator()
        p95 = acc._compute_p95_latency()
        assert p95 == LatencyBreakdown()


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUsageTracking:
    """Audio byte and character accumulation."""

    def test_add_stt_audio_bytes(self) -> None:
        acc = _make_accumulator()
        acc.add_stt_audio_bytes(1000)
        acc.add_stt_audio_bytes(2000)
        assert acc._stt_byte_count == 3000

    def test_tts_characters_accumulate(self) -> None:
        acc = _make_accumulator()
        acc.record_tts_complete("Hello")  # 5 chars
        acc.record_tts_complete("World!")  # 6 chars
        assert acc._total_tts_characters == 11

    def test_stt_audio_seconds_from_bytes(self) -> None:
        """When no audio_seconds tracked per-turn, byte count is used."""
        acc = _make_accumulator()
        # PCM16 at 16kHz: 32000 bytes/sec
        acc.add_stt_audio_bytes(32000)  # 1 second
        metrics = acc.end_call()
        # Should have computed ~1 second of audio
        assert acc._total_stt_audio_seconds == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCostCalculation:
    """Cost breakdown computation for different provider modes."""

    def test_pipeline_mode_cost(self) -> None:
        acc = _make_accumulator(provider_mode="pipeline")
        acc._total_stt_audio_seconds = 60.0  # 1 minute
        acc._total_tts_characters = 1000     # 1k chars
        cost = acc._compute_cost(duration_seconds=60.0)
        assert cost.stt > 0
        assert cost.tts > 0
        assert cost.telephony > 0
        assert cost.total == pytest.approx(
            cost.stt + cost.tts + cost.llm + cost.telephony, abs=1e-6
        )

    def test_openai_realtime_mode_cost(self) -> None:
        acc = _make_accumulator(provider_mode="openai_realtime")
        acc._total_realtime_cost = 0.05
        cost = acc._compute_cost(duration_seconds=60.0)
        assert cost.llm == pytest.approx(0.05, abs=1e-6)
        assert cost.stt == 0.0
        assert cost.tts == 0.0

    def test_actual_telephony_cost_overrides_estimate(self) -> None:
        acc = _make_accumulator()
        acc.set_actual_telephony_cost(0.123)
        cost = acc._compute_cost(duration_seconds=600.0)
        assert cost.telephony == pytest.approx(0.123, abs=1e-6)

    def test_actual_stt_cost_overrides_estimate(self) -> None:
        acc = _make_accumulator(provider_mode="pipeline")
        acc.set_actual_stt_cost(0.042)
        acc._total_stt_audio_seconds = 600.0  # large value that would give different estimate
        cost = acc._compute_cost(duration_seconds=60.0)
        assert cost.stt == pytest.approx(0.042, abs=1e-6)

    def test_get_cost_so_far(self) -> None:
        acc = _make_accumulator()
        cost = acc.get_cost_so_far()
        assert isinstance(cost, CostBreakdown)


# ---------------------------------------------------------------------------
# end_call — final metrics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEndCall:
    """end_call() produces a frozen CallMetrics."""

    def test_produces_call_metrics(self) -> None:
        acc = _make_accumulator()
        _record_turn(acc, "Hello", "Hi")
        metrics = acc.end_call()
        assert metrics.call_id == "test_call"
        assert metrics.duration_seconds >= 0.0
        assert len(metrics.turns) == 1
        assert isinstance(metrics.cost, CostBreakdown)
        assert isinstance(metrics.latency_avg, LatencyBreakdown)
        assert isinstance(metrics.latency_p95, LatencyBreakdown)

    def test_end_call_no_turns(self) -> None:
        acc = _make_accumulator()
        metrics = acc.end_call()
        assert len(metrics.turns) == 0
        assert metrics.latency_avg == LatencyBreakdown()

    def test_realtime_usage_accumulation(self) -> None:
        acc = _make_accumulator(provider_mode="openai_realtime")
        acc.record_realtime_usage({
            "input_token_details": {"audio_tokens": 100, "text_tokens": 50},
            "output_token_details": {"audio_tokens": 200, "text_tokens": 100},
        })
        acc.record_realtime_usage({
            "input_token_details": {"audio_tokens": 100, "text_tokens": 50},
            "output_token_details": {"audio_tokens": 200, "text_tokens": 100},
        })
        assert acc._total_realtime_cost > 0
        metrics = acc.end_call()
        assert metrics.cost.llm > 0


# ---------------------------------------------------------------------------
# Concurrent write safety
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConcurrentSafety:
    """Two coroutines writing to the same accumulator."""

    async def test_concurrent_add_stt_bytes(self) -> None:
        """add_stt_audio_bytes from two concurrent coroutines."""
        import asyncio

        acc = _make_accumulator()

        async def add_bytes():
            for _ in range(100):
                acc.add_stt_audio_bytes(10)
                await asyncio.sleep(0)

        await asyncio.gather(add_bytes(), add_bytes())
        # 200 calls * 10 bytes = 2000
        assert acc._stt_byte_count == 2000
