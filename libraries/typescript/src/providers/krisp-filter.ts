/**
 * Krisp VIVA noise-reduction AudioFilter — TypeScript scaffold.
 *
 * Mirrors the API of the Python `getpatter.providers.krisp_filter.KrispVivaFilter`
 * for SDK parity. As of 2026-05 Krisp does not publish an official Node.js
 * (server) SDK; third-party browser/RN wrappers exist but cannot process
 * server-received PCM/mulaw audio. This class throws at construction time
 * and points the caller at the available paths (Python SDK or DeepFilterNet
 * on TS).
 *
 * When Krisp publishes an official Node binding — or a community NAPI/WASM
 * wrapper becomes available — the import below and `process()` body will
 * fill in. The class signature is intentionally compatible with the Python
 * one so callers do not need to migrate code: `camelCase` ↔ `snake_case`,
 * `modelPath` ↔ `model_path`, etc.
 *
 * Krisp VIVA is a proprietary SDK and requires a commercial license plus a
 * `.kef` model file provided by the user. Patter ships only the
 * AudioFilter interface scaffold — never the SDK or model.
 *
 * @see https://krisp.ai/developers/
 */
import type { AudioFilter } from '../types';

/** Krisp-supported sample rates (parity with Python `KrispSampleRate`). */
export const KrispSampleRate = {
  HZ_8000: 8000,
  HZ_16000: 16000,
  HZ_32000: 32000,
  HZ_44100: 44100,
  HZ_48000: 48000,
} as const;
export type KrispSampleRate = (typeof KrispSampleRate)[keyof typeof KrispSampleRate];

/** Krisp-supported frame durations in ms (parity with Python `KrispFrameDuration`). */
export const KrispFrameDuration = {
  MS_10: 10,
  MS_15: 15,
  MS_20: 20,
  MS_30: 30,
  MS_32: 32,
} as const;
export type KrispFrameDuration = (typeof KrispFrameDuration)[keyof typeof KrispFrameDuration];

/** Options accepted by {@link KrispVivaFilter}. */
export interface KrispVivaFilterOptions {
  /**
   * Path to the Krisp `.kef` model file. If omitted, falls back to the
   * `KRISP_VIVA_FILTER_MODEL_PATH` environment variable.
   */
  readonly modelPath?: string;
  /** Noise-suppression strength in `[0, 100]`. Defaults to `100`. */
  readonly noiseSuppressionLevel?: number;
  /** Frame duration in ms. One of `10, 15, 20, 30, 32`. Defaults to `10`. */
  readonly frameDurationMs?: KrispFrameDuration | number;
  /** Initial sample rate in Hz. Defaults to `16000`. Re-created lazily if it changes mid-call. */
  readonly sampleRate?: KrispSampleRate | number;
}

const NODE_SDK_UNAVAILABLE_MESSAGE =
  'Krisp VIVA Filter is not yet available for the Patter TypeScript SDK.\n\n' +
  'As of 2026-05, Krisp does not publish an official Node.js (server) SDK. ' +
  'The Patter TypeScript SDK ships only the AudioFilter interface scaffold ' +
  '(this file) for parity with the Python implementation, since Patter runs ' +
  'server-side on a real-time audio stream from the telephony carrier.\n\n' +
  'Available paths today:\n' +
  '  1. Use the Python SDK: `from getpatter.providers.krisp_filter import ' +
  'KrispVivaFilter` — fully implemented, requires `pip install ' +
  'getpatter[krisp]` + `KRISP_VIVA_SDK_LICENSE_KEY` + ' +
  '`KRISP_VIVA_FILTER_MODEL_PATH`.\n' +
  '  2. Use DeepFilterNet on TS: `new DeepFilterNetFilter({ modelPath: ' +
  "'.../DeepFilterNet3.onnx' })` — community ONNX export, no license needed.\n\n" +
  'Browser/React Native (not applicable to Patter server-side, listed for ' +
  'completeness):\n' +
  '  - Browser WASM wrappers (various third-party packages) process local ' +
  'microphone capture, not server-received PCM/mulaw audio.\n' +
  '  - Mobile client wrappers (iOS/Android, various third-party packages) ' +
  'are likewise client-side only.\n\n' +
  'Track Node SDK status:\n' +
  '  - https://krisp.ai/developers/\n' +
  '  - Patter backlog: task #38 "Krisp TS port decision"\n';

/**
 * Krisp VIVA noise-reduction filter — TypeScript scaffold (NOT YET IMPLEMENTED).
 *
 * Construction throws with a guidance message because Krisp does not ship a
 * Node.js SDK. The class exists for API parity with the Python
 * `KrispVivaFilter` so that user code does not need to be rewritten when a
 * Node binding lands.
 *
 * For TS users today, use {@link DeepFilterNetFilter} from
 * `./deepfilternet-filter` instead — same `AudioFilter` interface, no
 * license required.
 *
 * @example
 * ```ts
 * // FUTURE — when Krisp publishes a Node SDK:
 * import { KrispVivaFilter } from 'getpatter/providers/krisp-filter';
 * const filter = new KrispVivaFilter({ modelPath: '/path/to/model.kef' });
 * const agent = phone.agent({ audioFilter: filter, ... });
 * ```
 */
export class KrispVivaFilter implements AudioFilter {
  static readonly providerKey = 'krisp_viva';

  constructor(_options: KrispVivaFilterOptions = {}) {
    throw new Error(NODE_SDK_UNAVAILABLE_MESSAGE);
  }

  // The two methods below are unreachable at runtime (constructor throws)
  // but kept so the class structurally satisfies `AudioFilter`. When the
  // Node binding lands, replace constructor + these stubs with the real
  // implementation.

  async process(pcmChunk: Buffer, _sampleRate: number): Promise<Buffer> {
    return pcmChunk;
  }

  async close(): Promise<void> {
    // no-op
  }
}
