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
import type { EOUMetrics, InterruptionMetrics } from './observability/metric-types';

// ---- Data types ----

export interface LatencyBreakdown {
  stt_ms: number;
  /** Time-to-first-token (UX-facing latency): stt_complete → first LLM token. */
  llm_ms: number;
  /** Same as llm_ms, retained as an alias for older consumers. */
  llm_ttft_ms?: number;
  /** Total LLM generation time: stt_complete → last LLM token. Useful for cost
   *  and throughput analysis (token-count proxy) but NOT what the user feels. */
  llm_total_ms?: number;
  tts_ms: number;
  total_ms: number;
}

export interface CostBreakdown {
  stt: number;
  tts: number;
  llm: number;
  telephony: number;
  total: number;
  /**
   * Amount saved on LLM cost thanks to OpenAI Realtime prompt caching.
   * ``llm`` above is the net cost AFTER this discount. Dashboards can
   * render ``saved $X (pct%)`` next to the LLM line when > 0.
   */
  llm_cached_savings?: number;
}

export interface TurnMetrics {
  turn_index: number;
  user_text: string;
  agent_text: string;
  latency: LatencyBreakdown;
  stt_audio_seconds: number;
  tts_characters: number;
  timestamp: number;
}

export interface CallMetrics {
  call_id: string;
  duration_seconds: number;
  turns: TurnMetrics[];
  cost: CostBreakdown;
  latency_avg: LatencyBreakdown;
  latency_p95: LatencyBreakdown;
  // Optional for backwards compatibility with external consumers that
  // construct CallMetrics literals. Always populated by endCall().
  latency_p50?: LatencyBreakdown;
  latency_p99?: LatencyBreakdown;
  provider_mode: string;
  stt_provider: string;
  tts_provider: string;
  llm_provider: string;
  telephony_provider: string;
}

// ---- CallControl interface ----

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

export class CallMetricsAccumulator {
  callId: string;
  readonly providerMode: string;
  readonly telephonyProvider: string;
  readonly sttProvider: string;
  readonly ttsProvider: string;
  readonly llmProvider: string;

  private readonly _pricing: Record<string, ProviderPricing>;
  private readonly _callStart: number;
  private readonly _turns: TurnMetrics[] = [];

  // Per-turn timing state
  private _turnStart: number | null = null;
  private _sttComplete: number | null = null;
  private _llmFirstToken: number | null = null;
  private _llmFirstSentenceComplete: number | null = null;
  private _llmComplete: number | null = null;
  private _ttsFirstByte: number | null = null;
  private _turnUserText = '';
  private _turnSttAudioSeconds = 0;

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

  constructor(opts: {
    callId: string;
    providerMode: string;
    telephonyProvider: string;
    sttProvider?: string;
    ttsProvider?: string;
    llmProvider?: string;
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

  startTurn(): void {
    this._turnStart = hrTimeMs();
    this._sttComplete = null;
    this._llmFirstToken = null;
    this._llmFirstSentenceComplete = null;
    this._llmComplete = null;
    this._ttsFirstByte = null;
    this._turnUserText = '';
    this._turnSttAudioSeconds = 0;
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

  recordSttComplete(text: string, audioSeconds = 0): void {
    this._sttComplete = hrTimeMs();
    this._sttFinalAt = this._sttComplete;
    this._turnUserText = text;
    this._turnSttAudioSeconds = audioSeconds;
    this._totalSttAudioSeconds += audioSeconds;
  }

  /** Record the timestamp of the first LLM token (TTFT). No-op after first call. */
  recordLlmFirstToken(): void {
    if (this._llmFirstToken === null) {
      this._llmFirstToken = hrTimeMs();
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

  recordLlmComplete(): void {
    this._llmComplete = hrTimeMs();
  }

  recordTtsFirstByte(): void {
    if (this._ttsFirstByte === null) {
      this._ttsFirstByte = hrTimeMs();
    }

    // item 6: gate subsequent TTFB emissions when reportOnlyInitialTtfb is set
    if (this._reportOnlyInitialTtfb && this._initialTtfbEmitted) {
      return;
    }
    this._initialTtfbEmitted = true;
  }

  recordTtsComplete(text: string): void {
    this._totalTtsCharacters += text.length;
  }

  recordTurnComplete(agentText: string): TurnMetrics {
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
    this._resetTurnState();
    this._eventBus?.emit('turn_ended', { callId: this.callId, turn });
    this._eventBus?.emit('metrics_collected', { callId: this.callId, turn });
    return turn;
  }

  recordTurnInterrupted(): TurnMetrics | null {
    if (this._turnStart === null) return null;
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
    this._resetTurnState();
    return turn;
  }

  // ---- EOU metrics (item 4) ----

  /**
   * Record the moment VAD emitted speech_end for the current utterance.
   * @param ts Optional override timestamp in hrTimeMs units (defaults to now).
   */
  recordVadStop(ts?: number): void {
    this._vadStoppedAt = ts ?? hrTimeMs();
  }

  /**
   * Record the moment the STT provider delivered its final transcript.
   * Aliased to the same instant as recordSttComplete() when called from
   * the standard pipeline; can be called independently for custom pipelines.
   * @param ts Optional override timestamp in hrTimeMs units.
   */
  recordSttFinalTimestamp(ts?: number): void {
    this._sttFinalAt = ts ?? hrTimeMs();
  }

  /**
   * Record the moment the transcript was committed to the LLM (turn start).
   * After this call, ``emitEouMetrics()`` can produce a complete EOUMetrics payload.
   * @param ts Optional override timestamp in hrTimeMs units.
   */
  recordTurnCommitted(ts?: number): void {
    this._turnCommittedAt = ts ?? hrTimeMs();
    this.emitEouMetrics();
  }

  /**
   * Record the delta (ms) between turn-committed and when on_user_turn_completed
   * pipeline hook finished.  Stored for inclusion in the next ``emitEouMetrics``
   * call (or an explicit re-emit if desired).
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

  addSttAudioBytes(byteCount: number): void {
    this._sttByteCount += byteCount;
  }

  recordRealtimeUsage(usage: {
    input_token_details?: {
      audio_tokens?: number;
      text_tokens?: number;
      cached_tokens_details?: { audio_tokens?: number; text_tokens?: number };
    };
    output_token_details?: { audio_tokens?: number; text_tokens?: number };
  }): void {
    this._totalRealtimeCost += calculateRealtimeCost(usage, this._pricing);
    this._totalRealtimeCachedSavings += calculateRealtimeCachedSavings(usage, this._pricing);
  }

  setActualTelephonyCost(cost: number): void {
    this._actualTelephonyCost = cost;
  }

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
    this._totalLlmCost += calculateLlmCost(
      provider, model,
      inputTokens, outputTokens,
      cacheReadTokens, cacheWriteTokens,
    );
  }

  // ---- Finalize ----

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
    const latencyP95 = this._computePercentileLatency(0.95);
    const latencyP99 = this._computePercentileLatency(0.99);

    const metrics: CallMetrics = {
      call_id: this.callId,
      duration_seconds: round(duration, 2),
      turns: [...this._turns],
      cost,
      latency_avg: latencyAvg,
      latency_p50: latencyP50,
      latency_p95: latencyP95,
      latency_p99: latencyP99,
      provider_mode: this.providerMode,
      stt_provider: this.sttProvider,
      tts_provider: this.ttsProvider,
      llm_provider: this.llmProvider,
      telephony_provider: this.telephonyProvider,
    };

    this._eventBus?.emit('call_ended', { callId: this.callId, metrics });
    return metrics;
  }

  getCostSoFar(): CostBreakdown {
    const duration = (hrTimeMs() - this._callStart) / 1000;
    return this._computeCost(duration);
  }

  // ---- Internal ----

  private _resetTurnState(): void {
    this._turnStart = null;
    this._sttComplete = null;
    this._llmFirstToken = null;
    this._llmFirstSentenceComplete = null;
    this._llmComplete = null;
    this._ttsFirstByte = null;
    this._turnUserText = '';
    this._turnSttAudioSeconds = 0;
  }

  private _computeTurnLatency(): LatencyBreakdown {
    let stt_ms = 0;
    let llm_ms = 0;
    let llm_ttft_ms: number | undefined;
    let tts_ms = 0;
    let total_ms = 0;

    // ``stt_ms`` is the wall-clock window from the first audio byte with
    // detected speech to the final transcript. It includes the user's speech
    // duration AND the provider's endpointing wait — both contribute to the
    // time the agent is blocked waiting on STT, so this is what matters for
    // UX. To isolate provider-only processing latency you'd need an external
    // VAD signalling end-of-speech *before* the STT provider's own decision,
    // which streaming providers like Deepgram do not expose separately
    // (they emit speech_final and is_final in the same chunk).
    if (this._turnStart !== null && this._sttComplete !== null) {
      stt_ms = this._sttComplete - this._turnStart;
    }
    // Note: an ``stt_endpointing_ms`` (post-speech wait) metric would be
    // useful but Deepgram emits speech_final and final-transcript in the same
    // chunk, so the gap collapses to ~0. To get a meaningful value we'd need
    // an external VAD (Silero) signalling end-of-speech earlier. Deferred.
    // ``llm_ms`` is the user-facing latency that maps to UX: time-to-first-token
    // from end-of-STT. The legacy "tokens-to-completion" is preserved as
    // ``llm_total_ms`` for cost/quality analysis.
    if (this._sttComplete !== null && this._llmFirstToken !== null) {
      llm_ms = Math.max(0, this._llmFirstToken - this._sttComplete);
      llm_ttft_ms = llm_ms;
    } else if (this._sttComplete !== null && this._llmComplete !== null) {
      // Fallback when the provider doesn't surface first-token timing
      // (e.g. non-streaming providers).
      llm_ms = this._llmComplete - this._sttComplete;
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

    // Note: in Realtime mode OpenAI handles STT+LLM+TTS as a single opaque
    // pipeline, so stt_ms / llm_ms / tts_ms stay 0 and only total_ms is
    // meaningful. Dashboards should prefer total_ms as the end-to-end proxy
    // and treat the component buckets as "unknown / bundled by provider"
    // when total_ms > 0 but all three are 0.
    let llm_total_ms: number | undefined;
    if (this._sttComplete !== null && this._llmComplete !== null) {
      llm_total_ms = this._llmComplete - this._sttComplete;
    }
    return {
      stt_ms: round(stt_ms, 1),
      llm_ms: round(llm_ms, 1),
      ...(llm_ttft_ms !== undefined ? { llm_ttft_ms: round(llm_ttft_ms, 1) } : {}),
      ...(llm_total_ms !== undefined ? { llm_total_ms: round(llm_total_ms, 1) } : {}),
      tts_ms: round(tts_ms, 1),
      total_ms: round(total_ms, 1),
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
          : calculateSttCost(this.sttProvider, this._totalSttAudioSeconds, this._pricing);
      tts = calculateTtsCost(this.ttsProvider, this._totalTtsCharacters, this._pricing);
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
    return {
      stt_ms: round(turns.reduce((s, t) => s + t.latency.stt_ms, 0) / n, 1),
      llm_ms: round(turns.reduce((s, t) => s + t.latency.llm_ms, 0) / n, 1),
      ...(ttftAvg !== undefined ? { llm_ttft_ms: ttftAvg } : {}),
      tts_ms: round(turns.reduce((s, t) => s + t.latency.tts_ms, 0) / n, 1),
      total_ms: round(turns.reduce((s, t) => s + t.latency.total_ms, 0) / n, 1),
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
    return {
      stt_ms: round(percentile(nonZero(turns.map((t) => t.latency.stt_ms)), p), 1),
      llm_ms: round(percentile(nonZero(turns.map((t) => t.latency.llm_ms)), p), 1),
      ...(ttftP !== undefined ? { llm_ttft_ms: ttftP } : {}),
      tts_ms: round(percentile(nonZero(turns.map((t) => t.latency.tts_ms)), p), 1),
      total_ms: round(percentile(nonZero(turns.map((t) => t.latency.total_ms)), p), 1),
    };
  }
}
