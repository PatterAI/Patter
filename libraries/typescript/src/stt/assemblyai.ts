/** AssemblyAI Universal Streaming STT for Patter pipeline mode. */
import {
  AssemblyAISTT as _AssemblyAISTT,
  type AssemblyAIDomain,
  type AssemblyAIEncoding,
  type AssemblyAIModel,
} from "../providers/assemblyai-stt";

/** Constructor options for the AssemblyAI `STT` adapter. */
export interface AssemblyAISTTOptions {
  /** API key. Falls back to ASSEMBLYAI_API_KEY env var when omitted. */
  apiKey?: string;
  model?: AssemblyAIModel;
  encoding?: AssemblyAIEncoding;
  sampleRate?: number;
  baseUrl?: string;
  languageDetection?: boolean;
  /**
   * BCP-47 language hint (e.g. ``"it"``, ``"en"``). AssemblyAI does NOT
   * expose a per-call language override — the language is determined by
   * the chosen ``model`` (English-only models reject non-English audio,
   * multilingual models auto-detect). This field is accepted for
   * cross-provider parity with ``DeepgramSTT``/``WhisperSTT``/
   * ``OpenAITranscribeSTT``/``CartesiaSTT`` but is currently a no-op:
   * pick a multilingual ``model`` (e.g. ``universal-streaming-pro``)
   * and the provider will detect Italian automatically.
   */
  language?: string;
  endOfTurnConfidenceThreshold?: number;
  minTurnSilence?: number;
  maxTurnSilence?: number;
  formatTurns?: boolean;
  keytermsPrompt?: readonly string[];
  prompt?: string;
  vadThreshold?: number;
  speakerLabels?: boolean;
  maxSpeakers?: number;
  domain?: AssemblyAIDomain;
}

/**
 * AssemblyAI Universal Streaming STT.
 *
 * @example
 * ```ts
 * import * as assemblyai from "getpatter/stt/assemblyai";
 * const stt = new assemblyai.STT();              // reads ASSEMBLYAI_API_KEY
 * const stt = new assemblyai.STT({ apiKey: "..." });
 * ```
 */
export class STT extends _AssemblyAISTT {
  static readonly providerKey = "assemblyai";
  constructor(opts: AssemblyAISTTOptions = {}) {
    const key = opts.apiKey ?? process.env.ASSEMBLYAI_API_KEY;
    if (!key) {
      throw new Error(
        "AssemblyAI STT requires an apiKey. Pass { apiKey: '...' } or " +
          "set ASSEMBLYAI_API_KEY in the environment.",
      );
    }
    // ``language`` is a parity-only option — not forwarded to the
    // provider (see field docs above). Strip it before passing the rest
    // through to the underlying constructor.
    const { apiKey: _ignored, language: _lang, ...rest } = opts;
    void _ignored;
    void _lang;
    super(key, rest);
  }
}
