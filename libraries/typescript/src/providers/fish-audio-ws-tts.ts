/**
 * WebSocket-based Fish Audio TTS provider — opt-in low-latency variant.
 *
 * **Beta: validated against the Fish Audio API spec (docs.fish.audio); not yet
 * exercised against a live phone call.**
 *
 * Targets `wss://api.fish.audio/v1/tts/live` instead of the HTTP `/v1/tts`
 * endpoint used by {@link FishAudioTTS}. The WebSocket path skips the
 * per-utterance HTTP request setup (~50 ms) and pairs with `s2-pro`'s ~100 ms
 * time-to-first-audio.
 *
 * Behaviour notes:
 * - **Model support is narrower than HTTP.** Fish restricts the `model:` header
 *   on this endpoint to `s1` and `s2-pro`; `s2.1-pro` is HTTP-only. The
 *   constructor rejects unsupported models with a message pointing at
 *   {@link FishAudioTTS} rather than letting the socket fail at call time.
 * - Frames are **MessagePack**, not JSON — the server returns raw audio bytes
 *   inline, which JSON cannot carry without base64. `@msgpack/msgpack` is
 *   therefore required for this transport only; the HTTP and ASR adapters have
 *   no such dependency.
 * - The socket is opened **per-utterance**, matching the HTTP semantics and the
 *   ElevenLabs WebSocket adapter.
 * - Protocol: `start` (config) → `text` (payload) → `flush` → `stop`, with the
 *   server replying `audio` frames followed by `finish`.
 */

import { createRequire } from 'node:module';
import * as path from 'node:path';
import WebSocket from 'ws';
import { getLogger } from '../logger';
import {
  FISH_AUDIO_DEFAULT_SAMPLE_RATE,
  FishAudioFormat,
  FishAudioLatency,
  FishAudioModel,
  FishAudioTTS,
  type FishAudioTTSOptions,
} from './fish-audio-tts';

/** Fish Audio streaming TTS endpoint. */
export const FISH_AUDIO_WS_URL = 'wss://api.fish.audio/v1/tts/live';

/**
 * Models Fish accepts on the `/v1/tts/live` socket. `s2.1-pro` and
 * `s2.1-pro-free` are HTTP-only.
 */
export const WS_SUPPORTED_MODELS: readonly string[] = [
  FishAudioModel.S1,
  FishAudioModel.S2_PRO,
];

/** Connect timeout — keeps the carrier WebSocket out of dead air on a stuck handshake. */
const DEFAULT_OPEN_TIMEOUT_MS = 5_000;
/** Per-frame receive timeout — a stalled server must not hang until carrier hangup. */
const DEFAULT_FRAME_TIMEOUT_MS = 30_000;
/** Sanity bound on a single audio frame; real frames are far smaller. */
const MAX_AUDIO_FRAME_SIZE = 1024 * 1024;

/** A parked reader waiting for the next inbound frame (or the frame timeout). */
interface FrameWaiter {
  resolve: () => void;
  timer: ReturnType<typeof setTimeout>;
}

/** Minimal `@msgpack/msgpack` surface used by this adapter. */
export interface MsgpackCodec {
  encode(value: unknown): Uint8Array;
  decode(buffer: Uint8Array): unknown;
}

let cachedCodec: MsgpackCodec | null = null;

/** Load `@msgpack/msgpack` lazily, with a remedy-shaped error when it is absent. */
async function loadMsgpack(): Promise<MsgpackCodec> {
  if (cachedCodec) return cachedCodec;
  try {
    cachedCodec = (await import('@msgpack/msgpack')) as unknown as MsgpackCodec;
    return cachedCodec;
  } catch (firstErr) {
    // Fallback: resolve from the user's project root. Catches file:-linked
    // workspaces where the SDK's own node_modules is elsewhere.
    try {
      const req = createRequire(path.join(process.cwd(), 'package.json'));
      cachedCodec = req('@msgpack/msgpack') as MsgpackCodec;
      return cachedCodec;
    } catch {
      throw new Error(
        'FishAudioWebSocketTTS requires the "@msgpack/msgpack" package — the Fish ' +
          'streaming socket frames are MessagePack, not JSON. Install it with ' +
          '`npm install @msgpack/msgpack`, or use the HTTP adapter (FishAudioTTS), ' +
          `which has no such requirement. Original error: ${
            (firstErr as Error)?.message ?? String(firstErr)
          }`,
      );
    }
  }
}

/** Constructor options for {@link FishAudioWebSocketTTS}. */
export interface FishAudioWebSocketTTSOptions extends FishAudioTTSOptions {
  /** Override the WebSocket endpoint (e.g. for tests). */
  wsUrl?: string;
  /** Connect timeout in ms. Defaults to 5000. */
  openTimeoutMs?: number;
  /** Per-frame receive timeout in ms. Defaults to 30000. */
  frameTimeoutMs?: number;
}

/**
 * Fish Audio TTS over the `/v1/tts/live` WebSocket (Beta).
 *
 * Same option surface as {@link FishAudioTTS}, except the default model is
 * `s2-pro` (the fastest model this endpoint serves) and `s2.1-pro` is rejected
 * up-front because Fish does not route it here.
 */
export class FishAudioWebSocketTTS extends FishAudioTTS {
  /**
   * Stable pricing/dashboard key. Shares the HTTP adapter's key: same models,
   * same per-byte rate.
   */
  static readonly providerKey = 'fish_audio';

  private readonly wsUrl: string;
  private readonly openTimeoutMs: number;
  private readonly frameTimeoutMs: number;

  constructor(apiKey: string, opts: FishAudioWebSocketTTSOptions = {}) {
    const model = opts.model ?? FishAudioModel.S2_PRO;
    if (!WS_SUPPORTED_MODELS.includes(model)) {
      throw new Error(
        `FishAudioWebSocketTTS: model "${model}" is not available on the ` +
          `/v1/tts/live socket (Fish accepts only ${WS_SUPPORTED_MODELS.join(', ')} ` +
          `there). Use the HTTP adapter instead: new FishAudioTTS(apiKey, ` +
          `{ model: "${model}" }).`,
      );
    }
    super(apiKey, {
      ...opts,
      model,
      format: opts.format ?? FishAudioFormat.PCM,
      sampleRate: opts.sampleRate ?? FISH_AUDIO_DEFAULT_SAMPLE_RATE,
      latency: opts.latency ?? FishAudioLatency.BALANCED,
    });
    this.wsUrl = opts.wsUrl ?? FISH_AUDIO_WS_URL;
    this.openTimeoutMs = opts.openTimeoutMs ?? DEFAULT_OPEN_TIMEOUT_MS;
    this.frameTimeoutMs = opts.frameTimeoutMs ?? DEFAULT_FRAME_TIMEOUT_MS;
  }

  /**
   * Build the `start` frame. The `request` object takes the same fields as the
   * HTTP TTS body with an empty `text` — the real text arrives in `text` frames.
   */
  private buildStartEvent(): Record<string, unknown> {
    return { event: 'start', request: this.buildPayload('') };
  }

  /**
   * Yield audio chunks as they arrive over a per-utterance WebSocket. With the
   * default `format: "pcm"` these are raw PCM16-LE bytes at `sampleRate`.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const codec = await loadMsgpack();
    const ws = await this.openSocket();

    // Frames can arrive while the consumer is between `await`s, so they are
    // queued rather than handled inline; `waiter` parks the loop until the next
    // frame (or the frame timeout) fires.
    const inbound: Array<Buffer | string> = [];
    const state: { closed: boolean; error: Error | null } = { closed: false, error: null };
    // Held in a ref cell rather than a plain `let` so TypeScript does not narrow
    // it to `null` across the callbacks that assign it.
    const waiter: { current: FrameWaiter | null } = { current: null };

    const wake = (): void => {
      const pending = waiter.current;
      if (!pending) return;
      waiter.current = null;
      clearTimeout(pending.timer);
      pending.resolve();
    };

    ws.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
      const buf = toBuffer(data);
      inbound.push(isBinary ? buf : buf.toString('utf8'));
      wake();
    });
    ws.on('error', (err: Error) => {
      state.error = err;
      state.closed = true;
      wake();
    });
    ws.on('close', () => {
      state.closed = true;
      wake();
    });

    const waitForFrame = (): Promise<void> =>
      new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => {
          waiter.current = null;
          reject(
            new Error(`Fish Audio WS stalled: no frame within ${this.frameTimeoutMs}ms`),
          );
        }, this.frameTimeoutMs);
        waiter.current = { resolve, timer };
      });

    try {
      ws.send(codec.encode(this.buildStartEvent()));
      ws.send(codec.encode({ event: 'text', text }));
      ws.send(codec.encode({ event: 'flush' }));
      ws.send(codec.encode({ event: 'stop' }));

      for (;;) {
        while (inbound.length > 0) {
          const { audio, done } = decodeServerFrame(inbound.shift()!, codec);
          if (audio) yield audio;
          if (done) return;
        }
        if (state.error) throw state.error;
        if (state.closed) return;
        await waitForFrame();
      }
    } finally {
      if (waiter.current) clearTimeout(waiter.current.timer);
      ws.removeAllListeners();
      try {
        ws.close();
      } catch {
        /* already closing */
      }
    }
  }

  /** Open the synthesis socket with the auth + model headers, bounded by a timeout. */
  private openSocket(): Promise<WebSocket> {
    return new Promise<WebSocket>((resolve, reject) => {
      const ws = new WebSocket(this.wsUrl, {
        headers: {
          Authorization: `Bearer ${this.apiKey}`,
          model: this.model,
        },
        handshakeTimeout: this.openTimeoutMs,
      });
      const onOpen = (): void => {
        ws.off('error', onError);
        resolve(ws);
      };
      const onError = (err: Error): void => {
        ws.off('open', onOpen);
        reject(err);
      };
      ws.once('open', onOpen);
      ws.once('error', onError);
    });
  }
}

/** Normalise a `ws` RawData payload into a single Buffer. */
function toBuffer(data: WebSocket.RawData): Buffer {
  if (Buffer.isBuffer(data)) return data;
  if (Array.isArray(data)) return Buffer.concat(data);
  return Buffer.from(data as ArrayBuffer);
}

/**
 * Decode one server frame into `{ audio, done }`.
 *
 * Returns `{ done: false }` with no audio for frames carrying neither audio nor
 * a terminal signal (`log` frames, unknown future event types) so the caller
 * keeps reading. Throws when the server reports `finish` with `reason: "error"`.
 */
export function decodeServerFrame(
  frame: Buffer | string,
  codec: MsgpackCodec,
): { audio: Buffer | null; done: boolean } {
  if (typeof frame === 'string') {
    // The protocol is msgpack-binary; a text frame is only ever an
    // out-of-band server message. Log it and keep going.
    getLogger().debug(`Fish Audio WS text frame ignored: ${frame.slice(0, 200)}`);
    return { audio: null, done: false };
  }

  let parsed: unknown;
  try {
    parsed = codec.decode(new Uint8Array(frame));
  } catch (err) {
    throw new Error(`Fish Audio WS sent an undecodable frame: ${String(err)}`);
  }
  if (typeof parsed !== 'object' || parsed === null) {
    return { audio: null, done: false };
  }

  const evt = parsed as { event?: string; audio?: unknown; reason?: string; message?: string };
  if (evt.event === 'audio') {
    const audio = evt.audio;
    if (!(audio instanceof Uint8Array) && !Buffer.isBuffer(audio)) {
      return { audio: null, done: false };
    }
    if (audio.length > MAX_AUDIO_FRAME_SIZE) {
      throw new Error(
        `Fish Audio WS audio frame is ${audio.length} bytes, over the ` +
          `${MAX_AUDIO_FRAME_SIZE}-byte sanity limit`,
      );
    }
    return { audio: Buffer.from(audio), done: false };
  }
  if (evt.event === 'finish') {
    if (evt.reason === 'error') {
      throw new Error(
        `Fish Audio WS finished with an error: ${evt.message ?? evt.reason}`,
      );
    }
    return { audio: null, done: true };
  }
  if (evt.event === 'log') {
    getLogger().debug(`Fish Audio WS log: ${String(evt.message ?? '').slice(0, 200)}`);
  }
  return { audio: null, done: false };
}
