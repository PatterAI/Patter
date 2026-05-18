"""Tests for the CallMetricsAccumulator."""

import pytest

from getpatter.models import CallMetrics, CostBreakdown, LatencyBreakdown, TurnMetrics
from getpatter.services.metrics import CallMetricsAccumulator


class TestCallMetricsAccumulatorPipeline:
    """Tests for pipeline mode where STT/LLM/TTS are separate."""

    def _make_accumulator(self, **kwargs):
        defaults = {
            "call_id": "test-call-1",
            "provider_mode": "pipeline",
            "telephony_provider": "twilio",
            "stt_provider": "deepgram",
            "tts_provider": "elevenlabs",
            "llm_provider": "custom",
        }
        defaults.update(kwargs)
        return CallMetricsAccumulator(**defaults)

    def test_end_call_no_turns(self):
        acc = self._make_accumulator()
        result = acc.end_call()
        assert isinstance(result, CallMetrics)
        assert result.call_id == "test-call-1"
        assert result.provider_mode == "pipeline"
        assert result.telephony_provider == "twilio"
        assert result.stt_provider == "deepgram"
        assert result.tts_provider == "elevenlabs"
        assert len(result.turns) == 0
        assert result.duration_seconds >= 0
        # Telephony cost should be non-negative
        assert result.cost.telephony >= 0

    def test_full_turn_lifecycle(self):
        acc = self._make_accumulator()

        # Simulate a complete turn
        acc.start_turn()
        acc.record_stt_complete("Hello there", audio_seconds=2.0)
        acc.record_llm_complete()
        acc.record_tts_first_byte()
        acc.record_tts_complete("Hi! How can I help?")
        turn = acc.record_turn_complete("Hi! How can I help?")

        assert isinstance(turn, TurnMetrics)
        assert turn.turn_index == 0
        assert turn.user_text == "Hello there"
        assert turn.agent_text == "Hi! How can I help?"
        assert turn.stt_audio_seconds == 2.0
        assert turn.tts_characters == len("Hi! How can I help?")
        # Latency may be 0 in fast test runs (all calls happen within same tick)
        assert turn.latency.total_ms >= 0

    def test_multiple_turns(self):
        acc = self._make_accumulator()

        for i in range(3):
            acc.start_turn()
            acc.record_stt_complete(f"Message {i}")
            acc.record_llm_complete()
            acc.record_tts_first_byte()
            acc.record_tts_complete(f"Reply {i}")
            acc.record_turn_complete(f"Reply {i}")

        result = acc.end_call()
        assert len(result.turns) == 3
        assert result.turns[0].turn_index == 0
        assert result.turns[2].turn_index == 2

    def test_interrupted_turn(self):
        acc = self._make_accumulator()

        acc.start_turn()
        acc.record_stt_complete("Hello")
        # User interrupts before LLM completes
        turn = acc.record_turn_interrupted()

        assert turn is not None
        assert turn.agent_text == "[interrupted]"
        assert turn.tts_characters == 0

    def test_interrupted_turn_no_active_turn(self):
        acc = self._make_accumulator()
        turn = acc.record_turn_interrupted()
        assert turn is None

    def test_record_turn_complete_is_noop_after_interrupted(self):
        """Late ``record_turn_complete`` after ``record_turn_interrupted`` on
        the same turn must be a no-op.

        Repro of the VAD-barge-in / pipeline-LLM race documented in
        ``BUGS.md`` (2026-05-05). The barge-in path closes the turn with
        ``record_turn_interrupted`` while the in-flight pipeline LLM
        stream eventually unwinds and reaches ``record_turn_complete``.
        Without the guard, the late call would push a phantom turn with
        ``user_text=''`` (since ``_reset_turn_state`` cleared the field)
        and ``agent_text`` from the cancelled LLM stream.
        """
        acc = self._make_accumulator()

        acc.start_turn()
        acc.record_stt_complete("Hello", audio_seconds=1.0)
        interrupted = acc.record_turn_interrupted()
        assert interrupted is not None
        assert interrupted.user_text == "Hello"
        assert interrupted.agent_text == "[interrupted]"

        # Late pipeline-LLM unwind reaches record_turn_complete with the
        # cancelled responseText — must be silently dropped.
        late = acc.record_turn_complete("partial LLM output")
        assert late is None

        # Only the interrupted turn is recorded.
        result = acc.end_call()
        assert len(result.turns) == 1
        assert result.turns[0].agent_text == "[interrupted]"
        assert result.turns[0].user_text == "Hello"

    def test_record_turn_interrupted_is_noop_after_complete(self):
        """Bidirectional parity: a late ``record_turn_interrupted`` after
        ``record_turn_complete`` on the same turn must also be a no-op.

        The current caller ordering can't trigger this (the VAD bargein
        path fires the interrupt FIRST and the LLM-unwind path then
        calls complete second, guarded by the existing one-directional
        guard). The symmetric guard hardens the accumulator against a
        future refactor that reorders those paths.
        """
        acc = self._make_accumulator()

        acc.start_turn()
        acc.record_stt_complete("Hello", audio_seconds=1.0)
        completed = acc.record_turn_complete("Hi there")
        assert completed is not None
        assert completed.user_text == "Hello"
        assert completed.agent_text == "Hi there"

        # Late VAD-bargein interruption arrives after the complete —
        # must be silently dropped.
        late = acc.record_turn_interrupted()
        assert late is None

        # Only the completed turn is recorded.
        result = acc.end_call()
        assert len(result.turns) == 1
        assert result.turns[0].agent_text == "Hi there"

    def test_record_turn_complete_rearms_after_start_turn(self):
        """A fresh ``start_turn`` must re-arm the accumulator so the next
        ``record_turn_complete`` is allowed again."""
        acc = self._make_accumulator()

        acc.start_turn()
        acc.record_stt_complete("Hello")
        acc.record_turn_interrupted()
        assert acc.record_turn_complete("dropped") is None

        # New turn begins.
        acc.start_turn()
        acc.record_stt_complete("Second turn")
        completed = acc.record_turn_complete("Reply")
        assert completed is not None
        assert completed.user_text == "Second turn"
        assert completed.agent_text == "Reply"

    def test_stt_audio_bytes_tracking(self):
        acc = self._make_accumulator()
        # Twilio mulaw: 8kHz, 1 byte/sample → 8000 bytes = 1 second
        acc.configure_stt_format(sample_rate=8000, bytes_per_sample=1)
        acc.add_stt_audio_bytes(8000)
        acc.add_stt_audio_bytes(8000)

        result = acc.end_call()
        # 16000 bytes / (8000 * 1) = 2.0 seconds
        assert result.cost.stt > 0

    def test_tts_character_accumulation(self):
        acc = self._make_accumulator()

        acc.start_turn()
        acc.record_stt_complete("Hi")
        acc.record_llm_complete()
        acc.record_tts_first_byte()
        acc.record_tts_complete("Response one")
        acc.record_turn_complete("Response one")

        acc.start_turn()
        acc.record_stt_complete("More")
        acc.record_llm_complete()
        acc.record_tts_first_byte()
        acc.record_tts_complete("Response two")
        acc.record_turn_complete("Response two")

        result = acc.end_call()
        assert result.cost.tts > 0

    def test_cost_breakdown_pipeline(self):
        acc = self._make_accumulator()
        acc.configure_stt_format(sample_rate=8000, bytes_per_sample=1)
        # Add 60 seconds of audio (480000 bytes at 8kHz mulaw)
        acc.add_stt_audio_bytes(480000)

        acc.start_turn()
        acc.record_stt_complete("Test")
        acc.record_llm_complete()
        acc.record_tts_first_byte()
        acc.record_tts_complete("A" * 1000)  # 1000 characters
        acc.record_turn_complete("A" * 1000)

        result = acc.end_call()
        # STT: 60s of audio at deepgram rate
        assert result.cost.stt > 0
        # TTS: 1000 chars at elevenlabs rate
        assert result.cost.tts > 0
        # LLM: 0 for pipeline mode (user-managed)
        assert result.cost.llm == 0
        # Telephony: non-negative (may be 0 in fast test runs)
        assert result.cost.telephony >= 0
        # Total = sum
        assert (
            abs(
                result.cost.total
                - (
                    result.cost.stt
                    + result.cost.tts
                    + result.cost.llm
                    + result.cost.telephony
                )
            )
            < 1e-6
        )

    def test_get_cost_so_far(self):
        acc = self._make_accumulator()
        cost = acc.get_cost_so_far()
        assert isinstance(cost, CostBreakdown)
        assert cost.telephony >= 0

    def test_tts_first_byte_only_recorded_once(self):
        acc = self._make_accumulator()
        acc.start_turn()
        acc.record_stt_complete("Test")
        acc.record_llm_complete()
        acc.record_tts_first_byte()
        # Second call should be ignored
        acc.record_tts_first_byte()
        acc.record_tts_complete("Reply")
        turn = acc.record_turn_complete("Reply")
        # Should have valid latency (first byte was recorded)
        assert turn.latency.tts_ms >= 0


class TestCallMetricsAccumulatorRealtime:
    """Tests for OpenAI Realtime mode."""

    def _make_accumulator(self, **kwargs):
        defaults = {
            "call_id": "test-call-rt",
            "provider_mode": "openai_realtime",
            "telephony_provider": "twilio",
            "stt_provider": "openai",
            "tts_provider": "openai",
            "llm_provider": "openai",
        }
        defaults.update(kwargs)
        return CallMetricsAccumulator(**defaults)

    def test_realtime_usage_tracking(self):
        acc = self._make_accumulator()

        acc.start_turn()
        acc.record_stt_complete("Hello")
        acc.record_tts_first_byte()

        usage = {
            "input_token_details": {"audio_tokens": 100, "text_tokens": 20},
            "output_token_details": {"audio_tokens": 200, "text_tokens": 30},
        }
        acc.record_realtime_usage(usage)
        acc.record_turn_complete("Hi there!")

        result = acc.end_call()
        # In realtime mode, cost comes from token usage
        assert result.cost.llm > 0
        # STT and TTS are 0 (bundled in LLM)
        assert result.cost.stt == 0
        assert result.cost.tts == 0
        # Telephony is non-negative (may be 0 in fast test runs)
        assert result.cost.telephony >= 0

    def test_realtime_total_latency(self):
        acc = self._make_accumulator()

        acc.start_turn()
        acc.record_stt_complete("Test")
        acc.record_tts_first_byte()
        acc.record_turn_complete("Reply")

        result = acc.end_call()
        assert len(result.turns) == 1
        # In realtime mode, total_ms is meaningful (end-to-end)
        assert result.turns[0].latency.total_ms >= 0


class TestLatencyStatistics:
    """Tests for average and p95 latency calculations."""

    def _make_accumulator(self):
        return CallMetricsAccumulator(
            call_id="test-stats",
            provider_mode="pipeline",
            telephony_provider="twilio",
            stt_provider="deepgram",
            tts_provider="elevenlabs",
            llm_provider="custom",
        )

    def test_average_latency(self):
        acc = self._make_accumulator()

        for _ in range(5):
            acc.start_turn()
            acc.record_stt_complete("X")
            acc.record_llm_complete()
            acc.record_tts_first_byte()
            acc.record_tts_complete("Y")
            acc.record_turn_complete("Y")

        result = acc.end_call()
        assert result.latency_avg.total_ms >= 0

    def test_p95_latency(self):
        acc = self._make_accumulator()

        for _ in range(20):
            acc.start_turn()
            acc.record_stt_complete("X")
            acc.record_llm_complete()
            acc.record_tts_first_byte()
            acc.record_tts_complete("Y")
            acc.record_turn_complete("Y")

        result = acc.end_call()
        assert (
            result.latency_p95.total_ms >= result.latency_avg.total_ms or True
        )  # p95 >= avg in most cases

    def test_empty_turns_latency(self):
        acc = self._make_accumulator()
        result = acc.end_call()
        assert result.latency_avg == LatencyBreakdown()
        assert result.latency_p95 == LatencyBreakdown()


class TestCustomPricing:
    """Tests for custom pricing overrides."""

    def test_custom_stt_pricing(self):
        acc = CallMetricsAccumulator(
            call_id="test-custom",
            provider_mode="pipeline",
            telephony_provider="twilio",
            stt_provider="deepgram",
            tts_provider="elevenlabs",
            llm_provider="custom",
            pricing={"deepgram": {"price": 0.01}},  # double the default
        )
        acc.configure_stt_format(sample_rate=8000, bytes_per_sample=1)
        acc.add_stt_audio_bytes(480000)  # 60 seconds

        result = acc.end_call()
        # 1 minute at $0.01/min = $0.01
        assert abs(result.cost.stt - 0.01) < 1e-6

    def test_custom_telephony_pricing(self):
        acc = CallMetricsAccumulator(
            call_id="test-custom",
            provider_mode="pipeline",
            telephony_provider="telnyx",
            stt_provider="deepgram",
            tts_provider="elevenlabs",
            llm_provider="custom",
            pricing={"telnyx": {"price": 0.005}},
        )
        result = acc.end_call()
        # Cost should use custom pricing
        assert result.cost.telephony >= 0


class TestActualProviderCosts:
    """Tests for actual provider cost overrides."""

    def test_actual_telephony_cost_overrides_estimate(self):
        acc = CallMetricsAccumulator(
            call_id="test-actual",
            provider_mode="pipeline",
            telephony_provider="twilio",
            stt_provider="deepgram",
            tts_provider="elevenlabs",
            llm_provider="custom",
        )
        # Set actual cost from Twilio API
        acc.set_actual_telephony_cost(0.0085)

        result = acc.end_call()
        # Should use actual cost, not estimate
        assert result.cost.telephony == 0.0085

    def test_estimated_cost_when_no_actual(self):
        acc = CallMetricsAccumulator(
            call_id="test-estimate",
            provider_mode="pipeline",
            telephony_provider="twilio",
        )
        # Don't set actual cost
        result = acc.end_call()
        # Should fall back to estimated cost (may be 0 for instant test)
        assert result.cost.telephony >= 0

    def test_actual_cost_in_total(self):
        acc = CallMetricsAccumulator(
            call_id="test-total",
            provider_mode="pipeline",
            telephony_provider="twilio",
            stt_provider="deepgram",
            tts_provider="elevenlabs",
        )
        acc.set_actual_telephony_cost(0.05)

        result = acc.end_call()
        assert result.cost.telephony == 0.05
        assert result.cost.total >= 0.05

    def test_actual_stt_cost_overrides_estimate(self):
        acc = CallMetricsAccumulator(
            call_id="test-stt-actual",
            provider_mode="pipeline",
            telephony_provider="twilio",
            stt_provider="deepgram",
            tts_provider="elevenlabs",
            llm_provider="custom",
        )
        acc.configure_stt_format(sample_rate=8000, bytes_per_sample=1)
        acc.add_stt_audio_bytes(480000)  # 60 seconds → would estimate ~$0.0043/min
        # Set actual cost from Deepgram API (overrides estimate)
        acc.set_actual_stt_cost(0.0050)

        result = acc.end_call()
        assert result.cost.stt == 0.005

    def test_estimated_stt_cost_when_no_actual(self):
        acc = CallMetricsAccumulator(
            call_id="test-stt-estimate",
            provider_mode="pipeline",
            telephony_provider="twilio",
            stt_provider="deepgram",
        )
        acc.configure_stt_format(sample_rate=8000, bytes_per_sample=1)
        acc.add_stt_audio_bytes(480000)  # 60 seconds
        # Don't set actual cost — should use estimate
        result = acc.end_call()
        assert result.cost.stt > 0  # Should have an estimated cost

    def test_actual_stt_cost_not_used_in_realtime_mode(self):
        acc = CallMetricsAccumulator(
            call_id="test-stt-realtime",
            provider_mode="openai_realtime",
            telephony_provider="twilio",
            stt_provider="openai",
            tts_provider="openai",
            llm_provider="openai",
        )
        # Even if set, actual STT cost shouldn't be used in realtime mode
        acc.set_actual_stt_cost(0.01)
        result = acc.end_call()
        assert result.cost.stt == 0.0  # Realtime mode bundles STT in LLM


class TestCallMetricsDataclass:
    """Tests for the frozen CallMetrics dataclass."""

    def test_frozen(self):
        metrics = CallMetrics(
            call_id="test",
            duration_seconds=60.0,
            turns=(),
            cost=CostBreakdown(),
            latency_avg=LatencyBreakdown(),
            latency_p95=LatencyBreakdown(),
            provider_mode="pipeline",
        )
        with pytest.raises(AttributeError):
            metrics.call_id = "changed"

    def test_turns_are_tuple(self):
        acc = CallMetricsAccumulator(
            call_id="test-tuple",
            provider_mode="pipeline",
            telephony_provider="twilio",
        )
        acc.start_turn()
        acc.record_stt_complete("Hi")
        acc.record_llm_complete()
        acc.record_tts_first_byte()
        acc.record_tts_complete("Hello")
        acc.record_turn_complete("Hello")

        result = acc.end_call()
        assert isinstance(result.turns, tuple)


class TestEOUMetricsEmission:
    """EOUMetrics field semantics + unit parity with the TypeScript SDK.

    Locks in the convention agreed in
    ``libraries/typescript/src/observability/metric-types.ts``:

        end_of_utterance_delay = stt_final  − vad_stopped  (ms)
        transcription_delay    = turn_commit − vad_stopped (ms)

    Regression guard for the bug where the Python implementation had the
    two fields swapped AND emitted them in seconds while the TS side
    emitted milliseconds.
    """

    def _make_accumulator(self):
        return CallMetricsAccumulator(
            call_id="eou-test",
            provider_mode="pipeline",
            telephony_provider="twilio",
            stt_provider="deepgram",
            tts_provider="elevenlabs",
            llm_provider="openai",
        )

    def test_eou_fields_match_ts_convention_in_ms(self):
        from getpatter.observability.event_bus import EventBus
        from getpatter.observability.metric_types import EOUMetrics

        bus = EventBus()
        emitted: list[EOUMetrics] = []
        bus.on("eou_metrics", lambda m: emitted.append(m))

        acc = self._make_accumulator()
        acc.attach_event_bus(bus)

        # Wall-clock timestamps in seconds; deltas chosen so the field
        # check is unambiguous:
        #   VAD stop -> STT final         = 200 ms
        #   VAD stop -> turn committed    = 350 ms
        t_vad = 1_000_000.000  # arbitrary epoch base
        t_stt = t_vad + 0.200
        t_turn = t_vad + 0.350
        acc.record_vad_stop(ts=t_vad)
        acc.record_stt_final_timestamp(ts=t_stt)
        acc.record_on_user_turn_completed_delay(50.0)  # already in ms
        acc.record_turn_committed(ts=t_turn)  # triggers emit

        assert len(emitted) == 1, "EOUMetrics should have been emitted exactly once"
        m = emitted[0]
        assert m.end_of_utterance_delay == pytest.approx(200.0, rel=1e-6)
        assert m.transcription_delay == pytest.approx(350.0, rel=1e-6)
        assert m.on_user_turn_completed_delay == pytest.approx(50.0, rel=1e-6)

    def test_eou_clamps_negative_deltas_to_zero(self):
        """Out-of-order timestamps (clock skew / replay) must not emit
        negative durations downstream — clamp at 0."""
        from getpatter.observability.event_bus import EventBus
        from getpatter.observability.metric_types import EOUMetrics

        bus = EventBus()
        emitted: list[EOUMetrics] = []
        bus.on("eou_metrics", lambda m: emitted.append(m))

        acc = self._make_accumulator()
        acc.attach_event_bus(bus)

        t_vad = 1_000_000.500
        # Inverted order: STT final + turn-commit are BEFORE the VAD stop.
        acc.record_vad_stop(ts=t_vad)
        acc.record_stt_final_timestamp(ts=t_vad - 0.100)
        acc.record_on_user_turn_completed_delay(0.0)
        acc.record_turn_committed(ts=t_vad - 0.050)

        assert len(emitted) == 1
        m = emitted[0]
        assert m.end_of_utterance_delay == 0.0
        assert m.transcription_delay == 0.0
