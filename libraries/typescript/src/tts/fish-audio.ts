/** Fish Audio TTS for Patter pipeline mode (Beta). */
import {
  FishAudioTTS as _FishAudioTTS,
  type FishAudioTTSOptions as _FishAudioTTSOptions,
} from "../providers/fish-audio-tts";
import {
  FishAudioWebSocketTTS as _FishAudioWebSocketTTS,
  type FishAudioWebSocketTTSOptions as _FishAudioWebSocketTTSOptions,
} from "../providers/fish-audio-ws-tts";

/** Constructor options for the Fish Audio `TTS` adapter. */
export interface FishAudioTTSOptions extends _FishAudioTTSOptions {
  /** API key. Falls back to the FISH_AUDIO_API_KEY env var when omitted. */
  apiKey?: string;
}

/** Constructor options for the Fish Audio `WebSocketTTS` adapter. */
export interface FishAudioWebSocketTTSOptions extends _FishAudioWebSocketTTSOptions {
  /** API key. Falls back to the FISH_AUDIO_API_KEY env var when omitted. */
  apiKey?: string;
}

/** Options for the carrier-specific factories — same minus `format`/`sampleRate`. */
export type FishAudioCarrierOptions = Omit<
  FishAudioTTSOptions,
  "format" | "sampleRate"
>;

function resolveApiKey(apiKey: string | undefined): string {
  const key = apiKey ?? process.env.FISH_AUDIO_API_KEY;
  if (!key) {
    throw new Error(
      "Fish Audio TTS requires an apiKey. Pass { apiKey: '...' } or " +
        "set FISH_AUDIO_API_KEY in the environment.",
    );
  }
  return key;
}

/**
 * Fish Audio HTTP TTS — defaults to the `s2.1-pro` model (Beta).
 *
 * @example
 * ```ts
 * import * as fishAudio from "getpatter/tts/fish-audio";
 * const tts = new fishAudio.TTS();                                 // reads FISH_AUDIO_API_KEY
 * const tts2 = new fishAudio.TTS({ apiKey: "...", voice: "<reference-id>", latency: "low" });
 * ```
 *
 * `voice` is a Fish `reference_id` — a voice-model id from the Fish voice
 * library or one you cloned. Omit it to use the model's built-in voice; pass an
 * array of ids for multi-speaker synthesis with `<|speaker:0|>` markers.
 *
 * **Telephony** — use {@link TTS.forTwilio} (pcm @ 8 kHz, no resample) or
 * {@link TTS.forTelnyx} (pcm @ 16 kHz) on phone calls.
 */
export class TTS extends _FishAudioTTS {
  static readonly providerKey = "fish_audio";

  constructor(opts: FishAudioTTSOptions = {}) {
    const key = resolveApiKey(opts.apiKey);
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }

  /** Pipeline TTS pre-configured for Twilio Media Streams (pcm @ 8 kHz — no resample). */
  static override forTwilio(opts?: FishAudioCarrierOptions): TTS;
  // Parent-compatible overload — accepts the legacy positional form too.
  static override forTwilio(apiKey: string, options?: FishAudioCarrierOptions): TTS;
  static override forTwilio(
    arg1?: string | FishAudioCarrierOptions,
    arg2?: FishAudioCarrierOptions,
  ): TTS {
    const opts: FishAudioCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, format: "pcm", sampleRate: 8000 });
  }

  /** Pipeline TTS pre-configured for Telnyx bidirectional media (pcm @ 16 kHz). */
  static override forTelnyx(opts?: FishAudioCarrierOptions): TTS;
  static override forTelnyx(apiKey: string, options?: FishAudioCarrierOptions): TTS;
  static override forTelnyx(
    arg1?: string | FishAudioCarrierOptions,
    arg2?: FishAudioCarrierOptions,
  ): TTS {
    const opts: FishAudioCarrierOptions =
      typeof arg1 === "string" ? { apiKey: arg1, ...(arg2 ?? {}) } : (arg1 ?? {});
    return new TTS({ ...opts, format: "pcm", sampleRate: 16000 });
  }
}

/**
 * Fish Audio WebSocket TTS — opt-in low-latency path, `s2-pro` (Beta).
 *
 * @example
 * ```ts
 * import * as fishAudio from "getpatter/tts/fish-audio";
 * const tts = new fishAudio.WebSocketTTS();                        // reads FISH_AUDIO_API_KEY
 * ```
 *
 * Fish serves only `s1` and `s2-pro` on the streaming socket — for `s2.1-pro`
 * use {@link TTS} (HTTP) instead. Requires the `@msgpack/msgpack` package.
 */
export class WebSocketTTS extends _FishAudioWebSocketTTS {
  static readonly providerKey = "fish_audio";

  constructor(opts: FishAudioWebSocketTTSOptions = {}) {
    const key = resolveApiKey(opts.apiKey);
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }
}
