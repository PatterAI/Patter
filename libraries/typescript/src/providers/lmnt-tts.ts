/**
 * LMNT TTS provider — HTTP `/v1/ai/speech/bytes` endpoint.
 *
 * Defaults to `format='raw'` (PCM_S16LE) at 16 kHz so the output drops
 * directly into Patter's telephony pipeline without transcoding.
 */

const LMNT_BASE_URL = 'https://api.lmnt.com/v1/ai/speech/bytes';

/** Supported LMNT audio output formats. `RAW` is PCM_S16LE. */
export const LMNTAudioFormat = {
  AAC: 'aac',
  MP3: 'mp3',
  MULAW: 'mulaw',
  RAW: 'raw',
  WAV: 'wav',
} as const;
export type LMNTAudioFormat = (typeof LMNTAudioFormat)[keyof typeof LMNTAudioFormat];

/** LMNT TTS model families. */
export const LMNTModel = {
  BLIZZARD: 'blizzard',
  AURORA: 'aurora',
} as const;
export type LMNTModel = (typeof LMNTModel)[keyof typeof LMNTModel];

/** Supported PCM sample rates for LMNT raw output. */
export const LMNTSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_24000: 24000,
} as const;
export type LMNTSampleRate = (typeof LMNTSampleRate)[keyof typeof LMNTSampleRate];

/** Constructor options for {@link LMNTTTS}. */
export interface LMNTTTSOptions {
  model?: LMNTModel;
  voice?: string;
  language?: string;
  format?: LMNTAudioFormat;
  sampleRate?: LMNTSampleRate;
  temperature?: number;
  topP?: number;
  baseUrl?: string;
}

/** LMNT TTS adapter backed by the `/v1/ai/speech/bytes` HTTP streaming endpoint. */
export class LMNTTTS {
  private readonly apiKey: string;
  private readonly model: LMNTModel;
  private readonly voice: string;
  private readonly language: string;
  private readonly format: LMNTAudioFormat;
  private readonly sampleRate: LMNTSampleRate;
  private readonly temperature: number;
  private readonly topP: number;
  private readonly baseUrl: string;

  constructor(apiKey: string, opts: LMNTTTSOptions = {}) {
    this.apiKey = apiKey;
    this.model = opts.model ?? LMNTModel.BLIZZARD;
    this.voice = opts.voice ?? 'leah';
    // Language defaults: blizzard => auto, else => en.
    this.language =
      opts.language ?? (this.model === LMNTModel.BLIZZARD ? 'auto' : 'en');
    this.format = opts.format ?? LMNTAudioFormat.RAW;
    this.sampleRate = opts.sampleRate ?? LMNTSampleRate.HZ_16000;
    this.temperature = opts.temperature ?? 1.0;
    this.topP = opts.topP ?? 0.8;
    this.baseUrl = opts.baseUrl ?? LMNT_BASE_URL;
  }

  private buildPayload(text: string): Record<string, unknown> {
    return {
      text,
      voice: this.voice,
      language: this.language,
      sample_rate: this.sampleRate,
      model: this.model,
      format: this.format,
      temperature: this.temperature,
      top_p: this.topP,
    };
  }

  /** Synthesize text and return the concatenated audio buffer. */
  async synthesize(text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of this.synthesizeStream(text)) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /** Yield audio chunks as they arrive — raw PCM_S16LE by default. */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const response = await fetch(this.baseUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-API-Key': this.apiKey,
      },
      body: JSON.stringify(this.buildPayload(text)),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`LMNT TTS error ${response.status}: ${body}`);
    }

    if (!response.body) {
      throw new Error('LMNT TTS: no response body');
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
