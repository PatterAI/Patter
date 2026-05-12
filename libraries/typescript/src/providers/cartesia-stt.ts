/**
 * Cartesia STT (ink-whisper) adapter for the Patter SDK pipeline mode.
 *
 * Implements a `DeepgramSTT`-shaped provider using Cartesia's streaming
 * WebSocket API. Pure `ws` transport — does NOT depend on the vendor SDK.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

/** Patter-normalised transcript event emitted by {@link CartesiaSTT}. */
export interface Transcript {
  readonly text: string;
  readonly isFinal: boolean;
  readonly confidence: number;
}

type TranscriptCallback = (transcript: Transcript) => void;

/** Known Cartesia STT models. */
export const CartesiaSTTModel = {
  INK_WHISPER: 'ink-whisper',
} as const;
export type CartesiaSTTModel = (typeof CartesiaSTTModel)[keyof typeof CartesiaSTTModel];

/** Audio encodings accepted by Cartesia's STT websocket endpoint. */
export const CartesiaSTTEncoding = {
  PCM_S16LE: 'pcm_s16le',
} as const;
export type CartesiaSTTEncoding = (typeof CartesiaSTTEncoding)[keyof typeof CartesiaSTTEncoding];

/** Common PCM sample rates accepted by Cartesia STT. */
export const CartesiaSTTSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_24000: 24000,
  HZ_44100: 44100,
  HZ_48000: 48000,
} as const;
export type CartesiaSTTSampleRate = (typeof CartesiaSTTSampleRate)[keyof typeof CartesiaSTTSampleRate];

/** Cartesia STT server event `type` values. */
export const CartesiaSTTServerEvent = {
  TRANSCRIPT: 'transcript',
  FLUSH_DONE: 'flush_done',
  DONE: 'done',
  ERROR: 'error',
} as const;
export type CartesiaSTTServerEvent = (typeof CartesiaSTTServerEvent)[keyof typeof CartesiaSTTServerEvent];

/** Cartesia STT client-side text frames. */
export const CartesiaSTTClientFrame = {
  FINALIZE: 'finalize',
} as const;
export type CartesiaSTTClientFrame = (typeof CartesiaSTTClientFrame)[keyof typeof CartesiaSTTClientFrame];

/** Cartesia STT currently only accepts 16-bit PCM little-endian. */
/** Legacy encoding alias kept for callers using the bare string form. */
export type CartesiaEncoding = 'pcm_s16le';

/** Constructor options for {@link CartesiaSTT}. */
export interface CartesiaSTTOptions {
  /** Cartesia STT model. Currently only `"ink-whisper"`. */
  readonly model?: CartesiaSTTModel | string;
  /** BCP-47 language code. */
  readonly language?: string;
  /** PCM encoding; Cartesia only supports `pcm_s16le`. */
  readonly encoding?: CartesiaSTTEncoding | CartesiaEncoding;
  /** Sample rate in Hz. Cartesia accepts 8000, 16000, 24000, 44100, 48000. */
  readonly sampleRate?: CartesiaSTTSampleRate | number;
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

/** Streaming STT adapter for Cartesia's ink-whisper WebSocket API. */
export class CartesiaSTT {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'cartesia_stt';
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

  /**
   * Open a fresh WebSocket without arming any message / keepalive handlers
   * and without taking ownership on `this.ws`. Returns the OPEN socket so
   * the caller (the prewarm pipeline) can park it for later adoption via
   * `adoptWebSocket`. Bounded by `CONNECT_TIMEOUT_MS`.
   *
   * Billing safety: opening + parking the WS does not stream audio
   * (Cartesia STT bills on streamed audio seconds), so no charge is
   * incurred. Close the returned WS yourself if it is never adopted.
   */
  async openParkedConnection(): Promise<WebSocket> {
    const url = this.buildWsUrl();
    const ws = new WebSocket(url, {
      headers: { 'User-Agent': USER_AGENT },
    });
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(
        () => reject(new Error('Cartesia STT park connect timeout')),
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
    return ws;
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
      model: opts.model ?? CartesiaSTTModel.INK_WHISPER,
      sample_rate: String(opts.sampleRate ?? CartesiaSTTSampleRate.HZ_16000),
      encoding: opts.encoding ?? CartesiaSTTEncoding.PCM_S16LE,
      cartesia_version: API_VERSION,
      api_key: this.apiKey,
      language,
    });
    return `${base}/stt/websocket?${params.toString()}`;
  }

  /**
   * Pre-call WebSocket warmup for the Cartesia STT `/stt/websocket` endpoint.
   *
   * Opens the WS (DNS + TLS + auth handshake), idles ~250 ms so the
   * Cartesia edge keeps session state warm, then closes. By the time
   * `connect()` is invoked at call-pickup the resolver and TLS session
   * are hot — net wire time saving of 200-500 ms.
   *
   * Billing safety: Cartesia STT bills on streamed audio seconds (per
   * https://docs.cartesia.ai/2025-04-16/api-reference/stt/stt). Opening
   * + closing the WebSocket without forwarding audio does not consume
   * billable seconds. Best-effort: failures logged at debug level.
   */
  async warmup(): Promise<void> {
    const url = this.buildWsUrl();
    let ws: WebSocket | null = null;
    try {
      ws = await new Promise<WebSocket>((resolve, reject) => {
        const sock = new WebSocket(url, {
          headers: { 'User-Agent': USER_AGENT },
        });
        const timer = setTimeout(() => {
          try {
            sock.close();
          } catch {
            // ignore
          }
          reject(new Error('Cartesia STT warmup connect timeout'));
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
      // Idle briefly so the provider edge keeps session state warm.
      await new Promise<void>((r) => setTimeout(r, 250));
    } catch (err) {
      // IMPORTANT: ``String(err)`` for a `ws` handshake failure can
      // include the request URL, which carries the API key as a
      // query-string parameter (Cartesia auth pattern). Log only the
      // HTTP status (when present) or the error class name — never the
      // full URL or message.
      getLogger().debug(
        `Cartesia STT warmup failed (best-effort): ${describeWarmupError(err)}`,
      );
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

  /** Open the streaming WebSocket and arm message + keepalive handlers. */
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

    this.armMessageAndKeepalive();
  }

  /**
   * Adopt a pre-opened, already-OPEN WebSocket produced by the prewarm
   * pipeline (see `Patter.parkProviderConnections`). Skips the fresh
   * `new WebSocket()` + handshake — the WS is already through DNS, TLS
   * and HTTP-101 so audio frames can flow on this turn instead of
   * paying ~150-400 ms of handshake.
   *
   * Caller MUST verify `ws.readyState === OPEN` before calling. If the
   * parked WS died between park and adopt, fall back to `connect()`.
   */
  adoptWebSocket(ws: WebSocket): void {
    this.ws = ws;
    this.armMessageAndKeepalive();
  }

  private armMessageAndKeepalive(): void {
    if (!this.ws) return;
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
    if (type === CartesiaSTTServerEvent.TRANSCRIPT) {
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

    if (type === CartesiaSTTServerEvent.ERROR) {
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

  /** Send a binary PCM16-LE audio chunk to Cartesia for transcription. */
  sendAudio(audio: Buffer): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(audio);
  }

  /**
   * Force Cartesia to finalise the in-flight utterance immediately.
   *
   * Sends a ``finalize`` text frame on the live WebSocket. Cartesia
   * replies with the final transcript followed by ``flush_done``,
   * bypassing its conservative internal silence heuristic (which can
   * wait 2-7 s on PSTN audio before naturally finalising). Wired
   * into ``StreamHandler`` on the VAD ``speech_end`` event so the
   * SDK's authoritative end-of-speech detection forces an immediate
   * STT finalisation — turning Cartesia's natural-pause endpointing
   * into a deterministic VAD-driven one, parity with the Deepgram
   * fast-path. No-op when the WS isn't open. Parity with Python
   * ``CartesiaSTT.finalize``.
   */
  async finalize(): Promise<void> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    await new Promise<void>((resolve) => {
      this.ws!.send(CartesiaSTTClientFrame.FINALIZE, (err) => {
        if (err) {
          getLogger().debug(`Cartesia finalize send failed: ${String(err)}`);
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
        this.ws.send(CartesiaSTTClientFrame.FINALIZE);
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
          ws.send(CartesiaSTTClientFrame.FINALIZE, (err) => {
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

/**
 * Render a warmup error for logging without leaking the request URL.
 *
 * `String(err)` on a `ws` handshake failure can include the upgrade
 * URL, which for Cartesia / AssemblyAI carries the API key as a
 * query-string parameter. This helper extracts only the HTTP status
 * (when present) or the error class name so the API key never lands
 * in logs.
 */
function describeWarmupError(err: unknown): string {
  if (typeof err === 'object' && err !== null) {
    // `ws` handshake failures expose `statusCode` (or `code` on some
    // versions) when the server returned an HTTP error during upgrade.
    const e = err as { statusCode?: number; code?: number; name?: string; constructor?: { name?: string } };
    if (typeof e.statusCode === 'number') return `HTTP ${e.statusCode}`;
    if (typeof e.code === 'number' && e.code >= 100 && e.code < 600) return `HTTP ${e.code}`;
    const ctor = e.constructor?.name;
    if (typeof ctor === 'string' && ctor !== 'Object') return ctor;
    if (typeof e.name === 'string') return e.name;
  }
  // Fallback: log the type, never the full string (which may contain URL).
  return typeof err;
}
