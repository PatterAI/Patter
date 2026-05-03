/**
 * AssemblyAI Universal Streaming STT adapter for the Patter SDK pipeline mode.
 *
 * Pure `ws` transport — does NOT depend on the vendor SDK.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

/** Patter-normalised transcript event emitted by {@link AssemblyAISTT}. */
export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
  /** Optional event hint, e.g. `"SpeechStarted"` for barge-in signals. */
  readonly eventType?: string;
}

type TranscriptCallback = (transcript: Transcript) => void;

/** Audio encodings accepted by AssemblyAI's v3 streaming endpoint. */
export const AssemblyAIEncoding = {
  PCM_S16LE: 'pcm_s16le',
  PCM_MULAW: 'pcm_mulaw',
} as const;
export type AssemblyAIEncoding = (typeof AssemblyAIEncoding)[keyof typeof AssemblyAIEncoding];

/** Known AssemblyAI Universal Streaming speech models. */
export const AssemblyAIModel = {
  UNIVERSAL_STREAMING_ENGLISH: 'universal-streaming-english',
  UNIVERSAL_STREAMING_MULTILINGUAL: 'universal-streaming-multilingual',
  U3_RT_PRO: 'u3-rt-pro',
  WHISPER_RT: 'whisper-rt',
} as const;
export type AssemblyAIModel = (typeof AssemblyAIModel)[keyof typeof AssemblyAIModel];

/** Valid `domain` values for AssemblyAI's v3 streaming endpoint. */
export const AssemblyAIDomain = {
  GENERAL: 'general',
  MEDICAL_V1: 'medical-v1',
} as const;
export type AssemblyAIDomain = (typeof AssemblyAIDomain)[keyof typeof AssemblyAIDomain];

/** Common PCM sample rates for AssemblyAI streaming input. */
export const AssemblyAISampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
} as const;
export type AssemblyAISampleRate = (typeof AssemblyAISampleRate)[keyof typeof AssemblyAISampleRate];

/** AssemblyAI v3 streaming server event types. */
export const AssemblyAIEventType = {
  BEGIN: 'Begin',
  TURN: 'Turn',
  SPEECH_STARTED: 'SpeechStarted',
  TERMINATION: 'Termination',
} as const;
export type AssemblyAIEventType = (typeof AssemblyAIEventType)[keyof typeof AssemblyAIEventType];

/** AssemblyAI v3 streaming client-side message types. */
export const AssemblyAIClientFrame = {
  UPDATE_CONFIGURATION: 'UpdateConfiguration',
  FORCE_ENDPOINT: 'ForceEndpoint',
  TERMINATE: 'Terminate',
} as const;
export type AssemblyAIClientFrame = (typeof AssemblyAIClientFrame)[keyof typeof AssemblyAIClientFrame];

/** Constructor options for {@link AssemblyAISTT}. */
export interface AssemblyAISTTOptions {
  /** One of the AssemblyAI speech models. */
  readonly model?: AssemblyAIModel;
  /** PCM encoding: 16-bit little-endian (default) or G.711 mu-law for telephony. */
  readonly encoding?: AssemblyAIEncoding;
  /** Sample rate in Hz — 16000 for wideband audio, 8000 for telephony. */
  readonly sampleRate?: number;
  /** Override the streaming base URL (e.g. EU: `wss://streaming.eu.assemblyai.com`). */
  readonly baseUrl?: string;
  /**
   * Authenticate via `?token=<apiKey>` in the URL instead of the
   * `Authorization` header. Default `false`.
   */
  readonly useQueryToken?: boolean;
  /** Enable automatic language detection (defaults: true for multilingual/u3-rt-pro). */
  readonly languageDetection?: boolean;
  /** 0..1 confidence required before end-of-turn is finalized. */
  readonly endOfTurnConfidenceThreshold?: number;
  /** Minimum ms of silence required before end-of-turn finalizes. */
  readonly minTurnSilence?: number;
  /** Maximum ms of silence before the turn is force-finalized. */
  readonly maxTurnSilence?: number;
  /** When true, wait for the formatted transcript before emitting final. */
  readonly formatTurns?: boolean;
  /** Bias keywords/phrases. */
  readonly keytermsPrompt?: readonly string[];
  /** Text prompt (u3-rt-pro only). */
  readonly prompt?: string;
  /** Accepted for backward compatibility but NOT sent — not a valid v3 param. */
  readonly vadThreshold?: number;
  /** Enable diarization / speaker labels. */
  readonly speakerLabels?: boolean;
  /** Max speakers for diarization. */
  readonly maxSpeakers?: number;
  /** Domain hint — must be `"general"` or `"medical-v1"`. */
  readonly domain?: AssemblyAIDomain;
}

const DEFAULT_BASE_URL = 'wss://streaming.assemblyai.com';
const DEFAULT_MIN_TURN_SILENCE_MS = 400;
const CONNECT_TIMEOUT_MS = 10000;
const TERMINATION_WAIT_TIMEOUT_MS = 500;
const MIN_CHUNK_DURATION_MS = 50;
const MAX_CHUNK_DURATION_MS = 1000;
const RECONNECT_ERROR_CODES: ReadonlySet<number> = new Set([3005, 3008]);
const VALID_DOMAINS: ReadonlySet<string> = new Set([
  AssemblyAIDomain.GENERAL,
  AssemblyAIDomain.MEDICAL_V1,
]);

interface AssemblyAIWord {
  readonly text?: string;
  readonly start?: number;
  readonly end?: number;
  readonly confidence?: number;
}

interface AssemblyAIEvent {
  readonly type?: string;
  readonly id?: string;
  readonly expires_at?: number;
  readonly transcript?: string;
  readonly end_of_turn?: boolean;
  readonly turn_is_formatted?: boolean;
  readonly words?: readonly AssemblyAIWord[];
}

/** Thrown when a method that needs an open WebSocket is called before connect. */
export class AssemblyAISTTNotConnectedError extends Error {
  constructor(message = 'AssemblyAISTT is not connected') {
    super(message);
    this.name = 'AssemblyAISTTNotConnectedError';
  }
}

/** Streaming STT adapter for AssemblyAI's v3 Universal Streaming API. */
export class AssemblyAISTT {
  private ws: WebSocket | null = null;
  private readonly callbacks: Set<TranscriptCallback> = new Set();
  private closing = false;
  private reconnectAttempts = 0;
  private terminationResolve: (() => void) | null = null;
  /**
   * Coalescing buffer for inbound audio frames. AssemblyAI's v3
   * streaming endpoint requires each ws frame to carry 50–1000 ms of
   * audio (server emits error 3007 below 50 ms — observed in the
   * field as a fully-billed call with zero transcripts). Twilio sends
   * 20 ms frames, so the SDK must batch ~3 frames before forwarding.
   *
   * We accumulate raw bytes here until the cumulative duration crosses
   * the configured target (default 60 ms — comfortably above the 50 ms
   * floor with one frame of headroom against jitter), then flush in a
   * single `ws.send()`.
   */
  private chunkBuffer: Buffer[] = [];
  private chunkBufferBytes = 0;
  /** Target send size in bytes — recomputed lazily once encoding/sample-rate is known. */
  private chunkBufferTargetBytes = 0;

  /** AssemblyAI session id — set when the `Begin` message arrives. */
  public sessionId: string | null = null;
  /** Unix timestamp when the AssemblyAI session expires. */
  public expiresAt: number | null = null;

  constructor(
    private readonly apiKey: string,
    private readonly options: AssemblyAISTTOptions = {},
  ) {
    if (!apiKey) {
      throw new Error('AssemblyAISTT requires a non-empty apiKey');
    }
    if (options.domain !== undefined && !VALID_DOMAINS.has(options.domain)) {
      const hint =
        (options.domain as string) === 'medical'
          ? ' — did you mean "medical-v1"?'
          : '';
      throw new Error(
        `AssemblyAISTT: invalid domain "${options.domain}"; expected one of [${Array.from(
          VALID_DOMAINS,
        )
          .map((d) => `"${d}"`)
          .join(', ')}]${hint}`,
      );
    }
  }

  /** Factory for Twilio calls — mulaw 8 kHz. */
  static forTwilio(
    apiKey: string,
    model: AssemblyAIModel = AssemblyAIModel.UNIVERSAL_STREAMING_ENGLISH,
  ): AssemblyAISTT {
    return new AssemblyAISTT(apiKey, {
      model,
      encoding: AssemblyAIEncoding.PCM_MULAW,
      sampleRate: AssemblyAISampleRate.HZ_8000,
    });
  }

  private buildUrl(): string {
    const opts = this.options;
    const model: AssemblyAIModel =
      opts.model ?? AssemblyAIModel.UNIVERSAL_STREAMING_ENGLISH;
    const encoding: AssemblyAIEncoding = opts.encoding ?? AssemblyAIEncoding.PCM_S16LE;
    const sampleRate: number = opts.sampleRate ?? AssemblyAISampleRate.HZ_16000;

    let minSilence: number | undefined;
    let maxSilence: number | undefined;
    if (model === AssemblyAIModel.U3_RT_PRO) {
      minSilence = opts.minTurnSilence ?? 100;
      maxSilence = opts.maxTurnSilence ?? minSilence;
    } else {
      minSilence = opts.minTurnSilence ?? DEFAULT_MIN_TURN_SILENCE_MS;
      maxSilence = opts.maxTurnSilence;
    }

    const languageDetection =
      opts.languageDetection ??
      (model.includes('multilingual') || model === AssemblyAIModel.U3_RT_PRO);

    const raw: Record<string, unknown> = {
      sample_rate: sampleRate,
      encoding,
      speech_model: model,
      format_turns: opts.formatTurns,
      end_of_turn_confidence_threshold: opts.endOfTurnConfidenceThreshold,
      min_turn_silence: minSilence,
      max_turn_silence: maxSilence,
      keyterms_prompt: opts.keytermsPrompt ? JSON.stringify(opts.keytermsPrompt) : undefined,
      language_detection: languageDetection,
      prompt: opts.prompt,
      // vad_threshold intentionally omitted — not a valid v3 parameter.
      speaker_labels: opts.speakerLabels,
      max_speakers: opts.maxSpeakers,
      domain: opts.domain,
    };

    if (opts.useQueryToken) {
      raw.token = this.apiKey;
    }

    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(raw)) {
      if (value === undefined || value === null) continue;
      if (typeof value === 'boolean') {
        params.set(key, value ? 'true' : 'false');
      } else {
        params.set(key, String(value));
      }
    }

    const base = opts.baseUrl ?? DEFAULT_BASE_URL;
    return `${base}/v3/ws?${params.toString()}`;
  }

  private buildHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'User-Agent': 'Patter/1.0',
    };
    if (!this.options.useQueryToken) {
      headers.Authorization = this.apiKey;
    }
    return headers;
  }

  /** Open the streaming WebSocket and arm message handlers. */
  async connect(): Promise<void> {
    this.closing = false;
    const url = this.buildUrl();
    this.ws = new WebSocket(url, { headers: this.buildHeaders() });
    await this.awaitOpen(this.ws);
    this.attachHandlers(this.ws);
  }

  private async awaitOpen(ws: WebSocket): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error('AssemblyAI connect timeout')),
        CONNECT_TIMEOUT_MS,
      );
      ws.once('open', () => {
        clearTimeout(timer);
        resolve();
      });
      ws.once('error', (err: Error) => {
        clearTimeout(timer);
        reject(err);
      });
    });
  }

  private attachHandlers(ws: WebSocket): void {
    ws.on('message', (raw: WebSocket.RawData) => {
      let event: AssemblyAIEvent;
      try {
        event = JSON.parse(raw.toString()) as AssemblyAIEvent;
      } catch {
        return;
      }
      this.handleEvent(event);
    });

    ws.on('close', (code: number) => {
      if (
        !this.closing &&
        RECONNECT_ERROR_CODES.has(code) &&
        this.reconnectAttempts < 1
      ) {
        this.reconnectAttempts += 1;
        getLogger().warn(
          `AssemblyAISTT: close code ${code} — attempting single reconnect.`,
        );
        this.reconnect().catch((err: unknown) => {
          getLogger().error('AssemblyAISTT reconnect failed', err);
        });
      }
    });
  }

  private async reconnect(): Promise<void> {
    const url = this.buildUrl();
    this.ws = new WebSocket(url, { headers: this.buildHeaders() });
    await this.awaitOpen(this.ws);
    this.attachHandlers(this.ws);
  }

  private handleEvent(event: AssemblyAIEvent): void {
    const type = event.type;

    if (type === AssemblyAIEventType.BEGIN) {
      this.sessionId = event.id ?? null;
      this.expiresAt = event.expires_at ?? null;
      return;
    }

    if (type === AssemblyAIEventType.TERMINATION) {
      if (this.terminationResolve) {
        this.terminationResolve();
        this.terminationResolve = null;
      }
      return;
    }

    if (type === AssemblyAIEventType.SPEECH_STARTED) {
      this.emit({
        text: '',
        isFinal: false,
        confidence: 0,
        eventType: AssemblyAIEventType.SPEECH_STARTED,
      });
      return;
    }

    if (type !== AssemblyAIEventType.TURN) {
      return;
    }

    const endOfTurn = Boolean(event.end_of_turn);
    const turnIsFormatted = Boolean(event.turn_is_formatted);
    const words = event.words ?? [];
    const transcriptText = (event.transcript ?? '').trim();

    if (endOfTurn) {
      if (this.options.formatTurns && !turnIsFormatted) return;
      if (!transcriptText) return;
      this.emit({
        text: transcriptText,
        isFinal: true,
        confidence: averageConfidence(words),
      });
      return;
    }

    if (!words.length) return;
    const interim = words
      .map((w) => (w.text ?? '').trim())
      .filter(Boolean)
      .join(' ');
    if (!interim) return;
    this.emit({
      text: interim,
      isFinal: false,
      confidence: averageConfidence(words),
    });
  }

  private emit(transcript: Transcript): void {
    for (const cb of this.callbacks) {
      cb(transcript);
    }
  }

  /** Send a binary PCM/mu-law audio chunk to AssemblyAI for transcription. */
  sendAudio(audio: Buffer): void {
    // Mirror Deepgram / other streaming STTs: silently drop audio while
    // the WebSocket is not yet OPEN. Twilio starts streaming media frames
    // immediately on call connect, but our STT WS handshake takes
    // 200–500 ms to complete — throwing here propagates an unhandled
    // exception out of `handleAudio` and kills the call. Losing the
    // first ~10 frames (~200 ms) is preferable to a hard crash; the
    // connect path already retries on close.
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }

    // Coalesce small frames before forwarding. Twilio's 20 ms frames are
    // below AssemblyAI's 50 ms floor, so without batching the server
    // accepts the WS bytes silently but emits no transcripts and bills
    // the call. See `chunkBufferTargetBytes` doc for the math.
    if (this.chunkBufferTargetBytes === 0) {
      this.chunkBufferTargetBytes = this.computeTargetChunkBytes();
    }
    this.chunkBuffer.push(audio);
    this.chunkBufferBytes += audio.length;
    if (this.chunkBufferBytes < this.chunkBufferTargetBytes) {
      return; // need more frames before we can flush
    }

    const merged = Buffer.concat(this.chunkBuffer, this.chunkBufferBytes);
    this.chunkBuffer = [];
    this.chunkBufferBytes = 0;

    const durationMs = this.estimateChunkDurationMs(merged.length);
    if (
      durationMs !== null &&
      (durationMs < MIN_CHUNK_DURATION_MS || durationMs > MAX_CHUNK_DURATION_MS)
    ) {
      getLogger().warn(
        `AssemblyAISTT: audio chunk duration ${durationMs.toFixed(1)}ms outside 50-1000ms bounds (may trigger error 3007).`,
      );
    }

    this.ws.send(merged);
  }

  /**
   * Compute the byte count corresponding to ~60 ms of audio for the
   * configured encoding / sample rate. Sits one Twilio frame (20 ms)
   * above AssemblyAI's 50 ms floor so jitter never dips below.
   */
  private computeTargetChunkBytes(): number {
    const targetMs = 60;
    const encoding = this.options.encoding ?? AssemblyAIEncoding.PCM_S16LE;
    const sampleRate = this.options.sampleRate ?? AssemblyAISampleRate.HZ_16000;
    if (encoding === AssemblyAIEncoding.PCM_MULAW) {
      // 1 byte per sample.
      return Math.ceil((sampleRate * targetMs) / 1000);
    }
    // PCM_S16LE: 2 bytes per sample.
    return Math.ceil((sampleRate * targetMs) / 1000) * 2;
  }

  private estimateChunkDurationMs(byteLength: number): number | null {
    if (byteLength <= 0) return null;
    const sampleRate = this.options.sampleRate ?? AssemblyAISampleRate.HZ_16000;
    if (sampleRate <= 0) return null;
    const bytesPerSample =
      (this.options.encoding ?? AssemblyAIEncoding.PCM_S16LE) ===
      AssemblyAIEncoding.PCM_S16LE
        ? 2
        : 1;
    const samples = byteLength / bytesPerSample;
    return (samples / sampleRate) * 1000;
  }

  /**
   * Send an `UpdateConfiguration` frame to change settings mid-stream.
   * Only defined fields are included.
   */
  updateConfiguration(params: {
    keytermsPrompt?: readonly string[];
    prompt?: string;
    minTurnSilence?: number;
    maxTurnSilence?: number;
  }): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new AssemblyAISTTNotConnectedError(
        'AssemblyAISTT.updateConfiguration: WebSocket is not open',
      );
    }
    const payload: Record<string, unknown> = {
      type: AssemblyAIClientFrame.UPDATE_CONFIGURATION,
    };
    if (params.keytermsPrompt !== undefined) {
      payload.keyterms_prompt = JSON.stringify(params.keytermsPrompt);
    }
    if (params.prompt !== undefined) {
      payload.prompt = params.prompt;
    }
    if (params.minTurnSilence !== undefined) {
      payload.min_turn_silence = params.minTurnSilence;
    }
    if (params.maxTurnSilence !== undefined) {
      payload.max_turn_silence = params.maxTurnSilence;
    }
    this.ws.send(JSON.stringify(payload));
  }

  /** Force the server to finalize the current turn (for barge-in). */
  forceEndpoint(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw new AssemblyAISTTNotConnectedError(
        'AssemblyAISTT.forceEndpoint: WebSocket is not open',
      );
    }
    this.ws.send(JSON.stringify({ type: AssemblyAIClientFrame.FORCE_ENDPOINT }));
  }

  /** Register a transcript listener. Returns an unsubscribe function. */
  onTranscript(callback: TranscriptCallback): () => void {
    this.callbacks.add(callback);
    return () => {
      this.callbacks.delete(callback);
    };
  }

  /** Send a Terminate frame, wait briefly for ack, and close the socket. */
  async close(): Promise<void> {
    this.closing = true;
    if (!this.ws) return;

    try {
      this.ws.send(JSON.stringify({ type: AssemblyAIClientFrame.TERMINATE }));
    } catch {
      // ignore
    }

    // Wait up to 500ms for Termination event from server.
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        this.terminationResolve = null;
        resolve();
      }, TERMINATION_WAIT_TIMEOUT_MS);
      this.terminationResolve = () => {
        clearTimeout(timer);
        resolve();
      };
    });

    try {
      this.ws.close();
    } catch {
      // ignore
    }
    this.ws = null;
  }
}

function averageConfidence(words: readonly AssemblyAIWord[]): number {
  if (!words.length) return 0;
  let total = 0;
  for (const w of words) {
    total += Number(w.confidence ?? 0);
  }
  return total / words.length;
}
