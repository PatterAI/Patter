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
import type { STTAdapter, TTSAdapter } from './provider-factory';
import { CallMetricsAccumulator } from './metrics';
import { mulawToPcm16, pcm16ToMulaw, resample8kTo16k, resample16kTo8k } from './transcoding';
import { LLMLoop } from './llm-loop';
import { RemoteMessageHandler, isRemoteUrl, isWebSocketUrl } from './remote-message';
import { createHistoryManager, executeToolWebhook } from './handler-utils';
import type { AgentOptions, Guardrail, HookContext, PipelineMessageHandler, ToolDefinition } from './types';
import type { MetricsStore } from './dashboard/store';
import { getLogger } from './logger';
import { validateTwilioSid } from './server';
import type { ProviderPricing } from './pricing';
import { SentenceChunker } from './sentence-chunker';
import { PipelineHookExecutor } from './pipeline-hooks';

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

export function sanitizeLogValue(v: string, maxLen = 200): string {
  // eslint-disable-next-line no-control-regex
  const cleaned = v.replace(/[\x00-\x1f\x7f]/g, '');
  return cleaned.length > maxLen ? cleaned.slice(0, maxLen) + '...' : cleaned;
}

function isValidE164(number: string): boolean {
  return /^\+[1-9]\d{6,14}$/.test(number);
}

// ---------------------------------------------------------------------------
// StreamHandler context (immutable per-call configuration)
// ---------------------------------------------------------------------------

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
  /** Build an AI adapter (OpenAI Realtime or ElevenLabs ConvAI). Injected to avoid circular imports. */
  readonly buildAIAdapter: (resolvedPrompt: string) => AIAdapter;
  /** Sanitize untrusted key-value variables map. */
  readonly sanitizeVariables: (raw: Record<string, unknown>) => Record<string, string>;
  /** Replace {key} placeholders in a template string. */
  readonly resolveVariables: (template: string, variables: Record<string, string>) => string;
}

// ---------------------------------------------------------------------------
// StreamHandler — manages a single call session
// ---------------------------------------------------------------------------

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
  private llmLoop: LLMLoop | null = null;
  private chunkCount = 0;
  private callEndFired = false;
  private sttClosed = false;
  private currentAgentText = '';
  private responseAudioStarted = false;
  private maxDurationTimer: ReturnType<typeof setTimeout> | null = null;
  private transcriptProcessing = false;
  private transcriptQueue: Array<{ isFinal?: boolean; text?: string }> = [];
  // BUG #22 throttle state — mirror Python impl.
  private lastCommitText = '';
  private lastCommitAt = 0;
  // PCM16 byte-alignment carry for TTS streaming (pipeline mode).
  // HTTP streams from ElevenLabs / OpenAI / Cartesia can yield chunks of any
  // size, including odd byte counts. Silently dropping the trailing odd byte
  // misaligns every subsequent int16 sample in the stream (hi/lo bytes get
  // swapped), producing a voice drowned in loud hiss. We buffer the odd byte
  // across chunks so resample/mulaw encoding always sees aligned int16 frames.
  private ttsByteCarry: Buffer | null = null;

  private readonly history: ReturnType<typeof createHistoryManager>;
  private readonly metricsAcc: CallMetricsAccumulator;

  constructor(deps: StreamHandlerDeps, ws: WSWebSocket, caller: string, callee: string) {
    this.deps = deps;
    this.ws = ws;
    this.caller = caller;
    this.callee = callee;

    this.history = createHistoryManager(200);

    // v0.5.0+: ``agent.stt`` / ``agent.tts`` are always STTAdapter / TTSAdapter
    // instances (or undefined). Derive a provider name from the class for
    // metrics; callers can identify providers by constructor name.
    const sttProviderName = deps.agent.stt
      ? (deps.agent.stt.constructor?.name ?? 'custom')
      : undefined;
    const ttsProviderName = deps.agent.tts
      ? (deps.agent.tts.constructor?.name ?? 'custom')
      : undefined;
    const providerMode = deps.agent.provider ?? 'openai_realtime';

    this.metricsAcc = new CallMetricsAccumulator({
      callId: '',
      providerMode,
      telephonyProvider: deps.bridge.telephonyProvider,
      sttProvider: sttProviderName,
      ttsProvider: ttsProviderName,
      pricing: deps.pricing,
    });

    getLogger().debug(`WebSocket connection opened (${deps.bridge.label})`);
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

    this.deps.metricsStore.recordCallStart({
      call_id: callId,
      caller: this.caller,
      callee: this.callee,
      direction: 'inbound',
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
        direction: 'inbound',
      });
    } catch { /* ignore */ }

    if (this.deps.onCallStart) {
      await this.deps.onCallStart({
        call_id: callId,
        caller: this.caller,
        callee: this.callee,
        direction: 'inbound',
        ...(Object.keys(customParams).length > 0 ? { custom_params: customParams } : {}),
      });
    }

    // Start recording (Twilio only)
    if (this.deps.recording && this.deps.config.twilioSid && this.deps.config.twilioToken && callId) {
      if (!validateTwilioSid(callId)) {
        getLogger().warn(`Recording skipped: invalid Twilio CallSid format ${JSON.stringify(callId)}`);
      } else {
        try {
          const recUrl = `https://api.twilio.com/2010-04-01/Accounts/${this.deps.config.twilioSid}/Calls/${callId}/Recordings.json`;
          const recResp = await fetch(recUrl, {
            method: 'POST',
            headers: {
              'Authorization': `Basic ${Buffer.from(`${this.deps.config.twilioSid}:${this.deps.config.twilioToken}`).toString('base64')}`,
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
    }

    // Resolve dynamic variables in system prompt
    const agentVars = this.deps.sanitizeVariables(this.deps.agent.variables ?? {});
    const safeCustomParams = this.deps.sanitizeVariables(customParams);
    const allVars = { ...agentVars, ...safeCustomParams };
    const resolvedPrompt = Object.keys(allVars).length > 0
      ? this.deps.resolveVariables(this.deps.agent.systemPrompt, allVars)
      : this.deps.agent.systemPrompt;

    const provider = this.deps.agent.provider ?? 'openai_realtime';

    if (provider === 'pipeline') {
      await this.initPipeline(resolvedPrompt);
    } else {
      await this.initRealtimeAdapter(resolvedPrompt);
    }
  }

  /** Set the stream SID (Twilio only, called after parsing 'start' event). */
  setStreamSid(sid: string): void {
    this.streamSid = sid;
  }

  /** Handle an incoming audio chunk (already decoded from base64). */
  async handleAudio(audioBuffer: Buffer): Promise<void> {
    const provider = this.deps.agent.provider ?? 'openai_realtime';
    if (provider === 'pipeline' && this.stt) {
      // BUG #20: keep forwarding caller audio to STT during TTS so barge-in
      // detection can trigger. Caller sets ``agent.bargeInThresholdMs=0`` to
      // disable barge-in on noisy links.
      if (this.isSpeaking && (this.deps.agent.bargeInThresholdMs ?? 300) === 0) {
        return;
      }
      // BUG #12 / #19 audio path: both Twilio and Telnyx with the default
      // streaming_start (PCMU bidirectional) deliver mulaw 8 kHz inbound
      // — we always transcode to PCM16 16 kHz before STT.
      const pcm8k = mulawToPcm16(audioBuffer);
      const pcm16k = resample8kTo16k(pcm8k);

      // BUG #15: run the before_send_to_stt hook before forwarding.
      const hooks = this.deps.agent.hooks;
      if (hooks) {
        const hookExecutor = new PipelineHookExecutor(hooks);
        const hookCtx = this.buildHookContext();
        const processed = await hookExecutor.runBeforeSendToStt(pcm16k, hookCtx);
        if (processed === null) return;
        this.stt.sendAudio(processed);
      } else {
        this.stt.sendAudio(pcm16k);
      }
    } else if (this.adapter) {
      // OpenAI Realtime is configured for g711_ulaw so Twilio mulaw is fine.
      // ElevenLabs ConvAI expects PCM 16kHz — transcode Twilio mulaw first.
      if (this.adapter instanceof ElevenLabsConvAIAdapter && this.deps.bridge.telephonyProvider === 'twilio') {
        const pcm8k = mulawToPcm16(audioBuffer);
        const pcm16k = resample8kTo16k(pcm8k);
        this.adapter.sendAudio(pcm16k);
      } else {
        this.adapter.sendAudio(audioBuffer);
      }
    }
  }

  /** Handle a DTMF keypress event (Twilio only). */
  async handleDtmf(digit: string): Promise<void> {
    getLogger().debug(`DTMF: ${digit}`);
    if (this.adapter instanceof OpenAIRealtimeAdapter) {
      await this.adapter.sendText(`The user pressed key ${digit} on their phone keypad.`);
    }
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({ role: 'user', text: `[DTMF: ${digit}]`, call_id: this.callId });
    }
  }

  /** Handle call stop / stream end. */
  async handleStop(): Promise<void> {
    await this.closeSttOnce();
    try { this.adapter?.close(); } catch { /* ignore */ }
    await this.fireCallEnd();
  }

  /** Handle WebSocket close event. */
  async handleWsClose(): Promise<void> {
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
   * Twilio requires mulaw 8kHz; Telnyx accepts PCM 16kHz natively.
   *
   * Maintains a 1-byte carry across calls so unaligned HTTP chunks from
   * streaming TTS providers never byte-swap the PCM16 samples downstream.
   */
  private encodePipelineAudio(pcm16k: Buffer): string {
    const aligned = this.alignPcm16(pcm16k);
    if (aligned.length === 0) return '';
    if (this.deps.bridge.telephonyProvider === 'twilio') {
      const pcm8k = resample16kTo8k(aligned);
      const mulaw = pcm16ToMulaw(pcm8k);
      return mulaw.toString('base64');
    }
    return aligned.toString('base64');
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

  // ---------------------------------------------------------------------------
  // Private: Pipeline mode
  // ---------------------------------------------------------------------------

  private async initPipeline(resolvedPrompt: string): Promise<void> {
    const label = this.deps.bridge.label;

    this.stt = await this.deps.bridge.createStt(this.deps.agent);

    // v0.5.0+: TTS is a pre-instantiated adapter on ``agent.tts`` or null.
    this.tts = await createTTS(this.deps.agent);

    if (!this.stt) {
      getLogger().debug(`Pipeline mode (${label}): no STT configured`);
    }
    if (!this.tts) {
      getLogger().debug(`Pipeline mode (${label}): no TTS configured`);
    }

    try {
      if (this.stt) await this.stt.connect();
      getLogger().debug(`Pipeline mode (${label}): STT + TTS connected`);
    } catch (e) {
      getLogger().error(`Pipeline connect FAILED (${label}):`, e);
      try { await this.deps.bridge.endCall(this.callId, this.ws); } catch { /* best effort */ }
      return;
    }

    if (this.deps.agent.firstMessage && !this.deps.onMessage && this.tts) {
      this.metricsAcc.startTurn();
      let firstChunkSent = false;
      this.ttsByteCarry = null;
      try {
        for await (const chunk of this.tts.synthesizeStream(this.deps.agent.firstMessage)) {
          if (!firstChunkSent) { firstChunkSent = true; this.metricsAcc.recordTtsFirstByte(); }
          const encoded = this.encodePipelineAudio(chunk);
          this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
        }
      } catch (e) {
        getLogger().error(`First message TTS error (${label}):`, e);
      } finally {
        // Drop any partial int16 byte to prevent cross-turn corruption
        // if the stream threw before a complete sample was delivered.
        this.ttsByteCarry = null;
      }
      if (firstChunkSent) {
        const turn = this.metricsAcc.recordTurnComplete(this.deps.agent.firstMessage);
        if (turn) {
          this.deps.metricsStore.recordTurn({ call_id: this.callId, turn });
          if (this.deps.onMetrics) await this.deps.onMetrics({ call_id: this.callId, turn });
        }
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
      this.llmLoop = new LLMLoop(
        '', // apiKey unused when llmProvider is supplied
        '', // model unused when llmProvider is supplied
        resolvedPrompt,
        this.deps.agent.tools as ToolDefinition[] | undefined,
        this.deps.agent.llm,
      );
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
      );
      getLogger().debug(`Built-in LLM loop active (pipeline, ${label})`);
    }

    if (this.stt) {
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

    this.ttsByteCarry = null;
    try {
      for await (const chunk of this.tts.synthesizeStream(processedText)) {
        if (!this.isSpeaking) break;

        // afterSynthesize hook (per-chunk)
        const processedAudio = await hookExecutor.runAfterSynthesize(chunk, processedText, hookCtx);
        if (processedAudio === null) continue;

        if (!ttsFirstByteSent.value) {
          ttsFirstByteSent.value = true;
          this.metricsAcc.recordTtsFirstByte();
        }
        const encoded = this.encodePipelineAudio(processedAudio);
        this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
      }
    } catch (e) {
      getLogger().error(`TTS streaming error (${this.deps.bridge.label}):`, e);
    } finally {
      this.ttsByteCarry = null;
    }
  }

  /** Handle a final transcript from STT in pipeline mode. */
  private async handleTranscript(transcript: { isFinal?: boolean; text?: string }): Promise<void> {
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

  private async processTranscript(transcript: { isFinal?: boolean; text?: string }): Promise<void> {
    // BUG #20 — barge-in: if TTS is mid-stream and the caller speaks,
    // any transcript with text flips ``isSpeaking`` to false so the TTS
    // sentence loop exits on its next check.
    if (transcript.text && this.isSpeaking) {
      getLogger().debug(
        `Barge-in: caller spoke over agent (${sanitizeLogValue(transcript.text.slice(0, 40))})`,
      );
      this.isSpeaking = false;
      try {
        this.deps.bridge.sendClear(this.ws, this.streamSid);
      } catch (err) {
        getLogger().debug(`sendClear during barge-in failed: ${String(err)}`);
      }
      this.metricsAcc.recordTurnInterrupted();
    }

    if (!transcript.isFinal || !transcript.text) return;

    // BUG #22 — dedup + throttle + hallucination filter, mirror of the
    // Python implementation in ``PipelineStreamHandler._stt_loop``.
    const now = Date.now();
    const normalised = transcript.text.trim().toLowerCase();
    const stripped = normalised.replace(/[.,!?;: ]+$/, '').trim();
    const sinceLastMs = now - this.lastCommitAt;
    const HALLUCINATIONS = new Set([
      'you', 'thank you', 'thanks', 'yeah', 'yes', 'no',
      'okay', 'ok', 'uh', 'um', 'mmm', 'hmm', '.', 'bye',
      'right', 'cool',
    ]);
    if (HALLUCINATIONS.has(stripped) || stripped === '') {
      getLogger().debug(`Dropped likely STT hallucination: ${sanitizeLogValue(normalised.slice(0, 40))}`);
      return;
    }
    if (sinceLastMs < 2000 && normalised === this.lastCommitText) {
      getLogger().debug(
        `Dropped duplicate final transcript (${(sinceLastMs / 1000).toFixed(1)}s since last): ${sanitizeLogValue(normalised.slice(0, 40))}`,
      );
      return;
    }
    if (sinceLastMs < 500) {
      getLogger().debug(
        `Dropped back-to-back final transcript (${(sinceLastMs / 1000).toFixed(2)}s since last): ${sanitizeLogValue(normalised.slice(0, 40))}`,
      );
      return;
    }
    this.lastCommitText = normalised;
    this.lastCommitAt = now;

    const label = this.deps.bridge.label;
    getLogger().debug(`User (${label} pipeline): ${sanitizeLogValue(transcript.text)}`);

    this.metricsAcc.startTurn();
    this.metricsAcc.recordSttComplete(transcript.text);

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
      return;
    }

    // Push filtered text to history (after hook, so LLM sees redacted/modified text)
    this.history.push({ role: 'user', text: filteredTranscript, timestamp: Date.now() });

    let responseText = '';

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
      } else {
        try {
          responseText = await this.deps.remoteHandler.callWebhook(this.deps.onMessage, msgData);
        } catch (e) {
          getLogger().error(`Webhook remote error (${label}):`, e);
          return;
        }
      }
    } else if (this.llmLoop) {
      // --- Streaming LLM with sentence chunking ---
      const callCtx = { call_id: this.callId, caller: this.caller, callee: this.callee };
      const chunker = new SentenceChunker();
      const allParts: string[] = [];
      const ttsFirstByteSent = { value: false };
      this.isSpeaking = true;
      let llmError = false;

      try {
        try {
          for await (const token of this.llmLoop.run(filteredTranscript, this.history.entries, callCtx)) {
            allParts.push(token);

            // Feed token to sentence chunker
            const sentences = chunker.push(token);
            for (const sentence of sentences) {
              if (!this.isSpeaking) break;

              // Guardrails check per-sentence
              const guard = checkGuardrails(sentence, this.deps.agent.guardrails);
              const sentenceText = guard
                ? (guard.replacement ?? "I'm sorry, I can't respond to that.")
                : sentence;

              await this.synthesizeSentence(sentenceText, hookExecutor, hookCtx, ttsFirstByteSent);
            }
            if (!this.isSpeaking) break;
          }
        } catch (e) {
          llmError = true;
          chunker.reset(); // discard partial content on LLM error
          getLogger().error(`LLM loop error (${label}):`, e);
        }

        this.metricsAcc.recordLlmComplete(); // record BEFORE TTS flush, not after

        // Flush remaining text from chunker (skip if LLM errored)
        if (!llmError && this.isSpeaking) {
          for (const sentence of chunker.flush()) {
            if (!this.isSpeaking) break;
            const guard = checkGuardrails(sentence, this.deps.agent.guardrails);
            const sentenceText = guard
              ? (guard.replacement ?? "I'm sorry, I can't respond to that.")
              : sentence;
            await this.synthesizeSentence(sentenceText, hookExecutor, hookCtx, ttsFirstByteSent);
          }
        }
      } finally {
        this.isSpeaking = false; // guaranteed reset
      }
      responseText = allParts.join('');
    } else {
      return;
    }

    if (!responseText) return;

    // For non-streaming paths (onMessage function/webhook): apply guardrails + TTS with chunking
    if (!this.llmLoop) {
      const guard = checkGuardrails(responseText, this.deps.agent.guardrails);
      if (guard) {
        getLogger().debug(`Guardrail '${guard.name}' triggered (pipeline)`);
        responseText = guard.replacement ?? "I'm sorry, I can't respond to that.";
      }

      this.metricsAcc.recordLlmComplete();
      this.history.push({ role: 'assistant', text: responseText, timestamp: Date.now() });

      // Sentence-chunk the complete response for TTS
      const chunker = new SentenceChunker();
      const sentences = [...chunker.push(responseText), ...chunker.flush()];
      const ttsFirstByteSent = { value: false };
      let interrupted = false;
      this.isSpeaking = true;

      try {
        for (const sentence of sentences) {
          if (!this.isSpeaking) { interrupted = true; break; }
          await this.synthesizeSentence(sentence, hookExecutor, hookCtx, ttsFirstByteSent);
        }
      } finally {
        this.isSpeaking = false; // guaranteed reset
      }

      if (!interrupted) {
        this.metricsAcc.recordTtsComplete(responseText);
      }
    } else {
      this.history.push({ role: 'assistant', text: responseText, timestamp: Date.now() });
      this.metricsAcc.recordTtsComplete(responseText);
    }

    const turn = this.metricsAcc.recordTurnComplete(responseText);
    if (turn) {
      this.deps.metricsStore.recordTurn({ call_id: this.callId, turn });
      if (this.deps.onMetrics) await this.deps.onMetrics({ call_id: this.callId, turn });
    }
  }

  /** Handle streaming WebSocket remote response with TTS. */
  private async handleWebSocketResponse(msgData: Record<string, unknown>): Promise<void> {
    const onMessage = this.deps.onMessage as string;
    const parts: string[] = [];
    this.metricsAcc.recordLlmComplete();
    this.isSpeaking = true;
    let wsTtsStarted = false;
    try {
      for await (const chunk of this.deps.remoteHandler.callWebSocket(onMessage, msgData)) {
        parts.push(chunk);
        if (this.tts) {
          this.ttsByteCarry = null;
          for await (const audioChunk of this.tts.synthesizeStream(chunk)) {
            if (!this.isSpeaking) break;
            if (!wsTtsStarted) { wsTtsStarted = true; this.metricsAcc.recordTtsFirstByte(); }
            const encoded = this.encodePipelineAudio(audioChunk);
            this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
          }
        }
      }
    } catch (e) {
      getLogger().error(`WebSocket remote error (${this.deps.bridge.label}):`, e);
    } finally {
      this.isSpeaking = false;
      this.ttsByteCarry = null;
    }
    const responseText = parts.join('');
    this.metricsAcc.recordTtsComplete(responseText);
    const turn = this.metricsAcc.recordTurnComplete(responseText);
    if (turn) {
      this.deps.metricsStore.recordTurn({ call_id: this.callId, turn });
      if (this.deps.onMetrics) await this.deps.onMetrics({ call_id: this.callId, turn });
    }
    if (responseText) this.history.push({ role: 'assistant', text: responseText, timestamp: Date.now() });
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
        await this.adapter.sendText(this.deps.agent.firstMessage);
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
    if (type === 'audio') {
      // Record time-to-first-audio-byte as latency (Realtime mode).
      // If no startTurn() was called yet (e.g. agent responding again without
      // user input), start a new turn now so latency is still measured.
      if (!this.responseAudioStarted) {
        this.responseAudioStarted = true;
        if (this.metricsAcc.turnActive === false) {
          this.metricsAcc.startTurn();
        }
        this.metricsAcc.recordTtsFirstByte();
      }
      let outAudio = eventData as Buffer;
      // OpenAI Realtime outputs g711_ulaw 8kHz. If telephony is Telnyx (PCM 16kHz),
      // transcode before sending. Twilio accepts mulaw natively.
      if (this.deps.bridge.telephonyProvider === 'telnyx') {
        outAudio = resample8kTo16k(mulawToPcm16(outAudio));
      }
      const encoded = outAudio.toString('base64');
      this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
      // Send mark for barge-in accuracy
      this.chunkCount++;
      this.deps.bridge.sendMark(this.ws, `audio_${this.chunkCount}`, this.streamSid);
    } else if (type === 'speech_stopped') {
      // OpenAI's server VAD detected end-of-user-speech. This is the earliest
      // reliable moment to start measuring turn latency in Realtime mode —
      // ``transcript_input`` (transcription.completed) arrives tens of ms to
      // seconds later and understates end-to-end latency dramatically.
      if (!this.metricsAcc.turnActive) {
        this.metricsAcc.startTurn();
      }
      this.currentAgentText = '';
      this.responseAudioStarted = false;
    } else if (type === 'transcript_input') {
      const inputText = eventData as string;
      getLogger().debug(`User (${this.deps.bridge.label}): ${sanitizeLogValue(inputText)}`);
      this.history.push({ role: 'user', text: inputText, timestamp: Date.now() });
      // Fallback: if speech_stopped was missed (server VAD disabled, custom
      // config, ...) still start the turn here so latency is non-zero.
      if (!this.metricsAcc.turnActive) {
        this.metricsAcc.startTurn();
        this.currentAgentText = '';
        this.responseAudioStarted = false;
      }
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
    } else if (type === 'transcript_output') {
      const outputText = eventData as string;
      if (outputText) {
        const triggered = checkGuardrails(outputText, this.deps.agent.guardrails);
        if (triggered) {
          getLogger().debug(`Guardrail '${triggered.name}' triggered`);
          if (this.adapter instanceof OpenAIRealtimeAdapter) {
            this.adapter.cancelResponse();
            await this.adapter.sendText(triggered.replacement ?? "I'm sorry, I can't respond to that.");
          }
        }
        // Accumulate text — a single history entry is pushed on response_done
        this.currentAgentText += outputText;
      }
    } else if (type === 'response_done') {
      // Realtime mode: record usage and complete the turn
      const responseData = eventData as Record<string, unknown> | null;
      if (responseData) {
        const usage = responseData.usage as {
          input_token_details?: { audio_tokens?: number; text_tokens?: number };
          output_token_details?: { audio_tokens?: number; text_tokens?: number };
        } | undefined;
        if (usage) {
          this.metricsAcc.recordRealtimeUsage(usage);
        }
      }
      if (this.currentAgentText) {
        // Push the complete response as a single transcript entry
        this.history.push({ role: 'assistant', text: this.currentAgentText, timestamp: Date.now() });
        const turn = this.metricsAcc.recordTurnComplete(this.currentAgentText);
        this.responseAudioStarted = false;
        if (this.deps.onMetrics) {
          await this.deps.onMetrics({
            call_id: this.callId,
            turn,
          });
        }
        this.deps.metricsStore.recordTurn({ call_id: this.callId, turn });
        this.currentAgentText = '';
      } else {
        // Empty response — discard the orphaned turn so it doesn't leak
        this.metricsAcc.recordTurnInterrupted();
        this.responseAudioStarted = false;
      }
    } else if (type === 'speech_started' || type === 'interruption') {
      this.deps.bridge.sendClear(this.ws, this.streamSid);
      if (this.adapter instanceof OpenAIRealtimeAdapter) {
        this.adapter.cancelResponse();
      }
      this.metricsAcc.recordTurnInterrupted();
      this.currentAgentText = '';
      this.responseAudioStarted = false;
    } else if (type === 'function_call' && this.adapter instanceof OpenAIRealtimeAdapter) {
      await this.handleFunctionCall(eventData as { call_id: string; name: string; arguments: string });
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
        await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ error: 'Invalid phone number format', status: 'rejected' }));
        return;
      }
      getLogger().debug(`Transferring call to ${transferTo}`);
      await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ status: 'transferring', to: transferTo }));
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
      await adapter.sendFunctionResult(fc.call_id, JSON.stringify({ status: 'ending', reason }));
      await this.deps.bridge.endCall(this.callId, this.ws);
      if (this.deps.onTranscript) {
        await this.deps.onTranscript({ role: 'system', text: `Call ended: ${reason}`, call_id: this.callId });
      }
      return;
    }

    // User-defined tool
    const toolDef = this.deps.agent.tools?.find((t) => t.name === fc.name);
    if (toolDef?.webhookUrl) {
      let parsedArgs: unknown;
      try {
        parsedArgs = JSON.parse(fc.arguments || '{}');
      } catch {
        parsedArgs = {};
      }
      const result = await executeToolWebhook(
        toolDef.webhookUrl,
        fc.name,
        parsedArgs,
        { callId: this.callId, caller: this.caller },
        this.deps.bridge.label === 'Twilio' ? '' : this.deps.bridge.label,
      );
      await adapter.sendFunctionResult(fc.call_id, result);
    }
  }

  // ---------------------------------------------------------------------------
  // Private: call end / metrics finalization
  // ---------------------------------------------------------------------------

  private async fireCallEnd(): Promise<void> {
    if (this.callEndFired) return;
    this.callEndFired = true;
    if (this.maxDurationTimer) { clearTimeout(this.maxDurationTimer); this.maxDurationTimer = null; }

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
    const cost = (finalMetrics.cost as { total?: number } | undefined)?.total ?? 0;
    const latencyP95 = (finalMetrics.latency_p95 as { total_ms?: number } | undefined)?.total_ms ?? 0;
    getLogger().info(
      `Call ended: ${this.callId} (${finalMetrics.duration_seconds.toFixed(1)}s, ` +
        `${finalMetrics.turns.length} turns, cost=$${cost.toFixed(4)}, p95=${Math.round(latencyP95)}ms)`,
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
