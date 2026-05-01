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
 * - {@link CartesiaTTS.forTwilio} requests `sampleRate=8000` natively from
 *   Cartesia. Twilio's media-stream WebSocket expects μ-law @ 8 kHz, so
 *   the SDK normally resamples 16 kHz → 8 kHz before doing the PCM →
 *   μ-law transcode in `TwilioAudioSender`. Asking Cartesia for 8 kHz
 *   PCM at the source skips the resample step (saves ~10–30 ms first-
 *   byte plus per-frame CPU and removes a potential aliasing source).
 *   The PCM → μ-law transcode still happens client-side.
 * - {@link CartesiaTTS.forTelnyx} requests `sampleRate=16000`. Telnyx
 *   negotiates L16/16000 on its bidirectional media WebSocket, so
 *   16 kHz PCM is already the format used end-to-end and no
 *   transcoding happens. This is the same as the bare-constructor
 *   default and exists for API symmetry with the Twilio factory.
 */

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

export interface CartesiaTTSOptions {
  model?: CartesiaTTSModel | string;
  voice?: string;
  language?: string;
  sampleRate?: CartesiaTTSSampleRate | number;
  speed?: string | number;
  emotion?: string | string[];
  volume?: number;
  baseUrl?: string;
  apiVersion?: string;
}

export class CartesiaTTS {
  private readonly apiKey: string;
  private readonly model: string;
  private readonly voice: string;
  private readonly language: string;
  private readonly sampleRate: number;
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
    this.speed = opts.speed;
    this.emotion =
      typeof opts.emotion === 'string' ? [opts.emotion] : opts.emotion;
    this.volume = opts.volume;
    this.baseUrl = opts.baseUrl ?? CARTESIA_BASE_URL;
    this.apiVersion = opts.apiVersion ?? CARTESIA_API_VERSION;
  }

  /**
   * Construct an instance pre-configured for Twilio Media Streams.
   *
   * Sets `sampleRate=8000` so Cartesia emits PCM_S16LE @ 8 kHz directly.
   * Twilio's media stream uses μ-law @ 8 kHz so the SDK still does the
   * PCM → μ-law transcode client-side, but the 16 kHz → 8 kHz resample
   * step is skipped. Saves ~10–30 ms first-byte plus per-frame CPU and
   * removes a potential aliasing source.
   */
  static forTwilio(
    apiKey: string,
    options: Omit<CartesiaTTSOptions, 'sampleRate'> = {},
  ): CartesiaTTS {
    return new CartesiaTTS(apiKey, {
      ...options,
      sampleRate: CartesiaTTSSampleRate.HZ_8000,
    });
  }

  /**
   * Construct an instance pre-configured for Telnyx bidirectional media.
   *
   * Sets `sampleRate=16000` to match Telnyx's L16/16000 default codec —
   * audio flows end-to-end with zero resampling or transcoding. Same as
   * the bare-constructor default; exists for API symmetry with
   * {@link CartesiaTTS.forTwilio}.
   */
  static forTelnyx(
    apiKey: string,
    options: Omit<CartesiaTTSOptions, 'sampleRate'> = {},
  ): CartesiaTTS {
    return new CartesiaTTS(apiKey, {
      ...options,
      sampleRate: CartesiaTTSSampleRate.HZ_16000,
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
        encoding: CartesiaTTSEncoding.PCM_S16LE,
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
