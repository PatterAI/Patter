/**
 * xAI Text-to-Speech provider — HTTP one-shot `/tts` bytes endpoint.
 *
 * **Beta: validated against the xAI TTS API spec; not yet exercised against a
 * live phone call.**
 *
 * Calls `POST https://api.x.ai/v1/tts`. The response body is raw audio bytes
 * whose codec matches the requested `output_format.codec` (e.g. `pcm`,
 * `mulaw`, `mp3`). This maps cleanly onto Patter's `synthesizeStream(text)`
 * contract and keeps the provider dependency-free (just `fetch`).
 *
 * xAI also exposes a WebSocket streaming variant (`wss://api.x.ai/v1/tts`) for
 * lower TTFB; this provider uses the REST bytes path which already satisfies
 * the pipeline's streaming contract — the same trade-off `SonioxTTS` makes. A
 * WebSocket variant would be a separate `xai_ws_tts` provider (precedent:
 * `elevenlabs_ws`).
 *
 * Credential: `XAI_API_KEY`. REST auth is an `Authorization: Bearer <key>` header.
 *
 * **Telephony optimization** — the constructor default `codec="pcm"` @
 * `sampleRate=16000` is correct for web playback and 16 kHz pipelines. For real
 * phone calls use the carrier-specific factories:
 *
 * - {@link XaiTTS.forTwilio} emits `mulaw` @ 8 kHz — Twilio's exact wire codec —
 *   so the pipeline skips resampling AND PCM → μ-law encoding (bit-clean
 *   passthrough). xAI supports G.711 μ-law @ 8 kHz natively.
 * - {@link XaiTTS.forTelnyx} emits `pcm` @ 16 kHz (the SDK's Telnyx pipeline rate).
 */

import { getLogger } from '../logger';
import { XAI_DEFAULT_VOICE, normalizeXaiVoice } from './xai-voices';

/** xAI one-shot TTS REST endpoint. */
export const XAI_TTS_REST_URL = 'https://api.x.ai/v1/tts';
/** xAI custom-voice creation REST endpoint. */
export const XAI_CUSTOM_VOICES_URL = 'https://api.x.ai/v1/custom-voices';
// WebSocket streaming endpoint (documented for reference; not used by this
// HTTP-bytes adapter).
export const XAI_TTS_WS_URL = 'wss://api.x.ai/v1/tts';

/**
 * Output codecs accepted by the xAI `output_format.codec` field. `MULAW` /
 * `ALAW` @ 8 kHz are the carrier-native G.711 codecs (telephony passthrough);
 * `PCM` targets 16 kHz pipelines; `MP3` / `WAV` target web/dashboard use.
 */
export const XaiTTSCodec = {
  PCM: 'pcm',
  MULAW: 'mulaw',
  ALAW: 'alaw',
  WAV: 'wav',
  MP3: 'mp3',
} as const;
export type XaiTTSCodec = (typeof XaiTTSCodec)[keyof typeof XaiTTSCodec];

/** Sample rates (Hz) accepted by the xAI `output_format.sample_rate` field. */
export const XaiTTSSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_22050: 22050,
  HZ_24000: 24000,
  HZ_44100: 44100,
  HZ_48000: 48000,
} as const;
export type XaiTTSSampleRate = (typeof XaiTTSSampleRate)[keyof typeof XaiTTSSampleRate];

/** Max characters accepted per one-shot TTS request. */
const XAI_TTS_MAX_CHARS = 15000;

/** Constructor options for {@link XaiTTS}. */
export interface XaiTTSOptions {
  /** Voice id (built-in or custom, case-insensitive). Defaults to `"eve"`. */
  voice?: string;
  /**
   * BCP-47 language tag, e.g. `"en"`, `"it"`, `"pt-BR"`, or `"auto"` for
   * autodetection. Required by xAI; defaults to `"auto"`.
   */
  language?: string;
  /**
   * Output codec. Default `pcm` (raw linear PCM16). Set `mulaw` with
   * `sampleRate=8000` to emit Twilio/Telnyx wire-native μ-law directly — the
   * pipeline then skips resampling and PCM → μ-law encoding (see
   * {@link XaiTTS.forTwilio}).
   */
  codec?: XaiTTSCodec | string;
  /** Output sample rate in Hz. Defaults to 16000. */
  sampleRate?: XaiTTSSampleRate | number;
  /** Bit rate (bits/sec) — MP3 only. */
  bitRate?: number;
  /** Speaking-rate multiplier in `[0.7, 1.5]` (default `1.0`). Omitted when unset. */
  speed?: number;
  /** Streaming latency optimization level `0 | 1 | 2` (xAI default `0`). */
  optimizeStreamingLatency?: number;
  /** Normalise numbers/abbreviations/symbols to spoken form before synthesis. Default `false`. */
  textNormalization?: boolean;
  /** Override the REST endpoint (e.g. for tests). */
  baseUrl?: string;
}

/** xAI TTS adapter backed by the HTTP one-shot `/tts` bytes endpoint. */
export class XaiTTS {
  /** Stable pricing/dashboard key — read by stream-handler/metrics. */
  static readonly providerKey = 'xai_tts';

  private readonly apiKey: string;
  private readonly voice: string;
  private readonly language: string;
  private readonly codec: string;
  private readonly sampleRate: number;
  private readonly bitRate?: number;
  private readonly speed?: number;
  private readonly optimizeStreamingLatency?: number;
  private readonly textNormalization: boolean;
  private readonly baseUrl: string;

  constructor(apiKey: string, opts: XaiTTSOptions = {}) {
    if (!apiKey) {
      throw new Error('xAI TTS: apiKey is required');
    }
    this.apiKey = apiKey;
    // Normalized here (not just at send time) so `this.voice` always reflects
    // exactly what goes on the wire — built-in ids lowercase, custom
    // `voice_id`s trimmed but otherwise untouched.
    this.voice = normalizeXaiVoice(opts.voice ?? XAI_DEFAULT_VOICE);
    this.language = opts.language ?? 'auto';
    this.codec = opts.codec ?? XaiTTSCodec.PCM;
    this.sampleRate = opts.sampleRate ?? XaiTTSSampleRate.HZ_16000;
    this.bitRate = opts.bitRate;
    this.speed = opts.speed;
    this.optimizeStreamingLatency = opts.optimizeStreamingLatency;
    this.textNormalization = opts.textNormalization ?? false;
    this.baseUrl = opts.baseUrl ?? XAI_TTS_REST_URL;
  }

  /**
   * Declare the audio format this adapter emits, so the pipeline sender can
   * derive the correct resample ratio (or skip it for μ-law passthrough)
   * instead of assuming a fixed 16 kHz source.
   *
   * - `mulaw` → `{ encoding: 'mulaw', sampleRate }` (carrier-native).
   * - `alaw`  → `{ encoding: 'alaw',  sampleRate }`.
   * - everything else → `{ encoding: 'pcm_s16le', sampleRate }`.
   */
  sourceAudioFormat(): {
    encoding: 'pcm_s16le' | 'mulaw' | 'alaw';
    sampleRate: number;
  } {
    if (this.codec === XaiTTSCodec.MULAW) {
      return { encoding: 'mulaw', sampleRate: this.sampleRate };
    }
    if (this.codec === XaiTTSCodec.ALAW) {
      return { encoding: 'alaw', sampleRate: this.sampleRate };
    }
    return { encoding: 'pcm_s16le', sampleRate: this.sampleRate };
  }

  /**
   * Construct an instance pre-configured for Twilio Media Streams.
   *
   * Emits `mulaw` @ 8 kHz — exactly Twilio's wire codec — so the pipeline takes
   * the passthrough path: zero resampling, zero PCM → μ-law encoding, bit-clean
   * audio. xAI supports μ-law @ 8 kHz natively (true carrier-native synthesis).
   */
  static forTwilio(
    apiKey: string,
    options: Omit<XaiTTSOptions, 'sampleRate' | 'codec'> = {},
  ): XaiTTS {
    return new XaiTTS(apiKey, {
      ...options,
      codec: XaiTTSCodec.MULAW,
      sampleRate: XaiTTSSampleRate.HZ_8000,
    });
  }

  /**
   * Construct an instance pre-configured for Telnyx bidirectional media.
   *
   * Emits `pcm` @ 16 kHz — the SDK's Telnyx pipeline rate — so the audio flows
   * through the standard PCM16 path with no codec transcode.
   */
  static forTelnyx(
    apiKey: string,
    options: Omit<XaiTTSOptions, 'sampleRate' | 'codec'> = {},
  ): XaiTTS {
    return new XaiTTS(apiKey, {
      ...options,
      codec: XaiTTSCodec.PCM,
      sampleRate: XaiTTSSampleRate.HZ_16000,
    });
  }

  /** Build the JSON payload for the xAI `/tts` endpoint. */
  private buildPayload(text: string): Record<string, unknown> {
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
    if (this.optimizeStreamingLatency !== undefined) {
      payload.optimize_streaming_latency = this.optimizeStreamingLatency;
    }
    if (this.textNormalization) payload.text_normalization = true;
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
   * Synthesize text and yield raw audio chunks as they arrive from xAI. With
   * the default `codec="pcm"` these are PCM16 bytes at the configured
   * `sampleRate`; with `mulaw` (see {@link XaiTTS.forTwilio}) they are
   * carrier-native G.711 μ-law bytes.
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    if (text.length > XAI_TTS_MAX_CHARS) {
      // xAI rejects >15,000 chars with HTTP 400. We do NOT silently truncate —
      // send as-is and let the API error surface, but warn so the cause is clear.
      getLogger().warn(
        `xAI TTS: text length ${text.length} exceeds the ${XAI_TTS_MAX_CHARS}-char ` +
          'per-request limit; the API will reject it. Split long text upstream.',
      );
    }
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
}

/** Options for {@link xaiCreateCustomVoice}. */
export interface XaiCreateCustomVoiceOptions {
  /** Display name for the cloned voice. */
  readonly name: string;
  /** BCP-47 language of the reference clip (e.g. `"en"`). */
  readonly language: string;
  /** Reference audio clip bytes (WAV, ≤120 s). */
  readonly audio: Buffer | Uint8Array;
  /** API key. Falls back to `XAI_API_KEY` when omitted. */
  readonly apiKey?: string;
  /** Filename for the multipart `file` part. Defaults to `reference.wav`. */
  readonly filename?: string;
  /** Override the REST endpoint (e.g. for tests). */
  readonly baseUrl?: string;
}

interface XaiCustomVoiceResponse {
  voice_id?: string;
}

/**
 * Clone a custom voice from a short reference clip via xAI's Custom Voices API
 * (`POST /v1/custom-voices`). Returns the resulting `voice_id`, usable as the
 * `voice` on {@link XaiTTS} and the xAI Voice Agent engine.
 *
 * **Beta.** Multipart order matters: the scalar `name` / `language` fields are
 * appended FIRST and the `file` part (audio/wav, ≤120 s clip) LAST.
 */
export async function xaiCreateCustomVoice(
  options: XaiCreateCustomVoiceOptions,
): Promise<string> {
  const apiKey = options.apiKey ?? process.env.XAI_API_KEY;
  if (!apiKey) {
    throw new Error(
      "xAI custom voice requires an apiKey. Pass { apiKey: '...' } or set XAI_API_KEY.",
    );
  }

  const bytes =
    options.audio instanceof Buffer ? options.audio : Buffer.from(options.audio);
  const arrayBuffer = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;

  const form = new FormData();
  // Scalar fields FIRST, file LAST.
  form.append('name', options.name);
  form.append('language', options.language);
  form.append(
    'file',
    new Blob([arrayBuffer], { type: 'audio/wav' }),
    options.filename ?? 'reference.wav',
  );

  const response = await fetch(options.baseUrl ?? XAI_CUSTOM_VOICES_URL, {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}` },
    body: form,
    signal: AbortSignal.timeout(120_000),
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`xAI custom voice error ${response.status}: ${body}`);
  }

  const data = (await response.json()) as XaiCustomVoiceResponse;
  if (!data.voice_id) {
    throw new Error('xAI custom voice: response did not include a voice_id');
  }
  return data.voice_id;
}
