/** Fish Audio batch STT for Patter pipeline mode (Beta). */
import {
  FishAudioSTT as _FishAudioSTT,
  type FishAudioSTTOptions as _FishAudioSTTOptions,
} from "../providers/fish-audio-stt";

/** Constructor options for the Fish Audio `STT` adapter. */
export interface FishAudioSTTOptions extends _FishAudioSTTOptions {
  /** API key. Falls back to the FISH_AUDIO_API_KEY env var when omitted. */
  apiKey?: string;
  /** ISO language code (e.g. `"en"`). Omit to let Fish auto-detect. */
  language?: string;
}

/**
 * Fish Audio ASR — buffered batch transcription (Beta).
 *
 * @example
 * ```ts
 * import * as fishAudio from "getpatter/stt/fish-audio";
 * const stt = new fishAudio.STT();                    // reads FISH_AUDIO_API_KEY
 * const stt2 = new fishAudio.STT({ apiKey: "...", language: "it" });
 * ```
 *
 * Fish has no streaming transcription socket, so this adapter uploads ~2 s
 * windows and emits one final transcript per window — no interim partials.
 * Prefer Deepgram / Soniox / AssemblyAI when turn latency matters most.
 */
export class STT extends _FishAudioSTT {
  static override readonly providerKey = "fish_audio_stt";

  constructor(opts: FishAudioSTTOptions = {}) {
    const key = opts.apiKey ?? process.env.FISH_AUDIO_API_KEY;
    if (!key) {
      throw new Error(
        "Fish Audio STT requires an apiKey. Pass { apiKey: '...' } or " +
          "set FISH_AUDIO_API_KEY in the environment.",
      );
    }
    const { apiKey: _ignored, language, ...rest } = opts;
    void _ignored;
    super(key, language ?? "en", rest);
  }
}
