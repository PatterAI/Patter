/**
 * xAI (Grok) streaming STT adapter for the Patter SDK pipeline mode.
 *
 * Implements a `DeepgramSTT`-shaped provider against xAI's streaming
 * WebSocket endpoint (`wss://api.x.ai/v1/stt`). The client streams raw
 * audio as binary WebSocket frames (no base64) and receives JSON
 * transcript events (`transcript.created` / `transcript.partial` /
 * `transcript.done`). Pure `ws` transport — no vendor SDK.
 *
 * Protocol (https://docs.x.ai/developers/model-capabilities/audio/speech-to-text):
 * configuration is done entirely via URL query parameters; the server
 * sends `transcript.created` once ready (audio sent earlier may be
 * dropped); `{"type": "finalize"}` forces the in-flight utterance to
 * finalize immediately; `{"type": "audio.done"}` flushes the stitched
 * transcript as `transcript.done` and closes the socket.
 *
 * Pricing: $0.20/hr of streamed audio (`xai_stt` in `pricing.ts`; the
 * REST batch endpoint is $0.10/hr).
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

/** Patter-normalised transcript event emitted by {@link XAISTT}. */
export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
  /** xAI utterance-final hint — the speaker stopped (VAD / Smart Turn). */
  readonly speechFinal?: boolean;
  /** Per-word timings (`text`/`start`/`end`, plus `speaker` when diarize=true). */
  readonly words?: ReadonlyArray<Record<string, unknown>>;
  /** Smart Turn end-of-turn confidence (only when `smartTurn` is enabled). */
  readonly endOfTurnConfidence?: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

/** Audio encodings accepted by xAI's streaming STT endpoint. */
export const XAISTTEncoding = {
  PCM: 'pcm',
  MULAW: 'mulaw',
  ALAW: 'alaw',
} as const;
export type XAISTTEncoding = (typeof XAISTTEncoding)[keyof typeof XAISTTEncoding];

/** Sample rates accepted by xAI's streaming STT endpoint. */
export const XAISTTSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_22050: 22050,
  HZ_24000: 24000,
  HZ_44100: 44100,
  HZ_48000: 48000,
} as const;
export type XAISTTSampleRate = (typeof XAISTTSampleRate)[keyof typeof XAISTTSampleRate];

/** xAI STT server event `type` values. */
export const XAISTTServerEvent = {
  CREATED: 'transcript.created',
  PARTIAL: 'transcript.partial',
  DONE: 'transcript.done',
  ERROR: 'error',
} as const;
export type XAISTTServerEvent = (typeof XAISTTServerEvent)[keyof typeof XAISTTServerEvent];

/** xAI STT client-side JSON control frame `type` values. */
export const XAISTTClientFrame = {
  FINALIZE: 'finalize',
  AUDIO_DONE: 'audio.done',
} as const;
export type XAISTTClientFrame = (typeof XAISTTClientFrame)[keyof typeof XAISTTClientFrame];

/** Constructor options for {@link XAISTT}. */
export interface XAISTTOptions {
  /**
   * Language code enabling xAI's Inverse Text Normalization (spoken
   * numbers/currency → written form). The model transcribes any supported
   * language regardless; pass `null` to skip formatting. Default `"en"`.
   */
  readonly language?: string | null;
  /** Audio encoding. Default `pcm` (Linear16 little-endian). */
  readonly encoding?: XAISTTEncoding | string;
  /** Sample rate in Hz. Default 16000 (the model's native rate). */
  readonly sampleRate?: XAISTTSampleRate | number;
  /** Emit partial transcripts (~500 ms cadence). Default `true`. */
  readonly interimResults?: boolean;
  /** Silence (ms) before the utterance-final event fires. Range 0–5000. */
  readonly endpointingMs?: number;
  /** Key terms to bias transcription toward (max 100 × 50 chars). */
  readonly keyterms?: readonly string[];
  /** Speaker diarization — words carry a `speaker` field. Default `false`. */
  readonly diarize?: boolean;
  /** Keep filler words ("uh", "um") in the transcript. Default `false`. */
  readonly fillerWords?: boolean;
  /**
   * Smart Turn end-of-turn confidence threshold (0.0–1.0). When set, an ML
   * model gates `speech_final` on natural turn endings so mid-sentence
   * pauses stop triggering finals.
   */
  readonly smartTurn?: number;
  /** Max silence (ms, 1–5000) before `speech_final` fires anyway. */
  readonly smartTurnTimeoutMs?: number;
  /** Override the WS endpoint (e.g. for tests). */
  readonly baseUrl?: string;
}

const XAI_STT_WS_URL = 'wss://api.x.ai/v1/stt';
const CONNECT_TIMEOUT_MS = 10000;

// xAI keyterm limits (per docs): max 100 terms, each up to 50 characters.
const MAX_KEYTERMS = 100;
const MAX_KEYTERM_LEN = 50;

interface XAISTTEvent {
  readonly type?: string;
  readonly text?: string;
  readonly is_final?: boolean;
  readonly speech_final?: boolean;
  readonly words?: Array<Record<string, unknown>>;
  readonly end_of_turn_confidence?: number;
  readonly message?: string;
}

/** Streaming STT adapter for xAI's `wss://api.x.ai/v1/stt` WebSocket API. */
export class XAISTT {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'xai_stt';
  private ws: WebSocket | null = null;
  private callbacks: Set<TranscriptCallback> = new Set();

  /** Construction args replayed by clone(). */
  private readonly patterCtorArgs: unknown[];

  constructor(
    private readonly apiKey: string,
    private readonly options: XAISTTOptions = {},
  ) {
    this.patterCtorArgs = [apiKey, options];
    if (!apiKey) {
      throw new Error('XAISTT requires a non-empty apiKey');
    }
    const keyterms = options.keyterms ?? [];
    if (keyterms.length > MAX_KEYTERMS) {
      throw new Error(`xAI STT accepts at most ${MAX_KEYTERMS} keyterms, got ${keyterms.length}`);
    }
    for (const term of keyterms) {
      if (term.length > MAX_KEYTERM_LEN) {
        throw new Error(
          `xAI STT keyterms are limited to ${MAX_KEYTERM_LEN} characters, got ${term.length}`,
        );
      }
    }
    if (options.smartTurn !== undefined && (options.smartTurn < 0 || options.smartTurn > 1)) {
      throw new Error(`smartTurn must be in [0.0, 1.0], got ${options.smartTurn}`);
    }
  }

  /**
   * Fresh adapter built with this instance's construction arguments —
   * called per call by the stream handler so concurrent calls never share
   * connection state (sockets/queues; cross-call transcript bleed).
   */
  clone(): this {
    const ctor = this.constructor as new (...args: unknown[]) => this;
    return new ctor(...this.patterCtorArgs);
  }

  private buildWsUrl(): string {
    const opts = this.options;
    const rawBase = opts.baseUrl ?? XAI_STT_WS_URL;
    let base: string;
    if (rawBase.startsWith('http://')) {
      base = `ws://${rawBase.slice('http://'.length)}`;
    } else if (rawBase.startsWith('https://')) {
      base = `wss://${rawBase.slice('https://'.length)}`;
    } else {
      base = rawBase;
    }

    const params = new URLSearchParams({
      sample_rate: String(opts.sampleRate ?? XAISTTSampleRate.HZ_16000),
      encoding: opts.encoding ?? XAISTTEncoding.PCM,
      interim_results: String(opts.interimResults ?? true),
    });
    // `language` defaults to "en" (text formatting on); `null` opts out.
    if (opts.language !== null) {
      params.set('language', opts.language ?? 'en');
    }
    if (opts.endpointingMs !== undefined) params.set('endpointing', String(opts.endpointingMs));
    if (opts.diarize) params.set('diarize', 'true');
    if (opts.fillerWords) params.set('filler_words', 'true');
    if (opts.smartTurn !== undefined) {
      params.set('smart_turn', String(opts.smartTurn));
      if (opts.smartTurnTimeoutMs !== undefined) {
        params.set('smart_turn_timeout', String(opts.smartTurnTimeoutMs));
      }
    }
    // `keyterm` is a repeated parameter — one entry per term.
    for (const term of opts.keyterms ?? []) {
      params.append('keyterm', term);
    }
    return `${base}?${params.toString()}`;
  }

  /**
   * Pre-call WebSocket warmup for the xAI `/v1/stt` endpoint.
   *
   * Opens the WS (DNS + TLS + auth handshake), waits briefly for the
   * `transcript.created` ready signal, then closes. By the time `connect()`
   * is invoked at call-pickup the resolver and TLS session are hot — net
   * wire time saving of 200-500 ms.
   *
   * Billing safety: xAI bills streaming STT per hour of streamed audio
   * (https://docs.x.ai/developers/pricing). Opening + closing the socket
   * without sending audio frames does not consume billable time.
   * Best-effort: failures are logged at debug level.
   */
  async warmup(): Promise<void> {
    let ws: WebSocket | null = null;
    try {
      ws = await new Promise<WebSocket>((resolve, reject) => {
        const sock = new WebSocket(this.buildWsUrl(), {
          headers: { Authorization: `Bearer ${this.apiKey}` },
        });
        const timer = setTimeout(() => {
          try {
            sock.close();
          } catch {
            // ignore
          }
          reject(new Error('xAI STT warmup connect timeout'));
        }, 5000);
        sock.once('open', () => {
          clearTimeout(timer);
          resolve(sock);
        });
        sock.once('error', (err: Error) => {
          clearTimeout(timer);
          reject(err);
        });
      });
      // Wait briefly for the ready signal so the server-side ASR backend
      // is initialised (and stays warm at the edge) before we close.
      await new Promise<void>((r) => setTimeout(r, 250));
    } catch (err) {
      getLogger().debug(`xAI STT warmup failed (best-effort): ${String(err)}`);
    } finally {
      if (ws) {
        try {
          ws.close();
        } catch {
          // ignore
        }
      }
    }
  }

  /**
   * Open the streaming WebSocket and wait for the `transcript.created`
   * ready signal — audio sent before it may be dropped by the server.
   */
  async connect(): Promise<void> {
    this.ws = new WebSocket(this.buildWsUrl(), {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });

    await new Promise<void>((resolve, reject) => {
      let settled = false;
      const ws = this.ws!;
      const cleanup = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ws.off('message', onMessage);
        ws.off('error', onError);
      };
      const onMessage = (raw: WebSocket.RawData): void => {
        let event: XAISTTEvent;
        try {
          event = JSON.parse(raw.toString()) as XAISTTEvent;
        } catch {
          return;
        }
        if (event.type === XAISTTServerEvent.CREATED) {
          cleanup();
          resolve();
        } else if (event.type === XAISTTServerEvent.ERROR) {
          cleanup();
          reject(new Error(`xAI STT setup error: ${event.message ?? 'unknown'}`));
        }
      };
      const onError = (err: Error): void => {
        cleanup();
        reject(err);
      };
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error('xAI STT connect timeout'));
      }, CONNECT_TIMEOUT_MS);
      ws.on('message', onMessage);
      ws.on('error', onError);
    });

    this.armMessageListener();
  }

  private armMessageListener(): void {
    if (!this.ws) return;
    this.ws.on('message', (raw: WebSocket.RawData) => {
      let event: XAISTTEvent;
      try {
        event = JSON.parse(raw.toString()) as XAISTTEvent;
      } catch {
        return;
      }
      this.handleEvent(event);
    });
  }

  private handleEvent(event: XAISTTEvent): void {
    const type = event.type;
    if (type === XAISTTServerEvent.PARTIAL || type === XAISTTServerEvent.DONE) {
      const text = (event.text ?? '').trim();
      if (!text) return;
      const speechFinal =
        type === XAISTTServerEvent.DONE ? true : Boolean(event.speech_final);
      // speech_final implies utterance completion — accept either so the
      // pipeline doesn't wait on chunk-final stitching.
      const isFinal =
        type === XAISTTServerEvent.DONE ? true : Boolean(event.is_final) || speechFinal;
      // xAI does not emit a per-transcript confidence; surface the Smart
      // Turn end-of-turn confidence when available so callers gating on
      // `confidence` still see a meaningful signal, else 1.0.
      const confidence = Number(event.end_of_turn_confidence ?? 1) || 1;
      this.emit({
        text,
        isFinal,
        confidence,
        speechFinal,
        words: event.words ?? [],
        ...(event.end_of_turn_confidence !== undefined
          ? { endOfTurnConfidence: event.end_of_turn_confidence }
          : {}),
      });
      return;
    }

    if (type === XAISTTServerEvent.ERROR) {
      // The connection stays open after an error event — log and skip.
      getLogger().error(`xAI STT error: ${event.message ?? 'unknown'}`);
      return;
    }
    // `transcript.created` is handled during connect(); anything else is
    // informational.
  }

  private emit(transcript: Transcript): void {
    for (const cb of this.callbacks) {
      // Contain both sync throws and async rejections — an unhandled
      // rejection from the (async) stream-handler callback kills the
      // Node process.
      try {
        Promise.resolve(cb(transcript)).catch((err) =>
          getLogger().error(`STT transcript callback failed: ${String(err)}`),
        );
      } catch (err) {
        getLogger().error(`STT transcript callback threw: ${String(err)}`);
      }
    }
  }

  /** Send a raw binary audio chunk (no base64). Empty chunks are dropped. */
  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (audio.length === 0) return;
    this.ws.send(audio);
  }

  /**
   * Force xAI to finalize the in-flight utterance as `speech_final`
   * immediately, rather than waiting for its own endpointing / Smart Turn
   * heuristics. Wired into `StreamHandler` on the VAD `speech_end` event —
   * parity with the Deepgram `Finalize` fast-path and the Python
   * `XAISTT.finalize`. No-op when the WS isn't open.
   */
  async finalize(): Promise<void> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    await new Promise<void>((resolve) => {
      this.ws!.send(JSON.stringify({ type: XAISTTClientFrame.FINALIZE }), (err) => {
        if (err) {
          getLogger().debug(`xAI STT finalize send failed: ${String(err)}`);
        }
        resolve();
      });
    });
  }

  /** Register a transcript listener. */
  onTranscript(callback: TranscriptCallback): void {
    this.callbacks.add(callback);
  }

  /** Remove a previously registered transcript callback. */
  offTranscript(callback: TranscriptCallback): void {
    this.callbacks.delete(callback);
  }

  /**
   * Synchronous best-effort close. Sends `finalize` + `audio.done` and
   * closes the socket without waiting for the server to flush remaining
   * transcripts. Use {@link closeAsync} when trailing transcripts matter.
   */
  close(): void {
    if (this.ws) {
      try {
        this.ws.send(JSON.stringify({ type: XAISTTClientFrame.FINALIZE }));
        this.ws.send(JSON.stringify({ type: XAISTTClientFrame.AUDIO_DONE }));
      } catch {
        // ignore
      }
      this.ws.close();
      this.ws = null;
    }
  }

  /**
   * Graceful close that awaits the `audio.done` send and the socket closing
   * handshake so any trailing `transcript.partial` / `transcript.done`
   * events are delivered through the message handler before teardown.
   */
  async closeAsync(): Promise<void> {
    const ws = this.ws;
    this.ws = null;
    if (!ws) return;

    if (ws.readyState === WebSocket.OPEN) {
      try {
        await new Promise<void>((resolve) => {
          ws.send(JSON.stringify({ type: XAISTTClientFrame.FINALIZE }), () => {
            ws.send(JSON.stringify({ type: XAISTTClientFrame.AUDIO_DONE }), (err) => {
              if (err) getLogger().warn(`XAISTT audio.done send failed: ${String(err)}`);
              resolve();
            });
          });
        });
      } catch (err) {
        getLogger().warn(`XAISTT close error: ${String(err)}`);
      }
    }

    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      await new Promise<void>((resolve) => {
        const done = (): void => {
          ws.off('close', done);
          ws.off('error', done);
          resolve();
        };
        ws.once('close', done);
        ws.once('error', done);
        try {
          ws.close();
        } catch {
          resolve();
        }
      });
    }
  }
}
