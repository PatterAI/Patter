/**
 * OpenAI Realtime 2 engine — marker class for Patter client dispatch.
 *
 * Wraps `gpt-realtime-2` (GA Realtime API). Separate marker from
 * {@link import('./openai').Realtime} because the GA endpoint speaks a
 * different `session.update` wire shape; the client dispatches to
 * `OpenAIRealtime2Adapter` when this marker is passed.
 */

/** Constructor options for the OpenAI `Realtime2` engine marker. */
export interface Realtime2Options {
  /** API key. Falls back to OPENAI_API_KEY env var when omitted. */
  apiKey?: string;
  /** GA Realtime model. Defaults to `gpt-realtime-2`. */
  model?: string;
  /** Voice preset. Defaults to alloy. */
  voice?: string;
  /**
   * Reasoning-effort tier. When omitted the field is not sent and the
   * server default applies. OpenAI recommends `"low"` for production
   * voice flows — higher tiers add measurable per-turn latency.
   */
  reasoningEffort?: 'minimal' | 'low' | 'medium' | 'high';
  /**
   * Override for `audio.input.transcription.model`. Omit to keep the
   * adapter default (`whisper-1`). Use `"gpt-realtime-whisper"` for
   * low-latency transcript partials.
   */
  inputAudioTranscriptionModel?: string;
}

/**
 * OpenAI Realtime 2 engine marker — selects `gpt-realtime-2` on the GA
 * Realtime API.
 *
 * @example
 * ```ts
 * import { Patter, Twilio, OpenAIRealtime2 } from "getpatter";
 *
 * const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+1..." });
 * const agent = phone.agent({
 *   engine: new OpenAIRealtime2({ reasoningEffort: "low" }),
 *   systemPrompt: "You are a friendly receptionist.",
 *   firstMessage: "Hello! How can I help?",
 * });
 * ```
 */
export class Realtime2 {
  readonly kind = "openai_realtime_2" as const;
  readonly apiKey: string;
  readonly model: string;
  readonly voice: string;
  readonly reasoningEffort?: 'minimal' | 'low' | 'medium' | 'high';
  readonly inputAudioTranscriptionModel?: string;

  constructor(opts: Realtime2Options = {}) {
    const key = opts.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        "OpenAI Realtime 2 requires an apiKey. Pass { apiKey: 'sk-...' } or " +
          "set OPENAI_API_KEY in the environment.",
      );
    }
    this.apiKey = key;
    this.model = opts.model ?? "gpt-realtime-2";
    this.voice = opts.voice ?? "alloy";
    this.reasoningEffort = opts.reasoningEffort;
    this.inputAudioTranscriptionModel = opts.inputAudioTranscriptionModel;
  }
}
