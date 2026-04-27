"""Call metrics accumulator — tracks cost and latency during a call."""

from __future__ import annotations

__all__ = ["CallMetricsAccumulator"]

import time
from typing import TYPE_CHECKING

from getpatter.models import (
    CallMetrics,
    CostBreakdown,
    LatencyBreakdown,
    TurnMetrics,
)
from getpatter.pricing import (
    calculate_llm_cost,
    calculate_realtime_cached_savings,
    calculate_realtime_cost,
    calculate_stt_cost,
    calculate_telephony_cost,
    calculate_tts_cost,
    merge_pricing,
)

if TYPE_CHECKING:
    from getpatter.observability.event_bus import EventBus


class CallMetricsAccumulator:
    """Mutable accumulator for per-call cost and latency metrics.

    Created at call start, collects data during the call, and produces
    a frozen ``CallMetrics`` via ``end_call()``.

    An optional :class:`~getpatter.observability.event_bus.EventBus` can be
    attached via :meth:`attach_event_bus`; it receives typed metric events in
    addition to the existing callback-based ``on_metrics`` path.
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
        report_only_initial_ttfb: bool = False,
    ) -> None:
        self.call_id = call_id
        self.provider_mode = provider_mode
        self.telephony_provider = telephony_provider
        self.stt_provider = stt_provider
        self.tts_provider = tts_provider
        self.llm_provider = llm_provider
        self._pricing = merge_pricing(pricing)
        self._report_only_initial_ttfb = report_only_initial_ttfb

        self._call_start = time.monotonic()
        self._turns: list[TurnMetrics] = []

        # --- Per-turn timing state ---
        self._turn_start: float | None = None
        self._stt_complete: float | None = None
        self._llm_first_token: float | None = None   # Fix 5: LLM TTFT start
        self._llm_first_sentence: float | None = None  # Fix 3: first sentence boundary
        self._llm_complete: float | None = None
        self._tts_first_byte: float | None = None
        # Last TTS audio byte sent (for tts_total_ms). Captured by
        # ``record_tts_complete`` which now also stamps the timestamp.
        self._tts_last_byte: float | None = None
        # Endpoint signal: silence detected (VAD stop OR STT speech_final).
        # Recorded by ``record_vad_stop`` / ``record_stt_final_timestamp`` —
        # whichever fires first becomes the endpoint reference.
        self._endpoint_signal_at: float | None = None
        # Barge-in detection / completion timestamps (for bargein_ms).
        self._bargein_detected_at: float | None = None
        self._bargein_stopped_at: float | None = None
        self._turn_user_text: str = ""
        self._turn_stt_audio_seconds: float = 0.0
        # Cross-turn TTFT storage so _emit_turn_metrics can read it after reset
        self._last_turn_llm_ttft_ms: float = 0.0

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
        # LLM token usage accumulated across all turns (pipeline mode)
        self._llm_total_input_tokens: int = 0
        self._llm_total_output_tokens: int = 0
        self._llm_total_cache_read_tokens: int = 0
        self._llm_total_cache_write_tokens: int = 0
        self._llm_provider_name: str = llm_provider
        self._llm_model: str = ""

        # --- EventBus integration (additive; does not replace callbacks) ---
        self._event_bus: EventBus | None = None

        # --- report_only_initial_ttfb guard flags ---
        self._llm_ttfb_emitted: bool = False
        self._tts_ttfb_emitted: bool = False

        # --- EOUMetrics timestamps (LiveKit Pattern A) ---
        self._vad_stopped_at: float | None = None
        self._stt_final_at: float | None = None
        self._turn_committed_at: float | None = None
        # Monotonic stamp of LLM dispatch — paired with
        # ``_endpoint_signal_at`` to compute ``endpoint_ms``.
        self._turn_committed_mono: float | None = None
        self._on_user_turn_completed_delay_ms: float | None = None

        # --- InterruptionMetrics counters (LiveKit Pattern B) ---
        self._num_interruptions: int = 0
        self._num_backchannels: int = 0
        self._overlap_started_at: float | None = None

    # ---- EventBus attachment ----

    def attach_event_bus(self, bus: "EventBus") -> None:
        """Attach an :class:`~getpatter.observability.event_bus.EventBus`.

        The bus receives structured metric events in addition to any existing
        callback-based ``on_metrics`` path. Safe to call multiple times (last
        bus wins).
        """
        self._event_bus = bus

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
        self._llm_first_token = None
        self._llm_first_sentence = None
        self._llm_complete = None
        self._tts_first_byte = None
        self._tts_last_byte = None
        self._endpoint_signal_at = None
        self._bargein_detected_at = None
        self._bargein_stopped_at = None
        self._turn_user_text = ""
        self._turn_stt_audio_seconds = 0.0
        # Reset per-turn TTFB guard flags
        self._llm_ttfb_emitted = False
        self._tts_ttfb_emitted = False
        # Reset EOU timestamps for the new turn
        self._vad_stopped_at = None
        self._stt_final_at = None
        self._turn_committed_at = None
        self._turn_committed_mono = None
        self._on_user_turn_completed_delay_ms = None

        if self._event_bus is not None:
            self._event_bus.emit("turn_started", {"call_id": self.call_id})

    def start_turn_if_idle(self) -> None:
        """Start a new turn only when no turn is already open.

        Call on the first inbound audio byte / non-final transcript so that
        STT latency is measured from the start of speech rather than from the
        final-transcript callback.  No-ops when a turn is already active so
        duplicate calls (e.g. from multiple non-final callbacks) are harmless.
        """
        if self._turn_start is None:
            self.start_turn()

    def record_llm_first_token(self) -> None:
        """Mark when the first LLM output token arrives (TTFT).

        Call on the first streaming token yielded by the LLM. Used to compute
        llm_ttft_ms = first_token - stt_complete, which is distinct from
        llm_ms = llm_complete - stt_complete (full generation time).

        When ``report_only_initial_ttfb=True`` is set, subsequent calls within
        the same turn are silently ignored after the first emission.
        """
        if self._llm_first_token is None:
            self._llm_first_token = time.monotonic()
            if not self._report_only_initial_ttfb or not self._llm_ttfb_emitted:
                self._llm_ttfb_emitted = True
                if self._event_bus is not None and self._stt_complete is not None:
                    from getpatter.observability.metric_types import TTFBMetrics
                    self._event_bus.emit(
                        "llm_metrics",
                        TTFBMetrics(
                            processor="llm",
                            value=self._llm_first_token - self._stt_complete,
                            model=self._llm_model or None,
                        ),
                    )

    def record_llm_first_sentence(self) -> None:
        """Mark when the first sentence boundary in the LLM stream is reached.

        Call when SentenceChunker.push() first returns a non-empty list.
        Used as the TTS span start instead of llm_complete so that
        tts_ms is positive even in streaming-pipeline mode where TTS begins
        before the full LLM response is done.  Falls back to llm_complete
        when not set (realtime / non-streaming paths).
        """
        if self._llm_first_sentence is None:
            self._llm_first_sentence = time.monotonic()

    def record_stt_complete(self, text: str, audio_seconds: float = 0.0) -> None:
        """Mark STT as complete for the current turn."""
        self._stt_complete = time.monotonic()
        self._turn_user_text = text
        self._turn_stt_audio_seconds = audio_seconds
        self._total_stt_audio_seconds += audio_seconds

        if self._event_bus is not None:
            from getpatter.observability.metric_types import ProcessingMetrics
            self._event_bus.emit(
                "stt_metrics",
                ProcessingMetrics(
                    processor="stt",
                    value=(
                        (self._stt_complete - self._turn_start)
                        if self._turn_start is not None
                        else 0.0
                    ),
                ),
            )

    def record_llm_complete(self) -> None:
        """Mark LLM/on_message as complete for the current turn."""
        self._llm_complete = time.monotonic()

    def record_tts_first_byte(self) -> None:
        """Mark first TTS audio byte received for the current turn.

        When ``report_only_initial_ttfb=True`` is set, the bus emission is
        suppressed after the first byte event per turn.
        """
        if self._tts_first_byte is None:
            self._tts_first_byte = time.monotonic()
            if not self._report_only_initial_ttfb or not self._tts_ttfb_emitted:
                self._tts_ttfb_emitted = True
                if self._event_bus is not None:
                    tts_ref = (
                        self._llm_first_sentence
                        if self._llm_first_sentence is not None
                        else self._llm_complete
                    )
                    if tts_ref is not None:
                        from getpatter.observability.metric_types import TTFBMetrics
                        self._event_bus.emit(
                            "tts_metrics",
                            TTFBMetrics(
                                processor="tts",
                                value=self._tts_first_byte - tts_ref,
                            ),
                        )

    def record_tts_complete(self, text: str) -> None:
        """Mark TTS synthesis as complete, accumulating character count.

        Also captures the monotonic timestamp of the last TTS audio byte
        sent on the wire so ``tts_total_ms`` (LLM-first-token → TTS done)
        can be computed.  Idempotent within a turn — the first call wins.
        """
        self._total_tts_characters += len(text)
        if self._tts_last_byte is None:
            self._tts_last_byte = time.monotonic()

    def record_tts_complete_ts(self, ts: float | None = None) -> None:
        """Capture the timestamp when the last TTS audio byte was sent.

        Useful when the caller wants to record the timing without bumping
        the character counter (e.g. interrupted turns where the byte
        actually went out but the synthesis was truncated).
        """
        self._tts_last_byte = ts if ts is not None else time.monotonic()

    def record_bargein_detected(self, ts: float | None = None) -> None:
        """Mark the moment a user interrupt (barge-in) was detected.

        Pairs with :meth:`record_tts_stopped` to compute ``bargein_ms``.
        """
        self._bargein_detected_at = ts if ts is not None else time.monotonic()

    def record_tts_stopped(self, ts: float | None = None) -> None:
        """Mark the moment TTS playback was actually halted after a barge-in.

        Call this *after* ``audio_sender.send_clear()`` returns. Combined with
        the matching ``record_bargein_detected`` it produces ``bargein_ms``.
        """
        self._bargein_stopped_at = ts if ts is not None else time.monotonic()

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
        if self._event_bus is not None:
            self._event_bus.emit(
                "turn_ended",
                {"call_id": self.call_id, "turn": turn},
            )
            self._event_bus.emit(
                "metrics_collected",
                {"call_id": self.call_id, "turn": turn},
            )
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

    # ---- EOUMetrics — LiveKit Pattern A ----

    def record_vad_stop(self, ts: float | None = None) -> None:
        """Record the timestamp when VAD detects end-of-speech.

        Args:
            ts: Wall-clock timestamp (seconds). Defaults to ``time.time()``.
        """
        self._vad_stopped_at = ts if ts is not None else time.time()
        # First endpoint signal wins for endpoint_ms calculation.
        if self._endpoint_signal_at is None:
            self._endpoint_signal_at = time.monotonic()

    def record_stt_final_timestamp(self, ts: float | None = None) -> None:
        """Record the timestamp when the STT provider returns a final transcript.

        Args:
            ts: Wall-clock timestamp (seconds). Defaults to ``time.time()``.
        """
        self._stt_final_at = ts if ts is not None else time.time()
        # First endpoint signal wins for endpoint_ms calculation.
        if self._endpoint_signal_at is None:
            self._endpoint_signal_at = time.monotonic()

    def record_turn_committed(self, ts: float | None = None) -> None:
        """Record the timestamp when the pipeline commits the turn for LLM processing.

        Args:
            ts: Wall-clock timestamp (seconds). Defaults to ``time.time()``.
        """
        self._turn_committed_at = ts if ts is not None else time.time()
        # Always stamp a monotonic reference for endpoint_ms even if the
        # caller passed an explicit wall-clock ts (they are different
        # clocks; we want monotonic for ms latency math).
        self._turn_committed_mono = time.monotonic()
        self._emit_eou_metrics()

    def record_on_user_turn_completed_delay(self, delay_ms: float) -> None:
        """Record the measured execution time of the on_user_turn_completed hook.

        Args:
            delay_ms: Hook execution time in milliseconds.
        """
        self._on_user_turn_completed_delay_ms = delay_ms

    def _emit_eou_metrics(self) -> None:
        """Emit ``EOUMetrics`` once all three EOU timestamps are available.

        Guards against emitting garbage data when only a subset of timestamps
        has been recorded (e.g. VAD skipped in non-local mode).
        """
        if (
            self._vad_stopped_at is None
            or self._stt_final_at is None
            or self._turn_committed_at is None
            or self._on_user_turn_completed_delay_ms is None
        ):
            return

        if self._event_bus is None:
            return

        from getpatter.observability.metric_types import EOUMetrics

        eou = EOUMetrics(
            end_of_utterance_delay=self._turn_committed_at - self._vad_stopped_at,
            transcription_delay=self._stt_final_at - self._vad_stopped_at,
            on_user_turn_completed_delay=self._on_user_turn_completed_delay_ms / 1000.0,
        )
        self._event_bus.emit("eou_metrics", eou)

    # ---- InterruptionMetrics — LiveKit Pattern B ----

    def record_overlap_start(self, ts: float | None = None) -> None:
        """Record when user speech begins overlapping agent playback.

        Args:
            ts: Wall-clock timestamp (seconds). Defaults to ``time.time()``.
        """
        self._overlap_started_at = ts if ts is not None else time.time()

    def record_overlap_end(
        self, was_interruption: bool = True, ts: float | None = None
    ) -> None:
        """Record when the overlap period ends and emit ``InterruptionMetrics``.

        Args:
            was_interruption: When ``True`` increments ``_num_interruptions``;
                when ``False`` increments ``_num_backchannels``.
            ts: End timestamp (seconds). Defaults to ``time.time()``.
        """
        if self._overlap_started_at is None:
            return

        end_ts = ts if ts is not None else time.time()
        detection_delay = end_ts - self._overlap_started_at
        self._overlap_started_at = None

        if was_interruption:
            self._num_interruptions += 1
        else:
            self._num_backchannels += 1

        if self._event_bus is not None:
            from getpatter.observability.metric_types import InterruptionMetrics

            metrics = InterruptionMetrics(
                total_duration=detection_delay,
                detection_delay=detection_delay,
                num_interruptions=self._num_interruptions,
                num_backchannels=self._num_backchannels,
            )
            self._event_bus.emit("interruption", metrics)

    # ---- Usage tracking ----

    def add_stt_audio_bytes(self, byte_count: int) -> None:
        """Accumulate raw audio bytes sent to STT (used for cost calculation)."""
        self._stt_byte_count += byte_count

    def record_realtime_usage(self, usage: dict) -> None:
        """Record OpenAI Realtime token usage from a ``response.done`` event."""
        self._total_realtime_cost += calculate_realtime_cost(usage, self._pricing)
        self._total_realtime_cached_savings += calculate_realtime_cached_savings(usage, self._pricing)

    def record_llm_usage(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Accumulate LLM token usage for pipeline-mode cost calculation.

        Call once per LLM response when a usage chunk is received from the
        provider stream. Stacks across turns so end_call() can produce a
        total LLM cost line.

        Args:
            provider: Provider key in LLM_PRICING (e.g. "anthropic", "openai").
            model: Model identifier (e.g. "claude-haiku-4-5").
            input_tokens: Non-cached input tokens at the full input rate.
            output_tokens: Output tokens at the output rate.
            cache_read_tokens: Tokens served from the provider prompt cache.
            cache_write_tokens: Tokens that populated the cache this call.
        """
        self._llm_provider_name = provider
        self._llm_model = model
        self._llm_total_input_tokens += input_tokens
        self._llm_total_output_tokens += output_tokens
        self._llm_total_cache_read_tokens += cache_read_tokens
        self._llm_total_cache_write_tokens += cache_write_tokens

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

        # Flush any dangling in-flight turn as interrupted so its partial state
        # doesn't evaporate into the void on abrupt hangup. ``_completed_turns``
        # drops it from percentile stats regardless.
        if self.turn_active:
            self.record_turn_interrupted()

        # Compute STT audio seconds from byte count if not already tracked
        if self._total_stt_audio_seconds == 0.0 and self._stt_byte_count > 0:
            self._total_stt_audio_seconds = (
                self._stt_byte_count
                / (self._stt_sample_rate * self._stt_bytes_per_sample)
            )

        cost = self._compute_cost(duration)
        latency_avg = self._compute_average_latency()
        latency_p50 = self._compute_percentile_latency(0.5)
        latency_p90 = self._compute_percentile_latency(0.90)
        latency_p95 = self._compute_percentile_latency(0.95)
        latency_p99 = self._compute_percentile_latency(0.99)

        result = CallMetrics(
            call_id=self.call_id,
            duration_seconds=round(duration, 2),
            turns=tuple(self._turns),
            cost=cost,
            latency_avg=latency_avg,
            latency_p50=latency_p50,
            latency_p90=latency_p90,
            latency_p95=latency_p95,
            latency_p99=latency_p99,
            provider_mode=self.provider_mode,
            stt_provider=self.stt_provider,
            tts_provider=self.tts_provider,
            llm_provider=self.llm_provider,
            telephony_provider=self.telephony_provider,
        )

        if self._event_bus is not None:
            self._event_bus.emit("call_ended", {"call_id": self.call_id, "metrics": result})

        return result

    def get_cost_so_far(self) -> CostBreakdown:
        """Return current accumulated cost (for real-time ``on_metrics``)."""
        duration = time.monotonic() - self._call_start
        return self._compute_cost(duration)

    # ---- Internal helpers ----

    def _reset_turn_state(self) -> None:
        self._turn_start = None
        self._stt_complete = None
        self._llm_first_token = None
        self._llm_first_sentence = None
        self._llm_complete = None
        self._tts_first_byte = None
        self._tts_last_byte = None
        self._endpoint_signal_at = None
        self._bargein_detected_at = None
        self._bargein_stopped_at = None
        self._turn_user_text = ""
        self._turn_stt_audio_seconds = 0.0

    def _compute_turn_latency(self) -> LatencyBreakdown:
        """Compute latency breakdown for the current turn."""
        stt_ms = 0.0
        llm_ms = 0.0
        llm_ttft_ms = 0.0
        tts_ms = 0.0
        total_ms = 0.0

        if self._turn_start is not None and self._stt_complete is not None:
            stt_ms = (self._stt_complete - self._turn_start) * 1000

        if self._stt_complete is not None and self._llm_complete is not None:
            llm_ms = (self._llm_complete - self._stt_complete) * 1000

        # Fix 5: LLM TTFT = first-token time minus stt_complete.
        if self._stt_complete is not None and self._llm_first_token is not None:
            llm_ttft_ms = max(0.0, (self._llm_first_token - self._stt_complete) * 1000)

        # Fix 3: TTS span starts from the first-sentence boundary rather than
        # llm_complete.  In streaming-pipeline mode, record_tts_first_byte fires
        # on the first audio chunk of the first sentence, which is always AFTER
        # llm_first_sentence — so tts_ms = tts_first_byte - llm_first_sentence
        # is always non-negative without clamping.  Fallback to llm_complete for
        # realtime / non-streaming paths where llm_first_sentence is never set.
        tts_ref = self._llm_first_sentence if self._llm_first_sentence is not None else self._llm_complete
        if tts_ref is not None and self._tts_first_byte is not None:
            tts_ms = max(0.0, (self._tts_first_byte - tts_ref) * 1000)

        if self._turn_start is not None and self._tts_first_byte is not None:
            total_ms = (self._tts_first_byte - self._turn_start) * 1000

        # Persist TTFT so _emit_turn_metrics can include it after _reset_turn_state.
        self._last_turn_llm_ttft_ms = llm_ttft_ms

        # llm_total_ms — full generation duration (stt_complete → llm_complete).
        llm_total_ms: float | None = None
        if self._stt_complete is not None and self._llm_complete is not None:
            llm_total_ms = max(0.0, (self._llm_complete - self._stt_complete) * 1000)

        # endpoint_ms — silence detected (VAD or STT speech_final) → LLM dispatch.
        endpoint_ms: float | None = None
        if (
            self._endpoint_signal_at is not None
            and self._turn_committed_mono is not None
        ):
            endpoint_ms = max(0.0, (self._turn_committed_mono - self._endpoint_signal_at) * 1000)

        # bargein_ms — interrupt detected → TTS actually halted.
        bargein_ms: float | None = None
        if (
            self._bargein_detected_at is not None
            and self._bargein_stopped_at is not None
        ):
            bargein_ms = max(0.0, (self._bargein_stopped_at - self._bargein_detected_at) * 1000)

        # tts_total_ms — LLM-first-token (or first-sentence boundary) → last
        # TTS audio byte sent.  Prefer ``llm_first_token`` so this captures
        # the entire user-perceptible TTS span; fall back to first-sentence
        # boundary (streaming) and finally to llm_complete.
        tts_total_ms: float | None = None
        tts_total_ref = (
            self._llm_first_token
            if self._llm_first_token is not None
            else (
                self._llm_first_sentence
                if self._llm_first_sentence is not None
                else self._llm_complete
            )
        )
        if tts_total_ref is not None and self._tts_last_byte is not None:
            tts_total_ms = max(0.0, (self._tts_last_byte - tts_total_ref) * 1000)

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
            llm_ttft_ms=round(llm_ttft_ms, 1) if llm_ttft_ms else None,
            llm_total_ms=round(llm_total_ms, 1) if llm_total_ms is not None else None,
            endpoint_ms=round(endpoint_ms, 1) if endpoint_ms is not None else None,
            bargein_ms=round(bargein_ms, 1) if bargein_ms is not None else None,
            tts_total_ms=round(tts_total_ms, 1) if tts_total_ms is not None else None,
        )

    @property
    def last_turn_llm_ttft_ms(self) -> float:
        """LLM TTFT (first-token latency, ms) from the most recently completed turn.

        Available after ``record_turn_complete`` or ``record_turn_interrupted``
        returns.  Zero when the LLM first-token was not recorded (e.g. Realtime
        / non-streaming paths).
        """
        return self._last_turn_llm_ttft_ms

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
            # Pipeline LLM cost: calculated from accumulated token usage when
            # record_llm_usage() was called; otherwise 0 (custom on_message).
            if self._llm_total_input_tokens or self._llm_total_output_tokens:
                llm_cost = calculate_llm_cost(
                    provider=self._llm_provider_name,
                    model=self._llm_model,
                    input_tokens=self._llm_total_input_tokens,
                    output_tokens=self._llm_total_output_tokens,
                    cache_read_tokens=self._llm_total_cache_read_tokens,
                    cache_write_tokens=self._llm_total_cache_write_tokens,
                )
            else:
                llm_cost = 0.0

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

        def _opt_avg(attr: str) -> float | None:
            vals = [getattr(t.latency, attr) for t in turns if getattr(t.latency, attr) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        ttft_avg = _opt_avg("llm_ttft_ms")
        return LatencyBreakdown(
            stt_ms=round(sum(t.latency.stt_ms for t in turns) / n, 1),
            llm_ms=round(sum(t.latency.llm_ms for t in turns) / n, 1),
            tts_ms=round(sum(t.latency.tts_ms for t in turns) / n, 1),
            total_ms=round(sum(t.latency.total_ms for t in turns) / n, 1),
            llm_ttft_ms=ttft_avg,
            llm_total_ms=_opt_avg("llm_total_ms"),
            endpoint_ms=_opt_avg("endpoint_ms"),
            bargein_ms=_opt_avg("bargein_ms"),
            tts_total_ms=_opt_avg("tts_total_ms"),
        )

    def _compute_percentile_latency(self, p: float) -> LatencyBreakdown:
        """Compute an arbitrary percentile latency across completed turns.

        Uses linear interpolation between order statistics (Hyndman-Fan type
        7, same as numpy.percentile default). Previous ``floor()`` variant
        returned the sample max for any n < 21, making p95/p99 on short calls
        indistinguishable from max. Linear interpolation is meaningful even
        on 2-3 sample sets.

        Fix 4: Per-component, zero-valued entries are excluded before computing
        percentiles.  Realtime turns and firstMessage turns never record
        component-level latencies (stt_ms/llm_ms/tts_ms stay 0) and including
        them would drag every component percentile bucket toward zero.  When all
        values for a component are zero the result is returned as 0.0.
        total_ms is NOT filtered because it is always populated for completed turns.
        """
        turns = self._completed_turns()
        if not turns:
            return LatencyBreakdown()

        def pct(values: list[float]) -> float:
            # Filter out zeros so Realtime / firstMessage turns don't drag
            # component-level buckets (stt, llm, tts) toward zero.
            non_zero = [v for v in values if v > 0]
            if not non_zero:
                return 0.0
            sorted_v = sorted(non_zero)
            if len(sorted_v) == 1:
                return sorted_v[0]
            rank = p * (len(sorted_v) - 1)
            lo = int(rank)
            hi = min(lo + 1, len(sorted_v) - 1)
            if lo == hi:
                return sorted_v[lo]
            frac = rank - lo
            return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * frac

        def pct_all(values: list[float]) -> float:
            """Percentile over all values, including zeros (used for total_ms)."""
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

        def _opt_pct(attr: str) -> float | None:
            vals = [getattr(t.latency, attr) for t in turns if getattr(t.latency, attr) is not None]
            v = pct(vals)
            return round(v, 1) if v else None

        ttft_pct_val = pct([t.latency.llm_ttft_ms for t in turns if t.latency.llm_ttft_ms is not None])
        return LatencyBreakdown(
            stt_ms=round(pct([t.latency.stt_ms for t in turns]), 1),
            llm_ms=round(pct([t.latency.llm_ms for t in turns]), 1),
            tts_ms=round(pct([t.latency.tts_ms for t in turns]), 1),
            total_ms=round(pct_all([t.latency.total_ms for t in turns]), 1),
            llm_ttft_ms=round(ttft_pct_val, 1) if ttft_pct_val else None,
            llm_total_ms=_opt_pct("llm_total_ms"),
            endpoint_ms=_opt_pct("endpoint_ms"),
            bargein_ms=_opt_pct("bargein_ms"),
            tts_total_ms=_opt_pct("tts_total_ms"),
        )
