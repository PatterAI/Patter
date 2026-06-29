/**
 * Cartesia TTS provider — HTTP `/tts/bytes` endpoint.
 *
 * Cartesia also offers a WebSocket streaming mode with word timestamps;
 * this provider focuses on the chunked-bytes HTTP API which maps cleanly
 * onto Patter's `synthesize(text)` contract and keeps the provider
 * dependency-free (just `fetch`).
 *
 * Default model is `sonic-3` (GA snapshot `sonic-3-2026-01-12`) — Cartesia's
 * current GA model with a documented ~90 ms TTFB target. Voice IDs from the
 * sonic-2 generation (including the default Katie voice) remain compatible.
 *
 * **Telephony optimization** — the constructor default
 * `sampleRate=16000` is correct for web playback, dashboard previews, and
 * 16 kHz pipelines. For real phone calls, use the carrier-specific
 * factories instead:
 *
 * - {@link CartesiaTTS.forTwilio} emits `pcm_mulaw` @ 8 kHz — Twilio's exact
 *   wire codec — so the pipeline skips resampling AND PCM → μ-law encoding
 *   (bit-clean passthrough). The sender now reads the declared output format
 *   rather than assuming a 16 kHz source, so this no longer chipmunks.
 * - {@link CartesiaTTS.forTelnyx} emits `pcm_mulaw` @ 8 kHz. The SDK pins the
 *   Telnyx wire to PCMU/μ-law @ 8 kHz, so this flows end-to-end with zero
 *   resampling or transcoding — same passthrough win as the Twilio factory.
 */

import { getLogger } from '../logger';

const CARTESIA_BASE_URL = 'https://api.cartesia.ai';
// Cartesia API version pin — matches our STT integration and the Cartesia
// Line skill. `2025-04-16` is the current GA snapshot.
const CARTESIA_API_VERSION = '2025-04-16';
const CARTESIA_DEFAULT_VOICE_ID = 'f786b574-daa5-4673-aa0c-cbe3e8534c02';

/** Known Cartesia TTS models. */
export const CartesiaTTSModel = {
  SONIC_3: 'sonic-3',
  SONIC_2: 'sonic-2',
  SONIC: 'sonic',
} as const;
export type CartesiaTTSModel = (typeof CartesiaTTSModel)[keyof typeof CartesiaTTSModel];

/** Audio container formats accepted by the Cartesia bytes endpoint. */
export const CartesiaTTSContainer = {
  RAW: 'raw',
  WAV: 'wav',
  MP3: 'mp3',
} as const;
export type CartesiaTTSContainer = (typeof CartesiaTTSContainer)[keyof typeof CartesiaTTSContainer];

/** Audio encodings accepted by the Cartesia bytes endpoint. */
export const CartesiaTTSEncoding = {
  PCM_S16LE: 'pcm_s16le',
  PCM_F32LE: 'pcm_f32le',
  PCM_MULAW: 'pcm_mulaw',
  PCM_ALAW: 'pcm_alaw',
} as const;
export type CartesiaTTSEncoding = (typeof CartesiaTTSEncoding)[keyof typeof CartesiaTTSEncoding];

/** Common PCM sample rates accepted by the Cartesia bytes endpoint. */
export const CartesiaTTSSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_22050: 22050,
  HZ_24000: 24000,
  HZ_44100: 44100,
} as const;
export type CartesiaTTSSampleRate = (typeof CartesiaTTSSampleRate)[keyof typeof CartesiaTTSSampleRate];

/** Voice-selection mode passed in the Cartesia bytes payload. */
export const CartesiaTTSVoiceMode = {
  ID: 'id',
  EMBEDDING: 'embedding',
} as const;
export type CartesiaTTSVoiceMode = (typeof CartesiaTTSVoiceMode)[keyof typeof CartesiaTTSVoiceMode];

/** Constructor options for {@link CartesiaTTS}. */
export interface CartesiaTTSOptions {
  model?: CartesiaTTSModel | string;
  voice?: string;
  language?: string;
  sampleRate?: CartesiaTTSSampleRate | number;
  /**
   * Audio encoding Cartesia produces. Default ``pcm_s16le`` (linear PCM16).
   * Set ``pcm_mulaw`` with ``sampleRate=8000`` to emit Twilio/Telnyx/Plivo
   * wire-native μ-law directly — the pipeline then skips resampling and PCM →
   * μ-law encoding entirely (see {@link CartesiaTTS.forTwilio}).
   */
  encoding?: CartesiaTTSEncoding;
  speed?: string | number;
  emotion?: string | string[];
  volume?: number;
  baseUrl?: string;
  apiVersion?: string;
}

/** Cartesia TTS provider backed by the HTTP `/tts/bytes` streaming endpoint. */
export class CartesiaTTS {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'cartesia_tts';
  private readonly apiKey: string;
  private readonly model: string;
  private readonly voice: string;
  private readonly language: string;
  private readonly sampleRate: number;
  private readonly encoding: CartesiaTTSEncoding;
  private readonly speed?: string | number;
  private readonly emotion?: string[];
  private readonly volume?: number;
  private readonly baseUrl: string;
  private readonly apiVersion: string;

  constructor(apiKey: string, opts: CartesiaTTSOptions = {}) {
    this.apiKey = apiKey;
    this.model = opts.model ?? CartesiaTTSModel.SONIC_3;
    this.voice = opts.voice ?? CARTESIA_DEFAULT_VOICE_ID;
    this.language = opts.language ?? 'en';
    this.sampleRate = opts.sampleRate ?? CartesiaTTSSampleRate.HZ_16000;
    this.encoding = opts.encoding ?? CartesiaTTSEncoding.PCM_S16LE;
    this.speed = opts.speed;
    this.emotion =
      typeof opts.emotion === 'string' ? [opts.emotion] : opts.emotion;
    this.volume = opts.volume;
    this.baseUrl = opts.baseUrl ?? CARTESIA_BASE_URL;
    this.apiVersion = opts.apiVersion ?? CARTESIA_API_VERSION;
  }

  /**
   * Declare the audio format this adapter emits, so the pipeline sender can
   * derive the correct resample ratio (or skip it for μ-law passthrough)
   * instead of assuming a fixed 16 kHz source. See ``audio/format.ts``.
   *
   * - ``pcm_mulaw`` → ``{ encoding: 'mulaw', sampleRate }`` (carrier-native).
   * - ``pcm_alaw``  → ``{ encoding: 'alaw',  sampleRate }``.
   * - everything else → ``{ encoding: 'pcm_s16le', sampleRate }``.
   */
  sourceAudioFormat(): { encoding: 'pcm_s16le' | 'mulaw' | 'alaw'; sampleRate: number } {
    if (this.encoding === CartesiaTTSEncoding.PCM_MULAW) {
      return { encoding: 'mulaw', sampleRate: this.sampleRate };
    }
    if (this.encoding === CartesiaTTSEncoding.PCM_ALAW) {
      return { encoding: 'alaw', sampleRate: this.sampleRate };
    }
    return { encoding: 'pcm_s16le', sampleRate: this.sampleRate };
  }

  /**
   * Construct an instance pre-configured for Twilio Media Streams.
   *
   * Emits ``pcm_mulaw`` @ 8 kHz — exactly Twilio's wire codec — so the
   * pipeline takes the passthrough path: zero resampling, zero PCM → μ-law
   * encoding, bit-clean audio. This generalises the ElevenLabs
   * ``setTelephonyCarrier`` μ-law-native trick to Cartesia and removes the
   * resampler entirely from the hot path (saves ~10–30 ms first-byte plus
   * per-frame CPU and all resampling aliasing).
   *
   * Previously this requested ``pcm_s16le`` @ 16 kHz because the sender
   * hardcoded a 16 kHz → 8 kHz decimator; now the sender reads the declared
   * format and an 8 kHz μ-law source flows straight to the wire.
   */
  static forTwilio(
    apiKey: string,
    options: Omit<CartesiaTTSOptions, 'sampleRate' | 'encoding'> = {},
  ): CartesiaTTS {
    return new CartesiaTTS(apiKey, {
      ...options,
      encoding: CartesiaTTSEncoding.PCM_MULAW,
      sampleRate: CartesiaTTSSampleRate.HZ_8000,
    });
  }

  /**
   * Construct an instance pre-configured for Telnyx bidirectional media.
   *
   * The SDK's ``streaming_start`` pins the Telnyx wire to PCMU/μ-law @ 8 kHz,
   * so emitting ``pcm_mulaw`` @ 8 kHz flows end-to-end with zero resampling or
   * transcoding — the same passthrough win as {@link CartesiaTTS.forTwilio}.
   */
  static forTelnyx(
    apiKey: string,
    options: Omit<CartesiaTTSOptions, 'sampleRate' | 'encoding'> = {},
  ): CartesiaTTS {
    return new CartesiaTTS(apiKey, {
      ...options,
      encoding: CartesiaTTSEncoding.PCM_MULAW,
      sampleRate: CartesiaTTSSampleRate.HZ_8000,
    });
  }

  /** Build the JSON payload for the Cartesia bytes endpoint. */
  private buildPayload(text: string): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      model_id: this.model,
      voice: { mode: CartesiaTTSVoiceMode.ID, id: this.voice },
      transcript: text,
      output_format: {
        container: CartesiaTTSContainer.RAW,
        encoding: this.encoding,
        sample_rate: this.sampleRate,
      },
      language: this.language,
    };

    const generationConfig: Record<string, unknown> = {};
    if (this.speed !== undefined) generationConfig.speed = this.speed;
    if (this.emotion && this.emotion.length > 0)
      generationConfig.emotion = this.emotion[0];
    if (this.volume !== undefined) generationConfig.volume = this.volume;
    if (Object.keys(generationConfig).length > 0) {
      payload.generation_config = generationConfig;
    }

    return payload;
  }

  /**
   * Pre-call HTTP warmup for the Cartesia `/tts/bytes` endpoint.
   *
   * Issues a lightweight `GET <baseUrl>/voices` so DNS, TLS, and HTTP/2
   * are already up by the time the first `synthesizeStream()` POST
   * lands. Best-effort: 5 s timeout, all exceptions swallowed at
   * debug level.
   *
   * Billing safety: `GET /voices` is a free metadata read on
   * Cartesia's REST surface (per https://docs.cartesia.ai). It does
   * not consume synthesis credits. The actual synthesis is billed
   * only when `POST /tts/bytes` runs with a non-empty `transcript`.
   *
   * Note: Cartesia TTS uses the HTTP path (vs the WebSocket variant
   * Cartesia also exposes) — connection warmup is therefore HTTP-GET
   * based, not WebSocket pre-handshake. The latency win is smaller
   * (~50-150 ms vs the ~200-500 ms of a WS prewarm) but still real.
   */
  async warmup(): Promise<void> {
    try {
      await fetch(`${this.baseUrl}/voices`, {
        method: 'GET',
        headers: {
          'X-API-Key': this.apiKey,
          'Cartesia-Version': this.apiVersion,
        },
        signal: AbortSignal.timeout(5_000),
      });
    } catch (err) {
      getLogger().debug(`Cartesia TTS warmup failed (best-effort): ${String(err)}`);
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
   * Synthesize text and yield raw PCM_S16LE chunks at the configured
   * `sampleRate` as they arrive from Cartesia.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(`${this.baseUrl}/tts/bytes`, {
      method: 'POST',
      headers: {
        'X-API-Key': this.apiKey,
        'Cartesia-Version': this.apiVersion,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(this.buildPayload(text)),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Cartesia TTS error ${response.status}: ${body}`);
    }

    if (!response.body) {
      throw new Error('Cartesia TTS: no response body');
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
      if (typeof reader.cancel === 'function')
        await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}
