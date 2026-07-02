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
  /**
   * Enable native-audio affective dialog — the model adapts its tone/prosody to
   * the caller's emotion. Opt-in (default off) for backward compatibility.
   */
  affectiveDialog?: boolean;
  /**
   * Enable native-audio proactive audio — the model decides when to respond and
   * can stay silent to non-directed speech. Opt-in (default off).
   */
  proactiveAudio?: boolean;
  /**
   * Tune the server-side voice-activity detector. `startSensitivity: 'LOW'`
   * rejects background noise/breathing; `endSensitivity: 'HIGH'` + a lower
   * `silenceDurationMs` cut the wait before the model replies. Opt-in.
   */
  vad?: GeminiVadOptions;
  /**
   * Enable the model's "thinking" (chain-of-thought) on this voice session.
   *
   * `false` (DEFAULT) — thinking is disabled (`thinkingBudget: 0`). A voice
   * agent must never speak or transcribe its private reasoning; on the
   * Gemini 3.x native-audio models the chain-of-thought otherwise leaks into
   * BOTH the spoken audio and the transcript (e.g. "**Initiating Call
   * Protocol**…" before the real reply). Keep this off for telephony.
   *
   * `true` — re-enables dynamic thinking (the model decides how much to
   * think). Only do this for non-voice / latency-tolerant flows.
   */
  thinking?: boolean;
  /**
   * Explicit Gemini thinking budget (tokens). Takes precedence over
   * {@link thinking}. `0` disables thinking, `-1` lets the model decide
   * (dynamic). Omit to use the {@link thinking}-derived default (0 = off).
   */
  thinkingBudget?: number;
  /**
   * Gemini API version for the Live channel. Auto-detected from the model
   * name when omitted: native-audio output models — including
   * `gemini-3.1-flash-live-preview` — are served on `v1alpha`; all others use
   * the SDK default (`v1beta`). Pass `'v1alpha'` / `'v1beta'` to override when
   * a model is unavailable on the auto-detected channel.
   */
  apiVersion?: string;
}

/** Server-side VAD tuning for the Gemini Live native-audio engine. */
export interface GeminiVadOptions {
  /** 'LOW' = less sensitive (ignores noise); 'HIGH' = triggers on quieter sound. */
  startSensitivity?: 'HIGH' | 'LOW';
  /** 'HIGH' = detects end-of-turn faster (lower latency); 'LOW' = waits longer. */
  endSensitivity?: 'HIGH' | 'LOW';
  /** Silence (ms) before the turn is considered over. Lower = snappier replies. */
  silenceDurationMs?: number;
  /** Audio (ms) retained before detected speech start. */
  prefixPaddingMs?: number;
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
  readonly affectiveDialog?: boolean;
  readonly proactiveAudio?: boolean;
  readonly vad?: GeminiVadOptions;
  readonly thinking?: boolean;
  readonly thinkingBudget?: number;
  readonly apiVersion?: string;

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
    this.affectiveDialog = opts.affectiveDialog;
    this.proactiveAudio = opts.proactiveAudio;
    this.vad = opts.vad;
    this.thinking = opts.thinking;
    this.thinkingBudget = opts.thinkingBudget;
    this.apiVersion = opts.apiVersion;
  }
}
