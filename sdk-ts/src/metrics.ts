/**
 * Call metrics accumulator — tracks cost and latency during a call.
 *
 * Port of the Python `CallMetricsAccumulator` from `sdk/patter/services/metrics.py`.
 */

import {
  calculateRealtimeCost,
  calculateSttCost,
  calculateTelephonyCost,
  calculateTtsCost,
  mergePricing,
  type ProviderPricing,
} from './pricing';

// ---- Data types ----

export interface LatencyBreakdown {
  stt_ms: number;
  llm_ms: number;
  tts_ms: number;
  total_ms: number;
}

export interface CostBreakdown {
  stt: number;
  tts: number;
  llm: number;
  telephony: number;
  total: number;
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
  provider_mode: string;
  stt_provider: string;
  tts_provider: string;
  llm_provider: string;
  telephony_provider: string;
}

// ---- CallControl interface ----

export interface CallControl {
  /** Transfer the call to a different number. */
  transfer(number: string): Promise<void>;
  /** Hang up the call. */
  hangup(): Promise<void>;
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

function p95(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(Math.floor(sorted.length * 0.95), sorted.length - 1);
  return sorted[idx];
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
  private _llmComplete: number | null = null;
  private _ttsFirstByte: number | null = null;
  private _turnUserText = '';
  private _turnSttAudioSeconds = 0;

  // Cumulative usage counters
  private _totalSttAudioSeconds = 0;
  private _totalTtsCharacters = 0;
  private _totalRealtimeCost = 0;
  private _sttByteCount = 0;
  private _sttSampleRate = 16000;
  private _sttBytesPerSample = 2;
  private _actualTelephonyCost: number | null = null;
  private _actualSttCost: number | null = null;

  constructor(opts: {
    callId: string;
    providerMode: string;
    telephonyProvider: string;
    sttProvider?: string;
    ttsProvider?: string;
    llmProvider?: string;
    pricing?: Record<string, Partial<ProviderPricing>> | null;
  }) {
    this.callId = opts.callId;
    this.providerMode = opts.providerMode;
    this.telephonyProvider = opts.telephonyProvider;
    this.sttProvider = opts.sttProvider ?? '';
    this.ttsProvider = opts.ttsProvider ?? '';
    this.llmProvider = opts.llmProvider ?? '';
    this._pricing = mergePricing(opts.pricing);
    this._callStart = hrTimeMs();
  }

  /** Configure audio format for STT byte-to-seconds conversion. */
  configureSttFormat(sampleRate = 16000, bytesPerSample = 2): void {
    this._sttSampleRate = sampleRate;
    this._sttBytesPerSample = bytesPerSample;
  }

  // ---- Turn lifecycle ----

  startTurn(): void {
    this._turnStart = hrTimeMs();
    this._sttComplete = null;
    this._llmComplete = null;
    this._ttsFirstByte = null;
    this._turnUserText = '';
    this._turnSttAudioSeconds = 0;
  }

  recordSttComplete(text: string, audioSeconds = 0): void {
    this._sttComplete = hrTimeMs();
    this._turnUserText = text;
    this._turnSttAudioSeconds = audioSeconds;
    this._totalSttAudioSeconds += audioSeconds;
  }

  recordLlmComplete(): void {
    this._llmComplete = hrTimeMs();
  }

  recordTtsFirstByte(): void {
    if (this._ttsFirstByte === null) {
      this._ttsFirstByte = hrTimeMs();
    }
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

  // ---- Usage tracking ----

  addSttAudioBytes(byteCount: number): void {
    this._sttByteCount += byteCount;
  }

  recordRealtimeUsage(usage: {
    input_token_details?: { audio_tokens?: number; text_tokens?: number };
    output_token_details?: { audio_tokens?: number; text_tokens?: number };
  }): void {
    this._totalRealtimeCost += calculateRealtimeCost(usage, this._pricing);
  }

  setActualTelephonyCost(cost: number): void {
    this._actualTelephonyCost = cost;
  }

  setActualSttCost(cost: number): void {
    this._actualSttCost = cost;
  }

  // ---- Finalize ----

  endCall(): CallMetrics {
    const duration = (hrTimeMs() - this._callStart) / 1000;

    if (this._totalSttAudioSeconds === 0 && this._sttByteCount > 0) {
      this._totalSttAudioSeconds =
        this._sttByteCount / (this._sttSampleRate * this._sttBytesPerSample);
    }

    const cost = this._computeCost(duration);
    const latencyAvg = this._computeAverageLatency();
    const latencyP95 = this._computeP95Latency();

    return {
      call_id: this.callId,
      duration_seconds: round(duration, 2),
      turns: [...this._turns],
      cost,
      latency_avg: latencyAvg,
      latency_p95: latencyP95,
      provider_mode: this.providerMode,
      stt_provider: this.sttProvider,
      tts_provider: this.ttsProvider,
      llm_provider: this.llmProvider,
      telephony_provider: this.telephonyProvider,
    };
  }

  getCostSoFar(): CostBreakdown {
    const duration = (hrTimeMs() - this._callStart) / 1000;
    return this._computeCost(duration);
  }

  // ---- Internal ----

  private _resetTurnState(): void {
    this._turnStart = null;
    this._sttComplete = null;
    this._llmComplete = null;
    this._ttsFirstByte = null;
    this._turnUserText = '';
    this._turnSttAudioSeconds = 0;
  }

  private _computeTurnLatency(): LatencyBreakdown {
    let stt_ms = 0;
    let llm_ms = 0;
    let tts_ms = 0;
    let total_ms = 0;

    if (this._turnStart !== null && this._sttComplete !== null) {
      stt_ms = this._sttComplete - this._turnStart;
    }
    if (this._sttComplete !== null && this._llmComplete !== null) {
      llm_ms = this._llmComplete - this._sttComplete;
    }
    if (this._llmComplete !== null && this._ttsFirstByte !== null) {
      tts_ms = this._ttsFirstByte - this._llmComplete;
    }
    if (this._turnStart !== null && this._ttsFirstByte !== null) {
      total_ms = this._ttsFirstByte - this._turnStart;
    }

    return {
      stt_ms: round(stt_ms, 1),
      llm_ms: round(llm_ms, 1),
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
      llm = 0;
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
    };
  }

  private _computeAverageLatency(): LatencyBreakdown {
    if (this._turns.length === 0) {
      return { stt_ms: 0, llm_ms: 0, tts_ms: 0, total_ms: 0 };
    }
    const n = this._turns.length;
    return {
      stt_ms: round(this._turns.reduce((s, t) => s + t.latency.stt_ms, 0) / n, 1),
      llm_ms: round(this._turns.reduce((s, t) => s + t.latency.llm_ms, 0) / n, 1),
      tts_ms: round(this._turns.reduce((s, t) => s + t.latency.tts_ms, 0) / n, 1),
      total_ms: round(this._turns.reduce((s, t) => s + t.latency.total_ms, 0) / n, 1),
    };
  }

  private _computeP95Latency(): LatencyBreakdown {
    if (this._turns.length === 0) {
      return { stt_ms: 0, llm_ms: 0, tts_ms: 0, total_ms: 0 };
    }
    return {
      stt_ms: round(p95(this._turns.map((t) => t.latency.stt_ms)), 1),
      llm_ms: round(p95(this._turns.map((t) => t.latency.llm_ms)), 1),
      tts_ms: round(p95(this._turns.map((t) => t.latency.tts_ms)), 1),
      total_ms: round(p95(this._turns.map((t) => t.latency.total_ms)), 1),
    };
  }
}
