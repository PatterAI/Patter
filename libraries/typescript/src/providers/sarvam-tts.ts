/**
 * Sarvam AI TTS provider — HTTP REST one-shot endpoint.
 *
 * Sarvam's Bulbul models synthesize 11 Indian languages (Hindi, Bengali,
 * Tamil, Telugu, Kannada, Malayalam, Marathi, Gujarati, Punjabi, Odia, Indian
 * English) plus code-mixed text. This adapter targets the REST one-shot
 * endpoint `POST https://api.sarvam.ai/text-to-speech`, which returns JSON of
 * the form `{ "request_id": "...", "audios": ["<base64-audio>", ...] }` — we
 * base64-decode each entry and yield the raw bytes, mapping cleanly onto
 * Patter's `synthesize(text)` contract with no vendor SDK (just `fetch`).
 *
 * Sarvam also exposes a WebSocket streaming transport
 * (`wss://api.sarvam.ai/text-to-speech/ws?model=<model_id>`); like the Cartesia
 * adapter we use the REST path because it already meets Patter's TTFB target
 * while keeping the provider dependency-free.
 *
 * Auth is an API subscription key sent as the `api-subscription-key` HTTP
 * header (no Bearer scheme), read from the constructor or `SARVAM_API_KEY`.
 *
 * The default config requests `codec="linear16"` (raw PCM_S16LE) at 16 kHz so
 * the output drops straight into the Patter pipeline without transcoding. For
 * real phone calls the {@link SarvamTTS.forTwilio} / {@link SarvamTTS.forTelnyx}
 * factories request `mulaw` @ 8 kHz directly (Sarvam supports both natively),
 * so the pipeline takes the carrier-native passthrough path.
 *
 * Bulbul v3 is the default model (latest; 35+ voices; `temperature` control).
 * Pass `model: "bulbul:v2"` for the legacy generation, which instead exposes
 * `pitch` / `loudness` / `enablePreprocessing`. Those controls are gated
 * per-model so we never send fields the selected model rejects.
 */

import { getLogger } from '../logger';

const SARVAM_BASE_URL = 'https://api.sarvam.ai/text-to-speech';

/** Sarvam Bulbul TTS model families. */
export const SarvamModel = {
  BULBUL_V3: 'bulbul:v3',
  BULBUL_V2: 'bulbul:v2',
} as const;
export type SarvamModel = (typeof SarvamModel)[keyof typeof SarvamModel];

/**
 * `output_audio_codec` values accepted by the REST API. `LINEAR16` is
 * headerless 16-bit PCM (the pipeline-friendly default); `MULAW` / `ALAW` are
 * G.711 telephony codecs.
 */
export const SarvamAudioCodec = {
  WAV: 'wav',
  MP3: 'mp3',
  LINEAR16: 'linear16',
  MULAW: 'mulaw',
  ALAW: 'alaw',
  OPUS: 'opus',
  FLAC: 'flac',
  AAC: 'aac',
} as const;
export type SarvamAudioCodec = (typeof SarvamAudioCodec)[keyof typeof SarvamAudioCodec];

/**
 * BCP-47 `target_language_code` values for the 11 supported languages. Note
 * Sarvam uses `od-IN` for Odia (not the more common `or-IN`).
 */
export const SarvamLanguage = {
  HINDI: 'hi-IN',
  BENGALI: 'bn-IN',
  TAMIL: 'ta-IN',
  TELUGU: 'te-IN',
  KANNADA: 'kn-IN',
  MALAYALAM: 'ml-IN',
  MARATHI: 'mr-IN',
  GUJARATI: 'gu-IN',
  PUNJABI: 'pa-IN',
  ODIA: 'od-IN',
  ENGLISH: 'en-IN',
} as const;
export type SarvamLanguage = (typeof SarvamLanguage)[keyof typeof SarvamLanguage];

/** Output sample rates (Hz) accepted by the REST `speech_sample_rate`. */
export const SarvamSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_22050: 22050,
  HZ_24000: 24000,
  HZ_32000: 32000,
  HZ_44100: 44100,
  HZ_48000: 48000,
} as const;
export type SarvamSampleRate = (typeof SarvamSampleRate)[keyof typeof SarvamSampleRate];

/** Constructor options for {@link SarvamTTS}. */
export interface SarvamTTSOptions {
  /** Model id. Defaults to `"bulbul:v3"`. */
  model?: SarvamModel | string;
  /** Speaker name (lowercase). Defaults to `"shubh"` (a bulbul:v3 voice). */
  speaker?: string;
  /** BCP-47 language code, e.g. `"hi-IN"`, `"ta-IN"`, `"en-IN"`. Default `"en-IN"`. */
  language?: SarvamLanguage | string;
  /**
   * Output audio codec. Default `linear16` (raw PCM16). Set `mulaw` with
   * `sampleRate=8000` to emit Twilio/Telnyx wire-native μ-law directly — the
   * pipeline then skips resampling and PCM → μ-law encoding entirely (see
   * {@link SarvamTTS.forTwilio}).
   */
  codec?: SarvamAudioCodec | string;
  /** Output sample rate in Hz. Defaults to 16000. */
  sampleRate?: SarvamSampleRate | number;
  /** Speaking pace multiplier (v3: 0.5–2.0, v2: 0.3–3.0; default 1.0 server-side). */
  pace?: number;
  /** Pitch shift (bulbul:v2 ONLY; −0.75 to 0.75). Ignored on v3. */
  pitch?: number;
  /** Loudness multiplier (bulbul:v2 ONLY; 0.3–3.0). Ignored on v3. */
  loudness?: number;
  /** Sampling temperature (bulbul:v3 ONLY; 0.01–2.0). Ignored on v2. */
  temperature?: number;
  /** Normalise numbers/entities (bulbul:v2 ONLY). Ignored on v3. */
  enablePreprocessing?: boolean;
  /** Custom pronunciation dictionary id (bulbul:v3 ONLY). Ignored on v2. */
  dictId?: string;
  /** Override the REST endpoint. */
  baseUrl?: string;
}

/** Audio format declared back to the pipeline sender via {@link SarvamTTS.sourceAudioFormat}. */
type SourceAudioFormat = { encoding: 'pcm_s16le' | 'mulaw' | 'alaw'; sampleRate: number };

/** Sarvam TTS provider backed by the HTTP REST `/text-to-speech` endpoint. */
export class SarvamTTS {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'sarvam';
  private readonly apiKey: string;
  private readonly model: string;
  private readonly speaker: string;
  private readonly language: string;
  private readonly codec: string;
  private readonly sampleRate: number;
  private readonly pace?: number;
  private readonly pitch?: number;
  private readonly loudness?: number;
  private readonly temperature?: number;
  private readonly enablePreprocessing?: boolean;
  private readonly dictId?: string;
  private readonly baseUrl: string;

  constructor(apiKey: string, opts: SarvamTTSOptions = {}) {
    if (!apiKey) {
      throw new Error('Sarvam TTS: apiKey is required');
    }
    this.apiKey = apiKey;
    this.model = opts.model ?? SarvamModel.BULBUL_V3;
    this.speaker = opts.speaker ?? 'shubh';
    this.language = opts.language ?? SarvamLanguage.ENGLISH;
    this.codec = opts.codec ?? SarvamAudioCodec.LINEAR16;
    this.sampleRate = opts.sampleRate ?? SarvamSampleRate.HZ_16000;
    this.pace = opts.pace;
    this.pitch = opts.pitch;
    this.loudness = opts.loudness;
    this.temperature = opts.temperature;
    this.enablePreprocessing = opts.enablePreprocessing;
    this.dictId = opts.dictId;
    this.baseUrl = opts.baseUrl ?? SARVAM_BASE_URL;
  }

  /**
   * Declare the audio format this adapter emits, so the pipeline sender can
   * derive the correct resample ratio (or skip it for μ-law/a-law passthrough)
   * instead of assuming a fixed 16 kHz source. See `audio/format.ts`.
   */
  sourceAudioFormat(): SourceAudioFormat {
    if (this.codec === SarvamAudioCodec.MULAW) {
      return { encoding: 'mulaw', sampleRate: this.sampleRate };
    }
    if (this.codec === SarvamAudioCodec.ALAW) {
      return { encoding: 'alaw', sampleRate: this.sampleRate };
    }
    return { encoding: 'pcm_s16le', sampleRate: this.sampleRate };
  }

  /**
   * Construct an instance pre-configured for Twilio Media Streams.
   *
   * Emits `mulaw` @ 8 kHz — exactly Twilio's wire codec — so the pipeline takes
   * the passthrough path: zero resampling, zero PCM → μ-law encoding. Sarvam
   * supports μ-law / 8 kHz natively, so this is a true carrier-native path.
   */
  static forTwilio(
    apiKey: string,
    options: Omit<SarvamTTSOptions, 'codec' | 'sampleRate'> = {},
  ): SarvamTTS {
    return new SarvamTTS(apiKey, {
      ...options,
      codec: SarvamAudioCodec.MULAW,
      sampleRate: SarvamSampleRate.HZ_8000,
    });
  }

  /**
   * Construct an instance pre-configured for Telnyx bidirectional media.
   *
   * Telnyx pins the wire to PCMU/μ-law @ 8 kHz, so emitting `mulaw` @ 8 kHz
   * flows end-to-end with zero resampling — the same passthrough win as
   * {@link SarvamTTS.forTwilio}.
   */
  static forTelnyx(
    apiKey: string,
    options: Omit<SarvamTTSOptions, 'codec' | 'sampleRate'> = {},
  ): SarvamTTS {
    return new SarvamTTS(apiKey, {
      ...options,
      codec: SarvamAudioCodec.MULAW,
      sampleRate: SarvamSampleRate.HZ_8000,
    });
  }

  /** True when the configured model is the legacy `bulbul:v2`. */
  private isV2(): boolean {
    return this.model === SarvamModel.BULBUL_V2;
  }

  /** Build the JSON payload for the Sarvam `/text-to-speech` endpoint. */
  private buildPayload(text: string): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      text,
      target_language_code: this.language,
      model: this.model,
      speaker: this.speaker,
      output_audio_codec: this.codec,
      speech_sample_rate: this.sampleRate,
    };

    if (this.pace !== undefined) payload.pace = this.pace;

    // Per-model controls: pitch / loudness / enable_preprocessing are
    // bulbul:v2-only; temperature / dict_id are bulbul:v3-only. Gate them so we
    // never POST a field the selected model rejects.
    if (this.isV2()) {
      if (this.pitch !== undefined) payload.pitch = this.pitch;
      if (this.loudness !== undefined) payload.loudness = this.loudness;
      if (this.enablePreprocessing !== undefined)
        payload.enable_preprocessing = this.enablePreprocessing;
    } else {
      if (this.temperature !== undefined) payload.temperature = this.temperature;
      if (this.dictId !== undefined) payload.dict_id = this.dictId;
    }

    return payload;
  }

  /**
   * Pre-call HTTP warmup for the Sarvam TTS API.
   *
   * Issues a lightweight `GET` against the API origin so DNS, TLS, and HTTP/2
   * are already up by the time the first `synthesizeStream()` POST lands.
   * Best-effort: 5 s timeout, all exceptions swallowed at debug level.
   *
   * Billing safety: Sarvam does not document a free voice/metadata GET, so this
   * only primes the connection against the API origin root — it never hits the
   * synthesis endpoint, so no characters are billed. Synthesis is billed only
   * when `POST /text-to-speech` runs with non-empty `text`.
   */
  async warmup(): Promise<void> {
    try {
      const origin = new URL(this.baseUrl).origin + '/';
      await fetch(origin, {
        method: 'GET',
        headers: { 'api-subscription-key': this.apiKey },
        signal: AbortSignal.timeout(5_000),
      });
    } catch (err) {
      getLogger().debug(`Sarvam TTS warmup failed (best-effort): ${String(err)}`);
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
   * Synthesize text and yield decoded audio chunks. With the default
   * `codec="linear16"` these are raw PCM_S16LE bytes at `sampleRate`. The REST
   * endpoint returns the whole clip in one JSON response (`audios` is a list of
   * base64 strings); we decode and yield each entry in order.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(this.baseUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'api-subscription-key': this.apiKey,
      },
      body: JSON.stringify(this.buildPayload(text)),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Sarvam TTS error ${response.status}: ${body}`);
    }

    const data = (await response.json()) as unknown;
    for (const audioB64 of iterAudioB64(data)) {
      const decoded = Buffer.from(audioB64, 'base64');
      if (decoded.length > 0) yield decoded;
    }
  }
}

/**
 * Extract the base64 audio strings from a Sarvam REST response. Accepts the
 * documented `{ audios: [...] }` shape and is defensive about a single-string
 * `audios` value. Returns `[]` for anything unexpected so the caller yields
 * nothing rather than throwing on a malformed body.
 */
function iterAudioB64(data: unknown): string[] {
  if (typeof data !== 'object' || data === null) return [];
  const audios = (data as { audios?: unknown }).audios;
  if (typeof audios === 'string') return audios ? [audios] : [];
  if (Array.isArray(audios)) {
    return audios.filter((a): a is string => typeof a === 'string' && a.length > 0);
  }
  return [];
}
