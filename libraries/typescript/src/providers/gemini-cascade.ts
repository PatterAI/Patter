/**
 * Gemini Cascade realtime adapter.
 *
 * STT+brain leg: Gemini Live (TEXT modality) — audio in, text tokens out.
 * Voice leg: Gemini TTS (generateContentStream) — text chunks → PCM L16 24kHz.
 * Playback: CascadePlaybackMachine drives one ~20 ms tick loop.
 *
 * The adapter SELF-TRIGGERS the latency-mask layer from the Live message
 * stream (the StreamHandler only knows the generic adapter interface):
 *   - caller turn-end (ACTIVITY_END / inputTranscription.finished) → play a
 *     pre-synth filler clip while Live+TTS compute the real reply;
 *   - caller mid-turn partials → optional short backchannel ("mhm"/"right");
 *   - firstMessage → play the pre-synth intro clip instantly (no Live round-trip).
 *
 * Clips are synthesised ONCE per process+voice into a module-level cache
 * (never per call, never blocking connect) so an always-on line stays instant.
 *
 * CRITICAL: Live is TEXT modality — no speechConfig, no outputAudioTranscription
 * (both broke setupComplete in the spike). setupComplete gates _ready; pre-setup
 * turns are silently dropped (prod incident 2026-06-18).
 *
 * Install peer dep:  npm install @google/genai
 */

import { getLogger } from '../logger';
import { sanitizeGeminiSchema } from './google-llm';
import { mulawToPcm16, pcm16ToMulaw, StatefulResampler } from '../audio/transcoding';
import {
  CascadePlaybackMachine,
  splitTtsChunk,
  selectFiller,
  type FillerKey,
} from './gemini-cascade-playback';

const CARRIER_SR = 8000;
const LIVE_INPUT_SR = 16000;
const TTS_OUTPUT_SR = 24000;
const MULAW_FRAME = 160;
const INBOUND_GAIN = 2;
const CONNECT_TIMEOUT_MS = 8000;
const BACKCHANNEL_COOLDOWN_MS = 4000;

type ClipKey = FillerKey | 'intro' | 'bc_mhm' | 'bc_right' | 'bc_aha';
type BackchannelKey = 'bc_mhm' | 'bc_right' | 'bc_aha';

/** Texts for pre-synthesised clips, keyed by clip name. */
const CLIP_TEXTS: Record<ClipKey, string> = {
  intro: "Hi, this is Ashley over at Patter. Thanks for calling in. So — what are you building these days?",
  mhm: "Mhm.", got_it: "Got it.", sure_one_sec: "Sure, one sec.",
  let_me_check: "Let me check that for you.",
  let_me_think: "Right, let me think about that for a second.",
  okay_moment: "Okay, give me just a moment.",
  bc_mhm: "Mhm.", bc_right: "Right.", bc_aha: "Aha, interesting.",
};

const BACKCHANNEL_PIVOT_RE =
  /\b(actually|so basically|the thing is|what happened|i should mention|to be honest)\b/;

/**
 * Process+voice-scoped clip cache. Clips are identical across calls for a given
 * voice, so synthesise each once and share. Keyed by `${voice}::${text}`.
 */
const clipCache = new Map<string, Buffer[]>();
const clipInFlight = new Map<string, Promise<Buffer[]>>();

/** Callback signature for events emitted by {@link GeminiCascadeAdapter}. */
export type GeminiCascadeEventHandler = (
  type:
    | 'audio' | 'transcript_input' | 'transcript_output'
    | 'function_call' | 'speech_started' | 'response_done' | 'error',
  data: unknown,
) => void | Promise<void>;

/** Constructor options (mirrors GeminiLiveAdapter shape). */
export interface GeminiCascadeAdapterOptions {
  model?: string;
  voice?: string;
  instructions?: string;
  language?: string;
  tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>;
  inputSampleRate?: number;
  temperature?: number;
  ttsModel?: string;
}

/** Realtime adapter for Gemini Cascade (Live STT + Gemini TTS). */
export class GeminiCascadeAdapter {
  private readonly apiKey: string;
  private readonly liveModel: string;
  private readonly ttsModel: string;
  private readonly voice: string;
  private readonly instructions: string;
  private readonly temperature: number;
  private readonly tools?: Array<{ name: string; description: string; parameters: Record<string, unknown> }>;

  private client: unknown = null;
  private liveSession: unknown = null;
  /** Serial chain — Live messages processed in arrival order. */
  private recvChain: Promise<void> = Promise.resolve();
  /** Serial chain — TTS chunks synthesised and enqueued in text order. */
  private ttsChain: Promise<void> = Promise.resolve();
  private pendingToolCalls = new Map<string, string>();
  private handlers: GeminiCascadeEventHandler[] = [];
  private running = false;
  private _readyResolve: (() => void) | null = null;
  private readonly _ready: Promise<void>;

  private inboundResampler: StatefulResampler | null = null;

  private readonly playback: CascadePlaybackMachine;
  private pendingText = '';

  // Caller-turn tracking → drives filler/backchannel timing.
  private callerTurnInput = '';
  private fillerArmed = false;
  private bcCursor = 0;
  private lastBackchannelAt = new Map<string, number>();
  private lastAnyBcAt = 0;

  // Intro clip playback (deferred until the first onEvent handler is attached,
  // so the very first frames are not dropped before the carrier path is wired).
  private pendingIntro = false;
  private introStarted = false;

  constructor(apiKey: string, options: GeminiCascadeAdapterOptions = {}) {
    this.apiKey = apiKey;
    this.liveModel = options.model ?? 'gemini-3.1-flash-live-preview';
    this.ttsModel = options.ttsModel ?? 'gemini-3.1-flash-tts-preview';
    this.voice = options.voice ?? 'Kore';
    this.instructions = options.instructions ?? '';
    this.temperature = options.temperature ?? 0.8;
    this.tools = options.tools;
    this._ready = new Promise<void>((res) => { this._readyResolve = res; });
    this.playback = new CascadePlaybackMachine({
      onFrame: (f) => { void this.emit('audio', f); },
    });
  }

  async connect(): Promise<void> {
    await this.ensureClient();

    const config = this.buildLiveConfig();
    const liveApi = (this.client as { live?: { connect?: (a: unknown) => Promise<unknown> } }).live;
    if (!liveApi?.connect) throw new Error('@google/genai: live.connect unavailable');

    this.liveSession = await liveApi.connect({
      model: this.liveModel,
      config,
      callbacks: {
        onopen: () => getLogger().debug('Gemini Cascade: socket open, awaiting setupComplete'),
        onmessage: (msg: unknown) => {
          this.recvChain = this.recvChain
            .then(() => this.handleLiveMessage(msg))
            .catch((e) => getLogger().error(`Gemini Cascade msg error: ${String(e)}`));
        },
        onerror: (e: unknown) => { void this.emit('error', e); },
        onclose: () => { this.running = false; },
      },
    });
    this.running = true;

    try {
      await Promise.race([
        this._ready,
        new Promise<never>((_, rej) =>
          setTimeout(() => rej(new Error('Gemini Cascade: session ready timeout')), CONNECT_TIMEOUT_MS),
        ),
      ]);
    } catch (err) {
      await this.close();
      throw err;
    }

    // Start the playout loop and warm the clip cache in the BACKGROUND — never
    // block connect() (a caller waiting on clip synthesis = dead air at pickup).
    this.playback.start();
    void this.ensureClips();
  }

  /**
   * Pre-warm the clip cache out of band (e.g. at server boot) so the first
   * caller hears an instant intro/fillers. Safe to call without a Live session.
   */
  async prewarm(): Promise<void> {
    await this.ensureClient();
    await this.ensureClips();
  }

  sendAudio(mulaw8k: Buffer): void {
    if (!this.liveSession || !this.running) return;
    const pcm = this.transcodeInbound(mulaw8k);
    if (pcm.length === 0) return;
    const result = (this.liveSession as { sendRealtimeInput?: (a: unknown) => unknown })
      .sendRealtimeInput?.({
        audio: { data: pcm.toString('base64'), mimeType: `audio/pcm;rate=${LIVE_INPUT_SR}` },
      });
    if (result instanceof Promise) {
      void result.catch((e) => getLogger().warn(`Gemini Cascade sendAudio: ${String(e)}`));
    }
  }

  /**
   * The greeting. Plays the pre-synth intro clip instantly instead of routing
   * the firstMessage through Live→TTS (which would add seconds). The clip is
   * enqueued when the first onEvent handler attaches (see {@link onEvent}) so
   * no frames are emitted before the carrier path is wired.
   */
  async sendFirstMessage(text: string): Promise<void> {
    await this.getClip('intro'); // ensure ready (cache hit after warm)
    this.pendingIntro = true;
    this.maybeStartIntro();
    // Prime Live history with the spoken greeting so the model knows it already
    // opened the call and responds to the caller instead of greeting again.
    if (this.liveSession && text) {
      try {
        await (this.liveSession as { sendClientContent?: (a: unknown) => Promise<void> })
          .sendClientContent?.({ turns: [{ role: 'model', parts: [{ text }] }], turnComplete: true });
      } catch (e) {
        getLogger().warn(`Gemini Cascade prime greeting: ${String(e)}`);
      }
    }
  }

  /** Legacy entry point — also used for greeting on older call paths. */
  async sendText(text: string): Promise<void> {
    if (!this.liveSession) return;
    await (this.liveSession as { sendClientContent?: (a: unknown) => Promise<void> })
      .sendClientContent?.({ turns: { role: 'user', parts: [{ text }] }, turnComplete: true });
  }

  async sendFunctionResult(callId: string, result: string): Promise<void> {
    if (!this.liveSession) return;
    const name = this.pendingToolCalls.get(callId) ?? callId;
    this.pendingToolCalls.delete(callId);
    await (this.liveSession as { sendToolResponse?: (a: unknown) => Promise<void> })
      .sendToolResponse?.({
        functionResponses: [{ id: callId, name, response: { result } }],
      });
  }

  cancelResponse(): void {
    getLogger().debug('Gemini Cascade: cancelResponse is implicit via VAD');
  }

  onEvent(handler: GeminiCascadeEventHandler): void {
    this.handlers.push(handler);
    this.maybeStartIntro(); // first handler attached → safe to play the intro
  }

  async close(): Promise<void> {
    this.running = false;
    this.playback.stop();
    const sess = this.liveSession as { close?: () => Promise<void> | void } | null;
    if (sess) {
      try { await sess.close?.(); } catch { /* ignore */ }
      this.liveSession = null;
    }
    this.client = null;
    await this.recvChain.catch(() => undefined);
    await this.ttsChain.catch(() => undefined);
    this.recvChain = Promise.resolve();
    this.ttsChain = Promise.resolve();
    this.pendingToolCalls.clear();
  }

  // ---------------------------------------------------------------------------
  // Private: Live message handling — self-triggers fillers/backchannels/intro
  // ---------------------------------------------------------------------------

  private async handleLiveMessage(response: unknown): Promise<void> {
    const r = response as {
      setupComplete?: unknown;
      serverContent?: {
        modelTurn?: { parts?: Array<{ text?: string }> };
        inputTranscription?: { text?: string; finished?: boolean };
        turnComplete?: boolean;
        interrupted?: boolean;
      };
      voiceActivity?: { voiceActivityType?: string };
      goAway?: { timeLeft?: string };
      toolCall?: { functionCalls?: Array<{ id?: string; name?: string; args?: unknown }> };
      toolCallCancellation?: { ids?: string[] };
    };

    if (r.setupComplete) { this._readyResolve?.(); this._readyResolve = null; }

    const sc = r.serverContent;
    if (sc) {
      for (const part of sc.modelTurn?.parts ?? []) {
        if (part.text) {
          await this.emit('transcript_output', part.text);
          this.pendingText += part.text;
          this.drainText(false);
        }
      }
      const it = sc.inputTranscription;
      if (it?.text) {
        await this.emit('transcript_input', it.text);
        this.callerTurnInput += it.text;
        this.fillerArmed = true;
        if (!it.finished) this.maybeBackchannel();
      }
      if (it?.finished) this.fireFiller();
      if (sc.turnComplete) { this.drainText(true); await this.emit('response_done', null); }
      if (sc.interrupted) { this.onBargeIn(); }
    }

    const vat = r.voiceActivity?.voiceActivityType;
    if (vat === 'ACTIVITY_START') { this.onBargeIn(); }
    else if (vat === 'ACTIVITY_END') { this.fireFiller(); }

    if (r.goAway) {
      getLogger().warn(`Gemini Cascade goAway — ends in ${r.goAway.timeLeft ?? 'unknown'}`);
      await this.close();
    }

    for (const fn of r.toolCall?.functionCalls ?? []) {
      const callId = fn.id ?? ''; const fnName = fn.name ?? '';
      if (callId && fnName) this.pendingToolCalls.set(callId, fnName);
      const args = fn.args ?? {};
      await this.emit('function_call', {
        call_id: callId, name: fnName,
        arguments: typeof args === 'string' ? args : JSON.stringify(args),
      });
    }

    for (const id of r.toolCallCancellation?.ids ?? []) this.pendingToolCalls.delete(id);
  }

  /** Caller started/resumed speaking → barge-in: stop playout, reset turn. */
  private onBargeIn(): void {
    this.playback.interrupt();
    this.callerTurnInput = '';
    this.fillerArmed = true;
    void this.emit('speech_started', null);
  }

  /** Caller turn ended → play a contextual filler while Live+TTS compute. */
  private fireFiller(): void {
    if (!this.fillerArmed) return;
    this.fillerArmed = false;
    const key = selectFiller(this.callerTurnInput);
    this.callerTurnInput = '';
    this.playback.startFiller(this.cachedFrames(key));
  }

  /** Optional short backchannel while the caller is mid-utterance. */
  private maybeBackchannel(): void {
    if (this.playback.currentState !== 'IDLE') return;
    const key = this.pickBackchannel(this.callerTurnInput);
    const now = Date.now();
    if (now - (this.lastBackchannelAt.get(key) ?? 0) < BACKCHANNEL_COOLDOWN_MS) return;
    if (now - this.lastAnyBcAt < BACKCHANNEL_COOLDOWN_MS) return;
    const frames = this.cachedFrames(key);
    if (frames.length === 0) return;
    this.lastBackchannelAt.set(key, now);
    this.lastAnyBcAt = now;
    for (const f of frames) void this.emit('audio', f);
  }

  private pickBackchannel(text: string): BackchannelKey {
    if (BACKCHANNEL_PIVOT_RE.test(text.toLowerCase())) return 'bc_aha';
    this.bcCursor++;
    return this.bcCursor % 2 === 0 ? 'bc_mhm' : 'bc_right';
  }

  /** Enqueue the pre-synth intro once: pending + a handler attached + ready. */
  private maybeStartIntro(): void {
    if (this.introStarted || !this.pendingIntro || this.handlers.length === 0) return;
    const frames = this.cachedFrames('intro');
    if (frames.length === 0) return; // not synth'd yet; retry on next attach/getClip
    this.introStarted = true;
    this.pendingIntro = false;
    this.playback.enqueueTtsFrames(frames, true);
  }

  // ---------------------------------------------------------------------------
  // Private: TTS pipeline
  // ---------------------------------------------------------------------------

  private drainText(flush: boolean): void {
    let rem = this.pendingText;
    for (;;) {
      const [chunk, rest] = splitTtsChunk(rem, flush);
      if (chunk === null) break;
      const t = chunk.trim();
      if (t.length > 0) {
        this.ttsChain = this.ttsChain.then(() => this.synthAndEnqueue(t));
      }
      if (rest === rem) break; // no progress
      rem = rest;
      if (flush && rest.trim().length === 0) break;
    }
    this.pendingText = rem;
    if (flush) this.ttsChain = this.ttsChain.then(() => { this.playback.markTtsComplete(); });
  }

  private async synthAndEnqueue(text: string): Promise<void> {
    const frames = await this.callTts(text);
    if (frames.length > 0) this.playback.enqueueTtsFrames(frames, false);
  }

  private async callTts(text: string): Promise<Buffer[]> {
    const ai = this.client as {
      models?: { generateContentStream?: (a: unknown) => Promise<AsyncIterable<unknown>> };
    };
    if (!ai?.models?.generateContentStream) return [];
    // Each TTS call needs its own outbound resampler pair: chunks are
    // independent synthesis requests, and sharing phase state across them would
    // corrupt the boundary. Filler/intro clips are pre-synth at 8 kHz already.
    const r24to16 = new StatefulResampler({ srcRate: TTS_OUTPUT_SR, dstRate: 16000 });
    const r16to8 = new StatefulResampler({ srcRate: 16000, dstRate: CARRIER_SR });
    const stream = await ai.models.generateContentStream({
      model: this.ttsModel,
      contents: [{ role: 'user', parts: [{ text }] }],
      config: {
        responseModalities: ['AUDIO'],
        speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: this.voice } } },
      },
    });
    const frames: Buffer[] = [];
    let carry = Buffer.alloc(0);
    for await (const chunk of stream) {
      const c = chunk as { candidates?: Array<{ content?: { parts?: Array<{ inlineData?: { data?: string } }> } }> };
      const data = c.candidates?.[0]?.content?.parts?.[0]?.inlineData?.data;
      if (!data) continue;
      const pcm24 = Buffer.from(data, 'base64');
      if (pcm24.length === 0) continue;
      const pcm8 = r16to8.process(r24to16.process(pcm24));
      if (pcm8.length === 0) continue;
      const mulaw = pcm16ToMulaw(pcm8);
      carry = Buffer.concat([carry, mulaw]);
      let off = 0;
      for (; off + MULAW_FRAME <= carry.length; off += MULAW_FRAME) {
        frames.push(Buffer.from(carry.subarray(off, off + MULAW_FRAME)));
      }
      carry = Buffer.from(carry.subarray(off));
    }
    if (carry.length > 0) {
      const pad = Buffer.alloc(MULAW_FRAME, 0xff);
      carry.copy(pad, 0);
      frames.push(pad);
    }
    return frames;
  }

  // ---------------------------------------------------------------------------
  // Private: clip cache (process+voice scoped)
  // ---------------------------------------------------------------------------

  private clipCacheKey(text: string): string { return `${this.voice}::${text}`; }

  private cachedFrames(key: ClipKey): Buffer[] {
    return clipCache.get(this.clipCacheKey(CLIP_TEXTS[key])) ?? [];
  }

  private async getClip(key: ClipKey): Promise<Buffer[]> {
    const text = CLIP_TEXTS[key];
    const ck = this.clipCacheKey(text);
    const cached = clipCache.get(ck);
    if (cached) return cached;
    let inflight = clipInFlight.get(ck);
    if (!inflight) {
      inflight = this.callTts(text)
        .then((frames) => { clipCache.set(ck, frames); clipInFlight.delete(ck); return frames; })
        .catch((e) => {
          clipInFlight.delete(ck);
          getLogger().warn(`Gemini Cascade: clip "${key}" failed: ${String(e)}`);
          return [];
        });
      clipInFlight.set(ck, inflight);
    }
    return inflight;
  }

  private async ensureClips(): Promise<void> {
    await Promise.all((Object.keys(CLIP_TEXTS) as ClipKey[]).map((k) => this.getClip(k)));
    this.maybeStartIntro(); // intro may have been pending while it synthesised
  }

  private async ensureClient(): Promise<void> {
    if (this.client) return;
    let genai: { GoogleGenAI: new (a: { apiKey: string }) => unknown };
    try {
      genai = (await import('@google/genai')) as typeof genai;
    } catch {
      throw new Error(
        'Gemini Cascade requires "@google/genai". Install: npm install @google/genai',
      );
    }
    this.client = new genai.GoogleGenAI({ apiKey: this.apiKey });
  }

  // ---------------------------------------------------------------------------
  // Private: audio transcoding (inbound caller leg)
  // ---------------------------------------------------------------------------

  private transcodeInbound(mulaw8k: Buffer): Buffer {
    const pcm8 = mulawToPcm16(mulaw8k);
    if (pcm8.length === 0) return Buffer.alloc(0);
    const boosted = Buffer.allocUnsafe(pcm8.length);
    for (let i = 0; i < pcm8.length / 2; i++) {
      const raw = pcm8.readInt16LE(i * 2) * INBOUND_GAIN;
      boosted.writeInt16LE(Math.max(-32768, Math.min(32767, raw)), i * 2);
    }
    if (!this.inboundResampler) {
      this.inboundResampler = new StatefulResampler({ srcRate: CARRIER_SR, dstRate: LIVE_INPUT_SR });
    }
    return this.inboundResampler.process(boosted);
  }

  // ---------------------------------------------------------------------------
  // Private: Live config builder
  // ---------------------------------------------------------------------------

  private buildLiveConfig(): Record<string, unknown> {
    const config: Record<string, unknown> = {
      responseModalities: ['TEXT'],
      inputAudioTranscription: {},
      enableAffectiveDialog: true,
      temperature: this.temperature,
    };
    if (this.instructions) config.systemInstruction = { parts: [{ text: this.instructions }] };
    if (this.tools?.length) {
      config.tools = [{
        functionDeclarations: this.tools.map((t) => ({
          name: t.name, description: t.description,
          parameters: sanitizeGeminiSchema(t.parameters),
        })),
      }];
    }
    return config;
  }

  // ---------------------------------------------------------------------------
  // Private: event emission
  // ---------------------------------------------------------------------------

  private async emit(
    type: 'audio' | 'transcript_input' | 'transcript_output' |
      'function_call' | 'speech_started' | 'response_done' | 'error',
    data: unknown,
  ): Promise<void> {
    for (const h of this.handlers) {
      try { await h(type, data); } catch (e) {
        getLogger().error(`Gemini Cascade handler threw: ${String(e)}`);
      }
    }
  }
}
