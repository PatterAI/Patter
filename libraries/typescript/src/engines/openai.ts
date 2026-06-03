/** OpenAI Realtime engine — marker class for Patter client dispatch. */

import type { RealtimeTurnDetection } from '../types';
import { validateRealtimeTurnDetection } from '../providers/openai-realtime';

/** Constructor options for the OpenAI `Realtime` engine marker. */
export interface RealtimeOptions {
  /** API key. Falls back to OPENAI_API_KEY env var when omitted. */
  apiKey?: string;
  /**
   * Realtime model. Defaults to ``gpt-realtime-mini`` (bumped from the
   * deprecated ``gpt-4o-mini-realtime-preview`` on 2026-05-25 for
   * parity with the Python SDK and the GA Realtime API surface).
   */
  model?: string;
  /** Voice preset. Defaults to alloy. */
  voice?: string;
  /**
   * Reasoning-effort tier for `gpt-realtime-2`. When omitted the
   * `session.reasoning` field is not sent and the server default applies.
   * OpenAI recommends `"low"` for production voice flows — higher tiers add
   * measurable per-turn latency. Has no effect on models that ignore the
   * field.
   */
  reasoningEffort?: 'minimal' | 'low' | 'medium' | 'high';
  /**
   * Override for the Realtime session's `input_audio_transcription.model`.
   * Omit to keep the adapter default (`whisper-1`). Use
   * `"gpt-realtime-whisper"` for low-latency transcript partials,
   * `"gpt-4o-transcribe"` for higher accuracy.
   */
  inputAudioTranscriptionModel?: string;
  /**
   * Input noise reduction for speakerphone / conference audio. `undefined`
   * (default) omits the field (no reduction). `"far_field"` recommended for
   * phone / speakerphone calls; `"near_field"` for a handset close to the
   * mouth. Mirrors `openai_realtime_noise_reduction` on `Patter.agent()`.
   */
  noiseReduction?: 'near_field' | 'far_field';
  /**
   * Turn-detection tuning. `undefined` (default) keeps the adapter's
   * current hardcoded `server_vad` / threshold `0.5` / silence 300 ms.
   * Raise threshold or switch to `semantic_vad` eagerness `'low'` to stop
   * speakerphone noise from triggering false barge-ins.
   *
   * Maps to `turn_detection` on the Python `engines.openai.Realtime` marker;
   * propagates to `realtimeTurnDetection` on `AgentOptions`.
   */
  turnDetection?: RealtimeTurnDetection;
}

/**
 * OpenAI Realtime engine marker.
 *
 * @example
 * ```ts
 * import * as openai from "getpatter/engines/openai";
 * const engine = new openai.Realtime();                     // reads OPENAI_API_KEY
 * const engine = new openai.Realtime({ voice: "alloy" });
 * const engine = new openai.Realtime({
 *   model: "gpt-realtime-2",
 *   reasoningEffort: "low",                                  // gpt-realtime-2 only
 *   inputAudioTranscriptionModel: "gpt-realtime-whisper",
 * });
 * ```
 */
export class Realtime {
  readonly kind = "openai_realtime" as const;
  readonly apiKey: string;
  readonly model: string;
  readonly voice: string;
  readonly reasoningEffort?: 'minimal' | 'low' | 'medium' | 'high';
  readonly inputAudioTranscriptionModel?: string;
  readonly noiseReduction?: 'near_field' | 'far_field';
  readonly turnDetection?: RealtimeTurnDetection;

  constructor(opts: RealtimeOptions = {}) {
    const key = opts.apiKey ?? process.env.OPENAI_API_KEY;
    if (!key) {
      throw new Error(
        "OpenAI Realtime requires an apiKey. Pass { apiKey: 'sk-...' } or " +
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
    this.model = opts.model ?? "gpt-realtime-mini";
    this.voice = opts.voice ?? "alloy";
    this.reasoningEffort = opts.reasoningEffort;
    this.inputAudioTranscriptionModel = opts.inputAudioTranscriptionModel;
    this.noiseReduction = opts.noiseReduction;
    this.turnDetection = opts.turnDetection;
  }
}
