/**
 * xAI (Grok) TTS provider — WebSocket streaming with an HTTP fallback.
 *
 * The default transport targets the bidirectional streaming endpoint
 * (`wss://api.x.ai/v1/tts`): the adapter opens a WebSocket per utterance,
 * sends the text as `text.delta` + `text.done` and yields the decoded
 * `audio.delta` chunks as they arrive — the lowest time-to-first-audio
 * path. `transport: 'http'` switches to the unary `POST /v1/tts` endpoint
 * (raw audio bytes streamed in the response body) for environments where
 * WebSockets are unavailable.
 *
 * Notes (https://docs.x.ai/developers/model-capabilities/audio/text-to-speech):
 * 25+ built-in voices (`eve` is the default) plus custom voice IDs cloned
 * via `POST /v1/custom-voices`; inline speech tags (`[pause]`, `[laugh]`,
 * `<whisper>…</whisper>`) pass through in the text; output codecs `mp3` /
 * `wav` / `pcm` / `mulaw` / `alaw` — the G.711 codecs are carrier-native at
 * 8 kHz (see {@link XAITTS.forTwilio}).
 *
 * Pricing: $15.00 per 1M input characters (`xai_tts` in `pricing.ts`).
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';

const XAI_TTS_HTTP_URL = 'https://api.x.ai/v1/tts';
const XAI_TTS_WS_URL = 'wss://api.x.ai/v1/tts';
const XAI_TTS_VOICES_URL = 'https://api.x.ai/v1/tts/voices';

// Connect timeout — keeps the carrier WebSocket from sitting in dead air
// while a stuck DNS / TLS handshake is retried.
const OPEN_TIMEOUT_MS = 5000;

// Per-frame receive timeout. If the server stalls (no audio, no audio.done,
// no close) the generator would otherwise hang until carrier hangup.
const FRAME_TIMEOUT_MS = 30000;

// xAI caps a single TTS request / text.delta at 15,000 characters.
const MAX_TEXT_LEN = 15000;

/**
 * Built-in xAI voice IDs (case-insensitive on the wire; `eve` is the
 * default). The same roster works across TTS and the Voice Agent API —
 * fetch it programmatically via {@link XAITTS.listVoices}.
 */
export const XAIVoice = {
  ARA: 'ara',
  EVE: 'eve',
  LEO: 'leo',
  REX: 'rex',
  SAL: 'sal',
  ALTAIR: 'altair',
  ATLAS: 'atlas',
  CARINA: 'carina',
  CASTOR: 'castor',
  CELESTE: 'celeste',
  COSMO: 'cosmo',
  HELIOS: 'helios',
  HELIX: 'helix',
  IRIS: 'iris',
  KEPLER: 'kepler',
  LUMEN: 'lumen',
  LUNA: 'luna',
  LUX: 'lux',
  NAKSH: 'naksh',
  ORION: 'orion',
  PERSEUS: 'perseus',
  RIGEL: 'rigel',
  SIRIUS: 'sirius',
  URSA: 'ursa',
  ZAGAN: 'zagan',
  ZENITH: 'zenith',
} as const;
export type XAIVoice = (typeof XAIVoice)[keyof typeof XAIVoice];

/**
 * Output codecs accepted by xAI TTS. `mulaw` / `alaw` @ 8 kHz are the
 * carrier-native G.711 codecs (telephony passthrough).
 */
export const XAITTSCodec = {
  MP3: 'mp3',
  WAV: 'wav',
  PCM: 'pcm',
  MULAW: 'mulaw',
  ALAW: 'alaw',
} as const;
export type XAITTSCodec = (typeof XAITTSCodec)[keyof typeof XAITTSCodec];

/** Sample rates (Hz) accepted by xAI TTS. */
export const XAITTSSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_22050: 22050,
  HZ_24000: 24000,
  HZ_44100: 44100,
  HZ_48000: 48000,
} as const;
export type XAITTSSampleRate = (typeof XAITTSSampleRate)[keyof typeof XAITTSSampleRate];

/** Constructor options for {@link XAITTS}. */
export interface XAITTSOptions {
  /** Voice ID — a built-in {@link XAIVoice} or a custom voice ID. Default `eve`. */
  voice?: XAIVoice | string;
  /** BCP-47 language code or `"auto"` for detection. Default `"en"`. */
  language?: string;
  /** Output codec. Default `pcm`; `mulaw` @ 8 kHz = carrier-native. */
  codec?: XAITTSCodec | string;
  /** Output sample rate in Hz. Default 16000. */
  sampleRate?: XAITTSSampleRate | number;
  /** Bit rate in bps — MP3 codec only (32000–192000). */
  bitRate?: number;
  /** Speech speed multiplier, 0.7–1.5. Default 1.0. */
  speed?: number;
  /** Normalize written-form text (numbers, symbols) before synthesis. */
  textNormalization?: boolean;
  /**
   * Latency optimization level. `1` (default here): reduced first-chunk
   * size for lower time-to-first-audio — the right call for live
   * conversations. `0`: best-quality chunking for offline synthesis.
   */
  optimizeStreamingLatency?: 0 | 1;
  /** Transport. Default `websocket` (streaming); `http` = unary POST. */
  transport?: 'websocket' | 'http';
  /** Override the endpoint base (e.g. for tests). */
  baseUrl?: string;
}

/** Options for the carrier-specific factories — constructor minus the pinned format. */
export type XAICarrierOptions = Omit<XAITTSOptions, 'codec' | 'sampleRate'>;

/** A built-in voice entry returned by `GET /v1/tts/voices`. */
export interface XAIVoiceInfo {
  readonly voice_id: string;
  readonly name: string;
  readonly language?: string | null;
}

/** xAI (Grok) TTS adapter — WebSocket streaming by default. */
export class XAITTS {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'xai_tts';
  private readonly apiKey: string;
  private readonly voice: string;
  private readonly language: string;
  private codec: string;
  private sampleRate: number;
  private readonly bitRate?: number;
  private readonly speed?: number;
  private readonly textNormalization: boolean;
  private readonly optimizeStreamingLatency: 0 | 1;
  private readonly transport: 'websocket' | 'http';
  private readonly baseUrl?: string;
  /** True when the format was pinned explicitly (factories / options). */
  private formatExplicit: boolean;
  /**
   * Reference to the in-flight synthesis WS so `cancelActiveStream` can
   * force-close it from outside the generator (barge-in path).
   */
  private activeStreamWs: WebSocket | null = null;

  constructor(apiKey: string, opts: XAITTSOptions = {}) {
    if (!apiKey) {
      throw new Error('xAI TTS: apiKey is required');
    }
    if (opts.speed !== undefined && (opts.speed < 0.7 || opts.speed > 1.5)) {
      throw new Error(`speed must be in [0.7, 1.5], got ${opts.speed}`);
    }
    this.apiKey = apiKey;
    this.voice = String(opts.voice ?? XAIVoice.EVE);
    this.language = opts.language ?? 'en';
    this.codec = opts.codec ?? XAITTSCodec.PCM;
    this.sampleRate = opts.sampleRate ?? XAITTSSampleRate.HZ_16000;
    this.bitRate = opts.bitRate;
    this.speed = opts.speed;
    this.textNormalization = opts.textNormalization ?? false;
    this.optimizeStreamingLatency = opts.optimizeStreamingLatency ?? 1;
    this.transport = opts.transport ?? 'websocket';
    this.baseUrl = opts.baseUrl;
    this.formatExplicit = opts.codec !== undefined || opts.sampleRate !== undefined;
  }

  /**
   * xAI TTS pre-configured for Twilio Media Streams.
   *
   * Emits G.711 μ-law @ 8 kHz — exactly Twilio's wire codec — so the
   * pipeline skips resampling and PCM → μ-law encoding entirely (bit-clean
   * passthrough).
   */
  static forTwilio(apiKey: string, options: XAICarrierOptions = {}): XAITTS {
    return new XAITTS(apiKey, {
      ...options,
      codec: XAITTSCodec.MULAW,
      sampleRate: XAITTSSampleRate.HZ_8000,
    });
  }

  /** xAI TTS pre-configured for Telnyx (PCMU/μ-law @ 8 kHz wire passthrough). */
  static forTelnyx(apiKey: string, options: XAICarrierOptions = {}): XAITTS {
    return new XAITTS(apiKey, {
      ...options,
      codec: XAITTSCodec.MULAW,
      sampleRate: XAITTSSampleRate.HZ_8000,
    });
  }

  /**
   * Hook called by `StreamHandler` to advise the carrier wire format.
   * Flips unconfigured instances to the carrier's native μ-law @ 8 kHz
   * (all three supported carriers speak PCMU on the wire); no-op when the
   * user pinned the format or the carrier is unknown.
   */
  setTelephonyCarrier(carrier: string): void {
    if (this.formatExplicit) return;
    if (carrier !== 'twilio' && carrier !== 'telnyx' && carrier !== 'plivo') return;
    this.codec = XAITTSCodec.MULAW;
    this.sampleRate = XAITTSSampleRate.HZ_8000;
  }

  /**
   * Declare the audio format this adapter emits, so the pipeline sender can
   * derive the correct resample ratio (or skip it for μ-law passthrough).
   */
  sourceAudioFormat(): {
    encoding: 'pcm_s16le' | 'mulaw' | 'alaw';
    sampleRate: number;
  } {
    if (this.codec === XAITTSCodec.MULAW) {
      return { encoding: 'mulaw', sampleRate: this.sampleRate };
    }
    if (this.codec === XAITTSCodec.ALAW) {
      return { encoding: 'alaw', sampleRate: this.sampleRate };
    }
    return { encoding: 'pcm_s16le', sampleRate: this.sampleRate };
  }

  /**
   * Fetch the built-in voice roster from `GET /v1/tts/voices`. Custom
   * voices are returned by `GET /v1/custom-voices` only and do not appear
   * in this list.
   */
  static async listVoices(apiKey: string): Promise<XAIVoiceInfo[]> {
    const response = await fetch(XAI_TTS_VOICES_URL, {
      headers: { Authorization: `Bearer ${apiKey}` },
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) {
      throw new Error(`xAI list voices error ${response.status}: ${await response.text()}`);
    }
    const data = (await response.json()) as { voices?: XAIVoiceInfo[] };
    return data.voices ?? [];
  }

  private buildWsUrl(): string {
    const rawBase = this.baseUrl ?? XAI_TTS_WS_URL;
    let base: string;
    if (rawBase.startsWith('http://')) {
      base = `ws://${rawBase.slice('http://'.length)}`;
    } else if (rawBase.startsWith('https://')) {
      base = `wss://${rawBase.slice('https://'.length)}`;
    } else {
      base = rawBase;
    }
    const params = new URLSearchParams({
      voice: this.voice,
      language: this.language,
      codec: this.codec,
      sample_rate: String(this.sampleRate),
      optimize_streaming_latency: String(this.optimizeStreamingLatency),
    });
    if (this.bitRate !== undefined) params.set('bit_rate', String(this.bitRate));
    if (this.speed !== undefined) params.set('speed', String(this.speed));
    if (this.textNormalization) params.set('text_normalization', 'true');
    return `${base}?${params.toString()}`;
  }

  private buildHttpPayload(text: string): Record<string, unknown> {
    const outputFormat: Record<string, unknown> = {
      codec: this.codec,
      sample_rate: this.sampleRate,
    };
    if (this.bitRate !== undefined) outputFormat.bit_rate = this.bitRate;
    const payload: Record<string, unknown> = {
      text,
      voice_id: this.voice,
      language: this.language,
      output_format: outputFormat,
    };
    if (this.speed !== undefined) payload.speed = this.speed;
    if (this.textNormalization) payload.text_normalization = true;
    return payload;
  }

  /**
   * Force-close the currently open synthesis WebSocket (if any). Called by
   * `StreamHandler` on barge-in / stop / ws-close to unblock the in-flight
   * `synthesizeStream` generator immediately instead of waiting up to the
   * frame timeout. No-op when no synthesis is in progress. Parity with
   * `ElevenLabsWebSocketTTS.cancelActiveStream`.
   */
  cancelActiveStream(): void {
    const ws = this.activeStreamWs;
    if (!ws) return;
    this.activeStreamWs = null;
    try {
      ws.close();
    } catch {
      // defensive — socket may already be closed
    }
  }

  /** Synthesize text and return the concatenated audio buffer. */
  async synthesize(text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of this.synthesizeStream(text)) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /**
   * Synthesize text and yield raw audio chunks as they arrive from xAI.
   * With the default `codec: 'pcm'` these are PCM_S16LE bytes at the
   * configured `sampleRate`; with `mulaw` (see {@link XAITTS.forTwilio})
   * they are carrier-native G.711 μ-law bytes.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    if (text.length > MAX_TEXT_LEN) {
      throw new Error(
        `xAI TTS accepts at most ${MAX_TEXT_LEN} characters per request, got ${text.length}`,
      );
    }
    if (this.transport === 'http') {
      yield* this.synthesizeHttp(text);
      return;
    }
    yield* this.synthesizeWs(text);
  }

  private async *synthesizeHttp(text: string): AsyncGenerator<Buffer> {
    const url = this.baseUrl ?? XAI_TTS_HTTP_URL;
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(this.buildHttpPayload(text)),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`xAI TTS error ${response.status}: ${body}`);
    }
    if (!response.body) {
      throw new Error('xAI TTS: no response body');
    }

    const reader = response.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length > 0) {
          yield Buffer.from(value);
        }
      }
    } finally {
      if (typeof reader.cancel === 'function') await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  private async *synthesizeWs(text: string): AsyncGenerator<Buffer> {
    const ws = new WebSocket(this.buildWsUrl(), {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });

    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        try {
          ws.close();
        } catch {
          // ignore
        }
        reject(new Error('xAI TTS connect timeout'));
      }, OPEN_TIMEOUT_MS);
      ws.once('open', () => {
        clearTimeout(timer);
        resolve();
      });
      ws.once('error', (err: Error) => {
        clearTimeout(timer);
        reject(err);
      });
    });

    // Queue-based bridge between the ws message events and the async
    // generator — mirrors the ElevenLabs WS adapter's consumption model.
    const queue: Array<Buffer | Error | null> = [];
    let wakeup: (() => void) | null = null;
    const push = (item: Buffer | Error | null): void => {
      queue.push(item);
      wakeup?.();
      wakeup = null;
    };

    ws.on('message', (raw: WebSocket.RawData, isBinary: boolean) => {
      if (isBinary) {
        // The documented protocol is JSON text frames; tolerate binary
        // audio frames defensively.
        push(Buffer.from(raw as Buffer));
        return;
      }
      let msg: { type?: string; delta?: string; message?: string };
      try {
        msg = JSON.parse(raw.toString()) as typeof msg;
      } catch {
        getLogger().warn('xAI TTS WS sent non-JSON text frame');
        return;
      }
      if (msg.type === 'error') {
        push(new Error(`xAI TTS WS reported error: ${String(msg.message ?? 'unknown').slice(0, 200)}`));
      } else if (msg.type === 'audio.delta') {
        if (typeof msg.delta === 'string') {
          try {
            push(Buffer.from(msg.delta, 'base64'));
          } catch {
            getLogger().warn('xAI TTS WS sent malformed base64 audio');
          }
        }
      } else if (msg.type === 'audio.done') {
        push(null);
      }
    });
    ws.on('close', () => push(null));
    ws.on('error', (err: Error) => push(err));

    this.activeStreamWs = ws;
    try {
      ws.send(JSON.stringify({ type: 'text.delta', delta: text }));
      ws.send(JSON.stringify({ type: 'text.done' }));

      while (true) {
        if (queue.length === 0) {
          const timedOut = await new Promise<boolean>((resolve) => {
            const timer = setTimeout(() => resolve(true), FRAME_TIMEOUT_MS);
            wakeup = () => {
              clearTimeout(timer);
              resolve(false);
            };
          });
          if (timedOut) {
            throw new Error(`xAI TTS WS no frame for ${FRAME_TIMEOUT_MS / 1000}s`);
          }
        }
        const item = queue.shift();
        if (item === null || item === undefined) return; // end-of-stream
        if (item instanceof Error) throw item;
        yield item;
      }
    } finally {
      if (this.activeStreamWs === ws) {
        this.activeStreamWs = null;
      }
      try {
        ws.close();
      } catch {
        // ignore
      }
    }
  }

  /**
   * Pre-call WebSocket warmup for the xAI `/v1/tts` endpoint. Opens the WS
   * (DNS + TLS + auth handshake), idles ~250 ms, then closes.
   *
   * Billing safety: xAI TTS bills per input character
   * (https://docs.x.ai/developers/pricing); opening + closing the socket
   * without sending `text.delta` consumes no billable characters.
   * Best-effort: failures are logged at debug level.
   */
  async warmup(): Promise<void> {
    if (this.transport !== 'websocket') return;
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
          reject(new Error('xAI TTS warmup connect timeout'));
        }, OPEN_TIMEOUT_MS);
        sock.once('open', () => {
          clearTimeout(timer);
          resolve(sock);
        });
        sock.once('error', (err: Error) => {
          clearTimeout(timer);
          reject(err);
        });
      });
      await new Promise<void>((r) => setTimeout(r, 250));
    } catch (err) {
      getLogger().debug(`xAI TTS warmup failed (best-effort): ${String(err)}`);
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

  /** No-op: connections are per-utterance and closed inline. */
  async close(): Promise<void> {
    this.cancelActiveStream();
  }
}

/**
 * Clone a voice from a reference clip via `POST /v1/custom-voices`.
 *
 * `file` is the reference audio (max 120 s; 90+ s recommended, WAV 24 kHz
 * mono preferred). Returns the created voice object — its `voice_id` works
 * as `voice` on {@link XAITTS} and the xAI realtime adapter exactly like a
 * built-in voice. The API endpoint is gated to xAI Enterprise plans; voices
 * can also be created for free in the xAI console.
 */
export async function createCustomVoice(
  apiKey: string,
  file: Buffer,
  opts: {
    filename?: string;
    contentType?: string;
    name?: string;
    description?: string;
    gender?: string;
    accent?: string;
    age?: string;
    language?: string;
    useCase?: string;
    tone?: string;
  } = {},
): Promise<Record<string, unknown>> {
  const form = new FormData();
  if (opts.name !== undefined) form.append('name', opts.name);
  if (opts.description !== undefined) form.append('description', opts.description);
  if (opts.gender !== undefined) form.append('gender', opts.gender);
  if (opts.accent !== undefined) form.append('accent', opts.accent);
  if (opts.age !== undefined) form.append('age', opts.age);
  if (opts.language !== undefined) form.append('language', opts.language);
  if (opts.useCase !== undefined) form.append('use_case', opts.useCase);
  if (opts.tone !== undefined) form.append('tone', opts.tone);
  // The file must be the LAST field in the multipart form (API requirement).
  form.append(
    'file',
    new Blob([new Uint8Array(file)], { type: opts.contentType ?? 'audio/wav' }),
    opts.filename ?? 'reference.wav',
  );
  const response = await fetch('https://api.x.ai/v1/custom-voices', {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}` },
    body: form,
    signal: AbortSignal.timeout(60_000),
  });
  if (!response.ok) {
    throw new Error(`xAI create custom voice error ${response.status}: ${await response.text()}`);
  }
  return (await response.json()) as Record<string, unknown>;
}

/**
 * List the team's cloned voices via `GET /v1/custom-voices`. Returns the
 * raw response (`voices` list plus an optional `pagination_token`).
 */
export async function listCustomVoices(
  apiKey: string,
  opts: { limit?: number; paginationToken?: string } = {},
): Promise<{ voices: Array<Record<string, unknown>>; pagination_token?: string }> {
  const params = new URLSearchParams({ limit: String(opts.limit ?? 100) });
  if (opts.paginationToken) params.set('pagination_token', opts.paginationToken);
  const response = await fetch(`https://api.x.ai/v1/custom-voices?${params.toString()}`, {
    headers: { Authorization: `Bearer ${apiKey}` },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) {
    throw new Error(`xAI list custom voices error ${response.status}: ${await response.text()}`);
  }
  return (await response.json()) as {
    voices: Array<Record<string, unknown>>;
    pagination_token?: string;
  };
}
