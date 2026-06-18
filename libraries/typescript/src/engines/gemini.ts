/**
 * Gemini Live engine — marker class for Patter client dispatch.
 *
 * Selects Google's Gemini Live native-audio API (audio-in → audio-out,
 * emotion-aware). Separate marker from the OpenAI / ElevenLabs engines
 * because the client dispatches to {@link import('../providers/gemini-live').GeminiLiveAdapter}
 * when this marker is passed to ``Patter.agent({ engine })``.
 *
 * Like the other engine markers this is a tiny immutable config object: it
 * carries credentials / voice / model only. The runtime session lives in
 * ``GeminiLiveAdapter`` (constructed server-side by ``buildAIAdapter``).
 *
 * @example
 * ```ts
 * import { Patter, Twilio, GeminiLive } from "getpatter";
 *
 * const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+1..." });
 * const agent = phone.agent({
 *   engine: new GeminiLive({ voice: "Puck", model: "gemini-3.1-flash-live-preview" }),
 *   systemPrompt: "You are a friendly receptionist.",
 *   firstMessage: "Hello! How can I help?",
 * });
 * ```
 */

/** Constructor options for the `GeminiLive` engine marker. */
export interface GeminiLiveOptions {
  /** API key. Falls back to GEMINI_API_KEY / GOOGLE_API_KEY env vars when omitted. */
  apiKey?: string;
  /** Gemini Live model. Defaults to the adapter's native-audio default when omitted. */
  model?: string;
  /** Prebuilt voice name. Defaults to `Puck`. */
  voice?: string;
  /** Language code. Defaults to `en-US`. */
  language?: string;
  /** Sampling temperature. Defaults to `0.8`. */
  temperature?: number;
}

/**
 * Gemini Live engine marker — selects Google's native-audio Live API.
 */
export class GeminiLive {
  readonly kind = "gemini_live" as const;
  readonly apiKey: string;
  readonly model?: string;
  readonly voice?: string;
  readonly language?: string;
  readonly temperature?: number;

  constructor(opts: GeminiLiveOptions = {}) {
    const key =
      opts.apiKey ?? process.env.GEMINI_API_KEY ?? process.env.GOOGLE_API_KEY;
    if (!key) {
      throw new Error(
        "Gemini Live requires an apiKey. Pass { apiKey: '...' } or set " +
          "GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment.",
      );
    }
    this.apiKey = key;
    this.model = opts.model;
    this.voice = opts.voice;
    this.language = opts.language;
    this.temperature = opts.temperature;
  }
}
