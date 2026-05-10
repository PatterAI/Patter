/**
 * Speech-edge events for turn-taking instrumentation — TypeScript mirror of
 * ``libraries/python/getpatter/_speech_events.py``.
 *
 * Defines `SpeechEvents`, the per-call dispatcher that fires user-facing async
 * callbacks and (when available) records OpenTelemetry span events on the
 * current call span. The 7 events expose the canonical voice-agent metric
 * set (user/agent state transitions, turn boundaries, TTFT, first-audio) so
 * downstream metrics work without translation, and align with OpenAI
 * Realtime where applicable.
 *
 * This module is private (leading underscore in the file name). The public
 * surface is the 7 ``on*`` getters/setters plus `conversationState` exposed
 * on the `Patter` instance, and `SpeechEvents` is re-exported at the package
 * root for advanced users (custom adapters, test harnesses).
 *
 * Event mapping (with the OpenAI Realtime equivalent where one exists):
 *
 *   User VAD start  : OpenAI Realtime input_audio_buffer.speech_started
 *   User VAD end    : OpenAI Realtime input_audio_buffer.speech_stopped
 *                     (raw VAD edge — *not* end-of-utterance)
 *   User EOU        : OpenAI Realtime input_audio_buffer.committed
 *   Agent first wire: first audio chunk of the agent turn that crosses the wire
 *   Agent done      : last audio chunk of the agent turn (or barge-in)
 *   LLM first token : per-turn TTFT marker
 *   TTS first audio : first TTS audio chunk per turn
 */
import { getLogger } from "./logger";

const logger = getLogger();

/** Async-or-sync callback. Sync return values are silently ignored. */
export type SpeechEventCallback = (
  payload: Readonly<Record<string, unknown>>,
) => void | Promise<void>;

export type UserState = "listening" | "speaking" | "thinking" | "away";
export type AgentState =
  | "initializing"
  | "idle"
  | "listening"
  | "thinking"
  | "speaking";

export interface ConversationStateSnapshot {
  readonly user: UserState;
  readonly agent: AgentState;
}

export type EouTrigger = "vad_silence" | "semantic_turn_detector" | "manual_commit";

interface UserSpeechStartedOptions {
  readonly vadConfidence?: number;
  readonly audioOffsetMs?: number;
  readonly timestampMs?: number;
}

interface UserSpeechEndedOptions extends UserSpeechStartedOptions {
  readonly speechDurationMs: number;
}

interface UserSpeechEosOptions {
  readonly trigger: EouTrigger;
  readonly trailingSilenceMs?: number;
  readonly transcriptSoFar?: string;
  readonly timestampMs?: number;
}

interface AgentSpeechStartedOptions {
  readonly ttsProvider?: string;
  readonly engine?: string;
  readonly timestampMs?: number;
}

interface AgentSpeechEndedOptions {
  readonly speechDurationMs: number;
  readonly interrupted?: boolean;
  readonly timestampMs?: number;
}

interface LlmFirstTokenOptions {
  readonly llmProvider: string;
  readonly model: string;
  readonly timestampMs?: number;
}

interface AudioOutOptions {
  readonly ttsProvider: string;
  readonly timestampMs?: number;
}

/** Lazily-loaded OTel handle. Stays null when the optional peer dep is
 * missing; in that case all `addEvent` calls are no-ops. */
interface OtelTraceApi {
  getActiveSpan?: () => OtelSpanLike | undefined;
  // Older `@opentelemetry/api` versions used `getSpan(context.active())`.
}

interface OtelSpanLike {
  isRecording: () => boolean;
  addEvent: (name: string, attrs?: Record<string, unknown>) => void;
}

let otelTrace: OtelTraceApi | null = null;
let otelLoaded = false;

function loadOtel(): OtelTraceApi | null {
  if (otelLoaded) return otelTrace;
  otelLoaded = true;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require("@opentelemetry/api") as { trace: OtelTraceApi };
    otelTrace = mod.trace;
  } catch {
    otelTrace = null;
  }
  return otelTrace;
}

function recordSpanEvent(name: string, attrs: Record<string, unknown>): void {
  const trace = loadOtel();
  if (trace === null) return;
  try {
    const span = trace.getActiveSpan?.();
    if (!span || !span.isRecording()) return;
    span.addEvent(name, attrs);
  } catch (err) {
    logger.debug?.(`Failed to record OTel span event ${name}: ${String(err)}`);
  }
}

function nowMs(): number {
  return Date.now();
}

/**
 * Per-call dispatcher for the seven turn-taking events. A single instance is
 * shared by every `Patter` instance and survives across calls — the per-turn
 * state (`turnIdx`, `firstTokenForTurn`, `firstAudioForTurn`) lives here too
 * so the runner sees a monotonically-increasing turn index across a session.
 *
 * Backwards compatibility: every callback defaults to `null`. Existing users
 * who never set a callback see exactly the previous behaviour and zero
 * overhead.
 */
export class SpeechEvents {
  // Public callback slots — any of them may be set by the user.
  public onUserSpeechStarted: SpeechEventCallback | null = null;
  public onUserSpeechEnded: SpeechEventCallback | null = null;
  public onUserSpeechEos: SpeechEventCallback | null = null;
  public onAgentSpeechStarted: SpeechEventCallback | null = null;
  public onAgentSpeechEnded: SpeechEventCallback | null = null;
  public onLlmToken: SpeechEventCallback | null = null;
  public onAudioOut: SpeechEventCallback | null = null;

  // State machine — read via `conversationState`.
  private userState: UserState = "listening";
  private agentState: AgentState = "initializing";

  // Per-turn cursors. `turnIdxValue` increments on every committed EOU.
  private turnIdxValue = 0;
  private firstTokenForTurn = true;
  private firstAudioForTurn = true;

  // Optional call start (ms since epoch) — used to compute `audioOffsetMs`
  // payloads when the caller does not provide one.
  private callStartMs: number | null = null;

  /** Snapshot of the current per-side state of the call. */
  get conversationState(): ConversationStateSnapshot {
    return { user: this.userState, agent: this.agentState };
  }

  /** Current 0-based turn index. Increments on every EOU commit. */
  get turnIdx(): number {
    return this.turnIdxValue;
  }

  /** Record the call-start wall-clock for ``audioOffsetMs`` math. */
  markCallStarted(tsMs?: number): void {
    this.callStartMs = tsMs ?? nowMs();
    this.userState = "listening";
    this.agentState = "idle";
  }

  /** Reset per-turn cursors. Called automatically on EOU commit. */
  resetTurnState(): void {
    this.firstTokenForTurn = true;
    this.firstAudioForTurn = true;
  }

  // ---- User-side events -----------------------------------------------

  /** Fire on the VAD positive edge of the inbound stream.
   *
   * Do not coalesce: the runner consumes positive→negative→positive
   * transitions in order. For server-VAD engines (OpenAI Realtime, Telnyx
   * Voice AI), forward the upstream signal directly — do not re-run a VAD
   * layer on top.
   */
  async fireUserSpeechStarted(opts: UserSpeechStartedOptions = {}): Promise<void> {
    const tsMs = opts.timestampMs ?? nowMs();
    const payload: Record<string, unknown> = { timestamp_ms: tsMs };
    if (opts.vadConfidence !== undefined)
      payload.vad_confidence = opts.vadConfidence;
    const offset = this.resolveOffset(opts.audioOffsetMs, tsMs);
    if (offset !== null) payload.audio_offset_ms = offset;
    this.userState = "speaking";
    await this.dispatch(this.onUserSpeechStarted, payload, {
      spanEvent: "patter.event.user_speech_started",
      spanAttrs: filterUndef({
        "patter.audio.offset_ms": payload.audio_offset_ms as number | undefined,
        "patter.vad.confidence": payload.vad_confidence as number | undefined,
      }),
    });
  }

  /** Fire on the VAD trailing edge (raw — *not* EOU).
   *
   * `speechDurationMs` is the length of the segment that just ended; the
   * runner uses it to compute talk-ratio.
   */
  async fireUserSpeechEnded(opts: UserSpeechEndedOptions): Promise<void> {
    const tsMs = opts.timestampMs ?? nowMs();
    const payload: Record<string, unknown> = {
      timestamp_ms: tsMs,
      speech_duration_ms: opts.speechDurationMs,
    };
    if (opts.vadConfidence !== undefined)
      payload.vad_confidence = opts.vadConfidence;
    const offset = this.resolveOffset(opts.audioOffsetMs, tsMs);
    if (offset !== null) payload.audio_offset_ms = offset;
    this.userState = "listening";
    await this.dispatch(this.onUserSpeechEnded, payload, {
      spanEvent: "patter.event.user_speech_ended",
      spanAttrs: { "patter.speech.duration_ms": opts.speechDurationMs },
    });
  }

  /** Fire on the committed end-of-utterance.
   *
   * This is the canonical "user finished" signal — VAD edge + trailing
   * silence + (optionally) a semantic turn-detector model agreement. The
   * runner uses the timestamp of this event to compute
   * `eos_to_first_token_ms` (Hamming AI threshold: <800 ms good, >1500 ms
   * critical).
   */
  async fireUserSpeechEos(opts: UserSpeechEosOptions): Promise<void> {
    const tsMs = opts.timestampMs ?? nowMs();
    const payload: Record<string, unknown> = {
      timestamp_ms: tsMs,
      trigger: opts.trigger,
    };
    if (opts.trailingSilenceMs !== undefined)
      payload.trailing_silence_ms = opts.trailingSilenceMs;
    if (opts.transcriptSoFar !== undefined)
      payload.transcript_so_far = opts.transcriptSoFar;

    // EOU commit advances turn_idx and arms first-token / first-audio.
    this.turnIdxValue += 1;
    this.resetTurnState();
    this.userState = "listening";
    this.agentState = "thinking";

    await this.dispatch(this.onUserSpeechEos, payload, {
      spanEvent: "patter.event.user_speech_eos",
      spanAttrs: filterUndef({
        "patter.eos.trigger": opts.trigger,
        "patter.eos.trailing_silence_ms": opts.trailingSilenceMs,
      }),
    });
  }

  // ---- Agent-side events ----------------------------------------------

  /** Fire on the FIRST audio chunk of the current agent turn that crosses
   * to the wire (not the first chunk produced by TTS).
   *
   * The user hears the wire chunk, so this is the timestamp the runner
   * anchors barge-in latency on.
   */
  async fireAgentSpeechStarted(
    opts: AgentSpeechStartedOptions = {},
  ): Promise<void> {
    const tsMs = opts.timestampMs ?? nowMs();
    const payload: Record<string, unknown> = {
      timestamp_ms: tsMs,
      turn_idx: this.turnIdxValue,
    };
    if (opts.ttsProvider !== undefined) payload.tts_provider = opts.ttsProvider;
    if (opts.engine !== undefined) payload.engine = opts.engine;
    this.agentState = "speaking";
    await this.dispatch(this.onAgentSpeechStarted, payload, {
      spanEvent: "patter.event.agent_speech_started",
      spanAttrs: filterUndef({
        "patter.turn.idx": this.turnIdxValue,
        "patter.tts.provider": opts.ttsProvider,
        "patter.engine": opts.engine,
      }),
    });
  }

  /** Fire on the LAST audio chunk of the current agent turn.
   *
   * `interrupted=true` marks the turn as cancelled by barge-in; the runner
   * treats it as the `agent_speech_stopped` half of a barge-in pair.
   */
  async fireAgentSpeechEnded(opts: AgentSpeechEndedOptions): Promise<void> {
    const tsMs = opts.timestampMs ?? nowMs();
    const interrupted = opts.interrupted ?? false;
    const payload: Record<string, unknown> = {
      timestamp_ms: tsMs,
      turn_idx: this.turnIdxValue,
      speech_duration_ms: opts.speechDurationMs,
      interrupted,
    };
    this.agentState = "idle";
    await this.dispatch(this.onAgentSpeechEnded, payload, {
      spanEvent: "patter.event.agent_speech_ended",
      spanAttrs: {
        "patter.turn.idx": this.turnIdxValue,
        "patter.speech.duration_ms": opts.speechDurationMs,
        "patter.turn.interrupted": interrupted,
      },
    });
  }

  // ---- LLM / TTS events -----------------------------------------------

  /** Fire on the FIRST LLM token of the current turn (TTFT marker).
   *
   * Idempotent within a turn — guarded by `firstTokenForTurn`. Combined
   * with `on_user_speech_eos.timestamp_ms` the runner computes
   * `eos_to_first_token_ms`.
   */
  async fireLlmFirstToken(opts: LlmFirstTokenOptions): Promise<void> {
    if (!this.firstTokenForTurn) return;
    this.firstTokenForTurn = false;
    const tsMs = opts.timestampMs ?? nowMs();
    const payload: Record<string, unknown> = {
      timestamp_ms: tsMs,
      turn_idx: this.turnIdxValue,
      llm_provider: opts.llmProvider,
      model: opts.model,
    };
    await this.dispatch(this.onLlmToken, payload, {
      spanEvent: "patter.event.llm_first_token",
      spanAttrs: {
        "gen_ai.request.model": opts.model,
        "gen_ai.provider.name": opts.llmProvider,
        "patter.turn.idx": this.turnIdxValue,
      },
    });
  }

  /** Fire on the FIRST TTS audio chunk for the current turn.
   *
   * Distinct from `fireAgentSpeechStarted`: this is the agent-side buffer
   * arrival (TTS warmup), not the wire-time chunk. Idempotent within a
   * turn — guarded by `firstAudioForTurn`.
   */
  async fireAudioOut(opts: AudioOutOptions): Promise<void> {
    if (!this.firstAudioForTurn) return;
    this.firstAudioForTurn = false;
    const tsMs = opts.timestampMs ?? nowMs();
    const payload: Record<string, unknown> = {
      timestamp_ms: tsMs,
      turn_idx: this.turnIdxValue,
      tts_provider: opts.ttsProvider,
    };
    await this.dispatch(this.onAudioOut, payload, {
      spanEvent: "patter.event.tts_first_audio",
      spanAttrs: {
        "patter.turn.idx": this.turnIdxValue,
        "patter.tts.provider": opts.ttsProvider,
      },
    });
  }

  // ---- Internal -------------------------------------------------------

  private resolveOffset(given: number | undefined, tsMs: number): number | null {
    if (given !== undefined) return given;
    if (this.callStartMs !== null) return Math.max(0, tsMs - this.callStartMs);
    return null;
  }

  private async dispatch(
    cb: SpeechEventCallback | null,
    payload: Record<string, unknown>,
    opts: { spanEvent: string; spanAttrs: Record<string, unknown> },
  ): Promise<void> {
    recordSpanEvent(opts.spanEvent, opts.spanAttrs);
    if (cb === null) return;
    try {
      await cb(payload);
    } catch (err) {
      // Never propagate observer errors to the live call.
      logger.warn?.(
        `Speech-event callback ${opts.spanEvent} raised: ${String(err)}`,
      );
    }
  }
}

function filterUndef(obj: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v !== undefined) out[k] = v;
  }
  return out;
}
