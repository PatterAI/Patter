/**
 * xAI Speech-to-Text adapter for Patter (TypeScript).
 *
 * **Beta: validated against the xAI STT API spec; not yet exercised against a
 * live phone call.**
 *
 * Pure WebSocket client for xAI's real-time STT endpoint
 * (`wss://api.x.ai/v1/stt`). Config is carried entirely in the URL query string
 * (there is NO setup message); audio is sent as raw binary frames. The adapter
 * WAITS for the server's `transcript.created` event before declaring the
 * connection ready, then maps `transcript.partial` (`is_final` / `speech_final`)
 * onto Patter's interim / chunk-final / utterance-final transcript semantics —
 * the same two-flag scheme Deepgram uses.
 *
 * Also exports {@link xaiTranscribe}, a one-shot REST helper for the batch
 * transcription endpoint, plus the {@link XaiTranscription} / {@link XaiWord}
 * result shapes.
 *
 * Credential: `XAI_API_KEY`. Auth is an `Authorization: Bearer <key>` header
 * on both the streaming and batch endpoints.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

/** xAI real-time STT WebSocket endpoint. */
export const XAI_STT_WS_URL = 'wss://api.x.ai/v1/stt';
/** xAI batch (one-shot) STT REST endpoint. */
export const XAI_STT_REST_URL = 'https://api.x.ai/v1/stt';

/** Raw audio encodings accepted by xAI's STT endpoints. */
export const XaiSTTEncoding = {
  PCM: 'pcm',
  MULAW: 'mulaw',
  ALAW: 'alaw',
} as const;
export type XaiSTTEncoding = (typeof XaiSTTEncoding)[keyof typeof XaiSTTEncoding];

/** Per-word timing / metadata for a Patter transcript. */
export interface XaiSTTWord {
  readonly word?: string;
  readonly start?: number;
  readonly end?: number;
  readonly speaker?: number;
}

/** Patter-normalised transcript event emitted by {@link XaiSTT}. */
export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  /** Overall / end-of-turn confidence in [0, 1]. */
  readonly confidence?: number;
  /** xAI utterance-end hint (`speech_final`) — faster than `isFinal`. */
  readonly speechFinal?: boolean;
  /** True when this final was produced in response to a `finalize` command. */
  readonly fromFinalize?: boolean;
  /** Per-word timings/metadata when xAI emits them. */
  readonly words?: ReadonlyArray<XaiSTTWord>;
  /** Which provider event this transcript represents (always `Results`). */
  readonly eventType?: string;
}

type TranscriptCallback = (transcript: Transcript) => void | Promise<void>;

/** Raw `transcript.partial` / `transcript.done` word entry from xAI. */
interface XaiWireWord {
  text?: string;
  start?: number;
  end?: number;
  speaker?: number;
}

/** Raw xAI streaming server event (JSON text frame). */
interface XaiSTTMessage {
  type?: string;
  text?: string;
  words?: XaiWireWord[];
  is_final?: boolean;
  speech_final?: boolean;
  start?: number;
  duration?: number;
  end_of_turn_confidence?: number;
  message?: string;
}

function mapWords(words: XaiWireWord[] | undefined): ReadonlyArray<XaiSTTWord> | undefined {
  if (!words || words.length === 0) return undefined;
  return words.map((w) => ({
    word: w.text,
    start: w.start,
    end: w.end,
    speaker: w.speaker,
  }));
}

/** Constructor options for {@link XaiSTT}. */
export interface XaiSTTOptions {
  /**
   * Audio encoding of the frames fed to {@link XaiSTT.sendAudio}. Default
   * `pcm` — the Patter pipeline decodes carrier μ-law to linear PCM16 @ 16 kHz
   * before the STT stage, so `pcm` @ 16000 is the correct wire match. Set
   * `mulaw` / `alaw` @ 8000 only if you feed raw carrier audio directly.
   */
  readonly encoding?: XaiSTTEncoding | string;
  /** Input sample rate (Hz). Default `16000` (matches the pipeline's PCM16 feed). */
  readonly sampleRate?: number;
  /**
   * Emit interim (`is_final=false`) partials every ~500 ms. Default `true`
   * (mirrors Deepgram/Soniox — the pipeline consumes partials for barge-in).
   */
  readonly interimResults?: boolean;
  /**
   * BCP-47 language code enabling server-side text formatting (ITN). Omitted
   * when unset (auto).
   */
  readonly language?: string;
  /**
   * Silence (ms) before an utterance is marked final, `[0, 5000]` (xAI default
   * `10`). Omitted when unset.
   */
  readonly endpointingMs?: number;
  /**
   * Smart Turn end-of-turn confidence threshold in `[0.0, 1.0]`. Enables xAI's
   * ML end-of-turn model. Omitted when unset.
   */
  readonly smartTurn?: number;
  /**
   * Max silence (ms) before forcing `speech_final` when Smart Turn is on,
   * `[1, 5000]`. Omitted when unset.
   */
  readonly smartTurnTimeoutMs?: number;
  /** Up to 100 bias terms (≤50 chars each), sent as repeated `keyterm=` params. */
  readonly keyterms?: readonly string[];
  /** Speaker diarization — words gain a `speaker` index. Default `false`. */
  readonly diarize?: boolean;
  /** Include filler words ("uh"/"um") in the transcript. Default `false`. */
  readonly fillerWords?: boolean;
  /** Override the WebSocket base URL (e.g. for tests). */
  readonly baseUrl?: string;
}

/** Streaming STT adapter for xAI's real-time WebSocket API. */
export class XaiSTT {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'xai';

  private ws: WebSocket | null = null;
  private readonly callbacks = new Set<TranscriptCallback>();
  private finalizePending = false;

  private readonly apiKey: string;
  private readonly encoding: string;
  private readonly sampleRate: number;
  private readonly interimResults: boolean;
  private readonly language?: string;
  private readonly endpointingMs?: number;
  private readonly smartTurn?: number;
  private readonly smartTurnTimeoutMs?: number;
  private readonly keyterms: readonly string[];
  private readonly diarize: boolean;
  private readonly fillerWords: boolean;
  private readonly baseUrl: string;

  /** Construction args replayed by clone(). */
  private readonly patterCtorArgs: unknown[];

  constructor(apiKey: string, options: XaiSTTOptions = {}) {
    this.patterCtorArgs = [apiKey, options];
    if (!apiKey) {
      throw new Error('xAI STT: apiKey is required');
    }
    this.apiKey = apiKey;
    this.encoding = options.encoding ?? XaiSTTEncoding.PCM;
    this.sampleRate = options.sampleRate ?? 16000;
    this.interimResults = options.interimResults ?? true;
    this.language = options.language;
    this.endpointingMs = options.endpointingMs;
    this.smartTurn = options.smartTurn;
    this.smartTurnTimeoutMs = options.smartTurnTimeoutMs;
    this.keyterms = options.keyterms ?? [];
    this.diarize = options.diarize ?? false;
    this.fillerWords = options.fillerWords ?? false;
    this.baseUrl = options.baseUrl ?? XAI_STT_WS_URL;
  }

  /**
   * Fresh adapter built with this instance's construction arguments — called
   * per call by the stream handler so concurrent calls never share connection
   * state (sockets/queues; cross-call transcript bleed).
   */
  clone(): this {
    const ctor = this.constructor as new (...args: unknown[]) => this;
    return new ctor(...this.patterCtorArgs);
  }

  /** Build the query-parameterised WebSocket URL (config is URL-only). */
  private buildUrl(): string {
    const params = new URLSearchParams({
      sample_rate: String(this.sampleRate),
      encoding: this.encoding,
      interim_results: this.interimResults ? 'true' : 'false',
    });
    if (this.language !== undefined) params.set('language', this.language);
    if (this.endpointingMs !== undefined) params.set('endpointing', String(this.endpointingMs));
    if (this.smartTurn !== undefined) params.set('smart_turn', String(this.smartTurn));
    if (this.smartTurnTimeoutMs !== undefined) {
      params.set('smart_turn_timeout', String(this.smartTurnTimeoutMs));
    }
    if (this.diarize) params.set('diarize', 'true');
    if (this.fillerWords) params.set('filler_words', 'true');
    // `keyterm` is REPEATED (one param per term), not comma-joined.
    for (const term of this.keyterms) params.append('keyterm', term);
    return `${this.baseUrl}?${params.toString()}`;
  }

  /**
   * Open the streaming WebSocket and wait for the server's `transcript.created`
   * ready signal before resolving. Audio sent before `transcript.created` can
   * be dropped, so callers must await this promise.
   */
  async connect(): Promise<void> {
    this.finalizePending = false;
    this.ws = new WebSocket(this.buildUrl(), {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });

    await new Promise<void>((resolve, reject) => {
      let settled = false;
      const ws = this.ws!;

      const onSetupMessage = (raw: Buffer | string): void => {
        if (settled) return;
        let msg: XaiSTTMessage;
        try {
          msg = JSON.parse(raw.toString()) as XaiSTTMessage;
        } catch (e) {
          getLogger().warn(`xAI STT: failed to parse setup message: ${String(e)}`);
          return;
        }
        if (msg.type === 'transcript.created') {
          cleanup();
          resolve();
        } else if (msg.type === 'error') {
          cleanup();
          try { ws.close(); } catch { /* ignore */ }
          reject(new Error(`xAI STT setup error: ${msg.message ?? JSON.stringify(msg)}`));
        }
      };

      const onSetupError = (err: Error): void => {
        cleanup();
        try { ws.close(); } catch { /* ignore */ }
        reject(err);
      };

      const cleanup = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ws.off('message', onSetupMessage);
        ws.off('error', onSetupError);
      };

      const timer = setTimeout(() => {
        cleanup();
        try { ws.close(); } catch { /* ignore */ }
        reject(new Error('xAI STT connect timeout (no transcript.created within 10s)'));
      }, 10000);

      ws.on('message', onSetupMessage);
      ws.on('error', onSetupError);
    });

    // Persistent listeners for the live stream.
    this.ws.on('message', (raw) => this.handleMessage(raw.toString()));
    this.ws.on('error', (err) => {
      getLogger().error(`XaiSTT WebSocket error: ${String(err)}`);
    });
  }

  private handleMessage(raw: string): void {
    let msg: XaiSTTMessage;
    try {
      msg = JSON.parse(raw) as XaiSTTMessage;
    } catch {
      return;
    }

    switch (msg.type) {
      case 'transcript.partial': {
        const text = (msg.text ?? '').trim();
        if (!text) return;
        const speechFinal = Boolean(msg.speech_final);
        // Mirror Deepgram: is_final OR speech_final marks a stable utterance;
        // speech_final is the faster utterance-end hint.
        const isFinal = Boolean(msg.is_final) || speechFinal;
        const fromFinalize = isFinal && this.finalizePending;
        if (fromFinalize) this.finalizePending = false;
        this.emit({
          text,
          isFinal,
          confidence: msg.end_of_turn_confidence ?? 1,
          speechFinal,
          fromFinalize,
          words: mapWords(msg.words),
          eventType: 'Results',
        });
        break;
      }
      case 'transcript.done': {
        // Final flush after audio.done — surface any trailing text as a final.
        const text = (msg.text ?? '').trim();
        if (text) {
          this.emit({
            text,
            isFinal: true,
            confidence: msg.end_of_turn_confidence ?? 1,
            speechFinal: true,
            fromFinalize: this.finalizePending,
            words: mapWords(msg.words),
            eventType: 'Results',
          });
        }
        this.finalizePending = false;
        break;
      }
      case 'error':
        // xAI keeps the connection open on error frames — log, do not close.
        getLogger().error(`XaiSTT error: ${msg.message ?? JSON.stringify(msg)}`);
        break;
      default:
        // transcript.created (post-setup) and unknown events — ignore.
        break;
    }
  }

  private emit(transcript: Transcript): void {
    for (const cb of this.callbacks) {
      // The registered callback is async (stream-handler's handler) — contain
      // both sync throws and async rejections so an unhandled rejection can't
      // kill the process.
      try {
        Promise.resolve(cb(transcript)).catch((err) =>
          getLogger().error(`STT transcript callback failed: ${String(err)}`),
        );
      } catch (err) {
        getLogger().error(`STT transcript callback threw: ${String(err)}`);
      }
    }
  }

  /** Send a raw binary audio chunk (PCM16 / μ-law / A-law per `encoding`). */
  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (audio.length === 0) return;
    this.ws.send(audio);
  }

  /**
   * Force the current utterance to `speech_final` immediately (push-to-talk /
   * VAD speech-end fast-path). The pipeline duck-types `stt.finalize` — without
   * it every xAI turn waits out the full endpointing delay.
   */
  finalize(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.finalizePending = true;
    try {
      this.ws.send(JSON.stringify({ type: 'finalize' }));
    } catch (err) {
      getLogger().debug(`xAI STT finalize failed: ${String(err)}`);
    }
  }

  /** Register a transcript listener. */
  onTranscript(callback: TranscriptCallback): void {
    this.callbacks.add(callback);
  }

  /** Unregister a previously registered transcript listener. */
  offTranscript(callback: TranscriptCallback): void {
    this.callbacks.delete(callback);
  }

  /** Signal end-of-audio (`audio.done`) best-effort, then close the socket. */
  close(): void {
    if (this.ws) {
      try {
        if (this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'audio.done' }));
        }
      } catch {
        // ignore
      }
      try {
        this.ws.close();
      } catch {
        // ignore
      }
      this.ws = null;
    }
  }
}

/** A single word in a batch {@link XaiTranscription}. */
export interface XaiWord {
  readonly text: string;
  readonly start: number;
  readonly end: number;
  /** Speaker index — present only when `diarize` was requested. */
  readonly speaker?: number;
}

/** Parsed result of a batch {@link xaiTranscribe} call. */
export interface XaiTranscription {
  readonly text: string;
  readonly language: string;
  readonly duration: number;
  readonly words: ReadonlyArray<XaiWord>;
}

/** Options for the one-shot {@link xaiTranscribe} batch REST helper. */
export interface XaiTranscribeOptions {
  /** Raw audio bytes to transcribe. Provide this OR {@link url}. */
  readonly audio?: Buffer | Uint8Array;
  /** URL of an audio file for xAI to download and transcribe server-side. */
  readonly url?: string;
  /** API key. Falls back to `XAI_API_KEY` when omitted. */
  readonly apiKey?: string;
  /** BCP-47 language code (used with `format` for text formatting). */
  readonly language?: string;
  /** Inverse Text Normalization ("one hundred dollars" → "$100"). Requires `language`. */
  readonly format?: boolean;
  /** Speaker diarization — each word gains a `speaker` index. */
  readonly diarize?: boolean;
  /** Up to 100 bias terms (≤50 chars each), sent as repeated `keyterm` fields. */
  readonly keyterms?: readonly string[];
  /** Include filler words ("uh"/"um"). */
  readonly fillerWords?: boolean;
  /** Raw-audio encoding (`pcm` / `mulaw` / `alaw`). Only for headerless audio. */
  readonly audioFormat?: XaiSTTEncoding | string;
  /** Sample rate (Hz) — required for raw audio. */
  readonly sampleRate?: number;
  /** Filename for the multipart `file` part. Defaults to `audio.wav`. */
  readonly filename?: string;
  /** Override the REST endpoint (e.g. for tests). */
  readonly baseUrl?: string;
}

interface XaiTranscriptionResponse {
  text?: string;
  language?: string;
  duration?: number;
  words?: Array<{ text?: string; start?: number; end?: number; speaker?: number }>;
}

/**
 * One-shot batch transcription via xAI's REST endpoint (`POST /v1/stt`).
 *
 * **Beta.** Multipart order matters: all scalar fields are appended FIRST and
 * the `file` part LAST (xAI requires the audio to be the final form field). Pass
 * either `audio` bytes or a `url`.
 */
export async function xaiTranscribe(options: XaiTranscribeOptions): Promise<XaiTranscription> {
  const apiKey = options.apiKey ?? process.env.XAI_API_KEY;
  if (!apiKey) {
    throw new Error(
      "xAI transcribe requires an apiKey. Pass { apiKey: '...' } or set XAI_API_KEY.",
    );
  }
  if (!options.audio && !options.url) {
    throw new Error('xAI transcribe requires either `audio` bytes or a `url`.');
  }

  const form = new FormData();
  // Scalar fields FIRST.
  if (options.url !== undefined) form.append('url', options.url);
  if (options.language !== undefined) form.append('language', options.language);
  if (options.format !== undefined) form.append('format', String(options.format));
  if (options.diarize !== undefined) form.append('diarize', String(options.diarize));
  if (options.fillerWords !== undefined) form.append('filler_words', String(options.fillerWords));
  if (options.audioFormat !== undefined) form.append('audio_format', options.audioFormat);
  if (options.sampleRate !== undefined) form.append('sample_rate', String(options.sampleRate));
  for (const term of options.keyterms ?? []) form.append('keyterm', term);
  // File LAST.
  if (options.audio !== undefined) {
    const bytes = options.audio instanceof Buffer ? options.audio : Buffer.from(options.audio);
    const arrayBuffer = bytes.buffer.slice(
      bytes.byteOffset,
      bytes.byteOffset + bytes.byteLength,
    ) as ArrayBuffer;
    form.append('file', new Blob([arrayBuffer]), options.filename ?? 'audio.wav');
  }

  const response = await fetch(options.baseUrl ?? XAI_STT_REST_URL, {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}` },
    body: form,
    signal: AbortSignal.timeout(120_000),
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`xAI transcribe error ${response.status}: ${body}`);
  }

  const data = (await response.json()) as XaiTranscriptionResponse;
  return {
    text: data.text ?? '',
    language: data.language ?? '',
    duration: data.duration ?? 0,
    words: (data.words ?? []).map((w) => ({
      text: w.text ?? '',
      start: w.start ?? 0,
      end: w.end ?? 0,
      ...(w.speaker !== undefined ? { speaker: w.speaker } : {}),
    })),
  };
}
