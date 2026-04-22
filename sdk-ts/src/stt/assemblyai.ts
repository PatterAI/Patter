/** AssemblyAI Universal Streaming STT for Patter pipeline mode. */
import {
  AssemblyAISTT as _AssemblyAISTT,
  type AssemblyAIEncoding,
  type AssemblyAIModel,
} from "../providers/assemblyai-stt";

export interface AssemblyAISTTOptions {
  /** API key. Falls back to ASSEMBLYAI_API_KEY env var when omitted. */
  apiKey?: string;
  model?: AssemblyAIModel;
  encoding?: AssemblyAIEncoding;
  sampleRate?: number;
  baseUrl?: string;
  languageDetection?: boolean;
  endOfTurnConfidenceThreshold?: number;
  minTurnSilence?: number;
  maxTurnSilence?: number;
  formatTurns?: boolean;
  keytermsPrompt?: readonly string[];
  prompt?: string;
  vadThreshold?: number;
  speakerLabels?: boolean;
  maxSpeakers?: number;
  domain?: string;
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
  constructor(opts: AssemblyAISTTOptions = {}) {
    const key = opts.apiKey ?? process.env.ASSEMBLYAI_API_KEY;
    if (!key) {
      throw new Error(
        "AssemblyAI STT requires an apiKey. Pass { apiKey: '...' } or " +
          "set ASSEMBLYAI_API_KEY in the environment.",
      );
    }
    const { apiKey: _ignored, ...rest } = opts;
    void _ignored;
    super(key, rest);
  }
}
