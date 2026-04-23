"""Call metrics accumulator — tracks cost and latency during a call."""

from __future__ import annotations

__all__ = ["CallMetricsAccumulator"]

import time

from getpatter.models import (
    CallMetrics,
    CostBreakdown,
    LatencyBreakdown,
    TurnMetrics,
)
from getpatter.pricing import (
    calculate_realtime_cached_savings,
    calculate_realtime_cost,
    calculate_stt_cost,
    calculate_telephony_cost,
    calculate_tts_cost,
    merge_pricing,
)


class CallMetricsAccumulator:
    """Mutable accumulator for per-call cost and latency metrics.

    Created at call start, collects data during the call, and produces
    a frozen ``CallMetrics`` via ``end_call()``.
    """

    def __init__(
        self,
        call_id: str,
        provider_mode: str,
        telephony_provider: str,
        stt_provider: str = "",
        tts_provider: str = "",
        llm_provider: str = "",
        pricing: dict | None = None,
    ) -> None:
        self.call_id = call_id
        self.provider_mode = provider_mode
        self.telephony_provider = telephony_provider
        self.stt_provider = stt_provider
        self.tts_provider = tts_provider
        self.llm_provider = llm_provider
        self._pricing = merge_pricing(pricing)

        self._call_start = time.monotonic()
        self._turns: list[TurnMetrics] = []

        # --- Per-turn timing state ---
        self._turn_start: float | None = None
        self._stt_complete: float | None = None
        self._llm_complete: float | None = None
        self._tts_first_byte: float | None = None
        self._turn_user_text: str = ""
        self._turn_stt_audio_seconds: float = 0.0

        # --- Cumulative usage counters ---
        self._total_stt_audio_seconds: float = 0.0
        self._total_tts_characters: int = 0
        self._total_realtime_cost: float = 0.0
        self._total_realtime_cached_savings: float = 0.0
        # Byte counters for computing audio seconds from raw audio
        self._stt_byte_count: int = 0
        self._stt_sample_rate: int = 16000
        self._stt_bytes_per_sample: int = 2  # PCM16 = 2 bytes/sample
        # Actual provider costs (from post-call API queries)
        self._actual_telephony_cost: float | None = None
        self._actual_stt_cost: float | None = None

    def configure_stt_format(
        self, sample_rate: int = 16000, bytes_per_sample: int = 2
    ) -> None:
        """Configure audio format for STT byte → seconds conversion.

        Args:
            sample_rate: Audio sample rate in Hz (8000 for mulaw/Twilio,
                16000 for PCM/Telnyx).
            bytes_per_sample: Bytes per sample (1 for mulaw, 2 for PCM16).
        """
        self._stt_sample_rate = sample_rate
        self._stt_bytes_per_sample = bytes_per_sample

    # ---- Turn lifecycle ----

    @property
    def turn_active(self) -> bool:
        """True when ``start_turn`` was called and the turn is not yet completed."""
        return self._turn_start is not None

    def start_turn(self) -> None:
        """Begin tracking a new conversation turn."""
        self._turn_start = time.monotonic()
        self._stt_complete = None
        self._llm_complete = None
        self._tts_first_byte = None
        self._turn_user_text = ""
        self._turn_stt_audio_seconds = 0.0

    def record_stt_complete(self, text: str, audio_seconds: float = 0.0) -> None:
        """Mark STT as complete for the current turn."""
        self._stt_complete = time.monotonic()
        self._turn_user_text = text
        self._turn_stt_audio_seconds = audio_seconds
        self._total_stt_audio_seconds += audio_seconds

    def record_llm_complete(self) -> None:
        """Mark LLM/on_message as complete for the current turn."""
        self._llm_complete = time.monotonic()

    def record_tts_first_byte(self) -> None:
        """Mark first TTS audio byte received for the current turn."""
        if self._tts_first_byte is None:
            self._tts_first_byte = time.monotonic()

    def record_tts_complete(self, text: str) -> None:
        """Mark TTS synthesis as complete, accumulating character count."""
        self._total_tts_characters += len(text)

    def record_turn_complete(self, agent_text: str) -> TurnMetrics:
        """Finalize the current turn and return its metrics."""
        latency = self._compute_turn_latency()
        turn = TurnMetrics(
            turn_index=len(self._turns),
            user_text=self._turn_user_text,
            agent_text=agent_text,
            latency=latency,
            stt_audio_seconds=self._turn_stt_audio_seconds,
            tts_characters=len(agent_text),
            timestamp=time.time(),
        )
        self._turns.append(turn)
        self._reset_turn_state()
        return turn

    def record_turn_interrupted(self) -> TurnMetrics | None:
        """Handle a barge-in / interrupted turn.

        Returns partial ``TurnMetrics`` if a turn was in progress, else
        ``None``.
        """
        if self._turn_start is None:
            return None

        latency = self._compute_turn_latency()
        turn = TurnMetrics(
            turn_index=len(self._turns),
            user_text=self._turn_user_text,
            agent_text="[interrupted]",
            latency=latency,
            stt_audio_seconds=self._turn_stt_audio_seconds,
            tts_characters=0,
            timestamp=time.time(),
        )
        self._turns.append(turn)
        self._reset_turn_state()
        return turn

    # ---- Usage tracking ----

    def add_stt_audio_bytes(self, byte_count: int) -> None:
        """Accumulate raw audio bytes sent to STT (used for cost calculation)."""
        self._stt_byte_count += byte_count

    def record_realtime_usage(self, usage: dict) -> None:
        """Record OpenAI Realtime token usage from a ``response.done`` event."""
        self._total_realtime_cost += calculate_realtime_cost(usage, self._pricing)
        self._total_realtime_cached_savings += calculate_realtime_cached_savings(usage, self._pricing)

    def set_actual_telephony_cost(self, cost: float) -> None:
        """Set the actual telephony cost from the provider API (post-call).

        When set, this takes priority over the estimated cost based on
        duration and default pricing.
        """
        self._actual_telephony_cost = cost

    def set_actual_stt_cost(self, cost: float) -> None:
        """Set the actual STT cost from the provider API (post-call).

        When set, this takes priority over the estimated cost based on
        audio duration and default pricing.
        """
        self._actual_stt_cost = cost

    # ---- Finalize ----

    def end_call(self) -> CallMetrics:
        """Calculate final costs and return frozen ``CallMetrics``."""
        duration = time.monotonic() - self._call_start

        # Compute STT audio seconds from byte count if not already tracked
        if self._total_stt_audio_seconds == 0.0 and self._stt_byte_count > 0:
            self._total_stt_audio_seconds = (
                self._stt_byte_count
                / (self._stt_sample_rate * self._stt_bytes_per_sample)
            )

        cost = self._compute_cost(duration)
        latency_avg = self._compute_average_latency()
        latency_p50 = self._compute_percentile_latency(0.5)
        latency_p95 = self._compute_percentile_latency(0.95)
        latency_p99 = self._compute_percentile_latency(0.99)

        return CallMetrics(
            call_id=self.call_id,
            duration_seconds=round(duration, 2),
            turns=tuple(self._turns),
            cost=cost,
            latency_avg=latency_avg,
            latency_p50=latency_p50,
            latency_p95=latency_p95,
            latency_p99=latency_p99,
            provider_mode=self.provider_mode,
            stt_provider=self.stt_provider,
            tts_provider=self.tts_provider,
            llm_provider=self.llm_provider,
            telephony_provider=self.telephony_provider,
        )

    def get_cost_so_far(self) -> CostBreakdown:
        """Return current accumulated cost (for real-time ``on_metrics``)."""
        duration = time.monotonic() - self._call_start
        return self._compute_cost(duration)

    # ---- Internal helpers ----

    def _reset_turn_state(self) -> None:
        self._turn_start = None
        self._stt_complete = None
        self._llm_complete = None
        self._tts_first_byte = None
        self._turn_user_text = ""
        self._turn_stt_audio_seconds = 0.0

    def _compute_turn_latency(self) -> LatencyBreakdown:
        """Compute latency breakdown for the current turn."""
        stt_ms = 0.0
        llm_ms = 0.0
        tts_ms = 0.0
        total_ms = 0.0

        if self._turn_start is not None and self._stt_complete is not None:
            stt_ms = (self._stt_complete - self._turn_start) * 1000

        if self._stt_complete is not None and self._llm_complete is not None:
            llm_ms = (self._llm_complete - self._stt_complete) * 1000

        if self._llm_complete is not None and self._tts_first_byte is not None:
            tts_ms = (self._tts_first_byte - self._llm_complete) * 1000

        if self._turn_start is not None and self._tts_first_byte is not None:
            total_ms = (self._tts_first_byte - self._turn_start) * 1000

        # Note: in Realtime mode OpenAI handles STT+LLM+TTS as a single opaque
        # pipeline, so stt_ms / llm_ms / tts_ms stay 0 and only total_ms is
        # meaningful. Dashboards should prefer total_ms as the end-to-end
        # proxy and treat the component buckets as "unknown / bundled by
        # provider" when total_ms > 0 but all three are 0.
        return LatencyBreakdown(
            stt_ms=round(stt_ms, 1),
            llm_ms=round(llm_ms, 1),
            tts_ms=round(tts_ms, 1),
            total_ms=round(total_ms, 1),
        )

    def _compute_cost(self, duration_seconds: float) -> CostBreakdown:
        """Compute cost breakdown from accumulated usage data."""
        if self.provider_mode == "openai_realtime":
            # OpenAI Realtime: STT+LLM+TTS cost comes from token usage
            stt_cost = 0.0
            tts_cost = 0.0
            llm_cost = self._total_realtime_cost
        elif self.provider_mode == "elevenlabs_convai":
            # ElevenLabs ConvAI: bundled pricing, estimate from duration
            stt_cost = 0.0
            tts_cost = 0.0
            llm_cost = 0.0  # ElevenLabs doesn't expose per-token pricing
        else:
            # Pipeline mode: separate providers
            # Prefer actual STT cost from provider API over estimate
            if self._actual_stt_cost is not None:
                stt_cost = self._actual_stt_cost
            else:
                stt_cost = calculate_stt_cost(
                    self.stt_provider, self._total_stt_audio_seconds, self._pricing
                )
            tts_cost = calculate_tts_cost(
                self.tts_provider, self._total_tts_characters, self._pricing
            )
            llm_cost = 0.0  # Pipeline LLM cost is user-managed (their on_message)

        # Prefer actual telephony cost from provider API over estimate
        if self._actual_telephony_cost is not None:
            telephony_cost = self._actual_telephony_cost
        else:
            telephony_cost = calculate_telephony_cost(
                self.telephony_provider, duration_seconds, self._pricing
            )

        total = stt_cost + tts_cost + llm_cost + telephony_cost

        return CostBreakdown(
            stt=round(stt_cost, 6),
            tts=round(tts_cost, 6),
            llm=round(llm_cost, 6),
            telephony=round(telephony_cost, 6),
            total=round(total, 6),
            llm_cached_savings=round(self._total_realtime_cached_savings, 6),
        )

    def _completed_turns(self) -> list:
        """Turns eligible for latency statistics.

        Excludes turns marked ``[interrupted]`` (barge-in, cancelled
        replacements) because their recorded latency either reflects partial
        state or zero — including them would drag every p95/avg bucket toward
        meaningless numbers.
        """
        return [t for t in self._turns if t.agent_text != "[interrupted]" and t.latency.total_ms > 0]

    def _compute_average_latency(self) -> LatencyBreakdown:
        """Compute average latency across completed turns."""
        turns = self._completed_turns()
        if not turns:
            return LatencyBreakdown()

        n = len(turns)
        return LatencyBreakdown(
            stt_ms=round(sum(t.latency.stt_ms for t in turns) / n, 1),
            llm_ms=round(sum(t.latency.llm_ms for t in turns) / n, 1),
            tts_ms=round(sum(t.latency.tts_ms for t in turns) / n, 1),
            total_ms=round(sum(t.latency.total_ms for t in turns) / n, 1),
        )

    def _compute_percentile_latency(self, p: float) -> LatencyBreakdown:
        """Compute an arbitrary percentile latency across completed turns.

        Uses linear interpolation between order statistics (Hyndman-Fan type
        7, same as numpy.percentile default). Previous ``floor()`` variant
        returned the sample max for any n < 21, making p95/p99 on short calls
        indistinguishable from max. Linear interpolation is meaningful even
        on 2-3 sample sets.
        """
        turns = self._completed_turns()
        if not turns:
            return LatencyBreakdown()

        def pct(values: list[float]) -> float:
            if not values:
                return 0.0
            sorted_v = sorted(values)
            if len(sorted_v) == 1:
                return sorted_v[0]
            rank = p * (len(sorted_v) - 1)
            lo = int(rank)
            hi = min(lo + 1, len(sorted_v) - 1)
            if lo == hi:
                return sorted_v[lo]
            frac = rank - lo
            return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * frac

        return LatencyBreakdown(
            stt_ms=round(pct([t.latency.stt_ms for t in turns]), 1),
            llm_ms=round(pct([t.latency.llm_ms for t in turns]), 1),
            tts_ms=round(pct([t.latency.tts_ms for t in turns]), 1),
            total_ms=round(pct([t.latency.total_ms for t in turns]), 1),
        )
