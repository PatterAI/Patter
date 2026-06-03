/**
 * OpenAI Realtime 2 engine — marker class for Patter client dispatch.
 *
 * Wraps `gpt-realtime-2` (GA Realtime API). Separate marker from
 * {@link import('./openai').Realtime} because the GA endpoint speaks a
 * different `session.update` wire shape; the client dispatches to
 * `OpenAIRealtime2Adapter` when this marker is passed.
 */

import type { RealtimeTurnDetection } from '../types';
import { validateRealtimeTurnDetection } from '../providers/openai-realtime';

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
  /**
   * Input noise reduction for speakerphone / conference audio. `undefined`
   * (default) omits the field (no reduction). `"far_field"` recommended for
   * phone / speakerphone calls; `"near_field"` for a handset close to the
   * mouth. On the GA endpoint this is nested under
   * `audio.input.input_audio_noise_reduction: { type }`.
   * Mirrors `openai_realtime_noise_reduction` on `Patter.agent()`.
   */
  noiseReduction?: 'near_field' | 'far_field';
  /**
   * Turn-detection tuning. `undefined` (default) keeps the adapter's
   * current hardcoded `server_vad` / threshold `0.5` / silence 300 ms.
   * Raise threshold or switch to `semantic_vad` eagerness `'low'` to stop
   * speakerphone noise from triggering false barge-ins.
   *
   * Maps to `turn_detection` on the Python `engines.openai_realtime_2.Realtime2`
   * marker; propagates to `realtimeTurnDetection` on `AgentOptions`.
   */
  turnDetection?: RealtimeTurnDetection;
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
  readonly noiseReduction?: 'near_field' | 'far_field';
  readonly turnDetection?: RealtimeTurnDetection;

  constructor(opts: Realtime2Options = {}) {
    const key = opts.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        "OpenAI Realtime 2 requires an apiKey. Pass { apiKey: 'sk-...' } or " +
          "set OPENAI_API_KEY in the environment.",
      );
    }
    if (opts.noiseReduction !== undefined && opts.noiseReduction !== 'near_field' && opts.noiseReduction !== 'far_field') {
      throw new Error(
        `noiseReduction must be 'near_field' or 'far_field', got ${JSON.stringify(opts.noiseReduction)}`,
      );
    }
    validateRealtimeTurnDetection(opts.turnDetection);
    this.apiKey = key;
    this.model = opts.model ?? "gpt-realtime-2";
    this.voice = opts.voice ?? "alloy";
    this.reasoningEffort = opts.reasoningEffort;
    this.inputAudioTranscriptionModel = opts.inputAudioTranscriptionModel;
    this.noiseReduction = opts.noiseReduction;
    this.turnDetection = opts.turnDetection;
  }
}
