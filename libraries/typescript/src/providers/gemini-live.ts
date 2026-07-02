/**
 * Gemini Live realtime adapter.
 *
 * Implements Patter's realtime adapter surface — connect / sendAudio /
 * onEvent / close — matching OpenAIRealtimeAdapter.
 *
 * Uses the @google/genai SDK lazily imported at connect() so consumers that do
 * not use Gemini Live do not pay the load cost. Install with:
 *
 *    npm install @google/genai
 *
 * NOTE: Native-audio Gemini Live models are **v1alpha-only**. We pass
 * `httpOptions: { apiVersion: 'v1alpha' }` when constructing the client.
 * When Google promotes native audio to GA, switch to `v1beta` / `v1` and
 * update the default model below.
 * See: https://ai.google.dev/gemini-api/docs/live
 */

import { getLogger } from '../logger';
import { sanitizeGeminiSchema } from './google-llm';
import {
  mulawToPcm16,
  pcm16ToMulaw,
  StatefulResampler,
} from '../audio/transcoding';

export const GEMINI_DEFAULT_INPUT_SR = 16000;
export const GEMINI_DEFAULT_OUTPUT_SR = 24000;

/**
 * The carrier (Twilio/Telnyx media stream) always speaks mulaw 8 kHz in
 * BOTH directions. Gemini Live wants PCM-16-LE at 16 kHz in and emits
 * PCM-16-LE at 24 kHz out, so the adapter transcodes on both legs.
 */
const CARRIER_SAMPLE_RATE = 8000;

/** One mulaw frame Twilio's playout scheduler expects: 20 ms @ 8 kHz = 160 bytes. */
const MULAW_FRAME_BYTES = 160;

/**
 * Inbound gain boost applied to telephony audio before sending to Gemini.
 *
 * Telephony-band audio decoded from mulaw sits around ±8000 amplitude
 * (~-12 dB peak), well below the ±16000-±24000 of the studio audio that
 * server-side VAD is calibrated against — so without a boost inbound
 * utterances can sit under the speech threshold. 2× brings the level into
 * the expected band; samples are clamped to ±32767 to avoid Int16 wrap.
 * Mirrors the OpenAI Realtime 2 adapter. Tunable for the live phone test.
 */
const INBOUND_GAIN = 2;

/** Model ID for Gemini 3.1 Flash Live (audio-in → audio-out, emotion-aware). */
export const GEMINI_LIVE_3_1_FLASH_PREVIEW = 'gemini-3.1-flash-live-preview';

/**
 * Whether a Gemini Live model must be addressed on the `v1alpha` channel.
 *
 * Native-audio OUTPUT models are served only on `v1alpha`. That family is two
 * naming shapes:
 *   - the dated `*-native-audio-*` previews (e.g.
 *     `gemini-2.5-flash-native-audio-preview-09-2025`), and
 *   - the `*-flash-live-preview` line (e.g. `gemini-3.1-flash-live-preview`),
 *     which the Live API capabilities guide also classifies as a native-audio
 *     output model even though the id lacks the literal "native-audio" token.
 *
 * The id-substring heuristic used to miss the second shape, so
 * `gemini-3.1-flash-live-preview` fell through to the SDK default (`v1beta`),
 * where the model is not served — the socket opened but the server never sent
 * `setupComplete`, and `connect()` died with "session ready timeout" (dead
 * air on the call). Matching the live-preview family here selects `v1alpha`
 * so the handshake completes. The retired half-cascade `*-flash-live-NNN`
 * models (v1beta) are intentionally NOT matched.
 *
 * Callers can always override via the explicit `apiVersion` option.
 */
export function geminiRequiresV1Alpha(model: string): boolean {
  return model.includes('native-audio') || /flash-live-preview/.test(model);
}

/** Callback signature for events emitted by {@link GeminiLiveAdapter}. */
export type GeminiLiveEventHandler = (
  type:
    | 'audio'
    | 'transcript_input'
    | 'transcript_output'
    | 'function_call'
    | 'speech_started'
    | 'response_done'
    | 'error',
  data: unknown,
) => void | Promise<void>;

interface GeminiLiveOptions {
  model?: string;
  voice?: string;
  instructions?: string;
  language?: string;
  tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>;
  inputSampleRate?: number;
  outputSampleRate?: number;
  temperature?: number;
  /** Gemini API version. Auto-detected from model name when omitted:
   *  'native-audio' models → 'v1alpha'; all others → SDK default (v1beta). */
  apiVersion?: string;
  /** Native-audio affective dialog: model adapts tone to caller emotion. */
  affectiveDialog?: boolean;
  /** Native-audio proactive audio: model decides when to respond. */
  proactiveAudio?: boolean;
  /**
   * Enable the model's "thinking" (chain-of-thought). Default OFF for voice
   * (`thinkingBudget: 0`) — the reasoning otherwise leaks into the spoken
   * audio and transcript. `true` re-enables dynamic thinking.
   */
  thinking?: boolean;
  /**
   * Explicit thinking budget (tokens). Overrides {@link thinking}. `0` = off,
   * `-1` = dynamic (model decides). Omit to use the thinking-derived default.
   */
  thinkingBudget?: number;
  /** Server-side VAD tuning (noise rejection + reply latency). */
  vad?: {
    startSensitivity?: 'HIGH' | 'LOW';
    endSensitivity?: 'HIGH' | 'LOW';
    silenceDurationMs?: number;
    prefixPaddingMs?: number;
  };
  /**
   * Max time (ms) to wait for `setupComplete` before failing connect with the
   * actionable error. Internal/testability knob — defaults to 8000 (same
   * budget as the OpenAI Realtime parked-connection guard).
   */
  connectTimeoutMs?: number;
}

/** Realtime adapter for Google's Gemini Live native-audio API. */
export class GeminiLiveAdapter {
  private readonly options: GeminiLiveOptions;
  private readonly model: string;
  private readonly voice: string;
  private readonly instructions: string;
  private readonly language: string;
  private readonly tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>;
  private readonly inputSampleRate: number;
  /** Output sample rate — exposed so callers can configure downstream transcoding. */
  readonly outputSampleRate: number;
  private readonly temperature: number;
  private readonly affectiveDialog: boolean;
  private readonly proactiveAudio: boolean;
  private readonly vad: GeminiLiveOptions['vad'];
  private readonly thinking: boolean;
  private readonly thinkingBudget?: number;
  /** API version actually used for the Live session — surfaced in connect errors. */
  private resolvedApiVersion = 'v1beta (SDK default)';

  private client: unknown = null;
  private session: unknown = null;
  /**
   * Serial chain that processes inbound server messages in arrival order.
   * The JS @google/genai SDK delivers each message through the synchronous
   * `callbacks.onmessage` handler; we hand off to the async
   * {@link handleServerMessage} via this chain so audio frames reach the
   * carrier in order (out-of-order emits would scramble playback).
   */
  private recvChain: Promise<void> = Promise.resolve();

  /**
   * Inbound (carrier → Gemini) upsampler: mulaw 8 kHz is decoded to PCM-16
   * @ 8 kHz then resampled up to the model input rate (16 kHz default).
   * Created lazily per session.
   *
   * CRITICAL: this MUST be a separate instance from the outbound chain
   * below. The resamplers carry phase/history state across chunks; sharing
   * one across both directions interleaves unrelated sample streams and
   * corrupts the audio on BOTH legs.
   */
  private inboundResampler: StatefulResampler | null = null;

  /**
   * Outbound (Gemini → carrier) downsampling chain: PCM-16 @ 24 kHz →
   * 16 kHz → 8 kHz. We chain through 16 kHz instead of a direct 24k→8k
   * decimation because the 16k→8k stage applies a 5-tap FIR anti-alias
   * filter; without it, model voice energy above 4 kHz aliases down into
   * the audible band and is heard as raspy/scratchy speech. Created lazily
   * per session, separate from the inbound resampler (see above).
   */
  private outboundResampler24To16: StatefulResampler | null = null;
  private outboundResampler16To8: StatefulResampler | null = null;
  private handlers: GeminiLiveEventHandler[] = [];
  private running = false;
  private _readyResolve: (() => void) | null = null;
  private _readyReject: ((err: Error) => void) | null = null;
  /** Whether the connect-time ready gate has settled (setupComplete OR failed). */
  private setupSettled = false;
  private readonly _ready: Promise<void>;
  /**
   * Tracks call_id -> function name so tool responses can be sent back with
   * the correct `name` field (Gemini expects the original function name,
   * not the call_id).
   */
  private pendingToolCalls: Map<string, string> = new Map();

  constructor(
    private readonly apiKey: string,
    options: GeminiLiveOptions = {},
  ) {
    this.options = options;
    // gemini-2.0-flash-exp was experimental preview retired Dec 2024.
    // gemini-live-2.5-flash-preview was shut down Dec 9, 2025.
    // Current native-audio live model (v1alpha-only) is the dated preview.
    // Callers can override via GeminiLive({ model: ... }).
    // Default model — see https://ai.google.dev/gemini-api/docs/live
    this.model = options.model ?? 'gemini-2.5-flash-native-audio-preview-09-2025';
    this.voice = options.voice ?? 'Puck';
    this.instructions = options.instructions ?? '';
    this.language = options.language ?? 'en-US';
    this.tools = options.tools;
    this.inputSampleRate = options.inputSampleRate ?? GEMINI_DEFAULT_INPUT_SR;
    this.outputSampleRate = options.outputSampleRate ?? GEMINI_DEFAULT_OUTPUT_SR;
    this.temperature = options.temperature ?? 0.8;
    this.affectiveDialog = options.affectiveDialog ?? false;
    this.proactiveAudio = options.proactiveAudio ?? false;
    this.vad = options.vad;
    this.thinking = options.thinking ?? false;
    this.thinkingBudget = options.thinkingBudget;
    this._ready = new Promise<void>((resolve, reject) => {
      this._readyResolve = resolve;
      this._readyReject = reject;
    });
  }

  /**
   * Resolve the Gemini `thinkingConfig`.
   *
   * Voice default is thinking OFF (`{ thinkingBudget: 0 }`): the model's
   * chain-of-thought must never reach the caller's ear or transcript. An
   * explicit `thinkingBudget` wins (0 = off, -1 = dynamic, N = fixed).
   * Otherwise `thinking: true` re-enables dynamic thinking (`-1`), and the
   * default stays at `0`.
   */
  private resolveThinkingConfig(): { thinkingBudget: number } {
    if (this.thinkingBudget !== undefined) {
      return { thinkingBudget: this.thinkingBudget };
    }
    return { thinkingBudget: this.thinking ? -1 : 0 };
  }

  /** Lazily import @google/genai, open a Live session, and start the receive loop. */
  async connect(): Promise<void> {
    let genaiModule: { GoogleGenAI: new (args: { apiKey: string; httpOptions?: Record<string, unknown> }) => unknown };
    try {
      // Lazy dynamic import — keeps @google/genai optional.
      // Variable module name avoids TS2307 when the peer dep is not installed.
      const modName = '@google/genai';
      genaiModule = (await import(modName)) as typeof genaiModule;
    } catch {
      throw new Error(
        '\nGemini Live requires the "@google/genai" package, which is not installed.\n\n' +
          '  Install:  npm install @google/genai\n\n' +
          'This is an optional peer dependency of getpatter — it is only needed when\n' +
          'you use GeminiLive as an agent engine. Other LLM/engine providers do not\n' +
          'require it.\n',
      );
    }

    const { GoogleGenAI } = genaiModule;
    // Auto-detect the Live API version. Native-audio output models — the dated
    // `*-native-audio-*` previews AND the `*-flash-live-preview` family
    // (gemini-3.1-flash-live-preview) — are served only on v1alpha; all others
    // use the SDK default (v1beta). An explicit option always wins. Selecting
    // the wrong channel makes the server never send `setupComplete`, surfacing
    // as the "session ready timeout" dead-air bug (see geminiRequiresV1Alpha).
    const apiVersion =
      this.options?.apiVersion ??
      (geminiRequiresV1Alpha(this.model) ? 'v1alpha' : undefined);
    this.resolvedApiVersion = apiVersion ?? 'v1beta (SDK default)';
    this.client = new GoogleGenAI({
      apiKey: this.apiKey,
      ...(apiVersion ? { httpOptions: { apiVersion } } : {}),
    });

    const config: Record<string, unknown> = {
      responseModalities: ['AUDIO'],
      speechConfig: {
        voiceConfig: { prebuiltVoiceConfig: { voiceName: this.voice } },
        languageCode: this.language,
      },
      temperature: this.temperature,
      // Disable the model's "thinking" by default for voice. Gemini 3.x
      // native-audio models otherwise emit their chain-of-thought before the
      // reply, in BOTH the spoken audio and the transcript (e.g. "**Initiating
      // Call Protocol**…"). `{ thinkingBudget: 0 }` turns it off; the opt-in
      // `thinking` / `thinkingBudget` options re-enable it. This is the
      // proactive half of the fix; handleServerMessage also defensively drops
      // any `thought` parts the model returns regardless.
      thinkingConfig: this.resolveThinkingConfig(),
      // Without these, native-audio sessions produced NO user transcript
      // ever and no assistant transcript in AUDIO modality — logs/history/
      // metrics got nothing for Gemini Live calls. Mirrors Python.
      inputAudioTranscription: {},
      outputAudioTranscription: {},
    };
    // Native-audio humanization knobs (opt-in). enableAffectiveDialog makes the
    // model adapt prosody to the caller's emotion; proactivity lets it choose
    // when to speak. Both are v1alpha native-audio features.
    if (this.affectiveDialog) {
      config.enableAffectiveDialog = true;
    }
    if (this.proactiveAudio) {
      config.proactivity = { proactiveAudio: true };
    }
    // Tune turn-taking: LOW start sensitivity ignores background noise/breathing
    // (a known demo-line issue), HIGH end sensitivity + a shorter silence window
    // cut the wait before the model replies.
    if (this.vad) {
      const ad: Record<string, unknown> = {};
      if (this.vad.startSensitivity) ad.startOfSpeechSensitivity = `START_SENSITIVITY_${this.vad.startSensitivity}`;
      if (this.vad.endSensitivity) ad.endOfSpeechSensitivity = `END_SENSITIVITY_${this.vad.endSensitivity}`;
      if (this.vad.silenceDurationMs !== undefined) ad.silenceDurationMs = this.vad.silenceDurationMs;
      if (this.vad.prefixPaddingMs !== undefined) ad.prefixPaddingMs = this.vad.prefixPaddingMs;
      config.realtimeInputConfig = { automaticActivityDetection: ad };
    }
    if (this.instructions) {
      config.systemInstruction = { parts: [{ text: this.instructions }] };
    }
    if (this.tools?.length) {
      config.tools = [
        {
          functionDeclarations: this.tools.map((t) => ({
            name: t.name,
            description: t.description,
            // Strip JSON-Schema keys the Live API's proto Schema rejects
            // ($schema, additionalProperties — strict-mode and zod-derived
            // MCP tools): one such tool 400'd the whole session.
            parameters: sanitizeGeminiSchema(t.parameters),
          })),
        },
      ];
    }

    // The genai live surface is client.live.connect({model, config, callbacks}).
    // CRITICAL: the JS SDK delivers server messages ONLY through
    // `callbacks.onmessage` — the returned Session has NO async-iterable
    // `receive()` (that is the Python SDK's shape). Registering callbacks
    // here is what makes the agent hear/speak; without them the session
    // connects but no audio ever flows (prod incident 2026-06-18: 20 s of
    // silence). See https://ai.google.dev/gemini-api/docs/live (JS sample).
    const liveApi = (this.client as { live?: { connect?: (args: unknown) => Promise<unknown> } }).live;
    if (!liveApi?.connect) {
      throw new Error('@google/genai: live.connect is not available in this version');
    }
    this.session = await liveApi.connect({
      model: this.model,
      config,
      callbacks: {
        onopen: () => {
          // Socket open — but the session is NOT yet configured. Do NOT
          // unblock connect() here: the caller would send its firstMessage /
          // first audio before the server processes setup, and the server
          // SILENTLY DROPS pre-setup turns. The ready-gate resolves on
          // `setupComplete` instead (handled in handleServerMessage).
          getLogger().debug('Gemini Live: socket open, awaiting setupComplete');
        },
        onmessage: (msg: unknown) => {
          // Process strictly in arrival order (audio frame ordering matters).
          this.recvChain = this.recvChain
            .then(() => this.handleServerMessage(msg))
            .catch((err) =>
              getLogger().error(`Gemini Live message handler error: ${String(err)}`),
            );
        },
        onerror: (e: unknown) => {
          // A transport/server error BEFORE setupComplete is a connect
          // failure — fail the ready gate fast (with the underlying cause)
          // instead of waiting out the timeout. After setup it is a
          // mid-session error and flows to consumers as usual.
          if (this.failReadyGate(e)) return;
          void this.emit('error', e);
        },
        onclose: () => {
          this.running = false;
          // Socket closed before the session was ever configured — surface it
          // as a connect failure rather than a silent dead-air timeout.
          this.failReadyGate(new Error('Gemini Live socket closed before setupComplete'));
        },
      },
    });
    this.running = true;

    // Block until the session is open (onopen) before returning, so the caller
    // does not stream audio into a half-open session.
    // Timeout after 8 s — same budget as the parked-connection guard in
    // OpenAIRealtime2Adapter so callers get consistent failure behaviour.
    //
    // On timeout, tear down the already-opened session BEFORE rejecting —
    // otherwise a slow handshake leaks a live WebSocket for the rest of the
    // process.
    let timer: ReturnType<typeof setTimeout> | undefined;
    const connectTimeoutMs = this.options.connectTimeoutMs ?? 8000;
    try {
      await Promise.race([
        this._ready,
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new Error(`session ready timeout (no setupComplete within ${connectTimeoutMs}ms)`),
              ),
            connectTimeoutMs,
          );
        }),
      ]);
    } catch (err) {
      await this.close();
      // Surface a CLEAR, actionable error instead of a bare timeout: a dropped
      // call with dead air is almost always a wrong model id, the wrong
      // apiVersion for the model, or bad/insufficient credentials — name all
      // three so the operator can act. NEVER include transcript / thought text.
      throw this.buildConnectError(err);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  /**
   * Fail the connect-time ready gate exactly once. Returns `true` when this
   * call settled the gate (i.e. it was still pending), so the caller can stop
   * treating the event as a mid-session error. No-op after `setupComplete`.
   */
  private failReadyGate(cause: unknown): boolean {
    if (this.setupSettled) return false;
    this.setupSettled = true;
    const reject = this._readyReject;
    this._readyResolve = null;
    this._readyReject = null;
    reject?.(cause instanceof Error ? cause : new Error(String(cause)));
    return true;
  }

  /**
   * Wrap a connect failure (timeout or pre-setup error) with actionable
   * guidance. Mentions the model id + resolved apiVersion and the three
   * common root causes. Contains no PII (no transcript / thought text).
   */
  private buildConnectError(err: unknown): Error {
    const base = err instanceof Error ? err.message : String(err);
    return new Error(
      `Gemini Live failed to start a session for model "${this.model}" ` +
        `(apiVersion: ${this.resolvedApiVersion}): ${base}. Likely causes: ` +
        `(1) the model id is wrong or not enabled for this API key; ` +
        `(2) the model needs a different apiVersion — native-audio / ` +
        `*-flash-live-preview models are served on v1alpha (pass ` +
        `{ apiVersion: 'v1alpha' } to override); ` +
        `(3) the API key is invalid or lacks Gemini Live access.`,
    );
  }

  /**
   * Send a chunk of carrier audio to Gemini.
   *
   * The carrier always hands us mulaw 8 kHz (Twilio/Telnyx media stream),
   * but Gemini Live only accepts PCM-16-LE at its configured input rate
   * (16 kHz default). We transcode mulaw8 → PCM16 → upsample to
   * `inputSampleRate` and send it as base64 inline data with the matching
   * MIME rate. Without this conversion the model receives mulaw bytes
   * labelled as 16 kHz PCM and the call is pure static.
   */
  sendAudio(mulaw8: Buffer): void {
    if (!this.session || !this.running) return;
    const pcmIn = this.transcodeInboundMulaw8ToPcm(mulaw8);
    if (pcmIn.length === 0) return; // resampler warmup — nothing to send yet
    const mime = `audio/pcm;rate=${this.inputSampleRate}`;
    const sess = this.session as { sendRealtimeInput?: (args: unknown) => unknown };
    const result = sess.sendRealtimeInput?.({
      audio: { data: pcmIn.toString('base64'), mimeType: mime },
    });
    if (result instanceof Promise) {
      void result.catch((err) =>
        getLogger().warn(`Gemini Live sendAudio error: ${String(err)}`),
      );
    }
  }

  /**
   * mulaw 8 kHz Buffer → PCM-16-LE Buffer at `inputSampleRate`.
   *
   * Decodes mulaw to PCM-16 @ 8 kHz, applies the {@link INBOUND_GAIN} boost
   * (clamped to Int16), then upsamples 8 kHz → `inputSampleRate` via the
   * lazily-created inbound {@link StatefulResampler} so chunk boundaries do
   * not click. When `inputSampleRate === 8000` no resampling is needed.
   */
  private transcodeInboundMulaw8ToPcm(mulaw8: Buffer): Buffer {
    const pcm8 = mulawToPcm16(mulaw8);
    const sampleCount = pcm8.length / 2;
    if (sampleCount === 0) return Buffer.alloc(0);

    const boosted = Buffer.allocUnsafe(pcm8.length);
    for (let i = 0; i < sampleCount; i++) {
      const raw = pcm8.readInt16LE(i * 2) * INBOUND_GAIN;
      boosted.writeInt16LE(Math.max(-32768, Math.min(32767, raw)), i * 2);
    }

    if (this.inputSampleRate === CARRIER_SAMPLE_RATE) return boosted;

    if (!this.inboundResampler) {
      this.inboundResampler = new StatefulResampler({
        srcRate: CARRIER_SAMPLE_RATE,
        dstRate: this.inputSampleRate,
      });
    }
    return this.inboundResampler.process(boosted);
  }

  /** Send a text turn to Gemini and mark the turn complete. */
  async sendText(text: string): Promise<void> {
    if (!this.session) return;
    const sess = this.session as { sendClientContent?: (args: unknown) => Promise<void> };
    await sess.sendClientContent?.({
      turns: { role: 'user', parts: [{ text }] },
      turnComplete: true,
    });
  }

  /** Send a tool/function-call result back to Gemini. */
  async sendFunctionResult(callId: string, result: string): Promise<void> {
    if (!this.session) return;
    const sess = this.session as { sendToolResponse?: (args: unknown) => Promise<void> };
    // Gemini requires the original function name in the response, not the
    // call_id. Look it up from the map populated when the tool call was
    // emitted; fall back to callId if we never saw this id (defensive).
    const name = this.pendingToolCalls.get(callId) ?? callId;
    this.pendingToolCalls.delete(callId);
    await sess.sendToolResponse?.({
      functionResponses: [
        { id: callId, name, response: { result } },
      ],
    });
  }

  /** No-op — Gemini Live barge-in is VAD-driven, not client-cancelled. */
  cancelResponse(): void {
    // Gemini Live barge-in is VAD-driven — explicit cancel not in v1alpha wire protocol.
    getLogger().debug('Gemini Live: cancelResponse is implicit via VAD');
  }

  /** Register an event handler that receives every Gemini Live event. */
  onEvent(handler: GeminiLiveEventHandler): void {
    this.handlers.push(handler);
  }

  private async emit(
    type:
      | 'audio'
      | 'transcript_input'
    | 'transcript_output'
      | 'function_call'
      | 'speech_started'
      | 'response_done'
      | 'error',
    data: unknown,
  ): Promise<void> {
    for (const h of this.handlers) {
      try {
        await h(type, data);
      } catch (err) {
        getLogger().error(`Gemini Live handler threw: ${String(err)}`);
      }
    }
  }

  /**
   * Handle one server message delivered via {@link connect}'s
   * `callbacks.onmessage`. Dispatches audio (transcoded PCM →
   * mulaw 8 kHz, split into 20 ms frames), transcripts, turn-complete /
   * interrupted signals, goAway warnings, and tool calls. Invoked in arrival
   * order through {@link recvChain}.
   */
  private async handleServerMessage(response: unknown): Promise<void> {
    const r = response as {
      setupComplete?: unknown;
      serverContent?: {
        modelTurn?: {
          parts?: Array<{
            inlineData?: { data?: string };
            text?: string;
            /** Gemini marks chain-of-thought parts with thought=true. */
            thought?: boolean;
          }>;
        };
        inputTranscription?: { text?: string };
        outputTranscription?: { text?: string };
        turnComplete?: boolean;
        interrupted?: boolean;
      };
      goAway?: { timeLeft?: string };
      toolCall?: {
        functionCalls?: Array<{
          id?: string;
          name?: string;
          args?: Record<string, unknown> | string;
        }>;
      };
    };

    // setupComplete is the definitive "session configured" signal; unblock
    // connect() here too in case the onopen callback was missed.
    if (r.setupComplete) {
      this.setupSettled = true;
      this._readyResolve?.();
      this._readyResolve = null;
      this._readyReject = null;
    }

    const sc = r.serverContent;
    if (sc) {
      for (const part of sc.modelTurn?.parts ?? []) {
        // DEFENSIVE: never surface the model's chain-of-thought to the caller.
        // A voice agent must not speak OR transcribe its private reasoning.
        // Gemini marks thought parts with `thought: true`; drop them from BOTH
        // the audio stream and the text transcript. This backstops the
        // `thinkingBudget: 0` default — even if a thought slips through, it
        // never reaches the carrier. We deliberately do NOT log the thought
        // text (no PII).
        if (part.thought === true) continue;
        if (part.inlineData?.data) {
          // Gemini emits PCM-16-LE at outputSampleRate (24 kHz default);
          // the carrier wants mulaw 8 kHz in 20 ms (160-byte) frames.
          // Transcode + split so Twilio's playout scheduler does not
          // stall on an oversized frame.
          const mulaw = this.transcodeOutboundPcmToMulaw8(part.inlineData.data);
          for (let off = 0; off < mulaw.length; off += MULAW_FRAME_BYTES) {
            const frame = mulaw.subarray(off, Math.min(off + MULAW_FRAME_BYTES, mulaw.length));
            await this.emit('audio', Buffer.from(frame));
          }
        }
        if (part.text) await this.emit('transcript_output', part.text);
      }
      if (sc.inputTranscription?.text) {
        await this.emit('transcript_input', sc.inputTranscription.text);
      }
      if (sc.outputTranscription?.text) {
        await this.emit('transcript_output', sc.outputTranscription.text);
      }
      if (sc.turnComplete) await this.emit('response_done', null);
      if (sc.interrupted) await this.emit('speech_started', null);
    }
    if (r.goAway) {
      // Gemini Live hard-caps session length (~10-15 min without
      // resumption); goAway is the only warning before the server drops
      // the connection. Surface it loudly.
      getLogger().warn(
        `Gemini Live goAway received — session ends in ${r.goAway.timeLeft ?? 'unknown'}`,
      );
    }
    if (r.toolCall) {
      for (const fn of r.toolCall.functionCalls ?? []) {
        const args = fn.args ?? {};
        const callId = fn.id ?? '';
        const fnName = fn.name ?? '';
        if (callId && fnName) {
          this.pendingToolCalls.set(callId, fnName);
        }
        await this.emit('function_call', {
          call_id: callId,
          name: fnName,
          arguments: typeof args === 'string' ? args : JSON.stringify(args),
        });
      }
    }
  }

  /**
   * Base64 PCM-16-LE @ `outputSampleRate` → mulaw 8 kHz Buffer.
   *
   * Resamples `outputSampleRate` → 16 kHz → 8 kHz through the lazily-created
   * outbound resampler pair (anti-aliased 16k→8k stage), then mulaw-encodes.
   * The resamplers are reused across all deltas in the session so phase
   * carries across chunk boundaries and there are no per-frame clicks. When
   * `outputSampleRate === 8000` only the mulaw encode is needed.
   */
  private transcodeOutboundPcmToMulaw8(deltaB64: string): Buffer {
    const pcm = Buffer.from(deltaB64, 'base64');
    if (pcm.length === 0) return Buffer.alloc(0);

    if (this.outputSampleRate === CARRIER_SAMPLE_RATE) {
      return pcm16ToMulaw(pcm);
    }

    if (!this.outboundResampler24To16) {
      this.outboundResampler24To16 = new StatefulResampler({
        srcRate: this.outputSampleRate,
        dstRate: 16000,
      });
      this.outboundResampler16To8 = new StatefulResampler({
        srcRate: 16000,
        dstRate: CARRIER_SAMPLE_RATE,
      });
    }
    const pcm16 = this.outboundResampler24To16.process(pcm);
    const pcm8 = this.outboundResampler16To8!.process(pcm16);
    if (pcm8.length === 0) return Buffer.alloc(0);
    return pcm16ToMulaw(pcm8);
  }

  /** Close the Gemini Live session and stop the receive loop. */
  async close(): Promise<void> {
    this.running = false;
    if (this.session) {
      const sess = this.session as { close?: () => Promise<void> | void };
      try {
        await sess.close?.();
      } catch {
        /* ignore */
      }
      this.session = null;
    }
    this.client = null;
    // Drain any in-flight message handling so a closing session doesn't leave
    // a dangling emit mid-flight.
    await this.recvChain.catch(() => undefined);
    this.recvChain = Promise.resolve();
    this.pendingToolCalls.clear();
  }
}
