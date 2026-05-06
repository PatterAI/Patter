/** Inworld TTS for Patter pipeline mode. */
import {
  InworldTTS as _InworldTTS,
  type InworldAudioEncoding,
  type InworldDeliveryMode,
  type InworldModel,
} from "../providers/inworld-tts";

/** Constructor options for the Inworld `TTS` adapter. */
export interface InworldTTSOptions {
  /** Inworld Base64 auth token. Falls back to INWORLD_API_KEY env var. */
  apiKey?: string;
  model?: InworldModel | string;
  voice?: string;
  language?: string;
  audioEncoding?: InworldAudioEncoding | string;
  sampleRate?: number;
  bitrate?: number;
  temperature?: number;
  speakingRate?: number;
  deliveryMode?: InworldDeliveryMode | string;
  baseUrl?: string;
}

/**
 * Inworld TTS — defaults to the TTS-2 model.
 *
 * @example
 * ```ts
 * import * as inworld from "getpatter/tts/inworld";
 * const tts = new inworld.TTS();                        // reads INWORLD_API_KEY
 * const tts = new inworld.TTS({ apiKey: "...", voice: "Olivia", language: "en" });
 * ```
 */
export class TTS extends _InworldTTS {
  static readonly providerKey = "inworld";
  constructor(opts: InworldTTSOptions = {}) {
    const key = opts.apiKey ?? process.env.INWORLD_API_KEY;
    if (!key) {
      throw new Error(
        "Inworld TTS requires an apiKey. Pass { apiKey: '...' } or " +
          "set INWORLD_API_KEY in the environment.",
      );
    }
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }
}
