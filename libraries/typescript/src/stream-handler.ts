/**
 * Shared stream handling logic for Twilio and Telnyx WebSocket connections.
 *
 * Encapsulates provider initialization, audio routing, transcript management,
 * metrics, guardrails, tool calling, call control, and on_message dispatching.
 * The provider-specific handlers in server.ts parse their respective WebSocket
 * message formats and delegate to this shared layer.
 */

import { WebSocket as WSWebSocket } from 'ws';
import { OpenAIRealtimeAdapter } from './providers/openai-realtime';
import { ElevenLabsConvAIAdapter } from './providers/elevenlabs-convai';
import { DeepgramSTT } from './providers/deepgram-stt';
import { createTTS } from './provider-factory';
import type { STTAdapter, TTSAdapter, STTTranscript } from './provider-factory';
import { CallMetricsAccumulator } from './metrics';
import { mulawToPcm16, pcm16ToMulaw, StatefulResampler, createResampler8kTo16k, createResampler16kTo8k } from './audio/transcoding';
import { LLMLoop } from './llm-loop';
import { RemoteMessageHandler, isRemoteUrl, isWebSocketUrl } from './remote-message';
import { createHistoryManager } from './handler-utils';
import { DefaultToolExecutor } from './llm-loop';
import { MCPManager } from './tools/mcp-client';
import type { AgentOptions, Guardrail, HookContext, PipelineMessageHandler, ToolDefinition, VADProvider } from './types';
import type { MetricsStore } from './dashboard/store';
import { getLogger } from './logger';
import { validateTwilioSid } from './server';
import type { ProviderPricing } from './pricing';
import { SentenceChunker } from './sentence-chunker';
import { PipelineHookExecutor } from './pipeline-hooks';
import { EventBus } from './observability/event-bus';
import type { PatterEventType } from './observability/event-bus';
import {
  SPAN_BARGEIN,
  SPAN_ENDPOINT,
  SPAN_LLM,
  startSpan,
} from './observability/tracing';

type AIAdapter = OpenAIRealtimeAdapter | ElevenLabsConvAIAdapter;

// ---------------------------------------------------------------------------
// Telephony bridge — abstracts Twilio vs Telnyx wire differences
// ---------------------------------------------------------------------------

/** Provider-specific operations that differ between Twilio and Telnyx. */
export interface TelephonyBridge {
  /** Human-readable label for log messages. */
  readonly label: string;
  /** Telephony provider name for metrics. */
  readonly telephonyProvider: 'twilio' | 'telnyx';

  /** Send an audio chunk (base64-encoded) to the telephony WebSocket. */
  sendAudio(ws: WSWebSocket, audioBase64: string, streamSid: string): void;
  /** Send a mark event to track audio playback progress (no-op for Telnyx). */
  sendMark(ws: WSWebSocket, markName: string, streamSid: string): void;
  /** Send a clear/interrupt event to stop audio playback. */
  sendClear(ws: WSWebSocket, streamSid: string): void;

  /** Transfer the call to a different number or SIP URI via provider API. */
  transferCall(callId: string, toNumber: string): Promise<void>;
  /** Hang up the call via provider API. */
  endCall(callId: string, ws: WSWebSocket): Promise<void>;
  /** Send DTMF digits via provider API (optional; default no-op). */
  sendDtmf?(callId: string, digits: string, delayMs: number): Promise<void>;
  /** Start call recording via provider API (optional). */
  startRecording?(callId: string): Promise<void>;
  /** Stop call recording via provider API (optional). */
  stopRecording?(callId: string): Promise<void>;

  /** Create an STT instance appropriate for this provider's audio format.
   *  Returns any of the supported STT adapters (DeepgramSTT, WhisperSTT,
   *  CartesiaSTT, SonioxSTT, AssemblyAISTT) or null when no STT is configured. */
  createStt(agent: AgentOptions): Promise<STTAdapter | null>;
  /** Query actual telephony costs after call ends. */
  queryTelephonyCost(metricsAcc: CallMetricsAccumulator, callId: string): Promise<void>;
}

// ---------------------------------------------------------------------------
// Shared utility: guardrails
// ---------------------------------------------------------------------------

function checkGuardrails(text: string, guardrails: Guardrail[] | undefined): Guardrail | null {
  if (!guardrails) return null;
  for (const guard of guardrails) {
    let blocked = false;
    if (guard.blockedTerms) {
      blocked = guard.blockedTerms.some((term) => text.toLowerCase().includes(term.toLowerCase()));
    }
    if (!blocked && guard.check) {
      blocked = guard.check(text);
    }
    if (blocked) return guard;
  }
  return null;
}

/** Strip control characters and truncate a string before writing it to logs. */
export function sanitizeLogValue(v: string, maxLen = 200): string {
  // eslint-disable-next-line no-control-regex
  const cleaned = v.replace(/[\x00-\x1f\x7f]/g, '');
  return cleaned.length > maxLen ? cleaned.slice(0, maxLen) + '...' : cleaned;
}

/**
 * Mask an E.164 phone number for logging. Keeps only the last 4 characters
 * to preserve enough context for correlation while avoiding PII leakage.
 * Mirrors ``getpatter.utils.log_sanitize.mask_phone_number``.
 */
export function maskPhoneNumber(number: unknown): string {
  if (!number) return '***';
  const text = String(number);
  if (text.length <= 4) return '***';
  return `***${text.slice(-4)}`;
}

function isValidE164(number: string): boolean {
  return /^\+[1-9]\d{6,14}$/.test(number);
}

/**
 * Short words / phrases that Whisper (and, less often, Deepgram) routinely
 * emit when fed silence or TTS echo on mulaw 8 kHz. Dropping them as turns
 * prevents the caller from entering a feedback loop where every silent frame
 * triggers a new LLM+TTS turn.
 */
const HALLUCINATIONS = new Set([
  'you', 'thank you', 'thanks', 'yeah', 'yes', 'no',
  'okay', 'ok', 'uh', 'um', 'mmm', 'hmm', '.', 'bye',
  'right', 'cool',
]);

// ---------------------------------------------------------------------------
// StreamHandler context (immutable per-call configuration)
// ---------------------------------------------------------------------------

/** Per-call dependencies injected into `StreamHandler` (immutable for the call's lifetime). */
export interface StreamHandlerDeps {
  readonly config: {
    readonly openaiKey?: string;
    readonly twilioSid?: string;
    readonly twilioToken?: string;
  };
  readonly agent: AgentOptions;
  readonly bridge: TelephonyBridge;
  readonly metricsStore: MetricsStore;
  readonly pricing: Record<string, Partial<ProviderPricing>> | null;
  readonly remoteHandler: RemoteMessageHandler;
  readonly onCallStart?: (data: Record<string, unknown>) => Promise<void>;
  readonly onCallEnd?: (data: Record<string, unknown>) => Promise<void>;
  readonly onTranscript?: (data: Record<string, unknown>) => Promise<void>;
  readonly onMessage?: PipelineMessageHandler | string;
  readonly onMetrics?: (data: Record<string, unknown>) => Promise<void>;
  readonly recording: boolean;
  /** When true, only the first TTFB per call is forwarded to the event bus. Default false. */
  readonly reportOnlyInitialTtfb?: boolean;
  /**
   * Optional speech-edge events dispatcher. When provided, the handler emits
   * turn-taking edges (VAD start/stop, EOU commit, agent first/last wire
   * chunk) as the call progresses. ``undefined`` means no events are fired
   * — exact prior behaviour. See ``src/_speech-events.ts``.
   */
  readonly speechEvents?: import("./_speech-events").SpeechEvents;
  /** Build an AI adapter (OpenAI Realtime or ElevenLabs ConvAI). Injected to avoid circular imports. */
  readonly buildAIAdapter: (resolvedPrompt: string) => AIAdapter;
  /** Sanitize untrusted key-value variables map. */
  readonly sanitizeVariables: (raw: Record<string, unknown>) => Record<string, string>;
  /** Replace {key} placeholders in a template string. */
  readonly resolveVariables: (template: string, variables: Record<string, string>) => string;
  /**
   * Optional accessor returning pre-rendered first-message audio for
   * ``callId``. Wired by ``Patter.serve()`` when the parent client has
   * ``agent.prewarmFirstMessage: true``. Returning ``undefined`` means
   * "no prewarm — always run live TTS".
   */
  readonly popPrewarmAudio?: (callId: string) => Buffer | undefined;
  /**
   * Optional accessor returning pre-opened, fully-handshaked provider
   * WebSockets for ``callId`` so the per-call StreamHandler can
   * adopt them at ``start`` instead of paying the cold handshake on
   * the first turn. Wired by ``Patter.serve()``. Returning
   * ``undefined`` (or any sub-field unset) means "no parked socket
   * for this provider — fall back to fresh ``connect()``".
   */
  readonly popPrewarmedConnections?: (
    callId: string,
  ) => import('./client').ParkedProviderConnections | undefined;
}

// ---------------------------------------------------------------------------
// StreamHandler — manages a single call session
// ---------------------------------------------------------------------------

/** Per-call session controller — owns the AI adapter, STT/TTS pipeline, and metrics. */
export class StreamHandler {
  private readonly deps: StreamHandlerDeps;
  private readonly ws: WSWebSocket;
  private caller: string;
  private callee: string;

  // Mutable call state
  private streamSid = '';
  private callId = '';
  private adapter: AIAdapter | null = null;
  private stt: STTAdapter | null = null;
  private tts: TTSAdapter | null = null;
  private isSpeaking = false;
  /**
   * Ring buffer of inbound PCM16 16 kHz frames captured while the agent
   * is speaking and the self-hearing guard is dropping audio. On
   * barge-in we flush this buffer to STT so Deepgram (or any other
   * streaming STT) receives the user's first ~500 ms of speech — which
   * would otherwise be lost while the VAD's `minSpeechDuration` window
   * accumulated and fired `speech_start`. Each frame is 20 ms × 32 bytes
   * (16 kHz × 16-bit mono) ≈ 640 bytes.
   *
   * Capped to ``INBOUND_AUDIO_RING_FRAMES`` to recover only the
   * VAD-missed leading edge of the user's speech (default 250 ms,
   * matching SileroVAD ``minSpeechDuration``). Earlier values up to
   * 600 ms were including ~350 ms of pre-speech silence/agent-bleed in
   * the replay; on PSTN (where AEC is a no-op) Deepgram trained on
   * English happily transcribes that bleed as English garbage
   * (``"The same as Edgar,"``, ``"Permadees."``) and commits it to
   * the LLM as a phantom user transcript. See BUGS.md 2026-05-05
   * post-barge-in bleed-transcription entry.
   */
  private inboundAudioRing: Buffer[] = [];
  private static readonly INBOUND_AUDIO_RING_FRAMES = 13;
  /**
   * Cached LLM provider tag used by speech-event payloads. Mirrors the
   * value passed to the metrics accumulator at construction time so the
   * speech-edge events report the same provider classification as
   * dashboard / pricing rows.
   */
  private llmProviderTag: string = "openai";
  /** Set to true after a VAD error to suppress log spam for the rest of the call. */
  private vadDisabled = false;
  /**
   * Auto-loaded SileroVAD when ``agent.vad`` is undefined. Populated by
   * ``initPipeline`` and queried alongside ``agent.vad`` on every audio frame.
   * Stays null when ``onnxruntime-node`` is not installed — the pipeline
   * then falls back to the STT-endpoint heuristic (legacy behaviour).
   */
  private autoVad: VADProvider | null = null;
  /**
   * Acoustic echo canceller (NLMS adaptive filter). Lazily instantiated in
   * ``initPipeline`` when ``agent.echoCancellation`` is true. ``null``
   * otherwise — the mic path stays a pure pass-through for handset /
   * headset deployments that don't have TTS bleed.
   */
  private aec: import('./audio/aec').NlmsEchoCanceller | null = null;
  /**
   * Monotonic counter incremented on every TTS-start. The grace timer
   * scheduled by ``endSpeakingWithGrace`` only flips ``isSpeaking=false``
   * if the counter still matches its capture — a new turn that started in
   * the meantime invalidates the obsolete timer instead of clobbering its
   * own ``isSpeaking=true``.
   */
  private speakingGeneration = 0;
  /**
   * Wall-clock timestamp (ms since epoch) when the current TTS turn
   * started — captured by ``beginSpeaking`` and cleared by
   * ``cancelSpeaking`` / the grace flip. Used to gate barge-in: we
   * suppress the cancel for the first
   * ``MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_AEC`` of every turn (when AEC
   * is on) so the AEC filter has time to converge — otherwise residual
   * TTS bleed in the mic stream looks like user speech to VAD and
   * triggers an immediate self-cancellation of the agent's first
   * sentence.
   */
  private speakingStartedAt: number | null = null;
  /**
   * Wall-clock (ms) when the FIRST TTS audio chunk actually reached the
   * carrier wire — set in ``markFirstAudioSent`` after ``bridge.sendAudio``
   * succeeds, cleared by ``beginSpeaking`` / ``cancelSpeaking``. The barge-in
   * gate measures elapsed from this instant, NOT from ``speakingStartedAt``,
   * because ElevenLabs (and other cloud TTS) take 200-700 ms to emit the
   * first byte. A gate anchored to ``beginSpeaking`` would expire on
   * background noise before any audio went out, exit the TTS loop on
   * ``isSpeaking=false``, and silently cut the agent's first turn.
   */
  private firstAudioSentAt: number | null = null;
  /**
   * Optional barge-in confirmation strategies. With an empty array the
   * SDK falls back to the legacy "cancel on first VAD speech_start"
   * behaviour. With one or more strategies, a VAD speech_start during
   * TTS marks the barge-in as *pending* — TTS keeps streaming naturally
   * — and the strategies are consulted on every STT transcript via
   * ``handleBargeIn``. The first strategy that returns ``true`` cancels
   * the agent; if none confirm within ``bargeInConfirmMs`` the pending
   * state is dropped and the agent finishes its sentence.
   */
  private readonly bargeInStrategies: readonly import('./services/barge-in-strategies').BargeInStrategy[];
  /** Pending-barge-in confirmation timeout in milliseconds. */
  private readonly bargeInConfirmMs: number;
  /** Wall-clock (ms) when the current pending barge-in started, or
   * ``null`` if no barge-in is pending. */
  private bargeInPendingSince: number | null = null;
  /** Timer that fires the pending-barge-in timeout. */
  private bargeInPendingTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * Set to true when a VAD ``speech_start`` was suppressed by the
   * anti-echo gate during the current agent turn.  Cleared on
   * ``beginSpeaking`` and ``cancelSpeaking``.  When the turn ends
   * naturally (grace timer), the inbound audio ring is flushed to STT
   * so the user's speech is not silently discarded.
   */
  private suppressedSpeechPending = false;
  /**
   * Minimum wall-clock duration (ms) the agent must have been speaking
   * before barge-in is allowed to fire when AEC is active. Covers the
   * AEC warmup window (~500 ms) plus a safety margin so residual bleed
   * during the convergence period does not self-trigger barge-in.
   */
  private static readonly MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_AEC = 1000;
  /**
   * Same as the AEC variant but for deployments where AEC is OFF
   * (default on PSTN — Twilio/Telnyx). Without an adaptive filter to
   * converge, the only justification for a gate is anti-flicker on
   * micro-events (cough, click). 100 ms covers the first PSTN echo
   * round-trip (~40-100 ms) while allowing barge-in from 100 ms into
   * the agent's turn — covering nearly all of any response.
   * Previously 250 ms, which blocked barge-in entirely on short (<500 ms)
   * agent responses.
   */
  private static readonly MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_NO_AEC = 100;
  /** Handle for the pending grace-period timer, so it can be cleared on cleanup. */
  private graceTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * AbortController for the current LLM streaming consumption.  Aborted by
   * ``cancelSpeaking`` so the in-flight LLM stream stops generating tokens
   * we will never speak — saves provider cost and frees the connection
   * earlier.  Mirrors Python ``_llm_cancel_event``.
   */
  private llmAbort: AbortController | null = null;

  /**
   * Wall-clock timestamp of the most recent ``cancelSpeaking`` call, or
   * ``null`` if no cancel has fired since the call started. Used by
   * ``beginSpeaking`` to enforce a short post-cancel drain window so the
   * remote PSTN player finishes flushing the previous turn's in-flight
   * audio before the next TTS chunk lands on top of it. Without this,
   * the first sentence of a post-barge-in turn audibly overlaps with
   * the tail of the cancelled turn (~50-200 ms of doubled audio).
   */
  private lastCancelAt: number | null = null;
  /**
   * Promise queue tracking outstanding Twilio marks the SDK has sent but
   * not yet seen echoed back. Used by the firstMessage send loop to bound
   * the depth of audio queued at the carrier — without this the loop
   * pushes the entire TTS stream into Twilio's WebSocket in one burst,
   * and a sendClear issued mid-buffer races against several seconds of
   * already-queued media frames (BUG #128). The window depth is
   * ``FIRST_MESSAGE_MARK_WINDOW``; ``onMark`` drains entries as Twilio
   * confirms playback, ``cancelSpeaking`` resolves every pending entry so
   * any awaiter exits immediately. Telnyx never populates this queue
   * (Telnyx's media-stream protocol has no mark concept — the loop
   * falls back to time-based pacing on that carrier).
   */
  private pendingMarks: Array<{
    name: string;
    resolve: () => void;
    promise: Promise<void>;
  }> = [];
  /**
   * Monotonic counter for first-message mark names. Distinct from
   * ``chunkCount`` (which the Realtime path uses) so the two paths can
   * coexist without name collisions even when firstMessage finishes while
   * a Realtime turn is still streaming.
   */
  private firstMessageMarkCounter = 0;
  /**
   * Maximum unconfirmed Twilio marks while streaming firstMessage. Each
   * chunk is 40 ms of audio at 16 kHz PCM16, so a window of 3 caps
   * the in-flight queue at ~120 ms. This means a barge-in's
   * ``sendClear`` has at most 120 ms of already-buffered audio to flush
   * — vs. ~2-5 s with the previous burst-send code, which was the
   * root cause of "firstMessage non interrompibile". Higher values
   * smooth playback under jittery RTT (each mark echo adds ~150-250 ms
   * RTT on PSTN) at the cost of longer barge-in latency; lower values
   * risk under-buffering. 3 hit the smallest barge-in cap without
   * audible gaps in 2026-05 acceptance.
   */
  private static readonly FIRST_MESSAGE_MARK_WINDOW = 3;
  /**
   * Per-chunk soft timeout (ms) while awaiting a mark echo. Twilio's
   * mark echoes typically arrive within 100-250 ms of audio playback.
   * Capping at 500 ms guards against carriers (or test doubles) that
   * never echo — without it a stalled echo would deadlock the loop and
   * the agent would freeze mid-utterance. On timeout we drop the
   * waiter from the queue and continue: playout may glitch by one
   * chunk but the call stays alive.
   */
  private static readonly MARK_AWAIT_TIMEOUT_MS = 500;
  /**
   * Minimum drain window (ms) between a ``cancelSpeaking`` and the next
   * ``beginSpeaking``. 150 ms covers a typical PSTN jitter buffer drain
   * + Twilio Media Stream clear propagation. Lower values risk audio
   * overlap on the first chunk; higher values increase the perceived
   * "agent ack" latency after a barge-in. 150 ms is the smallest value
   * that consistently eliminated the overlap during 0.6.0 acceptance.
   */
  private static readonly POST_CANCEL_DRAIN_MS = 150;

  /**
   * Mark the start of a TTS span. Use instead of setting isSpeaking
   * directly. Awaits the post-cancel drain window before flipping state
   * so the remote player has time to flush the cancelled turn's tail.
   */
  private async beginSpeaking(isFirstMessage = false): Promise<void> {
    if (this.lastCancelAt !== null) {
      const elapsed = Date.now() - this.lastCancelAt;
      const remaining = StreamHandler.POST_CANCEL_DRAIN_MS - elapsed;
      if (remaining > 0) {
        await new Promise<void>((r) => setTimeout(r, remaining));
      }
    }
    this.speakingGeneration++;
    this.isSpeaking = true;
    this.speakingStartedAt = Date.now();
    this.suppressedSpeechPending = false;
    // Stamp ``firstAudioSentAt`` synchronously for EVERY turn so the
    // ``canBargeIn()`` gate (250ms anti-flicker for PSTN no-AEC) runs in
    // PARALLEL with LLM TTFT + TTS TTFB rather than starting only after
    // the first audio chunk reaches the wire. Without this, a turn with
    // a slow LLM (gpt-4o cold cache ~2 s) is effectively un-interruptible
    // for the entire LLM window: ``firstAudioSentAt`` stays null, so
    // ``canBargeIn`` returns false and every VAD ``speech_start`` is
    // suppressed silently. Previously this fix was firstMessage-only;
    // promoted to default on 2026-05-11 after the user reported
    // "barge-in non funziona più" with gpt-4o.
    //
    // Note: the ``isFirstMessage`` parameter is kept for backward
    // compatibility with the call site, but no longer changes behaviour.
    void isFirstMessage;
    this.firstAudioSentAt = Date.now();
    // Fresh turn — drop any stale pre-barge-in buffer from a previous turn
    // so we never replay yesterday's audio to STT.
    this.inboundAudioRing = [];
    // Reset the VAD detector so the next user utterance triggers a clean
    // SILENCE→SPEECH transition. Without this, PSTN echo from the previous
    // turn can keep the detector's smoothed probability above the
    // deactivation threshold (0.35) for the entire turn — the VAD never
    // returns to SILENCE, ``speech_start`` never fires for the user's next
    // utterance, and barge-in feels "one-shot" (works once, then never
    // again). The user's previous utterance was already committed by STT
    // before ``beginSpeaking`` is called, so resetting state here cannot
    // lose data.
    this.resetVad();
  }

  /**
   * Record that the first TTS audio chunk of the current turn has hit the
   * carrier wire. Idempotent within a turn — only the first call sets the
   * timestamp; later chunks are no-ops. Must be invoked AFTER the underlying
   * ``bridge.sendAudio`` resolves so the gate is anchored to "audio actually
   * went out", not "we asked the carrier to send it".
   */
  private markFirstAudioSent(): void {
    if (this.firstAudioSentAt === null) {
      this.firstAudioSentAt = Date.now();
    }
  }

  /**
   * Atomically end speaking AND invalidate any pending grace timer.
   * Use instead of ``this.isSpeaking = false`` at barge-in sites.
   *
   * Also aborts the in-flight LLM stream (if any) so the provider stops
   * billing tokens we will never speak.
   */
  private cancelSpeaking(): void {
    this.speakingGeneration++; // invalidates pending grace timers
    this.isSpeaking = false;
    this.speakingStartedAt = null;
    this.firstAudioSentAt = null;
    this.lastCancelAt = Date.now();
    this.suppressedSpeechPending = false;
    // Drain any firstMessage mark waiters so a loop blocked on
    // ``waitForMarkWindow`` exits on the next tick and observes
    // ``!isSpeaking``. Without this the loop would stay blocked until
    // each mark either echoes (carrier still draining its queue) or
    // hits ``MARK_AWAIT_TIMEOUT_MS`` — keeping the agent "speaking"
    // from the user's perspective for hundreds of extra ms after
    // barge-in.
    this.drainPendingMarks();
    if (this.llmAbort !== null) {
      try {
        this.llmAbort.abort();
      } catch {
        // No-op — abort() throws nothing in modern runtimes, but be defensive.
      }
    }
  }

  /**
   * Resolve every entry in ``pendingMarks`` and empty the queue. Idempotent
   * — safe to call from ``cancelSpeaking`` and again from the grace path
   * without leaking pending promises.
   */
  private drainPendingMarks(): void {
    if (this.pendingMarks.length === 0) return;
    for (const entry of this.pendingMarks) {
      try {
        entry.resolve();
      } catch {
        // No-op — pending entries always own a fresh resolve fn.
      }
    }
    this.pendingMarks.length = 0;
  }

  /**
   * Push a Twilio ``mark`` event AFTER the corresponding audio chunk and
   * return a promise that resolves when the mark is echoed back via
   * ``onMark`` (or when ``cancelSpeaking`` drains the queue, or after
   * ``MARK_AWAIT_TIMEOUT_MS``). Returns null on non-Twilio carriers — the
   * caller is expected to fall back to time-based pacing in that case.
   */
  private sendMarkAwaitable(): Promise<void> | null {
    if (this.deps.bridge.telephonyProvider !== 'twilio') return null;
    this.firstMessageMarkCounter += 1;
    const markName = `fm_${this.firstMessageMarkCounter}`;
    let resolve!: () => void;
    const promise = new Promise<void>((r) => {
      resolve = r;
    });
    this.pendingMarks.push({ name: markName, resolve, promise });
    try {
      this.deps.bridge.sendMark(this.ws, markName, this.streamSid);
    } catch (err) {
      getLogger().debug(`sendMark failed (${markName}): ${String(err)}`);
      // Drop the waiter immediately so the queue doesn't fill with
      // never-resolving entries that block the window.
      const idx = this.pendingMarks.findIndex((m) => m.name === markName);
      if (idx >= 0) this.pendingMarks.splice(idx, 1);
      return Promise.resolve();
    }
    return promise;
  }

  /**
   * If the in-flight mark queue is at or above ``FIRST_MESSAGE_MARK_WINDOW``
   * entries, wait for the oldest entry to clear (mark echoed, agent
   * cancelled, or per-mark timeout). Repeats until the queue depth is
   * within the window — under high RTT the carrier may have several
   * marks queued and we want every loop iteration to be naturally back-
   * pressured by playback.
   */
  private async waitForMarkWindow(): Promise<void> {
    while (
      this.isSpeaking &&
      this.pendingMarks.length >= StreamHandler.FIRST_MESSAGE_MARK_WINDOW
    ) {
      const oldest = this.pendingMarks[0];
      const timeout = new Promise<void>((resolve) =>
        setTimeout(resolve, StreamHandler.MARK_AWAIT_TIMEOUT_MS),
      );
      await Promise.race([oldest.promise, timeout]);
      // Drop the head if it's still the same entry — onMark would
      // have already removed it on echo; only a timeout leaves it
      // in place.
      if (this.pendingMarks[0] === oldest) {
        this.pendingMarks.shift();
      }
    }
  }

  /**
   * Bytes-per-millisecond for a 16 kHz PCM16 mono stream. Used by the
   * non-Twilio firstMessage pacing path to translate chunk size into a
   * playout-duration sleep. 16000 samples/sec × 2 bytes = 32 bytes/ms.
   */
  private static readonly PCM16_16K_BYTES_PER_MS = 32;

  /** Cancel and clear the pending grace timer, if any. */
  private clearGraceTimer(): void {
    if (this.graceTimer !== null) {
      clearTimeout(this.graceTimer);
      this.graceTimer = null;
    }
  }

  /**
   * Mark the agent as no longer producing TTS, honoring a grace period that
   * approximates the carrier's playback buffer. The user may still hear the
   * agent for ~1 s after we finish pushing audio (Twilio buffers ~1500 ms);
   * keeping isSpeaking=true through that window keeps the VAD-driven
   * barge-in armed during the audible tail. Tunable via env.
   */
  private endSpeakingWithGrace(): void {
    const grace = Number(process.env.PATTER_TTS_TAIL_GRACE_MS ?? 1500);
    // NOTE: we DO NOT flush ``inboundAudioRing`` here — the ring is only
    // drained on a real barge-in (where VAD confirmed user speech). Flushing
    // on every natural turn end was tried in an earlier iteration and
    // caused garbled out-of-order responses: the ring captured during the
    // agent's TTS contains audio with partially-cancelled echo and possibly
    // over-cancelled user voice (Geigel rho=0.6 misses quiet double-talk).
    // Replaying that to STT on every turn produced phantom transcripts that
    // raced live STT input and confused the LLM. Audio captured during the
    // agent's turn that VAD did NOT classify as speech is intentionally
    // dropped at the next ``beginSpeaking()``.
    if (grace > 0) {
      const gen = this.speakingGeneration;
      this.clearGraceTimer();
      this.graceTimer = setTimeout(() => {
        this.graceTimer = null;
        if (this.speakingGeneration === gen) {
          this.isSpeaking = false;
          this.speakingStartedAt = null;
          this.firstAudioSentAt = null;
          this.clearPendingBargeIn();
          void this.resetBargeInStrategies();
          // If VAD detected speech during the agent's turn but it was
          // gate-suppressed (agent hadn't been speaking long enough for
          // barge-in to fire), flush the ring buffer to STT now so the
          // user's words aren't silently lost.
          if (this.suppressedSpeechPending) {
            this.suppressedSpeechPending = false;
            this.flushInboundAudioRing();
          }
          // Reset VAD so any stuck SPEECH state from echo / loopback during
          // the agent's turn does not block the next user utterance from
          // emitting ``speech_start``.
          this.resetVad();
        }
      }, grace);
    } else {
      this.isSpeaking = false;
      this.speakingStartedAt = null;
      this.firstAudioSentAt = null;
      this.clearPendingBargeIn();
      void this.resetBargeInStrategies();
      if (this.suppressedSpeechPending) {
        this.suppressedSpeechPending = false;
        this.flushInboundAudioRing();
      }
      this.resetVad();
    }
  }

  private async resetBargeInStrategies(): Promise<void> {
    if (this.bargeInStrategies.length === 0) return;
    const { resetStrategies } = await import('./services/barge-in-strategies.js');
    await resetStrategies(this.bargeInStrategies);
  }

  /**
   * Reset the active VAD provider's per-utterance state. No-op when the
   * provider does not implement the optional ``reset()`` hook. Safe to call
   * from any context — failures are swallowed and the VAD is disabled for
   * the rest of the call so a flaky reset can never silently kill barge-in
   * for every subsequent turn.
   */
  private resetVad(): void {
    const activeVad = this.deps.agent.vad ?? this.autoVad;
    if (!activeVad || this.vadDisabled) return;
    try {
      const ret = activeVad.reset?.();
      if (ret instanceof Promise) {
        ret.catch((err) => {
          getLogger().debug(`VAD reset threw: ${String(err)}`);
        });
      }
    } catch (err) {
      getLogger().debug(`VAD reset threw: ${String(err)}`);
    }
  }

  /**
   * Whether barge-in is allowed to fire right now. Gate length depends
   * on whether AEC is active: 1 s with AEC (covers filter warmup),
   * 250 ms without (anti-flicker only — keeps PSTN barge-in responsive).
   */
  private canBargeIn(): boolean {
    if (this.speakingStartedAt === null) return true;
    // Anchor the gate on "first audio actually emitted", not on
    // ``beginSpeaking`` (which fires before the TTS provider's first-byte
    // latency has elapsed). Without this guard, background noise picked up
    // by VAD ~250 ms after ``beginSpeaking`` triggers a self-cancel BEFORE
    // any TTS chunk has reached the wire — the agent's first turn becomes
    // silence even though the SDK believes it spoke.
    if (this.firstAudioSentAt === null) return false;
    const elapsed = Date.now() - this.firstAudioSentAt;
    const gate = this.aec
      ? StreamHandler.MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_AEC
      : StreamHandler.MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_NO_AEC;
    return elapsed >= gate;
  }

  /**
   * Replay the audio captured by the self-hearing guard right before a
   * confirmed barge-in. VAD's ``minSpeechDuration`` window (default
   * 250 ms) means ``speech_start`` fires only AFTER the user has been
   * talking for that long; without this replay STT sees only the tail
   * of the user's interruption and produces "the line is breaking up"
   * partial transcripts. We deliberately do NOT call this on natural
   * turn end — see the comment in ``endSpeakingWithGrace`` for why.
   */
  private flushInboundAudioRing(): void {
    if (!this.stt || this.inboundAudioRing.length === 0) return;
    const replayed = this.inboundAudioRing.length;
    for (const buf of this.inboundAudioRing) {
      try {
        this.stt.sendAudio(buf);
      } catch (err) {
        getLogger().debug(`sendAudio replay failed: ${String(err)}`);
      }
    }
    this.inboundAudioRing = [];
    // [DIAG-2026-05-05] INFO so we can see in stdout whether the ring flush
    // is feeding STT bleed-only audio that produces phantom transcripts.
    getLogger().info(
      `[DIAG] Flushed ${replayed} pre-barge-in frame(s) (~${replayed * 20} ms) to STT`,
    );
  }
  private llmLoop: LLMLoop | null = null;
  /**
   * Per-call tool executor — provides retry-with-exponential-backoff and a
   * per-tool circuit breaker for Realtime function calls. Pipeline mode
   * uses its own executor inside ``LLMLoop``; this one is dedicated to
   * the Realtime path so a flaky downstream (DB outage, vendor rate
   * limit) returns a structured ``{ error, fallback: true }`` instead of
   * hanging the model on retries that will keep failing.
   */
  private readonly toolExecutor = new DefaultToolExecutor();
  /**
   * MCP server connection manager — populated lazily in
   * ``initMcpTools()`` when the agent declares ``mcpServers``. Holds
   * the open MCP client connections for the lifetime of the call so
   * we can dispatch ``tools/call`` without re-handshaking on every
   * function invocation. Cleared in ``fireCallEnd``.
   */
  private mcpManager: MCPManager | null = null;
  private chunkCount = 0;
  private callEndFired = false;
  private sttClosed = false;
  private currentAgentText = '';
  private responseAudioStarted = false;
  /**
   * Realtime turn ordering buffer. OpenAI Realtime emits
   * `input_audio_transcription.completed` (user transcript) AFTER
   * `response.done` (assistant complete) because Whisper transcription
   * runs in parallel with — and slower than — model response. Without
   * this buffer the pushed `history` order is [assistant, user, ...]
   * which renders out-of-order in the dashboard.
   *
   * Behaviour:
   *  - `onAdapterSpeechStopped` flips `userTranscriptPending = true`
   *  - `onAdapterResponseDone` checks the flag; if set, stashes the
   *    assistant text + a fallback timer
   *  - `onAdapterTranscriptInput` clears the flag, pushes user, then
   *    flushes any pending assistant turn
   *  - The fallback timer flushes the assistant alone if the user
   *    transcript never arrives (silence misclassified as speech, etc.)
   */
  private userTranscriptPending = false;
  private pendingAssistantTurn: string | null = null;
  private pendingAssistantTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * Hard cap on how long we wait for the user transcript before flushing
   * the buffered assistant turn alone. 3 s covers OpenAI Whisper's typical
   * 200-800 ms post-response delay with substantial headroom for slow
   * cellular audio uploads. Beyond this we accept the order will look
   * "assistant-only" rather than block the call's transcript display.
   */
  private static readonly REALTIME_USER_TRANSCRIPT_WAIT_MS = 3000;
  private maxDurationTimer: ReturnType<typeof setTimeout> | null = null;
  private transcriptProcessing = false;
  private transcriptQueue: STTTranscript[] = [];
  // Throttle state for back-to-back STT finals — see ``commitTranscript``.
  private lastCommitText = '';
  private lastCommitAt = 0;
  // PCM16 byte-alignment carry for TTS streaming (pipeline mode).
  // HTTP streams from ElevenLabs / OpenAI / Cartesia can yield chunks of any
  // size, including odd byte counts. Silently dropping the trailing odd byte
  // misaligns every subsequent int16 sample in the stream (hi/lo bytes get
  // swapped), producing a voice drowned in loud hiss. We buffer the odd byte
  // across chunks so resample/mulaw encoding always sees aligned int16 frames.
  private ttsByteCarry: Buffer | null = null;
  // Per-session stateful resamplers eliminate chunk-boundary discontinuities.
  // Created lazily on first use; reset() on call end.
  private readonly inboundResampler: StatefulResampler = createResampler8kTo16k();
  private readonly outboundResampler: StatefulResampler = createResampler16kTo8k();

  private readonly history: ReturnType<typeof createHistoryManager>;
  private readonly metricsAcc: CallMetricsAccumulator;
  private readonly _eventBus: EventBus;

  constructor(deps: StreamHandlerDeps, ws: WSWebSocket, caller: string, callee: string) {
    this.deps = deps;
    this.ws = ws;
    this.caller = caller;
    this.callee = callee;

    this.bargeInStrategies = (deps.agent.bargeInStrategies ?? []).slice();
    const confirmMs = deps.agent.bargeInConfirmMs;
    this.bargeInConfirmMs =
      typeof confirmMs === 'number' && Number.isFinite(confirmMs) && confirmMs > 0
        ? confirmMs
        : 1500;

    this.history = createHistoryManager(200);

    // v0.5.0+: ``agent.stt`` / ``agent.tts`` are always STTAdapter / TTSAdapter
    // instances (or undefined). Provider classes expose a static
    // ``providerKey`` so we get a stable pricing/dashboard key (e.g. "deepgram")
    // instead of the alias class name "STT". Falls back to constructor.name
    // for any custom adapter that doesn't declare providerKey.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sttKey = (deps.agent.stt?.constructor as any)?.providerKey;
    const sttProviderName = deps.agent.stt
      ? (sttKey ?? deps.agent.stt.constructor?.name ?? 'custom')
      : undefined;
    // Adapter ``model`` field powers per-model rate resolution in
    // pricing.calculateSttCost. Empty string → provider default.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sttModelName = String(((deps.agent.stt as any)?.model ?? '') || '');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const ttsKey = (deps.agent.tts?.constructor as any)?.providerKey;
    const ttsProviderName = deps.agent.tts
      ? (ttsKey ?? deps.agent.tts.constructor?.name ?? 'custom')
      : undefined;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const ttsModelName = String(((deps.agent.tts as any)?.model ?? '') || '');
    const providerMode = deps.agent.provider ?? 'openai_realtime';
    // Realtime collapses STT+LLM+TTS into one model — capture it so the
    // token-based cost calc picks the right per-model rate (e.g. gpt-
    // realtime-2 vs gpt-realtime-mini). Use the agent's declared model
    // when set; fall back to the adapter default.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const realtimeModelName =
      providerMode === 'openai_realtime'
        ? String(((deps.agent as any).model ?? '') || '') || 'gpt-realtime-mini'
        : '';
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const llmKey = (deps.agent.llm?.constructor as any)?.providerKey;
    let llmProviderName: string;
    if (deps.agent.llm) {
      if (llmKey) {
        llmProviderName = llmKey;
      } else {
        const stripped = (deps.agent.llm.constructor?.name ?? 'custom')
          .replace(/LLMProvider$/i, '')
          .replace(/LLM$/i, '')
          .replace(/Provider$/i, '')
          .toLowerCase();
        llmProviderName = stripped || 'custom';
      }
    } else {
      llmProviderName = providerMode === 'openai_realtime' ? 'openai_realtime' : 'openai';
    }
    this.llmProviderTag = llmProviderName;

    this._eventBus = new EventBus();
    this.metricsAcc = new CallMetricsAccumulator({
      callId: '',
      providerMode,
      telephonyProvider: deps.bridge.telephonyProvider,
      sttProvider: sttProviderName,
      ttsProvider: ttsProviderName,
      llmProvider: llmProviderName,
      sttModel: sttModelName,
      ttsModel: ttsModelName,
      realtimeModel: realtimeModelName,
      pricing: deps.pricing,
      eventBus: this._eventBus,
      reportOnlyInitialTtfb: deps.reportOnlyInitialTtfb ?? false,
    });

    getLogger().debug(`WebSocket connection opened (${deps.bridge.label})`);
  }

  /**
   * Record a completed turn in the dashboard store and fire the user-supplied
   * ``onMetrics`` callback. Centralises the 4 emit sites (firstMessage, pipeline
   * streaming/regular LLM, WebSocket remote, Realtime response_done) so the
   * payload shape lives in one place.
   */
  private async emitTurnMetrics(turn: unknown): Promise<void> {
    if (turn == null) return;
    this.deps.metricsStore.recordTurn({ call_id: this.callId, turn });
    if (!this.deps.onMetrics) return;
    // Fix 7 (Python parity, stream_handler.py:312): expose llm_ttft_ms at the
    // top level of the metrics payload so consumers can read it without
    // diving into turn.latency. The nested turn.latency.llm_ttft_ms is kept
    // for backwards compatibility.
    const turnMetrics = turn as { latency?: { llm_ttft_ms?: number } } | null;
    const llm_ttft_ms = turnMetrics?.latency?.llm_ttft_ms;
    await this.deps.onMetrics({
      call_id: this.callId,
      turn,
      ...(llm_ttft_ms !== undefined ? { llm_ttft_ms } : {}),
      cost_so_far: this.metricsAcc.getCostSoFar(),
    });
  }

  /** Reset the TTS odd-byte carry — call at every TTS stream entry/exit. */
  private resetTtsCarry(): void {
    this.ttsByteCarry = null;
  }

  /**
   * Flush both stateful resamplers and any TTS byte carry on call close.
   * Emits tail bytes through the telephony bridge so the last ~20 ms of audio
   * is not silently clipped on hangup. No-op if the WebSocket is already gone.
   */
  private flushResamplers(): void {
    // Flush inbound resampler (caller audio → STT)
    try {
      const inTail = this.inboundResampler.flush();
      if (inTail.length > 0 && this.stt) {
        this.stt.sendAudio(inTail);
      }
    } catch { /* best effort */ }

    // Flush outbound resampler (TTS → telephony, pipeline mode only)
    try {
      const outTail = this.outboundResampler.flush();
      if (outTail.length > 0 && this.ws.readyState === this.ws.OPEN) {
        const mulaw = pcm16ToMulaw(outTail);
        this.deps.bridge.sendAudio(this.ws, mulaw.toString('base64'), this.streamSid);
      }
    } catch { /* best effort */ }

    // Flush any leftover TTS carry byte (rare: only when last chunk was odd-length)
    this.ttsByteCarry = null;
  }

  /**
   * Start call recording when configured. Currently Twilio-only — bridges may
   * expose ``startRecording`` for parity when we add other carriers.
   */
  private async startRecordingIfRequested(callId: string): Promise<void> {
    const { recording, config } = this.deps;
    if (!recording || !config.twilioSid || !config.twilioToken || !callId) return;
    if (!validateTwilioSid(callId)) {
      getLogger().warn(`Recording skipped: invalid Twilio CallSid format ${JSON.stringify(callId)}`);
      return;
    }
    try {
      const recUrl = `https://api.twilio.com/2010-04-01/Accounts/${config.twilioSid}/Calls/${callId}/Recordings.json`;
      const recResp = await fetch(recUrl, {
        method: 'POST',
        headers: {
          'Authorization': `Basic ${Buffer.from(`${config.twilioSid}:${config.twilioToken}`).toString('base64')}`,
        },
      });
      if (recResp.ok) {
        getLogger().debug(`Recording started for ${callId}`);
      } else {
        getLogger().warn(`could not start recording: ${await recResp.text()}`);
      }
    } catch (e) {
      getLogger().warn(`could not start recording: ${String(e)}`);
    }
  }

  // ---------------------------------------------------------------------------
  // Public: observer API
  // ---------------------------------------------------------------------------

  /**
   * Subscribe to a Patter event on the per-call EventBus.
   *
   * The most common use-case is 'metrics_collected' — fired after every
   * completed turn with the TurnMetrics payload.
   *
   * Returns an unsubscribe function; call it to stop receiving events.
   *
   * @example
   * const off = handler.addObserver((payload) => {
   *   console.log('turn metrics:', payload);
   * });
   * // later:
   * off();
   */
  addObserver<T = unknown>(
    cb: (payload: T) => void | Promise<void>,
    event: PatterEventType = 'metrics_collected',
  ): () => void {
    return this._eventBus.on<T>(event, cb);
  }

  // ---------------------------------------------------------------------------
  // Public: called by the provider-specific parsers in server.ts
  // ---------------------------------------------------------------------------

  /**
   * Handle the call-start event.
   *
   * @param callId       Call SID (Twilio) or call_control_id (Telnyx)
   * @param customParams TwiML custom parameters (Twilio only, empty for Telnyx)
   */
  /** Initialize per-call state, build the AI adapter, and dispatch the `onCallStart` callback. */
  async handleCallStart(callId: string, customParams: Record<string, string> = {}): Promise<void> {
    this.callId = callId;
    this.metricsAcc.callId = callId;

    // Prefer TwiML <Parameter> values over WebSocket query params (Twilio
    // strips query params from the Stream URL, so customParams is the only
    // reliable source for caller/callee).
    if (customParams.caller && !this.caller) this.caller = customParams.caller;
    if (customParams.callee && !this.callee) this.callee = customParams.callee;

    // Single INFO line per call-start — full context in one place.
    const mode =
      this.deps.agent.engine
        ? `engine=${(this.deps.agent.engine as { kind?: string }).kind ?? 'unknown'}`
        : 'pipeline';
    getLogger().info(
      `Call started: ${callId} (${this.deps.bridge.label}, ${mode}, ${sanitizeLogValue(this.caller || '?')} → ${sanitizeLogValue(this.callee || '?')})`,
    );

    if (Object.keys(customParams).length > 0) {
      getLogger().debug(`Custom params: ${sanitizeLogValue(JSON.stringify(customParams))}`);
    }

    // Don't force direction='inbound' here. If the call was placed via
    // phone.call() the store already has direction='outbound' from
    // recordCallInitiated(); the store falls back to 'inbound' when no
    // existing record is present (i.e. true inbound webhook).
    this.deps.metricsStore.recordCallStart({
      call_id: callId,
      caller: this.caller,
      callee: this.callee,
    });

    // Safety: auto-hangup after 1 hour to prevent runaway billing
    const MAX_CALL_DURATION_MS = 60 * 60 * 1000;
    this.maxDurationTimer = setTimeout(async () => {
      getLogger().warn(`Call ${callId} hit max duration (${MAX_CALL_DURATION_MS / 60000}min), terminating`);
      try { await this.deps.bridge.endCall(callId, this.ws); } catch { /* best effort */ }
    }, MAX_CALL_DURATION_MS);

    // Notify standalone dashboard so active calls appear immediately
    try {
      const { notifyDashboard } = await import('./dashboard/persistence');
      notifyDashboard({
        call_id: callId,
        caller: this.caller,
        callee: this.callee,
      });
    } catch { /* ignore */ }

    if (this.deps.onCallStart) {
      // Resolve direction from the store: if the call was placed via
      // phone.call() the store has direction='outbound', otherwise inbound.
      const direction =
        this.deps.metricsStore.getActive(callId)?.direction ?? 'inbound';
      await this.deps.onCallStart({
        call_id: callId,
        caller: this.caller,
        callee: this.callee,
        direction,
        telephony_provider: this.deps.bridge.telephonyProvider,
        ...(Object.keys(customParams).length > 0 ? { custom_params: customParams } : {}),
      });
    }

    await this.startRecordingIfRequested(callId);

    // Resolve dynamic variables in system prompt
    const agentVars = this.deps.sanitizeVariables(this.deps.agent.variables ?? {});
    const safeCustomParams = this.deps.sanitizeVariables(customParams);
    const allVars = { ...agentVars, ...safeCustomParams };
    const resolvedPrompt = Object.keys(allVars).length > 0
      ? this.deps.resolveVariables(this.deps.agent.systemPrompt, allVars)
      : this.deps.agent.systemPrompt;

    const provider = this.deps.agent.provider ?? 'openai_realtime';

    // Resolve MCP servers BEFORE the adapter is built so the discovered
    // tools are visible to the model in its first session.update (Realtime)
    // or first LLM call (pipeline). One handshake + ``tools/list`` per
    // server, ~50-200 ms total. Failures are logged but not fatal — a
    // dead MCP server should not kill the entire call.
    await this.initMcpTools();

    if (provider === 'pipeline') {
      await this.initPipeline(resolvedPrompt);
    } else {
      await this.initRealtimeAdapter(resolvedPrompt);
    }
  }

  /**
   * Connect to every configured MCP server, discover their tools via
   * ``tools/list``, and merge them into ``agent.tools`` before the
   * adapter is built. The synthetic handlers dispatch back through the
   * MCP client so ``DefaultToolExecutor`` can invoke them like any
   * other handler-tool. No-op when ``agent.mcpServers`` is empty or the
   * optional ``@modelcontextprotocol/sdk`` is not installed.
   */
  private async initMcpTools(): Promise<void> {
    const servers = this.deps.agent.mcpServers;
    if (!servers || servers.length === 0) return;
    this.mcpManager = new MCPManager(servers);
    let discovered: ToolDefinition[];
    try {
      discovered = await this.mcpManager.connect();
    } catch (e) {
      getLogger().error(`MCP connect failed (continuing without MCP tools): ${String(e)}`);
      this.mcpManager = null;
      return;
    }
    if (discovered.length === 0) return;
    MCPManager.assertNoConflicts(this.deps.agent.tools as ToolDefinition[] | undefined, discovered);
    // Merge into agent.tools. The interface is readonly at compile time
    // but the underlying array is owned by the SDK at runtime — we cast
    // to mutate in place so ``buildAIAdapter`` and the LLM loop see
    // the discovered tools without a parallel plumbing parameter.
    const mutableAgent = this.deps.agent as { tools?: ToolDefinition[] };
    mutableAgent.tools = [...(mutableAgent.tools ?? []), ...discovered];
    getLogger().info(`MCP: merged ${discovered.length} tool(s) into agent`);
  }

  /** Set the stream SID (Twilio only, called after parsing 'start' event). */
  /** Set the carrier-side stream id (Twilio `streamSid` / Telnyx stream identifier). */
  setStreamSid(sid: string): void {
    this.streamSid = sid;
  }

  /** Handle an incoming audio chunk (already decoded from base64). */
  /** Forward inbound audio bytes to the AI adapter and (in pipeline mode) the STT provider. */
  async handleAudio(audioBuffer: Buffer): Promise<void> {
    const provider = this.deps.agent.provider ?? 'openai_realtime';
    if (provider === 'pipeline' && this.stt) {
      // Both Twilio and Telnyx (with default streaming_start PCMU bidirectional)
      // deliver mulaw 8 kHz — always transcode to PCM16 16 kHz before STT.
      const pcm8k = mulawToPcm16(audioBuffer);
      let pcm16k = this.inboundResampler.process(pcm8k);

      // Acoustic echo cancellation — subtract estimated TTS bleed from the
      // mic stream before VAD/STT see it. Pass-through until the canceller
      // has enough far-end history to fill its filter window (~128 ms),
      // then converges over the next 0.5–2 s of TTS-only frames.
      if (this.aec) {
        pcm16k = this.aec.processNearEnd(pcm16k);
      }

      // External VAD (e.g. Silero) when configured. Drives:
      //  - Self-hearing avoidance: while the agent is speaking we DO NOT pipe
      //    audio to STT, so STT can't transcribe the agent's own TTS feeding
      //    back through the caller microphone.
      //  - Fast barge-in: VAD speech_start during TTS triggers an immediate
      //    interruption (no waiting for STT to emit a transcript).
      //  - Endpointing-free STT: no need to wait for Deepgram's silence
      //    timeout — we already know when the user is talking.
      const activeVad = this.deps.agent.vad ?? this.autoVad;
      if (activeVad && !this.vadDisabled) {
        try {
          // H4: protect hot path against slow ONNX inference — if VAD takes
          // longer than 25 ms, treat the frame as silent and continue.
          const vadPromise = activeVad.processFrame(pcm16k, 16000);
          const timeoutPromise = new Promise<null>((resolve) => setTimeout(() => resolve(null), 25));
          const evt = await Promise.race([vadPromise, timeoutPromise]);
          if (evt) {
            // INFO-level log so the user can see VAD activity in the standard
            // server output without flipping debug logging.
            getLogger().info(
              `[VAD] ${evt.type}  agentSpeaking=${this.isSpeaking}`,
            );
          }
          if (evt?.type === 'speech_start') {
            const phantomSuppressed = this.isSpeaking && !this.canBargeIn();
            if (phantomSuppressed) {
              // Within the per-turn warmup gate. With AEC on this is the
              // ~1 s filter convergence window; without AEC it is just a
              // 100 ms anti-flicker margin. INFO so unexpected
              // suppressions are visible without enabling debug logs.
              //
              // CRITICAL: do NOT touch metrics state here. An earlier
              // bug (pre-0.6.1) called ``startTurnIfIdle()`` for every
              // ``speech_start`` including suppressed phantoms, which
              // stamped ``turnStart`` at echo/loopback time. The
              // legitimate user-speech ``speech_start`` that followed
              // then no-op'd (turn_start was already set), so the
              // dashboard reported ``user_speech_duration_ms`` of 5-7 s
              // even on short ~1 s utterances.
              getLogger().info(
                `[VAD] speech_start suppressed (agent speaking < gate, aec=${this.aec ? 'on' : 'off'})`,
              );
              // Mark that real user speech was detected but gated out.
              // The grace-timer callback will replay the ring buffer to
              // STT so the speech isn't silently discarded when the
              // agent finishes naturally without a barge-in.
              this.suppressedSpeechPending = true;
            } else if (this.isSpeaking) {
              if (this.bargeInStrategies.length > 0) {
                this.startPendingBargeIn();
                this.metricsAcc.anchorUserSpeechStart();
                return;
              }
              getLogger().info('[VAD] speech_start during TTS → BARGE-IN');
              this.metricsAcc.recordOverlapStart();
              this.metricsAcc.recordBargeinDetected();
              const bargeinSpan = startSpan(SPAN_BARGEIN, { 'patter.call.id': this.callId });
              try {
                this.cancelSpeaking();
                try {
                  this.deps.bridge.sendClear(this.ws, this.streamSid);
                } catch (err) {
                  getLogger().debug(`sendClear during VAD barge-in failed: ${String(err)}`);
                }
                // Replay the ring buffer of inbound frames captured while
                // the agent was speaking — those carry the user's first
                // ~500 ms of speech that the self-hearing guard had been
                // dropping on the floor. Without this flush, Deepgram
                // only sees audio AFTER `speech_start` fires (i.e. the
                // tail of the user's utterance), which is why short
                // interruptions like "stop" produced no transcript and
                // the agent kept talking.
                this.flushInboundAudioRing();
                this.metricsAcc.recordTtsStopped();
                this.metricsAcc.recordTurnInterrupted();
                this.metricsAcc.recordOverlapEnd(true);
              } finally {
                try {
                  bargeinSpan.end();
                } catch {
                  // Swallow.
                }
              }
            }
            if (!phantomSuppressed) {
              // Industry-standard pattern: every legitimate VAD speech_start re-anchors
              // the turn timestamp pre-commit. Repairs stale anchors from
              // rejected barge-ins / dropped final transcripts, plus the
              // original phantom-during-warmup-gate vulnerability.
              this.metricsAcc.anchorUserSpeechStart();
            }
          } else if (evt?.type === 'speech_end') {
            this.metricsAcc.recordVadStop();
            // The SDK's VAD has detected end-of-speech earlier and more
            // reliably than the provider's own endpointing on PSTN
            // (Deepgram's natural-pause endpointing can run 1-6 s before
            // it emits a final). Ask the provider to finalise the
            // in-flight utterance NOW so the next turn can dispatch
            // immediately. Optional chained — Whisper-class adapters
            // that don't support per-utterance finalisation simply skip.
            try {
              const ret = this.stt?.finalize?.();
              if (ret instanceof Promise) {
                ret.catch((err) =>
                  getLogger().debug(`STT finalize threw: ${String(err)}`),
                );
              }
            } catch (err) {
              getLogger().debug(`STT finalize threw: ${String(err)}`);
            }
          }
        } catch (err) {
          // Disable VAD for the rest of the call to avoid log spam on repeated failures.
          this.vadDisabled = true;
          getLogger().warn(`VAD processFrame failed — disabling VAD for this call: ${String(err)}`);
        }
      }

      // Self-hearing guard: when the agent is speaking, do NOT forward audio
      // to STT. The agent's own TTS audio bleeds back through the caller mic
      // and Deepgram would happily transcribe it. With external VAD we still
      // detected barge-in above; without VAD we fall back to the legacy
      // "always forward + bargeInThresholdMs" path so users without a VAD
      // adapter aren't regressed.
      //
      // Pre-barge-in buffer: instead of dropping the frame on the floor,
      // we push it into a small ring (last ~600 ms). On a future
      // BARGE-IN this ring is flushed to STT so the user's first words
      // — captured BEFORE the VAD's `minSpeechDuration` window let it
      // emit `speech_start` — actually reach Deepgram. Without this
      // buffer, short interruptions ("stop") never produced a
      // transcript and the agent kept talking; long ones produced
      // truncated transcripts and the agent answered to fragments.
      if (this.isSpeaking) {
        if (this.deps.agent.vad ?? this.autoVad) {
          this.inboundAudioRing.push(pcm16k);
          if (
            this.inboundAudioRing.length > StreamHandler.INBOUND_AUDIO_RING_FRAMES
          ) {
            this.inboundAudioRing.shift();
          }
          return;
        }
        if ((this.deps.agent.bargeInThresholdMs ?? 300) === 0) return;
      }

      // beforeSendToStt hook — gate/transform the audio chunk before it
      // reaches STT (custom VAD, echo cancellation, PII redaction, ...).
      const hooks = this.deps.agent.hooks;
      if (hooks) {
        const hookExecutor = new PipelineHookExecutor(hooks);
        const hookCtx = this.buildHookContext();
        const processed = await hookExecutor.runBeforeSendToStt(pcm16k, hookCtx);
        if (processed === null) return;
        this.stt.sendAudio(processed);
        this.metricsAcc.addSttAudioBytes(processed.length);
      } else {
        this.stt.sendAudio(pcm16k);
        this.metricsAcc.addSttAudioBytes(pcm16k.length);
      }
    } else if (this.adapter) {
      // OpenAI Realtime is configured for g711_ulaw so Twilio mulaw is fine.
      // ElevenLabs ConvAI defaults to PCM 16kHz — transcode Twilio mulaw
      // first. When ConvAI was constructed via ``ElevenLabsConvAIAdapter
      // .forTwilio(...)`` (or any path that sets ``inputAudioFormat
      // === 'ulaw_8000'``) we negotiated μ-law on both directions, so we
      // forward the caller's μ-law bytes untouched — saves a decode +
      // resample on every inbound frame.
      if (
        this.adapter instanceof ElevenLabsConvAIAdapter &&
        this.deps.bridge.telephonyProvider === 'twilio' &&
        this.adapter.inputAudioFormat !== 'ulaw_8000'
      ) {
        const pcm8k = mulawToPcm16(audioBuffer);
        const pcm16k = this.inboundResampler.process(pcm8k);
        this.adapter.sendAudio(pcm16k);
      } else {
        this.adapter.sendAudio(audioBuffer);
      }
    }
  }

  /** Handle a DTMF keypress event (Twilio only). */
  /** Handle an inbound DTMF tone from the caller. */
  async handleDtmf(digit: string): Promise<void> {
    getLogger().debug(`DTMF: ${digit}`);
    if (this.adapter instanceof OpenAIRealtimeAdapter) {
      await this.adapter.sendText(`The user pressed key ${digit} on their phone keypad.`);
    }
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({ role: 'user', text: `[DTMF: ${digit}]`, call_id: this.callId });
    }
  }

  /**
   * Last mark name Twilio has confirmed playback of. Mirrors the Python
   * ``TwilioAudioSender.last_confirmed_mark`` field — barge-in heuristics
   * compare this against the latest sent mark to decide whether the agent's
   * audio has actually reached the caller yet.
   */
  lastConfirmedMark = '';

  /**
   * Handle a Twilio ``mark`` event acknowledging that a previously sent
   * audio chunk has been played out. Mirrors Python's
   * ``twilio_handler.py``: ``audio_sender.on_mark_confirmed(mark_name)`` +
   * ``handler.on_mark(mark_name)``.
   */
  /** Handle a Twilio Media Streams `mark` event acknowledging audio playback boundaries. */
  async onMark(markName: string): Promise<void> {
    if (!markName) return;
    // Resolve the firstMessage mark waiter (if any) so the send loop
    // can advance its sliding window. We resolve the matched entry AND
    // every entry before it in the queue — Twilio sometimes batches
    // mark echoes, and dropping earlier entries first keeps FIFO order
    // even when the higher-numbered echo arrives before a lower-
    // numbered one (rare but observed on degraded edges).
    const idx = this.pendingMarks.findIndex((m) => m.name === markName);
    if (idx < 0) return;
    // Only record the echo after we have confirmed it matches a known
    // queued mark. Before this gate ``onMark`` clobbered
    // ``lastConfirmedMark`` with any mark name — including stale
    // echoes that no longer correspond to anything we sent, or marks
    // emitted by adapters outside the firstMessage queue — which
    // would contaminate any downstream barge-in heuristic gated on
    // ``lastConfirmedMark``. The Python parity here is structural:
    // ``stream_handler.py``'s ``on_mark`` never touches a handler-
    // level field at all (the equivalent state lives on
    // ``TwilioAudioSender.last_confirmed_mark``, updated only via
    // the carrier's own echo handler).
    this.lastConfirmedMark = markName;
    const resolved = this.pendingMarks.splice(0, idx + 1);
    for (const entry of resolved) {
      try {
        entry.resolve();
      } catch {
        // No-op.
      }
    }
  }

  /** Handle call stop / stream end. */
  /** Handle a carrier-emitted `stop` event signalling the call has ended. */
  async handleStop(): Promise<void> {
    // Drop any pending barge-in timer BEFORE we tear down metrics /
    // adapters. Without this, a call that ends while a barge-in is
    // pending leaves a setTimeout scheduled to fire ``bargeInConfirmMs``
    // later and call ``metricsAcc.recordOverlapEnd`` on a finalised
    // metrics object — a slow leak in long-running servers and a race
    // producing spurious overlap_end events. Idempotent.
    this.clearPendingBargeIn();
    // Resolve every pending firstMessage mark waiter before tearing the
    // adapter down. A call that ends mid firstMessage (carrier stop
    // arriving before the paced sender finished) would otherwise leak
    // unresolved promises owned by the send loop.
    this.drainPendingMarks();
    // Reset the firstMessage mark counter so a re-used handler starts
    // ``fm_<n>`` numbering at 1 on the next call. See
    // ``sendPacedFirstMessageBytes`` for the per-send reset that
    // protects the within-call path.
    this.firstMessageMarkCounter = 0;
    this.clearGraceTimer();
    this.flushResamplers();
    await this.closeSttOnce();
    try { this.adapter?.close(); } catch { /* ignore */ }
    await this.fireCallEnd();
  }

  /** Handle WebSocket close event. */
  /** Tear down adapter, STT/TTS, and per-call state when the carrier WebSocket closes. */
  async handleWsClose(): Promise<void> {
    // See handleStop — drop pending barge-in timer before cleanup so a
    // dead handler can never fire a stale recordOverlapEnd callback.
    this.clearPendingBargeIn();
    // See handleStop — drain pending firstMessage marks so an abnormal
    // carrier WS drop during the paced sender cannot leak unresolved
    // promises owned by the send loop, and reset the counter.
    this.drainPendingMarks();
    this.firstMessageMarkCounter = 0;
    this.clearGraceTimer();
    this.flushResamplers();
    // Drain STT first so in-flight transcripts fire before onCallEnd.
    await this.closeSttOnce();
    try { this.adapter?.close(); } catch { /* ignore */ }
    await this.fireCallEnd();
    // Ensure telephony call is terminated even if WebSocket closed abnormally
    try { await this.deps.bridge.endCall(this.callId, this.ws); } catch { /* best effort */ }
  }

  /** Close STT at most once; swallow errors. */
  private async closeSttOnce(): Promise<void> {
    if (this.sttClosed) return;
    this.sttClosed = true;
    try { await this.stt?.close(); } catch { /* ignore */ }
  }

  // ---------------------------------------------------------------------------
  // Private: Audio encoding for pipeline mode
  // ---------------------------------------------------------------------------

  /**
   * Encode a PCM 16kHz audio chunk for the telephony provider.
   *
   * Both Twilio and Telnyx negotiate PCMU (mulaw) 8 kHz on the bidirectional
   * media stream — Twilio always, and Telnyx because ``streaming_start``
   * (server.ts) requests ``stream_bidirectional_codec=PCMU`` at 8 kHz. So
   * the wire format for both providers is mulaw 8 kHz; we resample 16 kHz
   * PCM16 → 8 kHz then encode to mulaw. Mirrors the Python pipeline path
   * (libraries/python/getpatter/handlers/telnyx_handler.py::TelnyxAudioSender).
   *
   * Maintains a 1-byte carry across calls so unaligned HTTP chunks from
   * streaming TTS providers never byte-swap the PCM16 samples downstream.
   */
  private encodePipelineAudio(pcm16k: Buffer): string {
    const aligned = this.alignPcm16(pcm16k);
    if (aligned.length === 0) return '';
    const pcm8k = this.outboundResampler.process(aligned);
    const mulaw = pcm16ToMulaw(pcm8k);
    return mulaw.toString('base64');
  }

  /**
   * Prepend any carry byte from the previous chunk, return the even-length
   * portion, and stash the final odd byte (if any) for the next call.
   */
  private alignPcm16(chunk: Buffer): Buffer {
    const combined = this.ttsByteCarry
      ? Buffer.concat([this.ttsByteCarry, chunk])
      : chunk;
    const alignedLen = combined.length & ~1;
    this.ttsByteCarry =
      alignedLen < combined.length ? combined.subarray(alignedLen) : null;
    return combined.subarray(0, alignedLen);
  }

  /**
   * 40 ms @ 16 kHz mono PCM16 = 1280 bytes. Sized to mirror the smallest
   * live-TTS chunk boundary so cancel granularity (mark/clear bookkeeping)
   * is identical regardless of whether the firstMessage came from the
   * prewarm cache or a live ``tts.synthesizeStream`` stream.
   */
  private static readonly PREWARM_CHUNK_BYTES = 1280;

  /**
   * Stream a cached firstMessage buffer in pacing-friendly chunks.
   *
   * Splits ``prewarmBytes`` into ``PREWARM_CHUNK_BYTES`` slices and
   * forwards each through ``deps.bridge.sendAudio`` exactly like the
   * live TTS path does — preserving Twilio mark/clear granularity. A
   * single multi-second sendAudio call would push the whole intro into
   * the carrier in one go and a ``sendClear`` issued mid-buffer would
   * have nothing to clear ("agent keeps talking after barge-in" UX bug
   * on the very first turn).
   *
   * Returns ``true`` when at least one chunk hit the wire — the caller
   * uses that to decide whether to record TTS-first-byte / turn-complete
   * metrics.
   */
  private async streamPrewarmBytes(prewarmBytes: Buffer): Promise<boolean> {
    return this.sendPacedFirstMessageBytes(prewarmBytes);
  }

  /**
   * Iterate ``bytes`` as ``PREWARM_CHUNK_BYTES``-sized PCM16 slices and
   * forward each via ``deps.bridge.sendAudio`` with mark-gated pacing
   * (Twilio) or playout-time-based pacing (Telnyx). Caps the carrier-
   * side buffer at ``FIRST_MESSAGE_MARK_WINDOW`` chunks so a barge-in's
   * ``sendClear`` has ~120 ms (Twilio) or zero (Telnyx, immediately
   * after the latest sleep) of audio to flush.
   *
   * Bails immediately when ``isSpeaking`` flips to false — both via the
   * loop's pre-iter check and via ``drainPendingMarks`` (called from
   * ``cancelSpeaking``) which unblocks any in-flight ``waitForMarkWindow``.
   *
   * Returns ``true`` when at least one chunk hit the wire — the caller
   * uses that to decide whether to record TTS-first-byte / turn-complete
   * metrics. See BUG #128 for the regression this fix targets.
   */
  private async sendPacedFirstMessageBytes(bytes: Buffer): Promise<boolean> {
    // Reset the per-send mark counter so each invocation produces a
    // fresh ``fm_1, fm_2, ...`` sequence. Without this the counter
    // grows monotonically across turns on a re-used handler and a
    // stale ``fm_N`` echo from an earlier turn could match a mark
    // name issued later, corrupting the FIFO matching in ``onMark``.
    // The queue is also expected empty here by ``cancelSpeaking`` /
    // ``handleStop`` / ``handleWsClose``; drain defensively if not.
    if (this.pendingMarks.length > 0) this.drainPendingMarks();
    this.firstMessageMarkCounter = 0;
    let firstChunkSent = false;
    // Once the mark window is first filled we switch to playout-time pacing
    // to prevent batch-ACK bursts. Before that we send in burst so the first
    // FIRST_MESSAGE_MARK_WINDOW chunks pre-fill the PSTN jitter buffer.
    let initialFillComplete = false;
    for (let i = 0; i < bytes.length; i += StreamHandler.PREWARM_CHUNK_BYTES) {
      if (!this.isSpeaking) break; // barge-in mid-buffer — stop now
      // Back-pressure: if too many marks are unconfirmed, wait. Drains
      // immediately on cancelSpeaking.
      await this.waitForMarkWindow();
      if (!this.isSpeaking) break;
      const chunk = bytes.subarray(i, i + StreamHandler.PREWARM_CHUNK_BYTES);
      if (!firstChunkSent) firstChunkSent = true;
      if (this.aec) this.aec.pushFarEnd(chunk);
      const encoded = this.encodePipelineAudio(chunk);
      this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
      this.markFirstAudioSent();
      const markPromise = this.sendMarkAwaitable();
      if (!initialFillComplete && this.pendingMarks.length >= StreamHandler.FIRST_MESSAGE_MARK_WINDOW) {
        initialFillComplete = true;
      }
      // Telnyx has no mark concept — always pace by playout time.
      // Twilio: the first FIRST_MESSAGE_MARK_WINDOW chunks go out in burst
      // to pre-fill the PSTN jitter buffer (250–1500 ms), then playout-time
      // pacing kicks in (via the sticky initialFillComplete flag) to prevent
      // batch-ACK bursts from draining the buffer → crackling.
      if (markPromise === null || initialFillComplete) {
        const playoutMs = Math.max(
          1,
          Math.floor(chunk.length / StreamHandler.PCM16_16K_BYTES_PER_MS),
        );
        await new Promise<void>((resolve) => setTimeout(resolve, playoutMs));
      }
    }
    return firstChunkSent;
  }

  // ---------------------------------------------------------------------------
  // Private: Pipeline mode
  // ---------------------------------------------------------------------------

  private async initPipeline(resolvedPrompt: string): Promise<void> {
    const label = this.deps.bridge.label;

    this.stt = await this.deps.bridge.createStt(this.deps.agent);

    // v0.5.0+: TTS is a pre-instantiated adapter on ``agent.tts`` or null.
    this.tts = await createTTS(this.deps.agent);

    // Advise the TTS adapter of the telephony carrier so it can pick a
    // wire-native ``outputFormat`` (e.g. ``ulaw_8000`` on Twilio) and
    // skip a client-side transcode. The hook is opt-in per-adapter:
    // adapters that don't expose ``setTelephonyCarrier`` keep their
    // constructed format. Adapters that do (e.g. ElevenLabsWebSocketTTS)
    // only auto-flip when the user did NOT explicitly pass outputFormat.
    if (this.tts) {
      const carrierAware = this.tts as unknown as {
        setTelephonyCarrier?: (c: string) => void;
      };
      if (typeof carrierAware.setTelephonyCarrier === 'function') {
        try {
          carrierAware.setTelephonyCarrier(this.deps.bridge.telephonyProvider);
        } catch (e) {
          getLogger().debug(`TTS setTelephonyCarrier failed (${label}): ${String(e)}`);
        }
      }
    }

    if (!this.stt) {
      getLogger().debug(`Pipeline mode (${label}): no STT configured`);
    }
    if (!this.tts) {
      getLogger().debug(`Pipeline mode (${label}): no TTS configured`);
    }

    // Auto-VAD: load SileroVAD with telephony-tuned defaults if the user
    // didn't pass one. Falls back silently to the STT-endpoint heuristic
    // when onnxruntime-node is missing — same behaviour as before for
    // users who have not installed the optional dep.
    if (!this.deps.agent.vad) {
      try {
        const { SileroVAD } = await import('./providers/silero-vad');
        this.autoVad = await SileroVAD.forPhoneCall();
        getLogger().info(
          `auto-VAD enabled (SileroVAD, phone preset). Pass agent.vad=… to override.`,
        );
      } catch (e) {
        const msg = (e as Error)?.message ?? String(e);
        if (/Cannot find module|onnxruntime-node/i.test(msg)) {
          getLogger().info(
            'auto-VAD unavailable: onnxruntime-node not installed. ' +
              'Run `npm install onnxruntime-node@~1.18.0` for fast barge-in.',
          );
        } else {
          getLogger().warn(
            `auto-VAD load failed (${msg}); falling back to STT-endpoint heuristic`,
          );
        }
      }
    }

    // Acoustic echo cancellation: opt-in.
    //
    // Per the industry consensus on PSTN echo cancellation and Twilio's
    // own guidance, time-domain NLMS server-side AEC is the
    // RIGHT tool only when the SDK has near-direct access to the mic and
    // speaker (browser WebRTC, mobile native). PSTN paths route through
    // a 250–1500 ms Twilio jitter buffer + carrier loop — far outside
    // the 32 ms window of a 512-tap NLMS filter at 16 kHz, so the filter
    // cannot model the echo and silently degenerates into pass-through.
    // Emit a warning so the operator knows to either rely on the
    // self-hearing guard alone (handset / earpiece — minimal bleed) or
    // keep AEC off (default) and tune the VAD ``min_speech_duration`` if
    // bleed-driven false positives appear during firstMessage.
    if (this.deps.agent.echoCancellation) {
      const carrier = this.deps.bridge.telephonyProvider;
      if (carrier === 'twilio' || carrier === 'telnyx') {
        getLogger().warn(
          `echoCancellation: true on ${carrier} (PSTN). Server-side NLMS ` +
            `cannot model PSTN's ~250–1500 ms round-trip echo with a ` +
            `32 ms filter window — it will silently no-op. Best practice: ` +
            `keep echoCancellation: false; rely on the carrier + caller ` +
            `device's built-in echo suppression and Patter's self-hearing ` +
            `guard. Enable AEC only for browser/native deployments where ` +
            `the SDK owns the audio path end-to-end.`,
        );
      }
      try {
        const { NlmsEchoCanceller } = await import('./audio/aec');
        this.aec = new NlmsEchoCanceller({ sampleRate: 16000 });
        getLogger().info(
          'echo cancellation enabled (NLMS, 512 taps + 0.5 s warmup μ=0.5); ' +
            'filter converges within ~250 ms of TTS playback in low-latency loops.',
        );
      } catch (e) {
        getLogger().warn(
          `echo cancellation requested but failed to load: ${String(e)}; ` +
            `falling back to pass-through.`,
        );
      }
    }

    // Prewarm-handoff: try to adopt pre-opened provider WebSockets that
    // the prewarm pipeline (see ``Patter.parkProviderConnections``)
    // parked during the carrier ringing window. When a parked WS is
    // still OPEN we skip the cold ``connect()`` and the STT first-turn
    // can flow audio without paying the 150-400 ms TLS handshake.
    // Failures (cache miss, parked WS died) fall back transparently.
    let parked: import('./client').ParkedProviderConnections | undefined;
    if (this.deps.popPrewarmedConnections) {
      try {
        parked = this.deps.popPrewarmedConnections(this.callId);
      } catch (err) {
        getLogger().debug(`popPrewarmedConnections raised: ${String(err)}`);
      }
    }
    // Adopt the TTS WS first — it's a synchronous handoff (the live
    // ``synthesizeStream`` call below picks it up via the adapter's
    // single-slot adoption queue).
    const parkedTts = parked?.tts;
    if (parkedTts && this.tts) {
      const ttsAny = this.tts as { adoptWebSocket?: (p: typeof parkedTts) => void };
      if (typeof ttsAny.adoptWebSocket === 'function' && parkedTts.ws.readyState === 1 /* OPEN */) {
        try {
          ttsAny.adoptWebSocket(parkedTts);
          getLogger().info(`[CONNECT] callId=${this.callId} provider=tts source=adopted ms=0`);
        } catch (err) {
          getLogger().debug(`TTS adoptWebSocket failed: ${String(err)}; falling back`);
          try { parkedTts.ws.close(); } catch { /* ignore */ }
        }
      } else {
        try { parkedTts.ws.close(); } catch { /* ignore */ }
      }
    }

    // Kick off STT connect WITHOUT awaiting yet — we only need STT ready
    // to receive incoming user audio, not to send the first agent
    // message out. Parallelising STT.connect with the TTS firstMessage
    // synth shaves 200-400 ms off the perceived first-turn latency.
    let sttConnectPromise: Promise<void> | null = null;
    if (this.stt) {
      const sttAny = this.stt as { adoptWebSocket?: (ws: import('ws').WebSocket) => void };
      const sttStarted = Date.now();
      if (
        parked?.stt &&
        typeof sttAny.adoptWebSocket === 'function' &&
        parked.stt.readyState === 1 /* OPEN */
      ) {
        try {
          sttAny.adoptWebSocket(parked.stt);
          getLogger().info(
            `[CONNECT] callId=${this.callId} provider=stt source=adopted ms=${Date.now() - sttStarted}`,
          );
          sttConnectPromise = Promise.resolve();
        } catch (err) {
          getLogger().debug(`STT adoptWebSocket failed: ${String(err)}; falling back`);
          try { parked.stt.close(); } catch { /* ignore */ }
          sttConnectPromise = (async () => {
            await this.stt!.connect();
            getLogger().info(
              `[CONNECT] callId=${this.callId} provider=stt source=fresh ms=${Date.now() - sttStarted}`,
            );
          })();
        }
      } else {
        if (parked?.stt) {
          try { parked.stt.close(); } catch { /* ignore */ }
        }
        sttConnectPromise = (async () => {
          await this.stt!.connect();
          getLogger().info(
            `[CONNECT] callId=${this.callId} provider=stt source=fresh ms=${Date.now() - sttStarted}`,
          );
        })();
      }
    }
    getLogger().debug(`Pipeline mode (${label}): STT connect kicked off`);

    if (this.deps.agent.firstMessage && !this.deps.onMessage && this.tts) {
      this.metricsAcc.startTurn();
      // Mark the agent as speaking for the duration of the first
      // message — without this, the self-hearing guard never engages,
      // the user's audio (mixed with TTS bleed) is forwarded to STT
      // and produces garbage transcripts, and the ring buffer for
      // pre-barge-in audio is never populated. Mirrors the per-turn
      // behaviour in `runPipelineLlm` / `runRegularLlm`.
      // Pass isFirstMessage=true so the canBargeIn() anti-flicker gate
      // starts running NOW — TTFB on the TTS provider often eats 300-800ms,
      // and without an early anchor the firstMessage is uninterruptible
      // during that window.
      await this.beginSpeaking(true);
      let firstChunkSent = false;
      this.resetTtsCarry();
      // Check the prewarm cache first. When ``Patter.call`` was made
      // with ``agent.prewarmFirstMessage: true`` the firstMessage has
      // already been synthesised during the ringing window — we send
      // the bytes directly through the carrier-side encoder (which
      // handles native-rate → carrier-rate resampling) and skip the
      // TTS round-trip entirely.
      let prewarmBytes: Buffer | undefined;
      if (this.deps.popPrewarmAudio) {
        try {
          prewarmBytes = this.deps.popPrewarmAudio(this.callId);
        } catch (err) {
          getLogger().debug(`popPrewarmAudio raised: ${String(err)}`);
        }
      }
      try {
        if (prewarmBytes) {
          this.metricsAcc.recordTtsFirstByte();
          await this.emitAudioOut();
          firstChunkSent = await this.streamPrewarmBytes(prewarmBytes);
        } else {
          // Streaming TTS path (no prewarm cache). Uses the same simple
          // per-chunk send as synthesizeSentence — ElevenLabs HTTP streams
          // at near-real-time speed so the carrier-side buffer stays bounded
          // without mark-gated pacing.  Routing streaming chunks through
          // sendPacedFirstMessageBytes caused crackling: its drain+reset on
          // every HTTP chunk destroyed mark back-pressure continuity and the
          // per-sub-chunk sleep slowed delivery below Twilio's playout rate,
          // producing periodic buffer underruns.  The prewarm path (a single
          // pre-synthesised buffer) still uses sendPacedFirstMessageBytes
          // because that buffer can be several seconds long and needs pacing.
          for await (const chunk of this.tts.synthesizeStream(this.deps.agent.firstMessage)) {
            if (!this.isSpeaking) break;
            if (!firstChunkSent) {
              firstChunkSent = true;
              this.metricsAcc.recordTtsFirstByte();
              await this.emitAudioOut();
            }
            if (this.aec) this.aec.pushFarEnd(chunk);
            const encoded = this.encodePipelineAudio(chunk);
            this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
            this.markFirstAudioSent();
          }
        }
      } catch (e) {
        getLogger().error(`First message TTS error (${label}):`, e);
      } finally {
        // Drop any partial int16 byte to prevent cross-turn corruption
        // if the stream threw before a complete sample was delivered.
        this.resetTtsCarry();
        // Flip back to not-speaking with grace so the ring buffer
        // accumulated during the intro is flushed and the next user
        // utterance is recognised cleanly.
        this.endSpeakingWithGrace();
      }
      if (firstChunkSent) {
        // Bill the firstMessage TTS characters — they were synthesised
        // at ElevenLabs (or the configured TTS provider) and the
        // customer pays for them. The previous flow only called
        // ``recordTurnComplete`` here, which finalises the turn but does
        // NOT increment the TTS char counter — so a 5-turn call with an
        // 82-char greeting was under-billed by ~22% on TTS cost.
        // ``recordTtsComplete`` is the canonical accumulator entry
        // point for TTS char billing (parity with Python fix).
        this.metricsAcc.recordTtsComplete(this.deps.agent.firstMessage);
        await this.emitTurnMetrics(this.metricsAcc.recordTurnComplete(this.deps.agent.firstMessage));
        this.history.push({ role: 'assistant', text: this.deps.agent.firstMessage, timestamp: Date.now() });
      }
    }

    // Create LLM loop for pipeline mode when no onMessage handler provided.
    // Precedence: user-supplied ``agent.llm`` > OpenAI default (from openaiKey).
    if (this.deps.agent.llm) {
      if (this.deps.onMessage) {
        throw new Error(
          "Cannot pass both agent({ llm }) and serve({ onMessage }). Pick one — " +
            "`llm` for built-in LLMs, `onMessage` for custom logic.",
        );
      }
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const providerModel = (this.deps.agent.llm as any)?.model ?? '';
      this.llmLoop = new LLMLoop(
        '', // apiKey unused when llmProvider is supplied
        providerModel, // propagate so calculateLlmCost can match the price row
        resolvedPrompt,
        this.deps.agent.tools as ToolDefinition[] | undefined,
        this.deps.agent.llm,
        this.deps.agent.disablePhonePreamble ?? false,
      );
      this.llmLoop.setEventBus(this._eventBus);
      this.llmLoop.setOnToolCall((n, a, r) => this.recordToolCall(n, a, r));
      const llmLabel = this.deps.agent.llm.constructor?.name ?? 'custom';
      getLogger().debug(`Built-in LLM loop active (pipeline, ${label}, llm=${llmLabel})`);
    } else if (!this.deps.onMessage && this.deps.config.openaiKey) {
      let llmModel = this.deps.agent.model || 'gpt-4o-mini';
      if (llmModel.includes('realtime')) llmModel = 'gpt-4o-mini';
      this.llmLoop = new LLMLoop(
        this.deps.config.openaiKey,
        llmModel,
        resolvedPrompt,
        this.deps.agent.tools as ToolDefinition[] | undefined,
        undefined,
        this.deps.agent.disablePhonePreamble ?? false,
      );
      this.llmLoop.setEventBus(this._eventBus);
      this.llmLoop.setOnToolCall((n, a, r) => this.recordToolCall(n, a, r));
      getLogger().debug(`Built-in LLM loop active (pipeline, ${label})`);
    }

    if (this.stt) {
      // Make sure the STT WebSocket is OPEN before we install the
      // transcript handler — the parallel kickoff above may still be
      // resolving when we get here. Failures abort the call.
      if (sttConnectPromise) {
        try {
          await sttConnectPromise;
        } catch (e) {
          getLogger().error(`STT connect FAILED (${label}):`, e);
          try { await this.deps.bridge.endCall(this.callId, this.ws); } catch { /* best effort */ }
          return;
        }
      }
      this.stt.onTranscript(async (transcript) => {
        await this.handleTranscript(transcript);
      });
    }
  }

  /** Build a HookContext for the current call state. */
  private buildHookContext(): HookContext {
    return {
      callId: this.callId,
      caller: this.caller,
      callee: this.callee,
      history: [...this.history.entries],
    };
  }

  /** Synthesize a single sentence through TTS with hooks, sending audio to telephony. */
  private async synthesizeSentence(
    sentence: string,
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
    ttsFirstByteSent: { value: boolean },
  ): Promise<void> {
    if (!this.tts || !this.isSpeaking) return;

    // Apply text transforms before the beforeSynthesize hook
    let transformed = sentence;
    const transforms = this.deps.agent.textTransforms;
    if (transforms) {
      for (const fn of transforms) {
        transformed = fn(transformed);
      }
    }

    // beforeSynthesize hook (per-sentence)
    const processedText = await hookExecutor.runBeforeSynthesize(transformed, hookCtx);
    if (processedText === null) return;

    this.resetTtsCarry();
    try {
      for await (const chunk of this.tts.synthesizeStream(processedText)) {
        if (!this.isSpeaking) break;

        // afterSynthesize hook (per-chunk). The await may yield control to
        // the event loop long enough for VAD to fire `speech_start during
        // TTS → BARGE-IN`, which calls cancelSpeaking() and flips
        // ``isSpeaking`` to false. Re-check below before pushing the
        // resulting audio to the carrier — without this re-check, exactly
        // one trailing chunk (~20–100 ms of audio) would race past the
        // cancel and prolong the perceived "agent didn't stop" window.
        const processedAudio = await hookExecutor.runAfterSynthesize(chunk, processedText, hookCtx);
        if (processedAudio === null) continue;
        if (!this.isSpeaking) break;

        if (!ttsFirstByteSent.value) {
          ttsFirstByteSent.value = true;
          this.metricsAcc.recordTtsFirstByte();
          // Speech-event: per-turn first TTS audio chunk.
          await this.emitAudioOut();
        }
        // Far-end tap for the echo canceller. ``processedAudio`` is the
        // exact PCM 16 kHz Buffer that the carrier-side encoder is about
        // to transcode + send — i.e. the cleanest reference of "what the
        // speaker is about to play". Push BEFORE ``sendAudio`` so a very
        // fast carrier echo is still seen by the next mic frame.
        if (this.aec) {
          this.aec.pushFarEnd(processedAudio);
        }
        const encoded = this.encodePipelineAudio(processedAudio);
        this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
        this.markFirstAudioSent();
      }
    } catch (e) {
      getLogger().error(`TTS streaming error (${this.deps.bridge.label}):`, e);
    } finally {
      this.resetTtsCarry();
    }
  }

  /** Handle a final transcript from STT in pipeline mode. */
  private async handleTranscript(transcript: STTTranscript): Promise<void> {
    this.transcriptQueue.push(transcript);
    if (this.transcriptProcessing) return;
    this.transcriptProcessing = true;
    try {
      while (this.transcriptQueue.length > 0) {
        const next = this.transcriptQueue.shift()!;
        await this.processTranscript(next);
      }
    } finally {
      this.transcriptProcessing = false;
    }
  }

  private async processTranscript(transcript: STTTranscript): Promise<void> {
    // [DIAG-2026-05-05] Temporary INFO logging to diagnose post-barge-in
    // empty/phantom transcripts. Remove once root cause is understood.
    getLogger().info(
      `[DIAG] processTranscript text=${JSON.stringify((transcript.text ?? '').slice(0, 60))} isFinal=${transcript.isFinal} speechFinal=${transcript.speechFinal} isSpeaking=${this.isSpeaking}`,
    );
    // Function-scope barge-in flag — set either by the upfront barge-in
    // check, or by the TTS loops downstream when ``isSpeaking`` flips mid-
    // synthesis. Prevents recordTurnComplete double-counting a half-spoken
    // turn (Python uses the same pattern).
    let interrupted = this.handleBargeIn(transcript);

    // Fix 6 (Python parity): start the turn timer on the first non-empty STT
    // partial/final so stt_ms measures from real speech onset rather than from
    // the first silence audio byte. startTurnIfIdle() is a no-op if already open.
    if (transcript.text) {
      this.metricsAcc.startTurnIfIdle();
    }

    // Wave6B: record VAD stop timestamp when the STT provider signals speech end.
    if (transcript.speechFinal) {
      this.metricsAcc.recordVadStop();
    }

    if (!transcript.isFinal || !transcript.text) return;
    if (!this.commitTranscript(transcript.text)) {
      // Final transcript dropped (dedup / hallucination / back-to-back).
      // Any VAD ``speech_end`` that fired during this dropped utterance
      // already stamped ``_endpointSignalAt``; if we leave it there, the
      // NEXT legitimate utterance inherits the stale anchor (its
      // agent_response_ms then includes the silence gap between the
      // dropped utterance and the real one).
      this.metricsAcc.anchorUserSpeechStart();
      return;
    }

    const label = this.deps.bridge.label;
    // [DIAG-2026-05-05] Temporary INFO. Remove once root cause known.
    getLogger().info(
      `[DIAG] processTranscript COMMITTED → LLM (${label} pipeline): ${sanitizeLogValue(transcript.text.slice(0, 80))}`,
    );
    getLogger().debug(`User (${label} pipeline): ${sanitizeLogValue(transcript.text)}`);

    // Safety net: startTurnIfIdle() was already called above on first partial
    // text; this second call is a no-op in the normal path but guards code paths
    // (e.g. tests) that pass a final transcript without any preceding partial.
    this.metricsAcc.startTurnIfIdle();
    this.metricsAcc.recordSttComplete(transcript.text);
    this.metricsAcc.recordSttFinalTimestamp();

    // Endpoint span — silence-detected → LLM-dispatch window. The matching
    // ``end()`` lives below right before ``recordTurnCommitted``. We use a
    // small helper so every early-return path closes the span exactly once.
    const endpointSpan = startSpan(SPAN_ENDPOINT, { 'patter.call.id': this.callId });
    let endpointSpanClosed = false;
    const closeEndpointSpan = (): void => {
      if (endpointSpanClosed) return;
      endpointSpanClosed = true;
      try {
        endpointSpan.end();
      } catch {
        // Swallow — span teardown should never crash the call path.
      }
    };

    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'user',
        text: transcript.text,
        call_id: this.callId,
        history: [...this.history.entries],
      });
    }

    // --- afterTranscribe hook ---
    const hookExecutor = new PipelineHookExecutor(this.deps.agent.hooks);
    const hookCtx = this.buildHookContext();
    const filteredTranscript = await hookExecutor.runAfterTranscribe(transcript.text, hookCtx);
    if (filteredTranscript === null) {
      getLogger().debug(`afterTranscribe hook vetoed turn (${label})`);
      this.metricsAcc.recordTurnInterrupted();
      closeEndpointSpan();
      return;
    }

    // Push filtered text to history (after hook, so LLM sees redacted/modified text)
    this.history.push({ role: 'user', text: filteredTranscript, timestamp: Date.now() });

    let responseText = '';

    // Wave6B: record that the transcript is being committed to the LLM.
    // onUserTurnCompleted hook is not yet wired in TS — record 0 delay so EOU can still emit.
    this.metricsAcc.recordOnUserTurnCompletedDelay(0);
    this.metricsAcc.recordTurnCommitted();
    closeEndpointSpan();

    if (this.deps.onMessage && typeof this.deps.onMessage === 'function') {
      try {
        responseText = await this.deps.onMessage({
          text: filteredTranscript,
          call_id: this.callId,
          caller: this.caller,
          callee: this.callee,
          history: [...this.history.entries],
        });
      } catch (e) {
        getLogger().error(`onMessage error (${label}):`, e);
        return;
      }
      if (!responseText) {
        // Common misuse: onMessage was provided as an observer (returning void)
        // but it actually replaces the built-in LLM loop. Warn loudly — the caller
        // will hear no audio until the handler returns a non-empty string.
        getLogger().warn(
          `onMessage returned empty/void (${label}) — no TTS will play. ` +
          `If you intended to observe transcripts, use onTranscript instead; ` +
          `if you meant to answer via the built-in LLM, remove onMessage and pass openaiKey.`,
        );
      }
    } else if (this.deps.onMessage && isRemoteUrl(this.deps.onMessage)) {
      const msgData = {
        text: filteredTranscript,
        call_id: this.callId,
        caller: this.caller,
        callee: this.callee,
        history: [...this.history.entries],
      };
      if (isWebSocketUrl(this.deps.onMessage)) {
        await this.handleWebSocketResponse(msgData);
        return;
      }
      try {
        responseText = await this.deps.remoteHandler.callWebhook(this.deps.onMessage, msgData);
      } catch (e) {
        getLogger().error(`Webhook remote error (${label}):`, e);
        return;
      }
    } else if (this.llmLoop) {
      responseText = await this.runPipelineLlm(filteredTranscript, hookExecutor, hookCtx);
    } else {
      getLogger().warn(
        `Pipeline (${label}) has no llm/onMessage handler — transcript ` +
          `"${sanitizeLogValue(filteredTranscript.slice(0, 60))}" dropped. ` +
          'Check that agent.llm or onMessage is configured.',
      );
      return;
    }

    if (!responseText) return;

    if (this.llmLoop) {
      await this.emitAssistantTranscript(responseText);
      this.metricsAcc.recordTtsComplete(responseText);
    } else {
      interrupted = await this.runRegularLlm(responseText, hookExecutor, hookCtx) || interrupted;
      // ``runRegularLlm`` returns the possibly-replaced text via side effect on
      // history; recompute responseText from the last history entry for the
      // turn-complete record.
      responseText = this.history.entries[this.history.entries.length - 1]?.text ?? responseText;
    }

    // Skip turn-complete when barge-in already recorded the turn as
    // interrupted — mirrors Python ``if not interrupted``. Prevents
    // double-counting / turn-count inflation / polluting p95.
    if (!interrupted) {
      await this.emitTurnMetrics(this.metricsAcc.recordTurnComplete(responseText));
    }
  }

  /**
   * Barge-in: caller spoke over in-flight TTS. Flip ``isSpeaking`` so the
   * sentence loop exits on its next check, clear downstream audio buffers,
   * record the interruption, and return ``true`` so the caller skips the
   * turn-complete record.
   */
  private async handleBargeInAsync(transcript: {
    text?: string;
    isFinal?: boolean;
  }): Promise<boolean> {
    if (!transcript.text || !this.isSpeaking) return false;
    if (!this.canBargeIn()) {
      getLogger().info(
        `Barge-in transcript suppressed (agent speaking < gate, aec=${this.aec ? 'on' : 'off'})`,
      );
      return false;
    }
    if (this.bargeInStrategies.length > 0) {
      const { evaluateStrategies } = await import(
        './services/barge-in-strategies.js'
      );
      const confirmed = await evaluateStrategies(this.bargeInStrategies, {
        transcript: transcript.text,
        isInterim: transcript.isFinal === false,
        agentSpeaking: this.isSpeaking,
      });
      if (!confirmed) {
        getLogger().debug(
          `Barge-in NOT confirmed by any strategy (${sanitizeLogValue(
            transcript.text.slice(0, 40),
          )}); agent continues talking`,
        );
        return false;
      }
      getLogger().info(
        `Barge-in confirmed by strategy on transcript ${sanitizeLogValue(
          transcript.text.slice(0, 40),
        )}`,
      );
    }
    this.runBargeInCancel(transcript.text);
    return true;
  }

  /**
   * Synchronous wrapper that callers in legacy code paths can keep using.
   * When ``bargeInStrategies`` is empty the work is fully synchronous and
   * the result is correct. With strategies the call is dispatched as a
   * floating promise — non-confirmed transcripts simply skip the cancel
   * and the legacy boolean return is meaningless under that opt-in path.
   */
  private handleBargeIn(transcript: { text?: string; isFinal?: boolean }): boolean {
    if (!transcript.text || !this.isSpeaking) return false;
    if (this.bargeInStrategies.length === 0) {
      // Legacy synchronous path — preserve exact byte-for-byte behaviour
      // for users who haven't opted into the confirm pipeline.
      if (!this.canBargeIn()) {
        getLogger().info(
          `Barge-in transcript suppressed (agent speaking < gate, aec=${this.aec ? 'on' : 'off'})`,
        );
        return false;
      }
      this.runBargeInCancel(transcript.text);
      return true;
    }
    // Opt-in confirm path is async; fire-and-forget. The cancel inside
    // ``runBargeInCancel`` flips ``isSpeaking`` synchronously once it
    // resolves, which is what downstream loops actually observe.
    void this.handleBargeInAsync(transcript).catch((err) =>
      getLogger().debug(`handleBargeInAsync threw: ${String(err)}`),
    );
    return false;
  }

  /**
   * Run the cancel/flush sequence for a confirmed barge-in. Shared by
   * the legacy synchronous path and the strategy-confirmed async path.
   */
  private runBargeInCancel(transcriptText: string): void {
    // Capture pending state BEFORE clearPendingBargeIn() drops it — if VAD
    // already started the overlap window via ``startPendingBargeIn`` we MUST
    // NOT call ``recordOverlapStart`` again (that would overwrite T1 with
    // T2 and produce a near-zero ``InterruptionMetrics.detection_delay_ms``
    // on the strategy path).
    const hadPending = this.bargeInPendingSince !== null;
    this.clearPendingBargeIn();
    getLogger().debug(
      `Barge-in: caller spoke over agent (${sanitizeLogValue(transcriptText.slice(0, 40))})`,
    );
    if (!hadPending) {
      // Legacy path or VAD never fired — start the overlap window now.
      this.metricsAcc.recordOverlapStart();
    }
    this.metricsAcc.recordBargeinDetected();
    const bargeinSpan = startSpan(SPAN_BARGEIN, { 'patter.call.id': this.callId });
    try {
      this.cancelSpeaking();
      try {
        this.deps.bridge.sendClear(this.ws, this.streamSid);
      } catch (err) {
        getLogger().debug(`sendClear during barge-in failed: ${String(err)}`);
      }
      this.metricsAcc.recordTtsStopped();
      this.metricsAcc.recordTurnInterrupted();
      // Re-anchor turn metrics to the legitimate VAD speech_start so post-
      // barge-in latency anchors don't carry over from the interrupted turn.
      this.metricsAcc.anchorUserSpeechStart();
      this.metricsAcc.recordOverlapEnd(true);
    } finally {
      try {
        bargeinSpan.end();
      } catch {
        // Swallow.
      }
    }
  }

  /** Mark a VAD-detected barge-in as pending (no cancel yet). */
  private startPendingBargeIn(): void {
    if (this.bargeInPendingSince !== null) return;
    this.bargeInPendingSince = Date.now();
    this.metricsAcc.recordOverlapStart();
    getLogger().info(
      'Barge-in PENDING (VAD speech_start during TTS); awaiting strategy confirmation',
    );
    this.bargeInPendingTimer = setTimeout(() => {
      if (this.bargeInPendingSince === null) return;
      getLogger().info(
        `Pending barge-in timed out after ${this.bargeInConfirmMs}ms; agent resumes (no strategy confirmed)`,
      );
      this.metricsAcc.recordOverlapEnd(false);
      // Clear any anchors that drifted during the pending barge-in window.
      this.metricsAcc.anchorUserSpeechStart();
      this.bargeInPendingSince = null;
      this.bargeInPendingTimer = null;
    }, this.bargeInConfirmMs);
  }

  /** Drop pending state without cancelling — used on confirm and on
   * agent stop. Idempotent. */
  private clearPendingBargeIn(): void {
    if (this.bargeInPendingTimer !== null) {
      clearTimeout(this.bargeInPendingTimer);
      this.bargeInPendingTimer = null;
    }
    this.bargeInPendingSince = null;
  }

  /**
   * Dedup + throttle + hallucination filter for final STT transcripts.
   * Mirrors ``PipelineStreamHandler._stt_loop`` on the Python side.
   * Returns ``true`` when the transcript should be committed to a turn,
   * ``false`` when it must be dropped. Drop reasons:
   *   - text matches common short hallucinations ("you", "thanks", ...)
   *   - duplicate final within 2 s of previous commit
   *   - back-to-back finals under 500 ms (too tight to be real utterances)
   */
  private commitTranscript(text: string): boolean {
    const now = Date.now();
    const normalised = text.trim().toLowerCase();
    const stripped = normalised.replace(/[.,!?;: ]+$/, '').trim();
    const sinceLastMs = now - this.lastCommitAt;
    if (HALLUCINATIONS.has(stripped) || stripped === '') {
      getLogger().debug(`Dropped likely STT hallucination: ${sanitizeLogValue(normalised.slice(0, 40))}`);
      return false;
    }
    if (sinceLastMs < 2000 && normalised === this.lastCommitText) {
      getLogger().debug(
        `Dropped duplicate final transcript (${(sinceLastMs / 1000).toFixed(1)}s since last): ${sanitizeLogValue(normalised.slice(0, 40))}`,
      );
      return false;
    }
    if (sinceLastMs < 500) {
      getLogger().debug(
        `Dropped back-to-back final transcript (${(sinceLastMs / 1000).toFixed(2)}s since last): ${sanitizeLogValue(normalised.slice(0, 40))}`,
      );
      return false;
    }
    this.lastCommitText = normalised;
    this.lastCommitAt = now;
    return true;
  }

  /**
   * Streaming built-in LLM path with sentence chunking and per-sentence
   * guardrails/TTS. Returns the concatenated response text.
   */
  private async runPipelineLlm(
    filteredTranscript: string,
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
  ): Promise<string> {
    const label = this.deps.bridge.label;
    const callCtx = { call_id: this.callId, caller: this.caller, callee: this.callee };
    const chunker = new SentenceChunker({
      aggressiveFirstFlush: this.deps.agent.aggressiveFirstFlush ?? false,
      language: this.deps.agent.language,
    });
    const allParts: string[] = [];
    const ttsFirstByteSent = { value: false };
    await this.beginSpeaking();
    // Fresh AbortController per turn so a stale abort from a previous
    // barge-in cannot terminate this stream.  ``cancelSpeaking`` aborts
    // it; the consumption loop checks ``signal.aborted`` between tokens
    // to break early and free the upstream LLM connection.
    this.llmAbort = new AbortController();
    const llmSignal = this.llmAbort.signal;
    let llmError = false;

    // Span lifetime: LLM dispatch → final token / TTS handoff. Always closed
    // in the ``finally`` block so an early throw cannot leak a span.
    const llmSpan = startSpan(SPAN_LLM, { 'patter.call.id': this.callId });

    const guardAndSpeak = async (sentence: string, isFirst: boolean): Promise<void> => {
      // Fix 3/5: record first-sentence boundary before synthesizing first sentence.
      if (isFirst) this.metricsAcc.recordLlmFirstSentenceComplete();
      const guard = checkGuardrails(sentence, this.deps.agent.guardrails);
      let sentenceText = guard
        ? (guard.replacement ?? "I'm sorry, I can't respond to that.")
        : sentence;
      // Tier 2 — per-sentence after_llm transform. Runs between the
      // sentence chunker and TTS so PII redaction / persona overlay /
      // refusal swap can edit individual sentences without buffering the
      // full LLM response. Returning null from the hook drops the sentence.
      if (hookExecutor.hasAfterLlmSentence()) {
        const transformed = await hookExecutor.runAfterLlmSentence(sentenceText, hookCtx);
        if (transformed === null) return; // hook dropped this sentence
        sentenceText = transformed;
      }
      await this.synthesizeSentence(sentenceText, hookExecutor, hookCtx, ttsFirstByteSent);
    };
    let firstSentenceEmitted = false;

    try {
      try {
        for await (const token of this.llmLoop!.run(
          filteredTranscript,
          this.history.entries,
          callCtx,
          this.metricsAcc,
          hookExecutor,
          hookCtx,
          { signal: llmSignal },
        )) {
          if (llmSignal.aborted) break;
          // Fix 5: record first token for TTFT metric.
          this.metricsAcc.recordLlmFirstToken();
          // Speech-event: per-turn TTFT marker for SDK callback consumers.
          // Idempotent in the dispatcher.
          await this.emitLlmFirstToken();
          allParts.push(token);
          for (const sentence of chunker.push(token)) {
            if (!this.isSpeaking) break;
            await guardAndSpeak(sentence, !firstSentenceEmitted);
            firstSentenceEmitted = true;
          }
          if (!this.isSpeaking || llmSignal.aborted) break;
        }
      } catch (e) {
        // Treat AbortError as a clean barge-in cancellation, not an LLM error.
        const isAbort =
          (e as Error)?.name === 'AbortError' || llmSignal.aborted;
        if (!isAbort) {
          llmError = true;
          chunker.reset(); // discard partial content on LLM error
          getLogger().error(`LLM loop error (${label}):`, e);
          // Fix 8: record turn as interrupted so it does not leak in metrics when
          // the LLM throws without emitting any text.
          this.metricsAcc.recordTurnInterrupted();
        }
      }

      this.metricsAcc.recordLlmComplete(); // record BEFORE TTS flush, not after

      if (!llmError && this.isSpeaking) {
        for (const sentence of chunker.flush()) {
          if (!this.isSpeaking) break;
          await guardAndSpeak(sentence, !firstSentenceEmitted);
          firstSentenceEmitted = true;
        }
      }
    } finally {
      this.endSpeakingWithGrace();
      // Drop the per-turn abort controller so the next turn starts with a
      // fresh one and barge-ins on the next turn cannot accidentally fire
      // an already-aborted signal.
      this.llmAbort = null;
      try {
        llmSpan.end();
      } catch {
        // Swallow — span teardown should never crash the call path.
      }
    }
    return allParts.join('');
  }

  /**
   * Non-streaming path (onMessage function / webhook): apply output guardrails,
   * push to history, sentence-chunk the text, synthesize. Returns ``true`` if
   * TTS was interrupted mid-flight so the caller can skip turn-complete.
   */
  private async runRegularLlm(
    responseText: string,
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
  ): Promise<boolean> {
    const guard = checkGuardrails(responseText, this.deps.agent.guardrails);
    let text = responseText;
    if (guard) {
      getLogger().debug(`Guardrail '${guard.name}' triggered (pipeline)`);
      text = guard.replacement ?? "I'm sorry, I can't respond to that.";
    }

    this.metricsAcc.recordLlmComplete();
    await this.emitAssistantTranscript(text);

    const chunker = new SentenceChunker();
    const sentences = [...chunker.push(text), ...chunker.flush()];
    const ttsFirstByteSent = { value: false };
    await this.beginSpeaking();
    let interrupted = false;

    try {
      for (const sentence of sentences) {
        if (!this.isSpeaking) { interrupted = true; break; }
        let sentenceText = sentence;
        // Tier 2 — apply per-sentence after_llm hook on non-streaming
        // path too (parity with the streaming path's guardAndSpeak).
        if (hookExecutor.hasAfterLlmSentence()) {
          const transformed = await hookExecutor.runAfterLlmSentence(sentenceText, hookCtx);
          if (transformed === null) continue; // hook dropped this sentence
          sentenceText = transformed;
        }
        await this.synthesizeSentence(sentenceText, hookExecutor, hookCtx, ttsFirstByteSent);
      }
    } finally {
      this.endSpeakingWithGrace();
    }

    if (!interrupted) this.metricsAcc.recordTtsComplete(text);
    return interrupted;
  }

  /** Handle streaming WebSocket remote response with TTS. */
  private async handleWebSocketResponse(msgData: Record<string, unknown>): Promise<void> {
    const onMessage = this.deps.onMessage as string;
    const parts: string[] = [];
    this.metricsAcc.recordLlmComplete();
    await this.beginSpeaking();
    let wsTtsStarted = false;
    try {
      for await (const chunk of this.deps.remoteHandler.callWebSocket(onMessage, msgData)) {
        parts.push(chunk);
        if (this.tts) {
          this.resetTtsCarry();
          for await (const audioChunk of this.tts.synthesizeStream(chunk)) {
            if (!this.isSpeaking) break;
            if (!wsTtsStarted) { wsTtsStarted = true; this.metricsAcc.recordTtsFirstByte(); await this.emitAudioOut(); }
            const encoded = this.encodePipelineAudio(audioChunk);
            this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
            this.markFirstAudioSent();
          }
        }
      }
    } catch (e) {
      getLogger().error(`WebSocket remote error (${this.deps.bridge.label}):`, e);
    } finally {
      this.endSpeakingWithGrace();
      this.resetTtsCarry();
    }
    const responseText = parts.join('');
    this.metricsAcc.recordTtsComplete(responseText);
    await this.emitTurnMetrics(this.metricsAcc.recordTurnComplete(responseText));
    if (responseText) await this.emitAssistantTranscript(responseText);
  }

  // ---------------------------------------------------------------------------
  // Private: OpenAI Realtime / ElevenLabs ConvAI mode
  // ---------------------------------------------------------------------------

  private async initRealtimeAdapter(resolvedPrompt: string): Promise<void> {
    const label = this.deps.bridge.label;
    this.adapter = this.deps.buildAIAdapter(resolvedPrompt);

    try {
      await this.adapter.connect();
      getLogger().debug(`AI adapter connected (${label})`);
    } catch (e) {
      getLogger().error(`AI adapter connect FAILED (${label}):`, e);
      // Hang up the telephony call so it doesn't stay connected billing
      try { await this.deps.bridge.endCall(this.callId, this.ws); } catch { /* best effort */ }
      return;
    }

    if (this.deps.agent.firstMessage) {
      // Start measuring latency for the first turn (firstMessage → first audio byte)
      this.metricsAcc.startTurn();
      if (this.adapter instanceof OpenAIRealtimeAdapter) {
        // Use ``sendFirstMessage`` (role=assistant) so the AI treats
        // ``firstMessage`` as its OWN opening line, not a user prompt to
        // respond to. Older adapter builds without the method fall back to
        // ``sendText`` (legacy role=user behaviour).
        const sender =
          typeof (this.adapter as unknown as { sendFirstMessage?: (t: string) => Promise<void> }).sendFirstMessage === 'function'
            ? (this.adapter as unknown as { sendFirstMessage: (t: string) => Promise<void> }).sendFirstMessage.bind(this.adapter)
            : this.adapter.sendText.bind(this.adapter);
        await sender(this.deps.agent.firstMessage);
      }
      // ElevenLabs ConvAI sends firstMessage via connection config (handled in adapter.connect())
    }

    this.adapter.onEvent(async (type, eventData) => {
      try {
        await this.handleAdapterEvent(type, eventData);
      } catch (err) {
        getLogger().error(`Adapter event handler error (${label}):`, err);
      }
    });
  }

  private async handleAdapterEvent(type: string, eventData: unknown): Promise<void> {
    const handler = this.adapterEventHandlers[type];
    if (handler) await handler(eventData);
  }

  /** Event-type → handler dispatch table for the Realtime adapter. */
  private readonly adapterEventHandlers: Record<string, (eventData: unknown) => Promise<void>> = {
    audio: async (eventData) => this.onAdapterAudio(eventData as Buffer),
    speech_stopped: async () => this.onAdapterSpeechStopped(),
    transcript_input: async (eventData) => this.onAdapterTranscriptInput(eventData as string),
    transcript_output: async (eventData) => this.onAdapterTranscriptOutput(eventData as string),
    response_done: async (eventData) => this.onAdapterResponseDone(eventData as Record<string, unknown> | null),
    speech_started: async () => this.onAdapterSpeechInterrupt(),
    interruption: async () => this.onAdapterSpeechInterrupt(),
    function_call: async (eventData) => {
      if (this.adapter instanceof OpenAIRealtimeAdapter) {
        await this.handleFunctionCall(eventData as { call_id: string; name: string; arguments: string });
      }
    },
  };

  // ---- Speech-event helpers ------------------------------------------
  // No-op when the deps don't include a SpeechEvents dispatcher. Tracks
  // wall-clock for `speech_duration_ms` payloads.
  private userSpeechStartMs: number | null = null;
  private agentTurnStartMs: number | null = null;

  private async emitUserSpeechStarted(): Promise<void> {
    if (!this.deps.speechEvents) return;
    this.userSpeechStartMs = Date.now();
    await this.deps.speechEvents.fireUserSpeechStarted();
  }

  private async emitUserSpeechEnded(): Promise<void> {
    if (!this.deps.speechEvents) return;
    const duration =
      this.userSpeechStartMs !== null
        ? Math.max(0, Date.now() - this.userSpeechStartMs)
        : 0;
    this.userSpeechStartMs = null;
    await this.deps.speechEvents.fireUserSpeechEnded({
      speechDurationMs: duration,
    });
  }

  private async emitUserSpeechEos(transcriptSoFar?: string): Promise<void> {
    if (!this.deps.speechEvents) return;
    await this.deps.speechEvents.fireUserSpeechEos({
      trigger: "vad_silence",
      transcriptSoFar,
    });
  }

  private async emitAgentSpeechStarted(): Promise<void> {
    if (!this.deps.speechEvents) return;
    this.agentTurnStartMs = Date.now();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const ttsKey = (this.deps.agent.tts?.constructor as any)?.providerKey;
    await this.deps.speechEvents.fireAgentSpeechStarted({
      ttsProvider: ttsKey,
      engine: this.deps.agent.provider ?? "openai_realtime",
    });
  }

  private async emitAgentSpeechEnded(interrupted: boolean): Promise<void> {
    if (!this.deps.speechEvents) return;
    if (this.agentTurnStartMs === null) return;
    const duration = Math.max(0, Date.now() - this.agentTurnStartMs);
    this.agentTurnStartMs = null;
    await this.deps.speechEvents.fireAgentSpeechEnded({
      speechDurationMs: duration,
      interrupted,
    });
  }

  /** Fire the per-turn LLM TTFT marker. Idempotent in the dispatcher
   * — guarded by `firstTokenForTurn` on the SpeechEvents instance. */
  private async emitLlmFirstToken(): Promise<void> {
    if (!this.deps.speechEvents) return;
    await this.deps.speechEvents.fireLlmFirstToken({
      llmProvider: this.llmProviderTag,
      model: this.deps.agent.model ?? "",
    });
  }

  /** Fire the per-turn first-TTS-audio marker. Idempotent in the
   * dispatcher — guarded by `firstAudioForTurn`. The provider tag falls
   * back to the engine name for Realtime / ConvAI (no separate TTS). */
  private async emitAudioOut(): Promise<void> {
    if (!this.deps.speechEvents) return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const ttsKey = (this.deps.agent.tts?.constructor as any)?.providerKey;
    const provider =
      ttsKey ?? this.deps.agent.provider ?? "openai_realtime";
    await this.deps.speechEvents.fireAudioOut({ ttsProvider: provider });
  }

  private async onAdapterAudio(eventData: Buffer): Promise<void> {
    // Record time-to-first-audio-byte as latency (Realtime mode). If no
    // startTurn() was called yet (e.g. agent responding again without user
    // input), start a new turn now so latency is still measured.
    if (!this.responseAudioStarted) {
      this.responseAudioStarted = true;
      if (this.metricsAcc.turnActive === false) this.metricsAcc.startTurn();
      this.metricsAcc.recordTtsFirstByte();
      // Speech-event: first wire-time chunk of this agent turn.
      await this.emitAgentSpeechStarted();
      // Speech-event: in Realtime / ConvAI modes the model output IS the
      // TTS audio, so the same edge satisfies the per-turn
      // ``tts_first_audio`` marker for SDK callback consumers. The
      // dispatcher's idempotency guard prevents double-fires.
      await this.emitAudioOut();
    }
    // OpenAI Realtime outputs g711_ulaw 8 kHz (PCMU). Both Twilio and Telnyx
    // are configured for PCMU/mulaw 8 kHz (Telnyx uses stream_bidirectional_codec=PCMU)
    // so the audio is already in the correct wire format — pass through untransformed.
    // Do NOT resample here: inboundResampler is 8k→16k for the STT inbound path;
    // reusing it on the outbound path corrupts both directions.
    const outAudio = eventData;
    this.deps.bridge.sendAudio(this.ws, outAudio.toString('base64'), this.streamSid);
    this.markFirstAudioSent();
    // Send mark for barge-in accuracy.
    this.chunkCount++;
    this.deps.bridge.sendMark(this.ws, `audio_${this.chunkCount}`, this.streamSid);
  }

  private async onAdapterSpeechStopped(): Promise<void> {
    // Server VAD end-of-speech is the earliest reliable moment to start
    // measuring turn latency in Realtime mode — ``transcript_input``
    // (transcription.completed) arrives noticeably later and understates
    // end-to-end latency.
    if (!this.metricsAcc.turnActive) this.metricsAcc.startTurn();
    this.currentAgentText = '';
    this.responseAudioStarted = false;
    // Mark that a user transcript is expected so the assistant's
    // forthcoming `response.done` event waits for it before being
    // pushed into history. See `userTranscriptPending` doc comment.
    this.userTranscriptPending = true;
    // Speech-event: raw VAD trailing edge. EOU commit happens later on
    // ``transcript_input`` (Realtime emits it after
    // input_audio_buffer.committed).
    await this.emitUserSpeechEnded();
  }

  private async onAdapterTranscriptInput(inputText: string): Promise<void> {
    getLogger().debug(`User (${this.deps.bridge.label}): ${sanitizeLogValue(inputText)}`);
    this.history.push({ role: 'user', text: inputText, timestamp: Date.now() });
    // Fallback: if speech_stopped was missed (server VAD disabled, custom
    // config, ...) still start the turn here so latency is non-zero.
    if (!this.metricsAcc.turnActive) {
      this.metricsAcc.startTurn();
      this.currentAgentText = '';
      this.responseAudioStarted = false;
    }
    // Speech-event: end-of-utterance committed (Realtime mode emits this
    // on ``input_audio_buffer.committed``, the canonical "user finished"
    // signal). Advances `turnIdx` and arms first-token / first-audio.
    await this.emitUserSpeechEos(inputText);
    // Marks ASR as complete — exposes a stt_ms bucket in Realtime mode
    // distinct from the llm+tts portion. Parity with Python handler.
    this.metricsAcc.recordSttComplete(inputText);
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'user',
        text: inputText,
        call_id: this.callId,
        history: [...this.history.entries],
      });
    }
    // User transcript is in — clear the pending flag and flush any
    // assistant turn that was buffered waiting for this.
    this.userTranscriptPending = false;
    if (this.pendingAssistantTurn !== null) {
      const buffered = this.pendingAssistantTurn;
      this.pendingAssistantTurn = null;
      if (this.pendingAssistantTimer) {
        clearTimeout(this.pendingAssistantTimer);
        this.pendingAssistantTimer = null;
      }
      await this.flushAssistantTurn(buffered);
    }
  }

  /**
   * Push an assistant turn into history, fire `onTranscript`, and emit
   * turn-complete metrics. Shared between the immediate path (no user
   * transcript pending) and the buffered path (flushed after user
   * transcript arrives or fallback timer fires).
   */
  private async flushAssistantTurn(text: string): Promise<void> {
    this.history.push({ role: 'assistant', text, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'assistant',
        text,
        call_id: this.callId,
        history: [...this.history.entries],
      });
    }
    this.responseAudioStarted = false;
    await this.emitTurnMetrics(this.metricsAcc.recordTurnComplete(text));
  }

  /**
   * Push an assistant turn into history and fire `onTranscript` so host
   * applications observe pipeline-mode replies the same way they observe
   * realtime-mode replies. Mirrors `_emit_assistant_transcript` in the
   * Python SDK and parallels `flushAssistantTurn` (realtime path).
   * Caller is responsible for filtering empty strings.
   */
  private async emitAssistantTranscript(text: string): Promise<void> {
    this.history.push({ role: 'assistant', text, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'assistant',
        text,
        call_id: this.callId,
        history: [...this.history.entries],
      });
    }
  }

  /**
   * Surface a tool invocation from pipeline mode into the transcript
   * timeline. Emits TWO events: one for the call (`name(argsJson)`) and
   * one for the result (`name(...) → result`, truncated to 200 chars).
   * Mirrors realtime mode's two `emitToolEvent` calls in
   * `handleFunctionCall`. Wired as the `LLMLoop` `onToolCall` observer.
   */
  private async recordToolCall(
    name: string,
    args: Record<string, unknown>,
    result: string,
  ): Promise<void> {
    let argsText: string;
    try {
      argsText = JSON.stringify(args ?? {});
    } catch {
      argsText = '{}';
    }
    // 1) Call event
    const callText = `${name}(${argsText})`;
    this.history.push({ role: 'tool', text: callText, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'tool',
        text: callText,
        call_id: this.callId,
        tool_name: name,
        tool_args: args ?? {},
        tool_result: null,
      });
    }
    // 2) Result event (truncated for display, full payload in messages)
    const displayed = result.length > 200 ? result.slice(0, 200) + '…' : result;
    const resText = `${name}(...) → ${displayed}`;
    this.history.push({ role: 'tool', text: resText, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'tool',
        text: resText,
        call_id: this.callId,
        tool_name: name,
        tool_args: args ?? {},
        tool_result: result,
      });
    }
  }

  private async onAdapterTranscriptOutput(outputText: string): Promise<void> {
    if (!outputText) return;
    // Speech-event: per-turn TTFT marker. Idempotent in the dispatcher
    // — guarded by `firstTokenForTurn`. The provider tag matches the
    // engine that produced the transcript (Realtime or ConvAI).
    await this.emitLlmFirstToken();
    const triggered = checkGuardrails(outputText, this.deps.agent.guardrails);
    if (triggered) {
      getLogger().debug(`Guardrail '${triggered.name}' triggered`);
      if (this.adapter instanceof OpenAIRealtimeAdapter) {
        this.adapter.cancelResponse();
        await this.adapter.sendText(triggered.replacement ?? "I'm sorry, I can't respond to that.");
      }
    }
    // Accumulate text — a single history entry is pushed on response_done.
    this.currentAgentText += outputText;
  }

  private async onAdapterResponseDone(responseData: Record<string, unknown> | null): Promise<void> {
    if (responseData) {
      const usage = responseData.usage as {
        input_token_details?: { audio_tokens?: number; text_tokens?: number };
        output_token_details?: { audio_tokens?: number; text_tokens?: number };
      } | undefined;
      if (usage) {
        // ``response.done`` carries the model used for this turn (e.g.
        // ``gpt-realtime-2``); pass it so the cost calc auto-resolves the
        // per-model rate. Falls back to ``this.realtimeModel`` set at call
        // start when the field is absent on the payload.
        const turnModel =
          typeof responseData.model === 'string' ? (responseData.model as string) : null;
        this.metricsAcc.recordRealtimeUsage(usage, turnModel);
      }
    }
    if (!this.currentAgentText) {
      // Empty response — discard the orphaned turn so it doesn't leak.
      this.metricsAcc.recordTurnInterrupted();
      this.responseAudioStarted = false;
      // Speech-event: agent turn ended without text (cancelled).
      await this.emitAgentSpeechEnded(true);
      return;
    }
    // Speech-event: clean agent turn completion (text emitted).
    await this.emitAgentSpeechEnded(false);
    const text = this.currentAgentText;
    this.currentAgentText = '';
    if (this.userTranscriptPending) {
      // Buffer until the user transcript arrives so the rendered order
      // is [user, assistant, user, assistant, ...] rather than the
      // OpenAI Realtime native order [assistant, user, assistant, ...].
      this.pendingAssistantTurn = text;
      if (this.pendingAssistantTimer) clearTimeout(this.pendingAssistantTimer);
      this.pendingAssistantTimer = setTimeout(() => {
        const buffered = this.pendingAssistantTurn;
        this.pendingAssistantTurn = null;
        this.pendingAssistantTimer = null;
        this.userTranscriptPending = false;
        if (buffered !== null) {
          // Fire-and-forget — caller is a setTimeout, can't await.
          void this.flushAssistantTurn(buffered);
        }
      }, StreamHandler.REALTIME_USER_TRANSCRIPT_WAIT_MS);
      this.responseAudioStarted = false;
      return;
    }
    await this.flushAssistantTurn(text);
  }

  private async onAdapterSpeechInterrupt(): Promise<void> {
    this.deps.bridge.sendClear(this.ws, this.streamSid);
    if (this.adapter instanceof OpenAIRealtimeAdapter) this.adapter.cancelResponse();
    this.metricsAcc.recordTurnInterrupted();
    // Speech-event: user started speaking. If the agent was mid-turn this
    // is a barge-in — close the agent turn as interrupted before flagging
    // the new user-speech edge so consumers see ``agent_ended(true)`` →
    // ``user_started`` in causal order.
    if (this.responseAudioStarted) {
      await this.emitAgentSpeechEnded(true);
    }
    await this.emitUserSpeechStarted();
    this.currentAgentText = '';
    this.responseAudioStarted = false;
    // A barge-in invalidates any buffered assistant turn — the user
    // interrupted before the response was committed, so we should not
    // surface it as if the agent had finished speaking.
    this.pendingAssistantTurn = null;
    if (this.pendingAssistantTimer) {
      clearTimeout(this.pendingAssistantTimer);
      this.pendingAssistantTimer = null;
    }
    this.userTranscriptPending = false;
  }

  /**
   * Emit a tool-invocation event into the transcript timeline. Pushes a
   * `role=tool` entry into `history` (so it appears in the dashboard
   * transcript next to user/assistant turns) AND fires `onTranscript` so
   * the host application can log / persist / render it. `result` is
   * truncated for log readability — the full payload is in history.
   */
  private async emitToolEvent(
    name: string,
    args: unknown,
    result: string | null,
  ): Promise<void> {
    const argsText = JSON.stringify(args);
    const text = result === null
      ? `${name}(${argsText})`
      : `${name}(${argsText}) → ${result.length > 200 ? result.slice(0, 200) + '…' : result}`;
    this.history.push({ role: 'tool', text, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'tool',
        text,
        call_id: this.callId,
        tool_name: name,
        tool_args: args,
        tool_result: result,
      });
    }
  }

  private async handleFunctionCall(fc: { call_id: string; name: string; arguments: string }): Promise<void> {
    const adapter = this.adapter as OpenAIRealtimeAdapter;

    if (fc.name === 'transfer_call') {
      let transferArgs: { number?: string };
      try {
        transferArgs = JSON.parse(fc.arguments || '{}') as { number?: string };
      } catch {
        transferArgs = {};
      }
      const transferTo = transferArgs.number ?? '';
      if (!isValidE164(transferTo)) {
        getLogger().warn(`transfer_call rejected (${this.deps.bridge.label}): invalid number ${JSON.stringify(transferTo)}`);
        const rejection = JSON.stringify({ error: 'Invalid phone number format', status: 'rejected' });
        await adapter.sendFunctionResult(fc.call_id, rejection);
        await this.emitToolEvent('transfer_call', transferArgs, rejection);
        return;
      }
      getLogger().debug(`Transferring call to ${transferTo}`);
      const result = JSON.stringify({ status: 'transferring', to: transferTo });
      await adapter.sendFunctionResult(fc.call_id, result);
      await this.emitToolEvent('transfer_call', transferArgs, result);
      await this.deps.bridge.transferCall(this.callId, transferTo);
      if (this.deps.onTranscript) {
        await this.deps.onTranscript({ role: 'system', text: `Call transferred to ${transferTo}`, call_id: this.callId });
      }
      return;
    }

    if (fc.name === 'end_call') {
      let endArgs: { reason?: string };
      try {
        endArgs = JSON.parse(fc.arguments || '{}') as { reason?: string };
      } catch {
        endArgs = {};
      }
      const reason = endArgs.reason ?? 'conversation_complete';
      getLogger().debug(`Ending call (${this.deps.bridge.label}): ${reason}`);
      const result = JSON.stringify({ status: 'ending', reason });
      await adapter.sendFunctionResult(fc.call_id, result);
      await this.emitToolEvent('end_call', endArgs, result);
      await this.deps.bridge.endCall(this.callId, this.ws);
      if (this.deps.onTranscript) {
        await this.deps.onTranscript({ role: 'system', text: `Call ended: ${reason}`, call_id: this.callId });
      }
      return;
    }

    // User-defined tool — supports either `handler` (in-process function)
    // or `webhookUrl` (HTTP POST). Dispatched through ``DefaultToolExecutor``
    // so both paths get retry-with-exponential-backoff and a per-tool
    // circuit breaker. Previously only `webhookUrl` worked in Realtime
    // mode (handler tools fell through and hung the model); now both are
    // routed through the same robust executor used by pipeline mode.
    const toolDef = this.deps.agent.tools?.find((t) => t.name === fc.name);
    if (!toolDef) {
      getLogger().warn(`Realtime tool '${fc.name}' not found in agent.tools — skipping`);
      const result = JSON.stringify({ error: `Tool '${fc.name}' not registered`, fallback: true });
      await adapter.sendFunctionResult(fc.call_id, result);
      await this.emitToolEvent(fc.name, {}, result);
      return;
    }
    let parsedArgs: Record<string, unknown>;
    try {
      parsedArgs = JSON.parse(fc.arguments || '{}') as Record<string, unknown>;
    } catch {
      parsedArgs = {};
    }
    // Surface the invocation into the transcript before execution so it
    // appears in the dashboard timeline at the right point even if the
    // handler throws or hangs.
    await this.emitToolEvent(fc.name, parsedArgs, null);

    // Schedule a "reassurance" filler if this tool has one configured —
    // bridges the silence when a slow tool call would otherwise leave
    // the caller hanging. Cleared on tool completion below. Currently
    // Realtime-only (sendText path); pipeline mode silently skips.
    const reassurance = (toolDef as { reassurance?: string | { message: string; afterMs?: number } })
      .reassurance;
    let reassuranceTimer: ReturnType<typeof setTimeout> | null = null;
    if (reassurance) {
      const msg = typeof reassurance === 'string' ? reassurance : reassurance.message;
      const afterMs = typeof reassurance === 'string' ? 1500 : (reassurance.afterMs ?? 1500);
      if (msg && this.adapter instanceof OpenAIRealtimeAdapter) {
        const realtimeAdapter = this.adapter;
        reassuranceTimer = setTimeout(() => {
          // Fire-and-forget — caller is a setTimeout, can't await. Errors
          // are non-fatal: a missed reassurance is just a longer silence.
          realtimeAdapter.sendText(msg).catch((e: unknown) => {
            getLogger().warn(`Reassurance message failed for tool '${fc.name}': ${String(e)}`);
          });
        }, afterMs);
      }
    }

    // Progress sink: when the handler is an async generator that yields
    // ``{ progress: "..." }``, forward each progress message to the
    // OpenAI Realtime adapter so the agent speaks the update inline.
    // Pipeline mode and non-Realtime adapters silently drop progress
    // (no clean injection point yet — follow-up).
    const onProgress = this.adapter instanceof OpenAIRealtimeAdapter
      ? async (text: string): Promise<void> => {
          try {
            await (this.adapter as OpenAIRealtimeAdapter).sendText(text);
          } catch (e) {
            getLogger().warn(`Tool progress message failed for '${fc.name}': ${String(e)}`);
          }
        }
      : undefined;

    let result: string;
    try {
      result = await this.toolExecutor.execute(
        toolDef as ToolDefinition,
        parsedArgs,
        {
          call_id: this.callId,
          caller: this.caller,
        },
        onProgress,
      );
    } finally {
      if (reassuranceTimer) clearTimeout(reassuranceTimer);
    }
    await adapter.sendFunctionResult(fc.call_id, result);
    // Emit a follow-up event with the result so the dashboard timeline
    // shows both invocation and outcome.
    await this.emitToolEvent(fc.name, parsedArgs, result);
  }

  // ---------------------------------------------------------------------------
  // Private: call end / metrics finalization
  // ---------------------------------------------------------------------------

  private async fireCallEnd(): Promise<void> {
    if (this.callEndFired) return;
    this.callEndFired = true;
    if (this.maxDurationTimer) { clearTimeout(this.maxDurationTimer); this.maxDurationTimer = null; }
    // Flush any buffered assistant turn whose user transcript never
    // arrived — better to surface it (out of strict order) than lose it.
    if (this.pendingAssistantTimer) {
      clearTimeout(this.pendingAssistantTimer);
      this.pendingAssistantTimer = null;
    }
    if (this.pendingAssistantTurn !== null) {
      const buffered = this.pendingAssistantTurn;
      this.pendingAssistantTurn = null;
      try { await this.flushAssistantTurn(buffered); } catch { /* best effort */ }
    }
    // Close MCP connections — best effort, swallow errors so a flaky
    // MCP server can't derail call-end teardown.
    if (this.mcpManager) {
      try { await this.mcpManager.close(); } catch { /* ignore */ }
      this.mcpManager = null;
    }

    await this.deps.bridge.queryTelephonyCost(this.metricsAcc, this.callId);

    // Deepgram cost query — pull the key off the adapter when STT is a
    // DeepgramSTT instance.
    if (this.stt instanceof DeepgramSTT && this.stt.requestId) {
      const dgKey = (this.stt as unknown as { apiKey?: string }).apiKey;
      if (dgKey) {
        await queryDeepgramCost(this.metricsAcc, dgKey, this.stt.requestId);
      }
    }

    const finalMetrics = this.metricsAcc.endCall();
    const callEndData = {
      call_id: this.callId,
      caller: this.caller,
      callee: this.callee,
      ended_at: Date.now() / 1000,
      transcript: [...this.history.entries],
      metrics: finalMetrics as unknown as Record<string, unknown>,
    };

    // Single INFO line per call-end — duration, turns, cost, latency.
    // "p95 wait" = agent_response_ms (user-perceived wait after they stop
    // speaking). Matches the dashboard "p95 wait" tile. Fallback to total_ms
    // for legacy/short calls where agent_response_ms is undefined.
    const cost = (finalMetrics.cost as { total?: number } | undefined)?.total ?? 0;
    const p95Obj = finalMetrics.latency_p95 as
      | { agent_response_ms?: number; total_ms?: number }
      | undefined;
    const latencyP95 = p95Obj?.agent_response_ms ?? p95Obj?.total_ms ?? 0;
    getLogger().info(
      `Call ended: ${this.callId} (${finalMetrics.duration_seconds.toFixed(1)}s, ` +
        `${finalMetrics.turns.length} turns, cost=$${cost.toFixed(4)}, p95 wait=${Math.round(latencyP95)}ms)`,
    );
    this.deps.metricsStore.recordCallEnd(
      callEndData,
      finalMetrics as unknown as Record<string, unknown>,
    );
    // Notify standalone dashboard (if running)
    try {
      const { notifyDashboard } = await import('./dashboard/persistence');
      notifyDashboard(callEndData);
    } catch { /* ignore */ }
    if (this.deps.onCallEnd) {
      await this.deps.onCallEnd(callEndData);
    }
  }
}

// ---------------------------------------------------------------------------
// Shared cost query helper
// ---------------------------------------------------------------------------

async function queryDeepgramCost(
  metricsAcc: CallMetricsAccumulator,
  deepgramKey: string,
  deepgramRequestId: string,
): Promise<void> {
  try {
    const projResp = await fetch('https://api.deepgram.com/v1/projects', {
      headers: { 'Authorization': `Token ${deepgramKey}` },
      signal: AbortSignal.timeout(5000),
    });
    if (projResp.ok) {
      const projData = await projResp.json() as { projects?: Array<{ project_id?: string }> };
      const projectId = projData.projects?.[0]?.project_id;
      if (projectId) {
        const reqResp = await fetch(
          `https://api.deepgram.com/v1/projects/${projectId}/requests/${deepgramRequestId}`,
          {
            headers: { 'Authorization': `Token ${deepgramKey}` },
            signal: AbortSignal.timeout(5000),
          },
        );
        if (reqResp.ok) {
          const reqData = await reqResp.json() as { response?: { details?: { usd?: number } } };
          const usd = reqData.response?.details?.usd;
          if (usd != null) {
            metricsAcc.setActualSttCost(usd);
            getLogger().debug(`Deepgram actual cost: $${usd}`);
          }
        }
      }
    }
  } catch {
    // Fallback to estimated cost
  }
}
