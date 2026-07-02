/**
 * Inworld Realtime engine — marker class for Patter client dispatch.
 *
 * Selects Inworld's Realtime speech-to-speech API. Inworld advertises an
 * "OpenAI Realtime migration" path — the event schema, session structure, and
 * client/server events are compatible with OpenAI's Realtime API, so the
 * session can be driven with the same `session.update` / `response.create` /
 * streaming-delta wire shape. The runtime lives in
 * {@link import('../providers/inworld-realtime').InworldRealtimeAdapter},
 * which subclasses `OpenAIRealtimeAdapter` and only swaps the endpoint + auth.
 *
 * Like the other engine markers this is a tiny immutable config object: it
 * carries credentials / voice / model only. The session is constructed
 * server-side by `buildAIAdapter`.
 *
 * @example
 * ```ts
 * import { Patter, Twilio, InworldRealtime } from "getpatter";
 *
 * const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+1..." });
 * const agent = phone.agent({
 *   engine: new InworldRealtime({ voice: "Ashley" }), // reads INWORLD_API_KEY
 *   systemPrompt: "You are a friendly receptionist.",
 *   firstMessage: "Hello! How can I help?",
 * });
 * ```
 */

import type { RealtimeTurnDetection } from '../types';
import { validateRealtimeTurnDetection } from '../providers/openai-realtime';

/** Constructor options for the `InworldRealtime` engine marker. */
export interface InworldRealtimeOptions {
  /**
   * Inworld Realtime API key (JWT / Bearer "Realtime key"). Falls back to the
   * `INWORLD_API_KEY` env var when omitted.
   */
  apiKey?: string;
  /**
   * Realtime model id. Inworld is OpenAI-Realtime-compatible; the model is
   * passed through as the `?model=` query param. Defaults to
   * `"inworld-realtime"` — override with the exact model id from your Inworld
   * dashboard.
   */
  model?: string;
  /** Voice name (e.g. `"Ashley"`, `"Olivia"`). Defaults to `"Ashley"`. */
  voice?: string;
  /**
   * Override the WebSocket base URL (no query string). Defaults to
   * `wss://api.inworld.ai/v1/realtime`. Use this to point at an alternate /
   * on-prem deployment, or to supply a session-scoped URL if your Inworld
   * account requires the `/api/v1/realtime/session?key=<session-id>` flow.
   */
  baseUrl?: string;
  /**
   * ISO-639-1 language hint for input transcription (e.g. `"it"`, `"en"`).
   * Pins the transcription model to one language instead of auto-detecting
   * per utterance. Omit to keep auto-detect (default). Display-only.
   */
  transcriptionLanguage?: string;
  /**
   * Turn-detection tuning. `undefined` (default) keeps the server VAD
   * defaults. Raise the threshold or switch to `semantic_vad` eagerness
   * `'low'` to stop speakerphone noise from triggering false barge-ins.
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
 * Inworld Realtime engine marker — selects Inworld's OpenAI-Realtime-compatible
 * speech-to-speech API.
 */
export class InworldRealtime {
  readonly kind = 'inworld_realtime' as const;
  readonly apiKey: string;
  readonly model?: string;
  readonly voice?: string;
  readonly baseUrl?: string;
  readonly transcriptionLanguage?: string;
  readonly turnDetection?: RealtimeTurnDetection;
  readonly gateResponseOnTranscript?: boolean;

  constructor(opts: InworldRealtimeOptions = {}) {
    const key = opts.apiKey ?? process.env.INWORLD_API_KEY;
    if (!key) {
      throw new Error(
        "Inworld Realtime requires an apiKey. Pass { apiKey: '...' } or set " +
          'INWORLD_API_KEY in the environment.',
      );
    }
    validateRealtimeTurnDetection(opts.turnDetection);
    this.apiKey = key;
    this.model = opts.model;
    this.voice = opts.voice;
    this.baseUrl = opts.baseUrl;
    this.transcriptionLanguage = opts.transcriptionLanguage;
    this.turnDetection = opts.turnDetection;
    this.gateResponseOnTranscript = opts.gateResponseOnTranscript;
  }
}
