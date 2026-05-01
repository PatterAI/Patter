/**
 * Cartesia STT (ink-whisper) adapter for the Patter SDK pipeline mode.
 *
 * Implements a `DeepgramSTT`-shaped provider using Cartesia's streaming
 * WebSocket API. Pure `ws` transport — does NOT depend on the vendor SDK.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

/** Cartesia STT currently only accepts 16-bit PCM little-endian. */
export type CartesiaEncoding = 'pcm_s16le';

export interface CartesiaSTTOptions {
  /** Cartesia STT model. Currently only `"ink-whisper"`. */
  readonly model?: string;
  /** BCP-47 language code. */
  readonly language?: string;
  /** PCM encoding; Cartesia only supports `pcm_s16le`. */
  readonly encoding?: CartesiaEncoding;
  /** Sample rate in Hz. Cartesia accepts 8000, 16000, 24000, 44100, 48000. */
  readonly sampleRate?: number;
  /** Override base URL (HTTP or WS). Defaults to Cartesia prod. */
  readonly baseUrl?: string;
}

const DEFAULT_BASE_URL = 'https://api.cartesia.ai';
const API_VERSION = '2025-04-16';
const USER_AGENT = 'Patter/1.0';
const KEEPALIVE_INTERVAL_MS = 30000;
const CONNECT_TIMEOUT_MS = 10000;

interface CartesiaEvent {
  readonly type?: string;
  readonly text?: string;
  readonly is_final?: boolean;
  readonly probability?: number;
  readonly request_id?: string;
  readonly message?: string;
}

export class CartesiaSTT {
  private ws: WebSocket | null = null;
  private callbacks: Set<TranscriptCallback> = new Set();
  private keepaliveTimer: ReturnType<typeof setInterval> | null = null;
  /**
   * Cartesia request id — set from the server transcript events.
   * `null` until the first transcript event arrives (matches Python's `None`).
   */
  public requestId: string | null = null;

  constructor(
    private readonly apiKey: string,
    private readonly options: CartesiaSTTOptions = {},
  ) {
    if (!apiKey) {
      throw new Error('CartesiaSTT requires a non-empty apiKey');
    }
  }

  private buildWsUrl(): string {
    const opts = this.options;
    const rawBase = opts.baseUrl ?? DEFAULT_BASE_URL;
    let base: string;
    if (rawBase.startsWith('http://')) {
      base = `ws://${rawBase.slice('http://'.length)}`;
    } else if (rawBase.startsWith('https://')) {
      base = `wss://${rawBase.slice('https://'.length)}`;
    } else if (rawBase.startsWith('ws://') || rawBase.startsWith('wss://')) {
      base = rawBase;
    } else {
      base = `wss://${rawBase}`;
    }

    const language = opts.language ?? 'en';
    const params = new URLSearchParams({
      model: opts.model ?? 'ink-whisper',
      sample_rate: String(opts.sampleRate ?? 16000),
      encoding: opts.encoding ?? 'pcm_s16le',
      cartesia_version: API_VERSION,
      api_key: this.apiKey,
      language,
    });
    return `${base}/stt/websocket?${params.toString()}`;
  }

  async connect(): Promise<void> {
    const url = this.buildWsUrl();
    this.ws = new WebSocket(url, {
      headers: { 'User-Agent': USER_AGENT },
    });

    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error('Cartesia STT connect timeout')),
        CONNECT_TIMEOUT_MS,
      );
      this.ws!.once('open', () => {
        clearTimeout(timer);
        resolve();
      });
      this.ws!.once('error', (err: Error) => {
        clearTimeout(timer);
        reject(err);
      });
    });

    this.ws.on('message', (raw: WebSocket.RawData) => {
      let event: CartesiaEvent;
      try {
        event = JSON.parse(raw.toString()) as CartesiaEvent;
      } catch {
        return;
      }
      this.handleEvent(event);
    });

    this.keepaliveTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.ping();
        } catch {
          // ignore transient ping errors
        }
      }
    }, KEEPALIVE_INTERVAL_MS);
  }

  private handleEvent(event: CartesiaEvent): void {
    const type = event.type;
    if (type === 'transcript') {
      const text = (event.text ?? '').trim();
      const isFinal = Boolean(event.is_final);
      if (!text && !isFinal) return;
      if (event.request_id) {
        this.requestId = event.request_id;
      }
      if (!text) return;
      const confidence = Number(event.probability ?? 1);
      this.emit({ text, isFinal, confidence });
      return;
    }

    if (type === 'error') {
      getLogger().error(`Cartesia STT error: ${event.message ?? 'unknown'}`);
      return;
    }
    // `flush_done` and `done` are informational; no transcript to surface.
  }

  private emit(transcript: Transcript): void {
    for (const cb of this.callbacks) {
      cb(transcript);
    }
  }

  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(audio);
  }

  onTranscript(callback: TranscriptCallback): void {
    this.callbacks.add(callback);
  }

  /** Remove a previously registered transcript callback. */
  offTranscript(callback: TranscriptCallback): void {
    this.callbacks.delete(callback);
  }

  /**
   * Synchronous best-effort close. Sends `finalize` and closes the socket
   * without waiting for the server to flush any remaining transcripts.
   *
   * Limitation: any transcript events produced between the `finalize` send
   * and the socket close may be dropped. Callers that need to guarantee all
   * transcripts are delivered should await :meth:`closeAsync` instead.
   */
  close(): void {
    if (this.keepaliveTimer) {
      clearInterval(this.keepaliveTimer);
      this.keepaliveTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.send('finalize');
      } catch {
        // ignore
      }
      this.ws.close();
      this.ws = null;
    }
  }

  /**
   * Graceful close that awaits the `finalize` send and the socket closing
   * handshake, matching the Python adapter's behavior. Use this when you
   * need any in-flight transcripts to be flushed before teardown.
   */
  async closeAsync(): Promise<void> {
    if (this.keepaliveTimer) {
      clearInterval(this.keepaliveTimer);
      this.keepaliveTimer = null;
    }
    const ws = this.ws;
    this.ws = null;
    if (!ws) return;

    // Best-effort: send `finalize` so the server flushes any trailing transcripts.
    if (ws.readyState === WebSocket.OPEN) {
      try {
        await new Promise<void>((resolve) => {
          ws.send('finalize', (err) => {
            if (err) getLogger().warn(`CartesiaSTT finalize send failed: ${String(err)}`);
            resolve();
          });
        });
      } catch (err) {
        getLogger().warn(`CartesiaSTT finalize error: ${String(err)}`);
      }
    }

    // Wait for the socket to fully close so any final transcript events are
    // delivered through the existing message handler before we return.
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
          // ignore
          resolve();
        }
      });
    }
  }
}
