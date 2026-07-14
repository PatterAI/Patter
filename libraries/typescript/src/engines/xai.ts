/**
 * xAI Grok Voice Agent engine — marker class for Patter client dispatch.
 *
 * **Beta: validated against the xAI Voice Agent API spec; not yet exercised
 * against a live phone call.**
 *
 * Selects xAI's Voice Agent (Grok Voice) speech-to-speech API. xAI advertises
 * an "Migrating from OpenAI Realtime" path — the GA event schema, session
 * structure, and client/server events are compatible with OpenAI's Realtime GA
 * API, so the session is driven with the same `session.update` /
 * `response.create` / streaming-delta wire shape. The runtime lives in
 * {@link import('../providers/xai-realtime').XaiRealtimeAdapter}, which
 * subclasses `OpenAIRealtime2Adapter` and swaps the endpoint + grafts the
 * xAI-specific session keys.
 *
 * Like the other engine markers this is a tiny immutable config object: it
 * carries credentials / voice / model plus the xAI extensions. The session is
 * constructed server-side by `buildAIAdapter`.
 *
 * @example
 * ```ts
 * import { Patter, Twilio, XaiRealtime } from "getpatter";
 *
 * const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+1..." });
 * const agent = phone.agent({
 *   engine: new XaiRealtime({ voice: "eve" }), // reads XAI_API_KEY
 *   systemPrompt: "You are a friendly receptionist.",
 *   firstMessage: "Hello! How can I help?",
 * });
 * ```
 */

import type { RealtimeTurnDetection } from '../types';
import { validateRealtimeTurnDetection } from '../providers/openai-realtime';

/** Constructor options for the `XaiRealtime` engine marker. */
export interface XaiRealtimeOptions {
  /**
   * xAI API key (Bearer). Falls back to the `XAI_API_KEY` env var when omitted.
   */
  apiKey?: string;
  /**
   * Voice-agent model id. Defaults to `"grok-voice-latest"` (always the newest
   * model). Also accepts pinned ids such as `"grok-voice-think-fast-1.0"`.
   */
  model?: string;
  /** Voice id (built-in or custom, case-insensitive). Defaults to `"eve"`. */
  voice?: string;
  /** Override the WebSocket base URL (no query string). Defaults to `wss://api.x.ai/v1/realtime`. */
  baseUrl?: string;
  /**
   * Reasoning effort. `"high"` (xAI server default) enables reasoning;
   * `"none"` disables it. Omit to keep the server default.
   */
  reasoningEffort?: 'high' | 'none';
  /**
   * ISO / BCP-47 language hint for input transcription (e.g. `"it"`, `"es-MX"`).
   * Biases ASR toward one language instead of auto-detecting. Omit for
   * auto-detect. Display-only — does not gate the model.
   */
  languageHint?: string;
  /**
   * Up to 100 key terms (≤50 chars each) biasing transcription toward
   * domain vocabulary (product names, proper nouns). Omit for none.
   */
  keyterms?: readonly string[];
  /** Assistant playback speed multiplier in `[0.7, 1.5]` (default `1.0`). */
  speed?: number;
  /**
   * Server VAD activation threshold in `[0.1, 0.9]` (xAI default `0.85`).
   * Higher requires louder audio to trigger a turn.
   */
  vadThreshold?: number;
  /**
   * Trailing silence (ms) the server VAD waits for before ending the turn,
   * `[0, 10000]`. Maps to `turn_detection.silence_duration_ms`.
   */
  silenceDurationMs?: number;
  /**
   * Audio (ms) included before detected speech start, `[0, 10000]` (xAI
   * default `333`). Maps to `turn_detection.prefix_padding_ms`.
   */
  prefixPaddingMs?: number;
  /**
   * When set, the server re-engages the user after this many ms of silence
   * following an assistant turn. Maps to `turn_detection.idle_timeout_ms`.
   */
  idleTimeoutMs?: number;
  /**
   * Pronunciation map applied to the model's output before TTS, e.g.
   * `{ "Acme Mobile": "Acme Mobull" }`. Only the spoken audio changes.
   */
  replace?: Readonly<Record<string, string>>;
  /**
   * Opt in to session resumption — the server caches conversation turns and
   * replays them on reconnect. Defaults to `false`.
   */
  resumption?: boolean;
  /**
   * Raw xAI server-side tool objects appended verbatim to `session.tools`
   * (e.g. `{ type: 'web_search' }`, `{ type: 'x_search' }`,
   * `{ type: 'mcp', server_url, server_label }`). Executed server-side at xAI.
   */
  serverTools?: ReadonlyArray<Record<string, unknown>>;
  /**
   * Turn-detection tuning (shared shape with the other realtime engines).
   * `undefined` (default) keeps the server VAD defaults. Raise the threshold
   * or switch to `semantic_vad` eagerness `'low'` to stop speakerphone noise
   * from triggering false barge-ins.
   */
  turnDetection?: RealtimeTurnDetection;
  /**
   * Gate the model's response on the input transcript (legacy behavior).
   * `false` (default) — the model responds on `speech_stopped`, independent
   * of the transcript. `true` — restore transcript-gated responses.
   */
  gateResponseOnTranscript?: boolean;
}

/**
 * xAI Grok Voice Agent engine marker — selects xAI's OpenAI-GA-compatible
 * speech-to-speech API.
 */
export class XaiRealtime {
  readonly kind = 'xai_realtime' as const;
  readonly apiKey: string;
  readonly model?: string;
  readonly voice?: string;
  readonly baseUrl?: string;
  readonly reasoningEffort?: 'high' | 'none';
  readonly languageHint?: string;
  readonly keyterms?: readonly string[];
  readonly speed?: number;
  readonly vadThreshold?: number;
  readonly silenceDurationMs?: number;
  readonly prefixPaddingMs?: number;
  readonly idleTimeoutMs?: number;
  readonly replace?: Readonly<Record<string, string>>;
  readonly resumption?: boolean;
  readonly serverTools?: ReadonlyArray<Record<string, unknown>>;
  readonly turnDetection?: RealtimeTurnDetection;
  readonly gateResponseOnTranscript?: boolean;

  constructor(opts: XaiRealtimeOptions = {}) {
    const key = opts.apiKey ?? process.env.XAI_API_KEY;
    if (!key) {
      throw new Error(
        "xAI Realtime requires an apiKey. Pass { apiKey: '...' } or set " +
          'XAI_API_KEY in the environment.',
      );
    }
    validateRealtimeTurnDetection(opts.turnDetection);
    this.apiKey = key;
    this.model = opts.model;
    this.voice = opts.voice;
    this.baseUrl = opts.baseUrl;
    this.reasoningEffort = opts.reasoningEffort;
    this.languageHint = opts.languageHint;
    this.keyterms = opts.keyterms;
    this.speed = opts.speed;
    this.vadThreshold = opts.vadThreshold;
    this.silenceDurationMs = opts.silenceDurationMs;
    this.prefixPaddingMs = opts.prefixPaddingMs;
    this.idleTimeoutMs = opts.idleTimeoutMs;
    this.replace = opts.replace;
    this.resumption = opts.resumption;
    this.serverTools = opts.serverTools;
    this.turnDetection = opts.turnDetection;
    this.gateResponseOnTranscript = opts.gateResponseOnTranscript;
  }
}
