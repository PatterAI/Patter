/** OpenAI Realtime engine — marker class for Patter client dispatch. */

/** Constructor options for the OpenAI `Realtime` engine marker. */
export interface RealtimeOptions {
  /** API key. Falls back to OPENAI_API_KEY env var when omitted. */
  apiKey?: string;
  /** Realtime model. Defaults to gpt-4o-mini-realtime-preview. */
  model?: string;
  /** Voice preset. Defaults to alloy. */
  voice?: string;
}

/**
 * OpenAI Realtime engine marker.
 *
 * @example
 * ```ts
 * import * as openai from "getpatter/engines/openai";
 * const engine = new openai.Realtime();                     // reads OPENAI_API_KEY
 * const engine = new openai.Realtime({ voice: "alloy" });
 * ```
 */
export class Realtime {
  readonly kind = "openai_realtime" as const;
  readonly apiKey: string;
  readonly model: string;
  readonly voice: string;

  constructor(opts: RealtimeOptions = {}) {
    const key = opts.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        "OpenAI Realtime requires an apiKey. Pass { apiKey: 'sk-...' } or " +
          "set OPENAI_API_KEY in the environment.",
      );
    }
    this.apiKey = key;
    this.model = opts.model ?? "gpt-4o-mini-realtime-preview";
    this.voice = opts.voice ?? "alloy";
  }
}
