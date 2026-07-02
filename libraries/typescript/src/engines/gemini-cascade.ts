/**
 * Gemini Cascade engine — marker class for Patter client dispatch.
 *
 * Selects the cascade voice architecture: Gemini Live for STT + reasoning
 * (TEXT modality, returns transcript) piped into a Gemini TTS leg for
 * high-quality voice synthesis. The two legs communicate inside
 * {@link import('../providers/gemini-cascade').GeminiCascadeAdapter}.
 *
 * Like other engine markers this is a tiny immutable config object. The
 * runtime session lives in `GeminiCascadeAdapter` (constructed server-side
 * by `buildAIAdapter`).
 *
 * @example
 * ```ts
 * import { Patter, Twilio, GeminiCascade } from "getpatter";
 *
 * const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+1..." });
 * const agent = phone.agent({
 *   engine: new GeminiCascade({ voice: "Kore" }),
 *   systemPrompt: "You are a helpful assistant.",
 *   firstMessage: "Hi, how can I help you today?",
 * });
 * ```
 */

/** Constructor options for the `GeminiCascade` engine marker. */
export interface GeminiCascadeOptions {
  /** API key. Falls back to GEMINI_API_KEY / GOOGLE_API_KEY env vars when omitted. */
  apiKey?: string;
  /** Prebuilt TTS voice name. Defaults to `Kore`. */
  voice?: string;
  /** Gemini Live model (STT+brain leg, TEXT modality). Defaults to `gemini-3.1-flash-live-preview`. */
  liveModel?: string;
  /** Gemini TTS model (voice synthesis leg). Defaults to `gemini-3.1-flash-tts-preview`. */
  ttsModel?: string;
}

/**
 * Gemini Cascade engine marker — selects the Gemini Live (STT) + Gemini TTS
 * cascade architecture for higher-quality voice synthesis than native-audio mode.
 */
export class GeminiCascade {
  readonly kind = 'gemini_cascade' as const;
  readonly apiKey: string;
  readonly voice: string;
  readonly liveModel: string;
  readonly ttsModel: string;

  constructor(opts: GeminiCascadeOptions = {}) {
    const key =
      opts.apiKey ?? process.env.GEMINI_API_KEY ?? process.env.GOOGLE_API_KEY;
    if (!key) {
      throw new Error(
        'Gemini Cascade requires an apiKey. Pass { apiKey: \'...\' } or set ' +
          'GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment.',
      );
    }
    this.apiKey = key;
    this.voice = opts.voice ?? 'Kore';
    this.liveModel = opts.liveModel ?? 'gemini-3.1-flash-live-preview';
    this.ttsModel = opts.ttsModel ?? 'gemini-3.1-flash-tts-preview';
  }
}
