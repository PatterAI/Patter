/**
 * Call metrics accumulator — tracks cost and latency during a call.
 *
 * Port of the Python `CallMetricsAccumulator` from `sdk/patter/services/metrics.py`.
 */

import {
  calculateLlmCost,
  calculateRealtimeCachedSavings,
  calculateRealtimeCost,
  calculateSttCost,
  calculateTelephonyCost,
  calculateTtsCost,
  mergePricing,
  type ProviderPricing,
} from './pricing';
import type { EventBus } from './observability/event-bus';
import type {
  EOUMetrics,
  InterruptionMetrics,
  ProcessingMetrics,
  TTFBMetrics,
} from './observability/metric-types';

// ---- Data types ----

/** Per-turn latency breakdown across the STT/LLM/TTS pipeline. */
export interface LatencyBreakdown {
  /**
   * STT finalization time: end-of-speech (VAD stop or STT speech_final) →
   * final transcript delivery. This is the engineering metric — pure STT
   * processing latency, independent of how long the user spoke. Industry
   * benchmarks (Picovoice, Deepgram, Gladia, Speechmatics) all report this
   * number as "STT latency". Falls back to turn_start when the endpoint
   * signal is unavailable (degraded provider, batch STT, etc.).
   */
  readonly stt_ms: number;
  /**
   * Duration of the user's utterance (turn_start → end-of-speech). Useful
   * to distinguish "user spoke for 4s" from "STT took 4s to finalize" —
   * they used to be conflated in stt_ms before 0.6.1. Optional — undefined
   * when the endpoint signal is unavailable.
   */
  readonly user_speech_duration_ms?: number;
  /**
   * Backwards-compatible LLM bucket. With the split below, this now reflects
   * the user-perceived first-token latency (TTFT) when streaming is available
   * and the full generation time otherwise. Prefer ``llm_ttft_ms`` /
   * ``llm_total_ms`` in new code.
   */
  readonly llm_ms: number;
  /** Time-to-first-token (UX-facing latency): stt_complete → first LLM token. */
  readonly llm_ttft_ms?: number;
  /**
   * Total LLM generation time: stt_complete → last LLM token. Distinct from
   * ``llm_ms`` so cost/throughput analysis and TTFT can be tracked separately.
   */
  readonly llm_total_ms?: number;
  readonly tts_ms: number;
  readonly total_ms: number;
  /**
   * Endpoint latency: time from end-of-user-speech (VAD stop or STT
   * ``speech_final``) to LLM dispatch. Captures the silence-detection +
   * transcript-finalization gap. Optional — undefined when the source signal
   * is missing.
   */
  readonly endpoint_ms?: number;
  /**
   * Barge-in latency: time from user-interrupt detection to TTS playback
   * actually halting (i.e. after ``sendClear`` returned). Optional — only
   * populated on interrupted turns.
   */
  readonly bargein_ms?: number;
  /**
   * Total TTS time: LLM-first-token (or first-sentence boundary) to last
   * TTS audio byte sent. Optional — undefined when TTS never completed.
   */
  readonly tts_total_ms?: number;
  /**
   * **User-perceived agent response latency**: time from end-of-user-speech
   * (VAD stop or STT ``speech_final``) to the first audio byte the agent
   * sent back. Computed as ``endpoint_ms + llm_ttft_ms + tts_ms`` when all
   * three signals are available — falls back to undefined otherwise.
   *
   * This is the metric you should watch for SLO / p95 dashboards. Unlike
   * ``total_ms`` (which spans the user's entire utterance and therefore
   * grows with how long the user spoke), ``agent_response_ms`` isolates
   * the system-controlled latency: silence detection + LLM TTFT + TTS
   * first byte.
   */
  readonly agent_response_ms?: number;
}

/** Per-call cost breakdown by component (STT/TTS/LLM/telephony) plus the total. */
export interface CostBreakdown {
  readonly stt: number;
  readonly tts: number;
  readonly llm: number;
  readonly telephony: number;
  readonly total: number;
  /**
   * Amount saved on LLM cost thanks to OpenAI Realtime prompt caching.
   * ``llm`` above is the net cost AFTER this discount. Dashboards can
   * render ``saved $X (pct%)`` next to the LLM line when > 0.
   */
  readonly llm_cached_savings: number;
}

/** Metrics captured for a single conversation turn. */
export interface TurnMetrics {
  readonly turn_index: number;
  readonly user_text: string;
  readonly agent_text: string;
  readonly latency: LatencyBreakdown;
  readonly stt_audio_seconds: number;
  readonly tts_characters: number;
  readonly timestamp: number;
}

/** Aggregated metrics for an entire call (turns, costs, latency percentiles). */
export interface CallMetrics {
  readonly call_id: string;
  readonly duration_seconds: number;
  readonly turns: readonly TurnMetrics[];
  readonly cost: CostBreakdown;
  readonly latency_avg: LatencyBreakdown;
  readonly latency_p95: LatencyBreakdown;
  readonly latency_p50: LatencyBreakdown;
  readonly latency_p90: LatencyBreakdown;
  readonly latency_p99: LatencyBreakdown;
  readonly provider_mode: string;
  readonly stt_provider: string;
  readonly tts_provider: string;
  readonly llm_provider: string;
  readonly telephony_provider: string;
  /** Model identifiers per provider (e.g. "ink-whisper", "eleven_flash_v2_5",
   * "gpt-oss-120b"). Surface on the dashboard cost breakdown so operators
   * can attribute per-call spend to a specific model. */
  readonly stt_model?: string;
  readonly tts_model?: string;
  readonly llm_model?: string;
}

// ---- CallControl interface ----

/** Programmatic control surface for a live call (transfer, hangup, DTMF). */
export interface CallControl {
  /** Transfer the call to a different number or SIP URI. */
  transfer(number: string): Promise<void>;
  /** Hang up the call. */
  hangup(): Promise<void>;
  /**
   * Send DTMF digits (for IVR navigation, e.g. "1234#").
   *
   * @param digits  String of DTMF digits (0-9, *, #, A-D).
   * @param options Per-call tuning. `delayMs` defaults to `300`.
   */
  sendDtmf?(digits: string, options?: { delayMs?: number }): Promise<void>;
  /** Current call ID. */
  readonly callId: string;
  /** Caller number. */
  readonly caller: string;
  /** Callee number. */
  readonly callee: string;
}

// ---- Helper ----

function round(value: number, decimals: number): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

function hrTimeMs(): number {
  // High-resolution monotonic time in milliseconds.
  const [sec, ns] = process.hrtime();
  return sec * 1000 + ns / 1e6;
}

/**
 * Percentile with linear interpolation between order statistics
 * (Hyndman-Fan type 7, same as numpy.percentile default).
 *
 * Rationale: the previous ``floor(n * 0.95)`` variant returned the sample
 * maximum for any n < 21, so p95 on short calls was indistinguishable from
 * max. Linear interpolation produces sensible intermediate values even on
 * 2–3 sample sets.
 */
function percentile(values: number[], p: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  if (sorted.length === 1) return sorted[0];
  const rank = p * (sorted.length - 1);
  const lo = Math.floor(rank);
  const hi = Math.ceil(rank);
  if (lo === hi) return sorted[lo];
  const frac = rank - lo;
  return sorted[lo] + (sorted[hi] - sorted[lo]) * frac;
}


// ---- Accumulator ----

/** Mutable per-call accumulator that stamps timestamps and emits final `CallMetrics`. */
export class CallMetricsAccumulator {
  readonly callId: string;
  readonly providerMode: string;
  readonly telephonyProvider: string;
  readonly sttProvider: string;
  readonly ttsProvider: string;
  readonly llmProvider: string;
  /**
   * Model identifiers for per-model rate resolution (see pricing.ts). Empty
   * string means "not known" → cost calc falls back to provider defaults,
   * matching pre-2026.3 behaviour.
   */
  readonly sttModel: string;
  readonly ttsModel: string;
  readonly realtimeModel: string;

  private readonly _pricing: Record<string, ProviderPricing>;
  private readonly _callStart: number;
  private readonly _turns: TurnMetrics[] = []; // mutable internal array; immutable when exposed via TurnMetrics[] → readonly TurnMetrics[]

  // Per-turn timing state
  private _turnStart: number | null = null;
  private _sttComplete: number | null = null;
  private _llmFirstToken: number | null = null;
  private _llmFirstSentenceComplete: number | null = null;
  private _llmComplete: number | null = null;
  private _ttsFirstByte: number | null = null;
  /** Last TTS audio byte sent (hrTimeMs). Stamped by ``recordTtsComplete`` /
   *  ``recordTtsCompleteTs``. Used to compute ``tts_total_ms``. */
  private _ttsLastByte: number | null = null;
  /** Endpoint signal (hrTimeMs) — VAD stop or STT speech_final, whichever
   *  fires first. Used to compute ``endpoint_ms``. */
  private _endpointSignalAt: number | null = null;
  /** Monotonic stamp of LLM dispatch (paired with ``_endpointSignalAt``). */
  private _turnCommittedMono: number | null = null;
  /** Barge-in detected timestamp (hrTimeMs). */
  private _bargeinDetectedAt: number | null = null;
  /** TTS-stopped timestamp after barge-in (hrTimeMs). */
  private _bargeinStoppedAt: number | null = null;
  private _turnUserText = '';
  private _turnSttAudioSeconds = 0;
  /**
   * Guard against the recordTurnInterrupted / recordTurnComplete race.
   *
   * A VAD-path barge-in fires ``recordTurnInterrupted`` synchronously
   * inside ``handleAudioAsync`` while the in-flight pipeline LLM stream
   * keeps unwinding on its own task. When the LLM stream eventually
   * exits, the existing pipeline path falls through to
   * ``recordTurnComplete``, which would push a second turn for the same
   * logical exchange (this time carrying ``user_text=''`` because the
   * field was already reset). ``_turnAlreadyClosed`` is flipped by
   * ``recordTurnInterrupted`` and read by ``recordTurnComplete`` so the
   * late ``recordTurnComplete`` becomes a no-op until the next
   * ``startTurn`` re-arms the accumulator.
   */
  private _turnAlreadyClosed = false;

  // Cumulative usage counters
  private _totalSttAudioSeconds = 0;
  private _totalTtsCharacters = 0;
  private _totalRealtimeCost = 0;
  private _totalRealtimeCachedSavings = 0;
  private _sttByteCount = 0;
  private _sttSampleRate = 16000;
  private _sttBytesPerSample = 2;
  private _actualTelephonyCost: number | null = null;
  private _actualSttCost: number | null = null;
  // Fix 10: accumulated LLM token cost for non-Realtime pipeline mode.
  private _totalLlmCost = 0;
  // Last LLM model identifier from a recordLlmUsage call — emitted on
  // CallMetrics.llm_model so the dashboard cost panel can display
  // "Cerebras gpt-oss-120b" instead of just "Cerebras".
  private _llmModel = '';

  // ---- EventBus integration (item 3) ----
  private _eventBus: EventBus | undefined;

  // ---- EOUMetrics — 4 timestamps (item 4) ----
  /** Timestamp (hrTimeMs) when VAD emitted speech_end. */
  private _vadStoppedAt: number | null = null;
  /** Timestamp (hrTimeMs) when STT emitted its final transcript. */
  private _sttFinalAt: number | null = null;
  /** Timestamp (hrTimeMs) when the transcript was committed to the LLM. */
  private _turnCommittedAt: number | null = null;
  /** Delta (ms) from turn-committed to on_user_turn_completed hook done. */
  private _onUserTurnCompletedDelayMs: number | null = null;

  // ---- InterruptionMetrics — simplified no-ML (item 5) ----
  private _numInterruptions = 0;
  private _numBackchannels = 0;
  private _overlapStartedAt: number | null = null;

  // ---- report_only_initial_ttfb (item 6) ----
  private _reportOnlyInitialTtfb: boolean;
  private _initialTtfbEmitted = false;

  // ---- Barge-in anchor hygiene ----
  /**
   * Last barge-in detection timestamp (hrTimeMs). Used by
   * ``_computeTurnLatency`` to gate endpoint_ms / stt_ms emission on turns
   * that started immediately after a barge-in — those turns have unreliable
   * VAD/STT anchors and would otherwise pollute the p95 distribution with
   * synthetic 6+ second spikes.
   */
  private _lastBargeinAt: number | null = null;
  /**
   * Count of turns where ``recordSttComplete`` fired but no legitimate VAD
   * ``speech_end`` had stamped ``_endpointSignalAt``. Exposed via metrics so
   * we can spot environments where PSTN packet loss is dropping VAD stops
   * (the common cause of missing endpoint signals).
   */
  private _endpointSignalMissingCount = 0;

  constructor(opts: {
    callId: string;
    providerMode: string;
    telephonyProvider: string;
    sttProvider?: string;
    ttsProvider?: string;
    llmProvider?: string;
    /** Model identifier for the STT adapter (e.g. ``"nova-3-multilingual"``). */
    sttModel?: string;
    /** Model identifier for the TTS adapter (e.g. ``"eleven_multilingual_v2"``). */
    ttsModel?: string;
    /** Model identifier for the realtime adapter (e.g. ``"gpt-realtime-2"``). */
    realtimeModel?: string;
    pricing?: Record<string, Partial<ProviderPricing>> | null;
    eventBus?: EventBus;
    /** When true, only the first TTFB emission per call is forwarded to the event bus. */
    reportOnlyInitialTtfb?: boolean;
  }) {
    this.callId = opts.callId;
    this.providerMode = opts.providerMode;
    this.telephonyProvider = opts.telephonyProvider;
    this.sttProvider = opts.sttProvider ?? '';
    this.ttsProvider = opts.ttsProvider ?? '';
    this.llmProvider = opts.llmProvider ?? '';
    this.sttModel = opts.sttModel ?? '';
    this.ttsModel = opts.ttsModel ?? '';
    this.realtimeModel = opts.realtimeModel ?? '';
    this._pricing = mergePricing(opts.pricing);
    this._callStart = hrTimeMs();
    this._eventBus = opts.eventBus;
    this._reportOnlyInitialTtfb = opts.reportOnlyInitialTtfb ?? false;
  }

  /**
   * Attach (or replace) an EventBus after construction.
   * Useful when the bus is created after the accumulator (e.g. in tests).
   */
  attachEventBus(bus: EventBus): void {
    this._eventBus = bus;
  }

  /** Configure audio format for STT byte-to-seconds conversion. */
  configureSttFormat(sampleRate = 16000, bytesPerSample = 2): void {
    this._sttSampleRate = sampleRate;
    this._sttBytesPerSample = bytesPerSample;
  }

  // ---- Turn lifecycle ----

  /** Whether a turn is currently being measured (startTurn called, not yet completed). */
  get turnActive(): boolean {
    return this._turnStart !== null;
  }

  /** Begin a new turn — stamps the turn start timestamp and resets per-turn state. */
  startTurn(): void {
    this._turnStart = hrTimeMs();
    this._sttComplete = null;
    this._llmFirstToken = null;
    this._llmFirstSentenceComplete = null;
    this._llmComplete = null;
    this._ttsFirstByte = null;
    this._ttsLastByte = null;
    this._endpointSignalAt = null;
    this._turnCommittedMono = null;
    this._bargeinDetectedAt = null;
    this._bargeinStoppedAt = null;
    this._turnUserText = '';
    this._turnSttAudioSeconds = 0;
    this._turnAlreadyClosed = false;
    // Reset initial-TTFB latch so the first TTFB of each new turn is always
    // forwarded. Mirrors Python start_turn() which resets _llm_ttfb_emitted
    // and _tts_ttfb_emitted on every new turn start.
    this._initialTtfbEmitted = false;
    // Reset EOU state for this turn
    this._vadStoppedAt = null;
    this._sttFinalAt = null;
    this._turnCommittedAt = null;
    this._onUserTurnCompletedDelayMs = null;
    this._eventBus?.emit('turn_started', { callId: this.callId });
  }

  /**
   * Start a new turn only if no turn is currently open.
   * Use this at inbound-audio ingestion points so the turn timer begins
   * on the first audio byte rather than just before recordSttComplete().
   */
  startTurnIfIdle(): void {
    if (this._turnStart === null) {
      this.startTurn();
    }
  }

  /**
   * Anchor the current turn at a legitimate VAD ``speech_start`` event.
   *
   * Industry-standard pattern: every VAD ``speech_start`` that fires while the agent
   * is NOT in the suppressed warmup window re-anchors the turn timer to
   * the wall-clock moment the user actually started speaking. Re-anchors:
   *
   *  * ``_turnStart`` — fixes the case where a phantom ``speech_start``
   *    during agent TTS or a partial transcript from the previous user
   *    attempt already stamped the field. Without this, the legitimate
   *    user-speech ``speech_start`` no-op'd and ``user_speech_duration_ms``
   *    inflated from ~1 s to 5-7 s (the original "I waited 7 seconds"
   *    dashboard symptom).
   *  * ``_endpointSignalAt``, ``_vadStoppedAt``, ``_sttFinalAt`` — any
   *    stale anchor from a rejected barge-in / dropped final transcript
   *    on the same uncommitted turn is cleared, so the next
   *    ``recordVadStop`` / ``recordSttFinalTimestamp`` stamps fresh.
   *  * ``_sttComplete``, ``_llmFirstToken``, ``_initialTtfbEmitted`` — same
   *    rationale for the downstream pipeline timestamps.
   *
   * No-op once the turn is committed (``_turnCommittedMono`` set): a
   * VAD ``speech_start`` after commit belongs to the NEXT turn's
   * barge-in path, handled by ``recordTurnInterrupted`` instead.
   */
  anchorUserSpeechStart(): void {
    if (this._turnCommittedMono !== null) return;
    this._turnStart = hrTimeMs();
    // A pre-commit VAD speech_start opens a NEW turn — re-arm the guard
    // exactly as startTurn() does. Without this, the guard set by the
    // previous turn's recordTurnComplete persists, and every later
    // anchor-opened turn makes recordTurnComplete a no-op (dropping per-turn
    // metrics AND the live SSE transcript). Safe: this method no-ops after
    // commit (guard above), so we only ever re-arm at the start of a fresh,
    // uncommitted turn — never the post-commit barge-in path that
    // _turnAlreadyClosed protects.
    this._turnAlreadyClosed = false;
    this._endpointSignalAt = null;
    this._vadStoppedAt = null;
    this._sttFinalAt = null;
    this._sttComplete = null;
    this._llmFirstToken = null;
    this._initialTtfbEmitted = false;
  }

  /** Stamp end-of-STT, capture the user's transcript, and accrue billed STT seconds. */
  recordSttComplete(text: string, audioSeconds = 0): void {
    this._sttComplete = hrTimeMs();
    this._sttFinalAt = this._sttComplete;
    // Don't fake _endpointSignalAt from _sttComplete — that creates dishonest
    // endpoint_ms == stt_ms outliers. Honest "undefined" is better than a
    // 6818ms percentile spike. The counter lets us know if this happens often
    // (VAD speech_end being dropped on PSTN packets is the common cause).
    if (this._endpointSignalAt === null) {
      this._endpointSignalMissingCount++;
    }
    this._turnUserText = text;
    this._turnSttAudioSeconds = audioSeconds;
    this._totalSttAudioSeconds += audioSeconds;

    // Emit ProcessingMetrics for parity with Python services/metrics.py:
    // record_stt_complete().  ``value`` is in seconds.
    if (this._eventBus) {
      const valueSec =
        this._turnStart !== null
          ? (this._sttComplete - this._turnStart) / 1000
          : 0;
      const payload: ProcessingMetrics = {
        timestamp: Date.now() / 1000,
        processor: 'stt',
        model: null,
        value: valueSec,
      };
      this._eventBus.emit('stt_metrics', payload);
    }
  }

  /** Record the timestamp of the first LLM token (TTFT). No-op after first call. */
  recordLlmFirstToken(): void {
    if (this._llmFirstToken === null) {
      this._llmFirstToken = hrTimeMs();
      // Emit TTFBMetrics for parity with Python services/metrics.py:
      // record_llm_first_token().  ``value`` is in seconds.
      if (
        this._eventBus &&
        this._sttComplete !== null &&
        (!this._reportOnlyInitialTtfb || !this._initialTtfbEmitted)
      ) {
        const payload: TTFBMetrics = {
          timestamp: Date.now() / 1000,
          processor: 'llm',
          model: null,
          value: (this._llmFirstToken - this._sttComplete) / 1000,
        };
        this._eventBus.emit('llm_metrics', payload);
      }
    }
  }

  /**
   * Record when the sentence chunker emits the first complete sentence.
   * Used as the TTS span start so tts_ms reflects true TTS-provider latency
   * rather than the gap from llm_complete (which fires after the full response).
   * No-op after first call.
   */
  recordLlmFirstSentenceComplete(): void {
    if (this._llmFirstSentenceComplete === null) {
      this._llmFirstSentenceComplete = hrTimeMs();
    }
  }

  /** Stamp end-of-LLM (last token received). */
  recordLlmComplete(): void {
    this._llmComplete = hrTimeMs();
  }

  /** Stamp first TTS audio byte sent on the wire (used to compute TTS TTFB). */
  recordTtsFirstByte(): void {
    if (this._ttsFirstByte === null) {
      this._ttsFirstByte = hrTimeMs();
    }

    // item 6: gate subsequent TTFB emissions when reportOnlyInitialTtfb is set
    if (this._reportOnlyInitialTtfb && this._initialTtfbEmitted) {
      return;
    }
    this._initialTtfbEmitted = true;

    // Emit TTFBMetrics for parity with Python services/metrics.py:
    // record_tts_first_byte().  ``value`` is in seconds.  Use the
    // first-sentence-complete timestamp when available (matches Py),
    // otherwise fall back to llm_complete.
    if (this._eventBus && this._ttsFirstByte !== null) {
      const ttsRef =
        this._llmFirstSentenceComplete !== null
          ? this._llmFirstSentenceComplete
          : this._llmComplete;
      if (ttsRef !== null) {
        const payload: TTFBMetrics = {
          timestamp: Date.now() / 1000,
          processor: 'tts',
          model: null,
          value: (this._ttsFirstByte - ttsRef) / 1000,
        };
        this._eventBus.emit('tts_metrics', payload);
      }
    }
  }

  /** Record final TTS text length and stamp the last-byte timestamp. */
  recordTtsComplete(text: string): void {
    this._totalTtsCharacters += text.length;
    if (this._ttsLastByte === null) {
      this._ttsLastByte = hrTimeMs();
    }
  }

  /**
   * Capture the timestamp when the last TTS audio byte was sent on the wire.
   * Useful when the caller wants to record the timing without bumping the
   * character counter (e.g. interrupted turns where audio actually went out
   * but synthesis was truncated).
   */
  recordTtsCompleteTs(ts?: number): void {
    this._ttsLastByte = ts ?? hrTimeMs();
  }

  /**
   * Mark the moment a user interrupt (barge-in) was detected. Pairs with
   * ``recordTtsStopped`` to compute ``bargein_ms``.
   */
  recordBargeinDetected(ts?: number): void {
    const t = ts ?? hrTimeMs();
    this._bargeinDetectedAt = t;
    // Stamp _lastBargeinAt on the same monotonic clock as _turnStart so the
    // post-barge-in anchor-gating in _computeTurnLatency stays valid (see
    // the comment there for rationale).
    this._lastBargeinAt = t;
  }

  /**
   * Mark the moment TTS playback was actually halted after a barge-in. Call
   * this *after* ``sendClear`` returns. Pairs with ``recordBargeinDetected``
   * to compute ``bargein_ms``.
   */
  recordTtsStopped(ts?: number): void {
    this._bargeinStoppedAt = ts ?? hrTimeMs();
  }

  /**
   * Close the current turn cleanly and append a `TurnMetrics` record.
   *
   * Returns ``null`` when ``recordTurnInterrupted`` has already closed
   * the current turn — this protects against the VAD-barge-in /
   * pipeline-LLM race where both paths try to finalise the same logical
   * turn and the second would otherwise push a phantom entry with
   * ``user_text=''``. The caller treats ``null`` as "nothing to emit";
   * ``emitTurnMetrics`` is already null-safe.
   */
  recordTurnComplete(agentText: string): TurnMetrics | null {
    if (this._turnAlreadyClosed) return null;
    const latency = this._computeTurnLatency();
    const turn: TurnMetrics = {
      turn_index: this._turns.length,
      user_text: this._turnUserText,
      agent_text: agentText,
      latency,
      stt_audio_seconds: this._turnSttAudioSeconds,
      tts_characters: agentText.length,
      timestamp: Date.now() / 1000,
    };
    this._turns.push(turn);
    // Emit BEFORE reset so subscribers see the turn with its anchors intact.
    // Matches Python record_turn_complete (emits before _reset_turn_state) and
    // is consistent with recordTurnInterrupted which also emits before reset.
    this._eventBus?.emit('turn_ended', { callId: this.callId, turn });
    this._eventBus?.emit('metrics_collected', { callId: this.callId, turn });
    this._resetTurnState();
    // Bidirectional guard: mark the turn as closed so a late
    // recordTurnInterrupted (e.g. from a future refactor that reorders
    // the bargein + LLM-unwind paths) becomes a no-op instead of
    // overwriting the just-emitted turn record. Mirrors the inverse
    // guard in recordTurnInterrupted and keeps the two close paths
    // symmetric.
    this._turnAlreadyClosed = true;
    return turn;
  }

  /**
   * Close the current turn as interrupted (barge-in) and return the
   * recorded metrics. Returns ``null`` when no turn is open, OR when
   * ``recordTurnComplete`` has already finalised the current turn —
   * bidirectional parity with the guard at the top of
   * ``recordTurnComplete``. Prevents an out-of-order interruption (e.g.
   * a future refactor that reorders the bargein + LLM-unwind paths)
   * from overwriting a turn that the complete path already emitted.
   */
  recordTurnInterrupted(): TurnMetrics | null {
    if (this._turnStart === null) return null;
    if (this._turnAlreadyClosed) return null;
    const latency = this._computeTurnLatency();
    const turn: TurnMetrics = {
      turn_index: this._turns.length,
      user_text: this._turnUserText,
      agent_text: '[interrupted]',
      latency,
      stt_audio_seconds: this._turnSttAudioSeconds,
      tts_characters: 0,
      timestamp: Date.now() / 1000,
    };
    this._turns.push(turn);
    // Emit the turn record BEFORE reset so subscribers see the interrupted
    // turn with its anchors still intact. Parity with recordTurnComplete().
    this._eventBus?.emit('turn_ended', { callId: this.callId, turn });
    this._eventBus?.emit('metrics_collected', { callId: this.callId, turn });
    this._resetTurnState();
    // Mark the turn as closed so a late recordTurnComplete from the
    // pipeline-LLM unwind path becomes a no-op (see _turnAlreadyClosed).
    this._turnAlreadyClosed = true;
    // Extra paranoia: explicitly null out anchors that have caused leaks
    // into subsequent turns when a barge-in is in flight. _resetTurnState
    // already clears them, but keep this belt-and-braces line so future
    // refactors that touch _resetTurnState don't silently regress us.
    this._turnCommittedMono = null;
    this._endpointSignalAt = null;
    return turn;
  }

  // ---- EOU metrics (item 4) ----

  /**
   * Record the moment VAD emitted speech_end for the current utterance.
   * @param ts Optional override timestamp in hrTimeMs units (defaults to now).
   */
  recordVadStop(ts?: number): void {
    this._vadStoppedAt = ts ?? hrTimeMs();
    // First endpoint signal wins for endpoint_ms calculation.
    if (this._endpointSignalAt === null) {
      this._endpointSignalAt = this._vadStoppedAt;
    }
  }

  /**
   * Record the moment the STT provider delivered its final transcript.
   * Aliased to the same instant as recordSttComplete() when called from
   * the standard pipeline; can be called independently for custom pipelines.
   * @param ts Optional override timestamp in hrTimeMs units.
   */
  recordSttFinalTimestamp(ts?: number): void {
    this._sttFinalAt = ts ?? hrTimeMs();
    // First endpoint signal wins for endpoint_ms calculation.
    if (this._endpointSignalAt === null) {
      this._endpointSignalAt = this._sttFinalAt;
    }
  }

  /**
   * Record the moment the transcript was committed to the LLM (turn start).
   * After this call, ``emitEouMetrics()`` can produce a complete EOUMetrics payload.
   * @param ts Optional override timestamp in hrTimeMs units.
   */
  recordTurnCommitted(ts?: number): void {
    this._turnCommittedAt = ts ?? hrTimeMs();
    // Always stamp a monotonic-ish reference (hrTimeMs) for endpoint_ms math.
    this._turnCommittedMono = hrTimeMs();
    this.emitEouMetrics();
  }

  /**
   * Record the delta (ms) between turn-committed and when on_user_turn_completed
   * pipeline hook finished. Does NOT re-emit: like Python's
   * ``record_on_user_turn_completed_delay``, this only stores the value; the
   * single EOU emission happens on ``recordTurnCommitted`` (3-timestamp guard,
   * delay defaults to 0 if not yet recorded).
   */
  recordOnUserTurnCompletedDelay(delayMs: number): void {
    this._onUserTurnCompletedDelayMs = delayMs;
  }

  /**
   * Compute and emit EOUMetrics when all three prerequisite timestamps are
   * available (VAD stop, STT final, turn committed).
   *
   * ``endOfUtteranceDelay``     = sttFinal − vadStopped  (ms)
   * ``transcriptionDelay``       = turnCommitted − vadStopped  (ms)
   * ``onUserTurnCompletedDelay`` = caller-supplied delta (ms) or 0
   */
  /** Emit `EOUMetrics` once VAD-stop, STT-final, turn-committed, and on_user_turn_completed delay are all known. */
  emitEouMetrics(): void {
    if (
      this._vadStoppedAt === null ||
      this._sttFinalAt === null ||
      this._turnCommittedAt === null
    ) {
      return;
    }
    const payload: EOUMetrics = {
      timestamp: Date.now() / 1000,
      endOfUtteranceDelay: Math.max(0, this._sttFinalAt - this._vadStoppedAt),
      transcriptionDelay: Math.max(0, this._turnCommittedAt - this._vadStoppedAt),
      onUserTurnCompletedDelay: this._onUserTurnCompletedDelayMs ?? 0,
    };
    this._eventBus?.emit('eou_metrics', payload);
  }

  // ---- InterruptionMetrics (item 5) ----

  /**
   * Record that a caller utterance started overlapping with agent speech.
   * Call this when VAD detects speech_start during TTS playback.
   * @param ts Optional override timestamp in hrTimeMs units.
   */
  recordOverlapStart(ts?: number): void {
    this._overlapStartedAt = ts ?? hrTimeMs();
  }

  /**
   * Record that the overlap ended.  Emits ``InterruptionMetrics`` via the
   * event bus.
   *
   * @param wasInterruption  true → barge-in (increments ``numInterruptions``),
   *                         false → backchannel (increments ``numBackchannels``).
   * @param ts Optional override timestamp in hrTimeMs units.
   */
  recordOverlapEnd(wasInterruption: boolean, ts?: number): void {
    const now = ts ?? hrTimeMs();
    const detectionDelay = this._overlapStartedAt !== null
      ? Math.max(0, now - this._overlapStartedAt)
      : 0;
    this._overlapStartedAt = null;

    if (wasInterruption) {
      this._numInterruptions++;
    } else {
      this._numBackchannels++;
    }

    const payload: InterruptionMetrics = {
      timestamp: Date.now() / 1000,
      // Simplified: totalDuration == detectionDelay (no ML prediction window)
      totalDuration: detectionDelay,
      predictionDuration: 0,
      detectionDelay,
      numInterruptions: this._numInterruptions,
      numBackchannels: this._numBackchannels,
    };
    this._eventBus?.emit('interruption', payload);
  }

  // ---- Usage tracking ----

  /** Accumulate inbound STT audio bytes for cost calculation when seconds are unknown. */
  addSttAudioBytes(byteCount: number): void {
    this._sttByteCount += byteCount;
  }

  /**
   * Record an OpenAI Realtime usage payload and roll up its cost + cached-savings.
   *
   * `model` allows the cost calc to pick the per-model rate (e.g.
   * `gpt-realtime-2`). Defaults to whatever was supplied at construction
   * time (`this.realtimeModel`); pass an explicit value to override per-call
   * (the `response.done` payload carries the model used).
   */
  recordRealtimeUsage(
    usage: {
      input_token_details?: {
        audio_tokens?: number;
        text_tokens?: number;
        cached_tokens_details?: { audio_tokens?: number; text_tokens?: number };
      };
      output_token_details?: { audio_tokens?: number; text_tokens?: number };
    },
    model?: string | null,
  ): void {
    const resolvedModel = model || this.realtimeModel || null;
    this._totalRealtimeCost += calculateRealtimeCost(usage, this._pricing, resolvedModel);
    this._totalRealtimeCachedSavings += calculateRealtimeCachedSavings(
      usage,
      this._pricing,
      resolvedModel,
    );
  }

  /** Override the carrier-billed telephony cost (e.g. exact value reported via Twilio API). */
  setActualTelephonyCost(cost: number): void {
    this._actualTelephonyCost = cost;
  }

  /** Override the provider-billed STT cost when an exact figure is available. */
  setActualSttCost(cost: number): void {
    this._actualSttCost = cost;
  }

  /**
   * Accumulate LLM token cost for pipeline mode (non-Realtime).
   *
   * Called by LLMLoop.run() when a usage chunk arrives from the provider.
   * Mirrors Python's CallMetricsAccumulator.record_llm_usage().
   *
   * @param provider   LLM provider key (e.g. 'openai', 'anthropic')
   * @param model      Model name (e.g. 'gpt-4o-mini')
   * @param inputTokens       Total input tokens (includes cached)
   * @param outputTokens      Total output tokens
   * @param cacheReadTokens   Cached input tokens (subtracted from input before billing full rate)
   * @param cacheWriteTokens  Cache write tokens (billed at cache_write rate if present)
   */
  recordLlmUsage(
    provider: string,
    model: string,
    inputTokens: number,
    outputTokens: number,
    cacheReadTokens = 0,
    cacheWriteTokens = 0,
  ): void {
    this._llmModel = model;
    this._totalLlmCost += calculateLlmCost(
      provider, model,
      inputTokens, outputTokens,
      cacheReadTokens, cacheWriteTokens,
    );
  }

  // ---- Finalize ----

  /** Finalize the call: flush any in-flight turn, compute aggregates, and return `CallMetrics`. */
  endCall(): CallMetrics {
    const duration = (hrTimeMs() - this._callStart) / 1000;

    // Flush any dangling in-flight turn as interrupted so its partial state
    // doesn't evaporate into the void on abrupt hangup. The filter inside
    // _completedTurns drops it from percentile stats regardless.
    if (this.turnActive) {
      this.recordTurnInterrupted();
    }

    if (this._totalSttAudioSeconds === 0 && this._sttByteCount > 0) {
      this._totalSttAudioSeconds =
        this._sttByteCount / (this._sttSampleRate * this._sttBytesPerSample);
    }

    const cost = this._computeCost(duration);
    const latencyAvg = this._computeAverageLatency();
    const latencyP50 = this._computePercentileLatency(0.5);
    const latencyP90 = this._computePercentileLatency(0.9);
    const latencyP95 = this._computePercentileLatency(0.95);
    const latencyP99 = this._computePercentileLatency(0.99);

    const metrics: CallMetrics = {
      call_id: this.callId,
      duration_seconds: round(duration, 2),
      turns: [...this._turns],
      cost,
      latency_avg: latencyAvg,
      latency_p50: latencyP50,
      latency_p90: latencyP90,
      latency_p95: latencyP95,
      latency_p99: latencyP99,
      provider_mode: this.providerMode,
      stt_provider: this.sttProvider,
      tts_provider: this.ttsProvider,
      llm_provider: this.llmProvider,
      telephony_provider: this.telephonyProvider,
      stt_model: this.sttModel,
      tts_model: this.ttsModel,
      llm_model: this._llmModel,
    };

    this._eventBus?.emit('call_ended', { callId: this.callId, metrics });
    return metrics;
  }

  /** Return the cost breakdown for the call so far without ending it. */
  getCostSoFar(): CostBreakdown {
    const duration = (hrTimeMs() - this._callStart) / 1000;
    return this._computeCost(duration);
  }

  /**
   * Number of turns where recordSttComplete fired without a prior legitimate
   * VAD speech_end. Surfaced for diagnostics — a non-zero value points at
   * dropped VAD stops (commonly PSTN packet loss), which is why we stopped
   * faking _endpointSignalAt from _sttComplete in 0.6.x.
   */
  get endpointSignalMissingCount(): number {
    return this._endpointSignalMissingCount;
  }

  // ---- Internal ----

  private _resetTurnState(): void {
    this._turnStart = null;
    this._sttComplete = null;
    this._llmFirstToken = null;
    this._llmFirstSentenceComplete = null;
    this._llmComplete = null;
    this._ttsFirstByte = null;
    this._ttsLastByte = null;
    this._endpointSignalAt = null;
    this._turnCommittedMono = null;
    this._bargeinDetectedAt = null;
    this._bargeinStoppedAt = null;
    this._turnUserText = '';
    this._turnSttAudioSeconds = 0;
    // Reset initial-TTFB latch so EventBus TTFB emission re-fires on the new
    // turn. Without this, with reportOnlyInitialTtfb=true we lose the TTFB
    // metric on the first turn after a barge-in / new turn.
    this._initialTtfbEmitted = false;
  }

  private _computeTurnLatency(): LatencyBreakdown {
    let stt_ms = 0;
    let llm_ms = 0;
    let llm_ttft_ms: number | undefined;
    let llm_total_ms: number | undefined;
    let tts_ms = 0;
    let total_ms = 0;
    let endpoint_ms: number | undefined;
    let bargein_ms: number | undefined;
    let tts_total_ms: number | undefined;
    let user_speech_duration_ms: number | undefined;

    // Post-barge-in turns have unreliable anchors. Drop endpoint_ms / stt_ms
    // to avoid polluting the p95 distribution with synthetic spikes. The
    // honest "undefined" makes the metric usable for SLO/alerting; without
    // this gate, a single barge-in produces 6+ second p95 outliers.
    const postBargein =
      this._lastBargeinAt !== null &&
      this._turnStart !== null &&
      Math.abs(this._turnStart - this._lastBargeinAt) <= 100;

    // ``stt_ms`` measures pure STT finalization: end-of-speech (VAD stop or
    // STT speech_final) → final transcript delivery. This is the
    // engineering metric reported as "STT latency" by the industry. When
    // the endpoint signal is unavailable (degraded provider, batch STT)
    // fall back to the legacy turn_start anchor so the field is never
    // spuriously zero.
    if (this._sttComplete !== null) {
      const anchor = this._endpointSignalAt ?? this._turnStart;
      if (anchor !== null) {
        stt_ms = Math.max(0, this._sttComplete - anchor);
      }
    }
    if (this._turnStart !== null && this._endpointSignalAt !== null) {
      user_speech_duration_ms = Math.max(
        0,
        this._endpointSignalAt - this._turnStart,
      );
    }
    // ``llm_ms`` is the user-facing latency that maps to UX: time-to-first-token
    // from end-of-STT.  ``llm_total_ms`` captures the full generation duration
    // (stt_complete → llm_complete) so it can be tracked separately for
    // cost/throughput analysis.
    if (this._sttComplete !== null && this._llmFirstToken !== null) {
      llm_ttft_ms = Math.max(0, this._llmFirstToken - this._sttComplete);
      llm_ms = llm_ttft_ms;
    } else if (this._sttComplete !== null && this._llmComplete !== null) {
      // Fallback when the provider doesn't surface first-token timing
      // (e.g. non-streaming providers).
      llm_ms = this._llmComplete - this._sttComplete;
    }
    if (this._sttComplete !== null && this._llmComplete !== null) {
      llm_total_ms = Math.max(0, this._llmComplete - this._sttComplete);
    }
    // Fix 3: use first-sentence boundary as TTS span start when available.
    // In streaming pipeline mode recordTtsFirstByte fires mid-generation,
    // before recordLlmComplete. Using llmFirstSentenceComplete as the TTS
    // span start gives a meaningful tts_ms (provider synthesis latency)
    // instead of collapsing to 0. Fallback to llmComplete (legacy).
    const ttsSpanStart = this._llmFirstSentenceComplete ?? this._llmComplete;
    if (ttsSpanStart !== null && this._ttsFirstByte !== null) {
      tts_ms = this._ttsFirstByte - ttsSpanStart;
      if (tts_ms < 0) tts_ms = 0;
    }
    if (this._turnStart !== null && this._ttsFirstByte !== null) {
      total_ms = this._ttsFirstByte - this._turnStart;
    }

    // endpoint_ms — silence-detected (VAD / STT speech_final) → LLM dispatch.
    if (this._endpointSignalAt !== null && this._turnCommittedMono !== null) {
      endpoint_ms = Math.max(0, this._turnCommittedMono - this._endpointSignalAt);
    }
    // bargein_ms — interrupt detected → TTS actually halted.
    if (this._bargeinDetectedAt !== null && this._bargeinStoppedAt !== null) {
      bargein_ms = Math.max(0, this._bargeinStoppedAt - this._bargeinDetectedAt);
    }
    // tts_total_ms — LLM-first-token (or first-sentence boundary, fallback
    // llm_complete) → last TTS audio byte sent on the wire.
    const ttsTotalRef =
      this._llmFirstToken ??
      this._llmFirstSentenceComplete ??
      this._llmComplete;
    if (ttsTotalRef !== null && this._ttsLastByte !== null) {
      tts_total_ms = Math.max(0, this._ttsLastByte - ttsTotalRef);
    }

    // agent_response_ms — the user-perceived latency. Sum of the three
    // system-controlled segments (silence detection, LLM TTFT, TTS
    // first-byte). Undefined when any prerequisite signal is missing —
    // we deliberately do NOT fall back to total_ms so dashboards can
    // distinguish "metric available" vs "metric missing".
    let agent_response_ms: number | undefined;
    if (
      endpoint_ms !== undefined &&
      llm_ttft_ms !== undefined &&
      tts_ms > 0
    ) {
      agent_response_ms = round(endpoint_ms + llm_ttft_ms + tts_ms, 1);
    }

    // Post-barge-in anchor hygiene: when the current turn began within 100 ms
    // of the last detected barge-in, the VAD/STT anchors are unreliable. Drop
    // the polluted endpoint_ms and stt_ms so percentile aggregations ignore
    // them (stt_ms = 0 is excluded by nonZero() in _computePercentileLatency).
    if (postBargein) {
      stt_ms = 0;
      endpoint_ms = undefined;
    }

    // Note: in Realtime mode OpenAI handles STT+LLM+TTS as a single opaque
    // pipeline, so stt_ms / llm_ms / tts_ms stay 0 and only total_ms is
    // meaningful. Dashboards should prefer total_ms as the end-to-end proxy
    // and treat the component buckets as "unknown / bundled by provider"
    // when total_ms > 0 but all three are 0.
    return {
      stt_ms: round(stt_ms, 1),
      llm_ms: round(llm_ms, 1),
      ...(user_speech_duration_ms !== undefined
        ? { user_speech_duration_ms: round(user_speech_duration_ms, 1) }
        : {}),
      ...(llm_ttft_ms !== undefined ? { llm_ttft_ms: round(llm_ttft_ms, 1) } : {}),
      ...(llm_total_ms !== undefined ? { llm_total_ms: round(llm_total_ms, 1) } : {}),
      tts_ms: round(tts_ms, 1),
      total_ms: round(total_ms, 1),
      ...(endpoint_ms !== undefined ? { endpoint_ms: round(endpoint_ms, 1) } : {}),
      ...(bargein_ms !== undefined ? { bargein_ms: round(bargein_ms, 1) } : {}),
      ...(tts_total_ms !== undefined ? { tts_total_ms: round(tts_total_ms, 1) } : {}),
      ...(agent_response_ms !== undefined ? { agent_response_ms } : {}),
    };
  }

  private _computeCost(durationSeconds: number): CostBreakdown {
    let stt: number;
    let tts: number;
    let llm: number;

    if (this.providerMode === 'openai_realtime') {
      stt = 0;
      tts = 0;
      llm = this._totalRealtimeCost;
    } else if (this.providerMode === 'elevenlabs_convai') {
      stt = 0;
      tts = 0;
      llm = 0;
    } else {
      stt =
        this._actualSttCost !== null
          ? this._actualSttCost
          : calculateSttCost(
              this.sttProvider,
              this._totalSttAudioSeconds,
              this._pricing,
              this.sttModel || null,
            );
      tts = calculateTtsCost(
        this.ttsProvider,
        this._totalTtsCharacters,
        this._pricing,
        this.ttsModel || null,
      );
      // Fix 10: include accumulated LLM token cost (from recordLlmUsage).
      llm = this._totalLlmCost;
    }

    const telephony =
      this._actualTelephonyCost !== null
        ? this._actualTelephonyCost
        : calculateTelephonyCost(this.telephonyProvider, durationSeconds, this._pricing);

    const total = stt + tts + llm + telephony;

    return {
      stt: round(stt, 6),
      tts: round(tts, 6),
      llm: round(llm, 6),
      telephony: round(telephony, 6),
      total: round(total, 6),
      // Always emit (default 0) for parity with Python dataclass where
      // llm_cached_savings is a required field with default 0.0.
      llm_cached_savings: round(Math.max(0, this._totalRealtimeCachedSavings), 6),
    };
  }

  /**
   * Turns eligible for latency statistics.
   *
   * Excludes turns marked ``[interrupted]`` (barge-in, cancelled replacements)
   * because their recorded latency either reflects partial state or zero —
   * including them would drag every p95/avg bucket toward meaningless numbers.
   */
  private _completedTurns(): TurnMetrics[] {
    return this._turns.filter(
      (t) => t.agent_text !== '[interrupted]' && t.latency.total_ms > 0,
    );
  }

  private _computeAverageLatency(): LatencyBreakdown {
    const turns = this._completedTurns();
    if (turns.length === 0) {
      return { stt_ms: 0, llm_ms: 0, tts_ms: 0, total_ms: 0 };
    }
    const n = turns.length;
    // Fix 9: include llm_ttft_ms in aggregates (filter >0 so Realtime turns
    // where it is undefined/0 do not drag the average toward 0).
    const ttftValues = turns.map((t) => t.latency.llm_ttft_ms ?? 0).filter((v) => v > 0);
    const ttftAvg = ttftValues.length > 0
      ? round(ttftValues.reduce((s, v) => s + v, 0) / ttftValues.length, 1)
      : undefined;
    const optAvg = (key: keyof LatencyBreakdown): number | undefined => {
      const vals = turns
        .map((t) => t.latency[key])
        .filter((v): v is number => typeof v === 'number' && v > 0);
      return vals.length > 0
        ? round(vals.reduce((s, v) => s + v, 0) / vals.length, 1)
        : undefined;
    };
    const llmTotalAvg = optAvg('llm_total_ms');
    const endpointAvg = optAvg('endpoint_ms');
    const bargeinAvg = optAvg('bargein_ms');
    const ttsTotalAvg = optAvg('tts_total_ms');
    const userSpeechAvg = optAvg('user_speech_duration_ms');
    const agentResponseAvg = optAvg('agent_response_ms');
    return {
      stt_ms: round(turns.reduce((s, t) => s + t.latency.stt_ms, 0) / n, 1),
      llm_ms: round(turns.reduce((s, t) => s + t.latency.llm_ms, 0) / n, 1),
      ...(ttftAvg !== undefined ? { llm_ttft_ms: ttftAvg } : {}),
      ...(llmTotalAvg !== undefined ? { llm_total_ms: llmTotalAvg } : {}),
      tts_ms: round(turns.reduce((s, t) => s + t.latency.tts_ms, 0) / n, 1),
      total_ms: round(turns.reduce((s, t) => s + t.latency.total_ms, 0) / n, 1),
      ...(endpointAvg !== undefined ? { endpoint_ms: endpointAvg } : {}),
      ...(bargeinAvg !== undefined ? { bargein_ms: bargeinAvg } : {}),
      ...(ttsTotalAvg !== undefined ? { tts_total_ms: ttsTotalAvg } : {}),
      ...(userSpeechAvg !== undefined ? { user_speech_duration_ms: userSpeechAvg } : {}),
      ...(agentResponseAvg !== undefined ? { agent_response_ms: agentResponseAvg } : {}),
    };
  }

  private _computePercentileLatency(p: number): LatencyBreakdown {
    const turns = this._completedTurns();
    if (turns.length === 0) {
      return { stt_ms: 0, llm_ms: 0, tts_ms: 0, total_ms: 0 };
    }
    // Fix 4: exclude zero-valued samples per-component so turns where a
    // component was not measured (e.g. Realtime bundles STT+LLM) don't
    // drag percentiles toward 0 for turns that did measure it.
    const nonZero = (vals: number[]): number[] => vals.filter((v) => v > 0);
    // Fix 9: include llm_ttft_ms in percentile aggregates (filter >0 so
    // Realtime turns where it is undefined/0 do not bias the bucket).
    const ttftSamples = nonZero(turns.map((t) => t.latency.llm_ttft_ms ?? 0));
    const ttftP = ttftSamples.length > 0
      ? round(percentile(ttftSamples, p), 1)
      : undefined;
    const optPct = (key: keyof LatencyBreakdown): number | undefined => {
      const vals = turns
        .map((t) => t.latency[key])
        .filter((v): v is number => typeof v === 'number' && v > 0);
      return vals.length > 0 ? round(percentile(vals, p), 1) : undefined;
    };
    const llmTotalP = optPct('llm_total_ms');
    const endpointP = optPct('endpoint_ms');
    const bargeinP = optPct('bargein_ms');
    const ttsTotalP = optPct('tts_total_ms');
    const userSpeechP = optPct('user_speech_duration_ms');
    const agentResponseP = optPct('agent_response_ms');
    return {
      stt_ms: round(percentile(nonZero(turns.map((t) => t.latency.stt_ms)), p), 1),
      llm_ms: round(percentile(nonZero(turns.map((t) => t.latency.llm_ms)), p), 1),
      ...(ttftP !== undefined ? { llm_ttft_ms: ttftP } : {}),
      ...(llmTotalP !== undefined ? { llm_total_ms: llmTotalP } : {}),
      tts_ms: round(percentile(nonZero(turns.map((t) => t.latency.tts_ms)), p), 1),
      total_ms: round(percentile(nonZero(turns.map((t) => t.latency.total_ms)), p), 1),
      ...(endpointP !== undefined ? { endpoint_ms: endpointP } : {}),
      ...(bargeinP !== undefined ? { bargein_ms: bargeinP } : {}),
      ...(ttsTotalP !== undefined ? { tts_total_ms: ttsTotalP } : {}),
      ...(userSpeechP !== undefined ? { user_speech_duration_ms: userSpeechP } : {}),
      ...(agentResponseP !== undefined ? { agent_response_ms: agentResponseP } : {}),
    };
  }
}
