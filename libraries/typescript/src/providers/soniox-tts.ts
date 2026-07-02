/**
 * Soniox TTS provider — HTTP one-shot `/tts` bytes endpoint.
 *
 * Calls `POST https://tts-rt.soniox.com/tts`. The response is raw audio bytes
 * whose Content-Type matches the requested `audio_format` (e.g. `audio/pcm`,
 * `audio/wav`, `audio/mpeg`). This maps cleanly onto Patter's
 * `synthesize(text)` contract and keeps the provider dependency-free (just
 * `fetch`).
 *
 * Soniox also exposes a WebSocket streaming mode
 * (`wss://tts-rt.soniox.com/tts-websocket`) for lower TTFB; this provider uses
 * the REST bytes path which already maps onto Patter's streaming contract —
 * the same trade-off `CartesiaTTS` makes versus Cartesia's WebSocket variant.
 *
 * Credential: Soniox TTS shares the `SONIOX_API_KEY` env var / key family with
 * the existing Soniox STT integration (host `*.soniox.com`). REST auth is an
 * `Authorization: Bearer <key>` header.
 *
 * **Telephony optimization** — the constructor default
 * `audioFormat="pcm_s16le"` @ `sampleRate=16000` is correct for web playback,
 * dashboard previews, and 16 kHz pipelines. For real phone calls use the
 * carrier-specific factories instead:
 *
 * - {@link SonioxTTS.forTwilio} emits `pcm_mulaw` @ 8 kHz — Twilio's exact wire
 *   codec — so the pipeline skips resampling AND PCM → μ-law encoding
 *   (bit-clean passthrough). Soniox supports G.711 μ-law @ 8 kHz natively, so
 *   this is true carrier-native synthesis (no downsampling).
 * - {@link SonioxTTS.forTelnyx} emits `pcm_mulaw` @ 8 kHz. The SDK pins the
 *   Telnyx wire to PCMU/μ-law @ 8 kHz, so this flows end-to-end with zero
 *   resampling — same passthrough win as the Twilio factory.
 */

const SONIOX_TTS_BASE_URL = 'https://tts-rt.soniox.com/tts';
// WebSocket streaming endpoint (documented for reference; not used by this
// HTTP-bytes adapter).
export const SONIOX_TTS_WS_URL = 'wss://tts-rt.soniox.com/tts-websocket';

/** Soniox default voice — keeps its timbre across 60+ languages. */
const SONIOX_DEFAULT_VOICE = 'Adrian';

/** Known Soniox real-time TTS models. */
export const SonioxTTSModel = {
  TTS_RT_V1: 'tts-rt-v1',
  // Deprecated alias retained for back-compat — now points at tts-rt-v1.
  TTS_RT_V1_PREVIEW: 'tts-rt-v1-preview',
} as const;
export type SonioxTTSModel = (typeof SonioxTTSModel)[keyof typeof SonioxTTSModel];

/**
 * Output encodings accepted by the Soniox `audio_format` field. `PCM_MULAW` /
 * `PCM_ALAW` @ 8 kHz are the carrier-native G.711 codecs (telephony
 * passthrough); the rest target web/dashboard use.
 */
export const SonioxTTSAudioFormat = {
  PCM_S16LE: 'pcm_s16le',
  PCM_F32LE: 'pcm_f32le',
  PCM_MULAW: 'pcm_mulaw',
  PCM_ALAW: 'pcm_alaw',
  WAV: 'wav',
  MP3: 'mp3',
  AAC: 'aac',
  OPUS: 'opus',
  FLAC: 'flac',
} as const;
export type SonioxTTSAudioFormat =
  (typeof SonioxTTSAudioFormat)[keyof typeof SonioxTTSAudioFormat];

/** Sample rates (Hz) accepted by the Soniox `sample_rate` field. */
export const SonioxTTSSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_24000: 24000,
  HZ_44100: 44100,
  HZ_48000: 48000,
} as const;
export type SonioxTTSSampleRate =
  (typeof SonioxTTSSampleRate)[keyof typeof SonioxTTSSampleRate];

/** Constructor options for {@link SonioxTTS}. */
export interface SonioxTTSOptions {
  /** Model id. Defaults to `"tts-rt-v1"`. */
  model?: SonioxTTSModel | string;
  /** Voice name (e.g. `"Adrian"`, `"Maya"`) or a cloned-voice id. */
  voice?: string;
  /** BCP-47 language tag, e.g. `"en"`, `"it"`, `"es"`. Defaults to `"en"`. */
  language?: string;
  /**
   * Output audio encoding. Default `pcm_s16le` (linear PCM16). Set
   * `pcm_mulaw` with `sampleRate=8000` to emit Twilio/Telnyx wire-native
   * μ-law directly — the pipeline then skips resampling and PCM → μ-law
   * encoding entirely (see {@link SonioxTTS.forTwilio}).
   */
  audioFormat?: SonioxTTSAudioFormat | string;
  /** Output sample rate in Hz. Defaults to 16000. */
  sampleRate?: SonioxTTSSampleRate | number;
  /** Bitrate hint (bits/sec) for compressed formats (mp3/aac/opus). */
  bitrate?: number;
  /** Speaking-rate multiplier (optional). */
  speed?: number;
  /** Override the REST endpoint (e.g. for tests). */
  baseUrl?: string;
}

/** Soniox TTS adapter backed by the HTTP one-shot `/tts` bytes endpoint. */
export class SonioxTTS {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'soniox_tts';
  private readonly apiKey: string;
  private readonly model: string;
  private readonly voice: string;
  private readonly language: string;
  private readonly audioFormat: string;
  private readonly sampleRate: number;
  private readonly bitrate?: number;
  private readonly speed?: number;
  private readonly baseUrl: string;

  constructor(apiKey: string, opts: SonioxTTSOptions = {}) {
    if (!apiKey) {
      throw new Error('Soniox TTS: apiKey is required');
    }
    this.apiKey = apiKey;
    this.model = opts.model ?? SonioxTTSModel.TTS_RT_V1;
    this.voice = opts.voice ?? SONIOX_DEFAULT_VOICE;
    this.language = opts.language ?? 'en';
    this.audioFormat = opts.audioFormat ?? SonioxTTSAudioFormat.PCM_S16LE;
    this.sampleRate = opts.sampleRate ?? SonioxTTSSampleRate.HZ_16000;
    this.bitrate = opts.bitrate;
    this.speed = opts.speed;
    this.baseUrl = opts.baseUrl ?? SONIOX_TTS_BASE_URL;
  }

  /**
   * Declare the audio format this adapter emits, so the pipeline sender can
   * derive the correct resample ratio (or skip it for μ-law passthrough)
   * instead of assuming a fixed 16 kHz source. See `audio/format.ts`.
   *
   * - `pcm_mulaw` → `{ encoding: 'mulaw', sampleRate }` (carrier-native).
   * - `pcm_alaw`  → `{ encoding: 'alaw',  sampleRate }`.
   * - everything else → `{ encoding: 'pcm_s16le', sampleRate }`.
   */
  sourceAudioFormat(): {
    encoding: 'pcm_s16le' | 'mulaw' | 'alaw';
    sampleRate: number;
  } {
    if (this.audioFormat === SonioxTTSAudioFormat.PCM_MULAW) {
      return { encoding: 'mulaw', sampleRate: this.sampleRate };
    }
    if (this.audioFormat === SonioxTTSAudioFormat.PCM_ALAW) {
      return { encoding: 'alaw', sampleRate: this.sampleRate };
    }
    return { encoding: 'pcm_s16le', sampleRate: this.sampleRate };
  }

  /**
   * Construct an instance pre-configured for Twilio Media Streams.
   *
   * Emits `pcm_mulaw` @ 8 kHz — exactly Twilio's wire codec — so the pipeline
   * takes the passthrough path: zero resampling, zero PCM → μ-law encoding,
   * bit-clean audio. Soniox supports μ-law @ 8 kHz natively, so this is true
   * carrier-native synthesis (no downsampling from a PCM-only model).
   */
  static forTwilio(
    apiKey: string,
    options: Omit<SonioxTTSOptions, 'sampleRate' | 'audioFormat'> = {},
  ): SonioxTTS {
    return new SonioxTTS(apiKey, {
      ...options,
      audioFormat: SonioxTTSAudioFormat.PCM_MULAW,
      sampleRate: SonioxTTSSampleRate.HZ_8000,
    });
  }

  /**
   * Construct an instance pre-configured for Telnyx bidirectional media.
   *
   * The SDK pins the Telnyx wire to PCMU/μ-law @ 8 kHz, so emitting
   * `pcm_mulaw` @ 8 kHz flows end-to-end with zero resampling or transcoding —
   * the same passthrough win as {@link SonioxTTS.forTwilio}.
   */
  static forTelnyx(
    apiKey: string,
    options: Omit<SonioxTTSOptions, 'sampleRate' | 'audioFormat'> = {},
  ): SonioxTTS {
    return new SonioxTTS(apiKey, {
      ...options,
      audioFormat: SonioxTTSAudioFormat.PCM_MULAW,
      sampleRate: SonioxTTSSampleRate.HZ_8000,
    });
  }

  /** Build the JSON payload for the Soniox `/tts` endpoint. */
  private buildPayload(text: string): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      model: this.model,
      text,
      voice: this.voice,
      language: this.language,
      audio_format: this.audioFormat,
      sample_rate: this.sampleRate,
    };
    if (this.bitrate !== undefined) payload.bitrate = this.bitrate;
    if (this.speed !== undefined) payload.speed = this.speed;
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
   * Synthesize text and yield raw audio chunks as they arrive from Soniox.
   * With the default `audioFormat="pcm_s16le"` these are PCM_S16LE bytes at
   * the configured `sampleRate`; with `pcm_mulaw` (see {@link SonioxTTS.forTwilio})
   * they are carrier-native G.711 μ-law bytes.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(this.baseUrl, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(this.buildPayload(text)),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Soniox TTS error ${response.status}: ${body}`);
    }
    if (!response.body) {
      throw new Error('Soniox TTS: no response body');
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
