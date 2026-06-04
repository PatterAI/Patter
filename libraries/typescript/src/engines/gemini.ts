/** Gemini Live engine — marker class for Patter client dispatch. */

/** Constructor options for the Gemini Live engine marker. */
export interface GeminiLiveOptions {
  /** API key. Falls back to GEMINI_API_KEY env var when omitted. */
  apiKey?: string;
  /** Voice preset. Defaults to Puck. */
  voice?: string;
  /** Gemini Live model. Defaults to gemini-2.5-flash-native-audio-preview-09-2025. */
  model?: string;
  /** Input (microphone) sample rate in Hz. Defaults to 16000. */
  inputSampleRate?: number;
  /** Output (playback) sample rate in Hz. Defaults to 24000. */
  outputSampleRate?: number;
}

/**
 * Gemini Live engine marker.
 *
 * @example
 * ```ts
 * import { GeminiLive } from "getpatter/engines/gemini";
 * const engine = new GeminiLive();                     // reads GEMINI_API_KEY
 * const engine = new GeminiLive({ voice: "Puck" });
 * ```
 */
export class GeminiLive {
  readonly kind = 'gemini_live' as const;
  readonly apiKey: string;
  readonly voice: string;
  readonly model: string;
  readonly inputSampleRate: number;
  readonly outputSampleRate: number;

  constructor(opts: GeminiLiveOptions = {}) {
    const key = opts.apiKey ?? process.env.GEMINI_API_KEY;
    if (!key) {
      throw new Error(
        "Gemini Live requires an apiKey. Pass { apiKey: '...' } or set GEMINI_API_KEY.",
      );
    }
    this.apiKey = key;
    this.voice = opts.voice ?? 'Puck';
    this.model = opts.model ?? 'gemini-2.5-flash-native-audio-preview-09-2025';
    this.inputSampleRate = opts.inputSampleRate ?? 16000;
    this.outputSampleRate = opts.outputSampleRate ?? 24000;
  }
}
