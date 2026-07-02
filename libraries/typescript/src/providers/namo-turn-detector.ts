/**
 * NAMO Turn Detector v1 — text-based semantic end-of-utterance model (ONNX).
 *
 * Open, clean-weights end-of-turn (EOT) detector from the NAMO Turn Detector
 * v1 project (VideoSDK, Apache-2.0). Unlike an audio-native detector such as
 * `SmartTurnDetector` — which scores the *prosody* of the last few seconds of
 * caller audio — NAMO is a text transformer: it reads the rolling
 * conversation transcript (the last few turns plus the caller's current
 * in-flight utterance) and predicts whether the caller has *finished their
 * turn* or is merely pausing mid-sentence ("My email is john at … and …").
 *
 * Wiring: pass an instance as `agent.turnDetector` exactly like smart-turn.
 * The pipeline stream handler defers the STT finalize that normally fires on
 * a VAD `speech_end` until the model agrees the turn is complete (probability
 * ≥ {@link NamoTurnDetector.threshold}), holding for at most
 * `agent.maxSemanticHoldMs` (default 1200 ms). The handler assembles the
 * rolling transcript and passes it to {@link predict} as the `transcript`
 * argument (the audio window is ignored).
 *
 * Model + tokenizer: weights are NOT bundled with the SDK. Download a NAMO
 * Turn Detector v1 sequence-classification model (an `.onnx` file plus its
 * Hugging Face tokenizer files) and point the SDK at the `.onnx` file via the
 * `PATTER_NAMO_MODEL` environment variable or the `modelPath` option of
 * {@link NamoTurnDetector.load}. The tokenizer is loaded with
 * `@huggingface/transformers` (`AutoTokenizer`) from the directory holding
 * the model file, or from `PATTER_NAMO_TOKENIZER` / the `tokenizerPath`
 * option. NAMO ships per-language models, each with its own recommended
 * threshold — pick your language's model and set {@link threshold}.
 *
 * `onnxruntime-node` and `@huggingface/transformers` are loaded lazily as
 * optional dependencies (same pattern as `providers/silero-vad.ts`); a clear
 * "install X" error is thrown when either is absent.
 *
 * If `tokenizerPath` resolves to a Hugging Face Hub repo id rather than a
 * local directory, pin the download to an immutable commit/tag via the
 * `PATTER_NAMO_REVISION` environment variable or the `revision` option of
 * {@link NamoTurnDetector.load} — it is a no-op for the documented local
 * default.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import { getLogger } from '../logger';
import type { TurnDetectorProvider } from '../types';
import {
  loadOnnxRuntime,
  type OnnxInferenceSession,
  type OnnxRuntime,
  type OnnxTensor,
} from './silero-vad';

/** Env var consulted by {@link NamoTurnDetector.load} when no `modelPath` is given. */
export const NAMO_MODEL_ENV_VAR = 'PATTER_NAMO_MODEL';
/** Env var consulted for the tokenizer source when no `tokenizerPath` is given. */
export const NAMO_TOKENIZER_ENV_VAR = 'PATTER_NAMO_TOKENIZER';
/**
 * Env var consulted for the Hugging Face Hub revision (commit SHA or tag) to
 * pin the tokenizer download to when no `revision` option is given. No
 * effect on the documented default — a local directory shipped alongside
 * the model file.
 */
export const NAMO_REVISION_ENV_VAR = 'PATTER_NAMO_REVISION';

/**
 * Default decision threshold. NAMO ships per-language models, each with its
 * own recommended value — override via `load({ threshold })`.
 */
export const DEFAULT_NAMO_THRESHOLD = 0.5;

/**
 * Rolling transcript is truncated to this many tokens before inference so the
 * detector always sees a bounded prompt (matches the ~128-token window the
 * handler assembles).
 */
export const DEFAULT_NAMO_MAX_TOKENS = 128;

/** Index of the "turn complete" class in a 2-class classification head. */
const COMPLETE_LABEL_INDEX = 1;

const DOWNLOAD_HINT =
  'Download a NAMO Turn Detector v1 model (an .onnx file plus its Hugging ' +
  'Face tokenizer files) and either set the ' +
  `${NAMO_MODEL_ENV_VAR} environment variable to the .onnx path or pass ` +
  'modelPath to NamoTurnDetector.load(). The model is not bundled with the SDK.';

/** Options accepted by {@link NamoTurnDetector.load}. */
export interface NamoTurnDetectorOptions {
  /**
   * End-of-turn probability at/above which the turn is considered complete.
   * Default 0.5 — override with your NAMO language model's recommended value.
   */
  readonly threshold?: number;
  /** Path to the NAMO `.onnx` file. Falls back to `PATTER_NAMO_MODEL`. */
  readonly modelPath?: string;
  /**
   * Directory (or repo id) for the Hugging Face tokenizer. Falls back to
   * `PATTER_NAMO_TOKENIZER`, then the directory containing the model file.
   */
  readonly tokenizerPath?: string;
  /**
   * Hugging Face Hub revision (commit SHA or tag) to pin the tokenizer
   * download to when `tokenizerPath` resolves to a repo id. Falls back to
   * `PATTER_NAMO_REVISION`. No effect for the documented default — a local
   * directory shipped alongside the model file.
   */
  readonly revision?: string;
  /** Restrict ONNX Runtime to the CPU execution provider (default true). */
  readonly forceCpu?: boolean;
  /** Truncate the rolling transcript to this many tokens (default 128). */
  readonly maxTokens?: number;
}

/**
 * Minimal tokenizer surface — mirrors a Hugging Face `AutoTokenizer` call.
 * The real path wraps `@huggingface/transformers`; tests inject a fake.
 */
export interface NamoTokenizer {
  /**
   * Tokenize `text` (truncated to `maxTokens`), returning int64 token ids and
   * attention mask for a batch of one.
   */
  encode(text: string, maxTokens: number): {
    inputIds: BigInt64Array;
    attentionMask: BigInt64Array;
  };
}

/**
 * Resolve the NAMO ONNX file from the option or the env var. Throws with
 * download instructions when unset or missing.
 * @internal
 */
export function resolveNamoModelPath(modelPath?: string): string {
  let resolved = modelPath;
  if (!resolved) {
    resolved = (process.env[NAMO_MODEL_ENV_VAR] ?? '').trim();
    if (!resolved) {
      throw new Error(`NamoTurnDetector has no model file configured. ${DOWNLOAD_HINT}`);
    }
  }
  if (!fs.existsSync(resolved)) {
    throw new Error(`NAMO model file not found: ${resolved}. ${DOWNLOAD_HINT}`);
  }
  if (!fs.statSync(resolved).isFile()) {
    throw new Error(`NAMO model path is not a file: ${resolved}. ${DOWNLOAD_HINT}`);
  }
  return path.resolve(resolved);
}

/**
 * Resolve the tokenizer source (directory or repo id): explicit option, then
 * `PATTER_NAMO_TOKENIZER`, then the directory holding the model file (NAMO
 * ships the tokenizer alongside the weights).
 * @internal
 */
export function resolveNamoTokenizerPath(tokenizerPath: string | undefined, modelPath: string): string {
  if (tokenizerPath) return tokenizerPath;
  const env = (process.env[NAMO_TOKENIZER_ENV_VAR] ?? '').trim();
  if (env) return env;
  return path.dirname(modelPath);
}

/**
 * Resolve the Hugging Face Hub revision to pin the tokenizer download to:
 * explicit option, then `PATTER_NAMO_REVISION`. Returns `undefined` when
 * neither is set — the documented default (`tokenizerPath` resolving to a
 * local directory shipped alongside the model) has no revision concept; set
 * this when `tokenizerPath` instead resolves to a Hugging Face Hub repo id,
 * so the download is pinned to an immutable commit/tag rather than a mutable
 * branch.
 * @internal
 */
export function resolveNamoRevision(revision?: string): string | undefined {
  if (revision) return revision;
  const env = (process.env[NAMO_REVISION_ENV_VAR] ?? '').trim();
  return env || undefined;
}

/** Coerce a transformers.js tensor / typed array / nested array to BigInt64Array. @internal */
function toBigInt64Array(value: unknown): BigInt64Array {
  if (value instanceof BigInt64Array) return value;
  // transformers.js tensors expose the flat buffer on `.data`.
  if (value && typeof value === 'object' && 'data' in (value as Record<string, unknown>)) {
    return toBigInt64Array((value as { data: unknown }).data);
  }
  const flat: bigint[] = [];
  const push = (v: unknown): void => {
    if (Array.isArray(v)) {
      for (const item of v) push(item);
    } else {
      flat.push(BigInt(Math.trunc(Number(v))));
    }
  };
  if (value instanceof Float32Array || value instanceof Float64Array || Array.isArray(value)) {
    push(Array.from(value as ArrayLike<number>));
  } else if (value !== undefined && value !== null) {
    push(value);
  }
  return BigInt64Array.from(flat);
}

/** Lazily import the optional `@huggingface/transformers` package. @internal */
async function importTransformers(): Promise<{
  AutoTokenizer: { from_pretrained(src: string, options?: { revision?: string }): Promise<unknown> };
}> {
  try {
    // @ts-ignore — optional peer dep; types may be absent when not installed
    return (await import('@huggingface/transformers')) as never;
  } catch {
    throw new Error(
      'NamoTurnDetector requires the "@huggingface/transformers" package — it is not installed.\n' +
        '  Install:  npm install @huggingface/transformers\n\n' +
        '  (Only needed when you actually use NamoTurnDetector in pipeline mode.)',
    );
  }
}

/**
 * Load the Hugging Face tokenizer for NAMO and wrap it in a
 * {@link NamoTokenizer}. Throws a descriptive install error when
 * `@huggingface/transformers` is missing.
 *
 * `revision` (see {@link resolveNamoRevision}) pins the Hugging Face Hub
 * commit/tag when `tokenizerPath` is a repo id; it is a no-op when
 * `tokenizerPath` is the documented local directory.
 * @internal
 */
export async function loadNamoTokenizer(tokenizerPath: string, revision?: string): Promise<NamoTokenizer> {
  const { AutoTokenizer } = await importTransformers();
  const hf = (await AutoTokenizer.from_pretrained(tokenizerPath, { revision })) as (
    text: string,
    options?: Record<string, unknown>,
  ) => { input_ids: unknown; attention_mask?: unknown };
  return {
    encode(text: string, maxTokens: number) {
      const enc = hf(text, { truncation: true, max_length: maxTokens });
      const inputIds = toBigInt64Array(enc.input_ids);
      const attentionMask =
        enc.attention_mask !== undefined
          ? toBigInt64Array(enc.attention_mask)
          : BigInt64Array.from(inputIds, () => 1n);
      return { inputIds, attentionMask };
    },
  };
}

/**
 * Turn-complete probability from a NAMO classification head. A single-logit
 * head is passed through a sigmoid; a 2-(or more)-class head is soft-maxed and
 * the COMPLETE class is taken. Clamped to `[0, 1]`.
 * @internal
 */
export function completionProbability(data: Float32Array | BigInt64Array | undefined): number {
  if (!data || data.length === 0) return 0;
  const arr: number[] = [];
  for (let k = 0; k < data.length; k++) arr.push(Number(data[k]));
  let probability: number;
  if (arr.length === 1) {
    probability = 1 / (1 + Math.exp(-arr[0]));
  } else {
    const max = Math.max(...arr);
    const exps = arr.map((v) => Math.exp(v - max));
    const sum = exps.reduce((a, b) => a + b, 0);
    const idx = COMPLETE_LABEL_INDEX < exps.length ? COMPLETE_LABEL_INDEX : exps.length - 1;
    probability = exps[idx] / sum;
  }
  return Math.min(1, Math.max(0, probability));
}

/**
 * Text-based semantic end-of-utterance detector backed by NAMO v1 (ONNX).
 *
 * Load via {@link NamoTurnDetector.load} (throws with actionable instructions
 * when the optional deps or the model/tokenizer are missing) or
 * {@link NamoTurnDetector.maybeLoad} (warns once and resolves `undefined`
 * instead, so the agent degrades to plain VAD-silence endpointing rather than
 * crashing):
 *
 * ```ts
 * const detector = await NamoTurnDetector.load();   // PATTER_NAMO_MODEL
 * // or: await NamoTurnDetector.load({ modelPath: '…/model.onnx' });
 * // or: await NamoTurnDetector.maybeLoad();         // undefined when unprovisioned
 *
 * const agent = phone.agent({ ..., turnDetector: detector });
 * ```
 *
 * {@link predict} ignores the audio window and scores the rolling
 * `transcript` the pipeline handler assembles (the last few turns plus the
 * caller's current in-flight utterance). The handler compares the returned
 * probability against {@link threshold} (default 0.5; set it to your NAMO
 * language model's recommended value). The model is stateless, so a single
 * instance may be shared across concurrent calls.
 */
export class NamoTurnDetector implements TurnDetectorProvider {
  private closed = false;

  private constructor(
    private readonly runtime: OnnxRuntime,
    private session: OnnxInferenceSession | null,
    private tokenizer: NamoTokenizer | null,
    private readonly thresholdValue: number,
    private readonly maxTokensValue: number,
  ) {}

  /**
   * Load the NAMO v1 ONNX model + tokenizer and return a ready detector.
   * Throws with download instructions when no model file is configured (see
   * {@link NAMO_MODEL_ENV_VAR}), and with install instructions when
   * `onnxruntime-node` or `@huggingface/transformers` is missing.
   */
  static async load(options: NamoTurnDetectorOptions = {}): Promise<NamoTurnDetector> {
    const threshold = options.threshold ?? DEFAULT_NAMO_THRESHOLD;
    if (!(threshold >= 0 && threshold <= 1)) {
      throw new Error('threshold must be within [0.0, 1.0]');
    }
    const maxTokens = options.maxTokens ?? DEFAULT_NAMO_MAX_TOKENS;
    if (!(maxTokens > 0)) {
      throw new Error('maxTokens must be positive');
    }
    // Resolve the model path BEFORE touching the optional deps so a missing
    // model file surfaces the actionable download hint first. The ONNX
    // runtime is loaded before the tokenizer so a missing native addon
    // surfaces its specific install hint first.
    const modelPath = resolveNamoModelPath(options.modelPath);
    const tokenizerPath = resolveNamoTokenizerPath(options.tokenizerPath, modelPath);
    const revision = resolveNamoRevision(options.revision);
    const runtime = await loadOnnxRuntime('NamoTurnDetector');
    const session = await runtime.InferenceSession.create(modelPath, {
      interOpNumThreads: 1,
      intraOpNumThreads: 1,
      executionMode: 'sequential',
      graphOptimizationLevel: 'all',
      executionProviders: options.forceCpu === false ? undefined : ['cpu'],
    });
    const tokenizer = await loadNamoTokenizer(tokenizerPath, revision);
    return new NamoTurnDetector(runtime, session, tokenizer, threshold, maxTokens);
  }

  /**
   * Like {@link load}, but degrade instead of throw. Resolves to `undefined`
   * — after a single clear warning — when semantic turn detection is not
   * provisioned: the optional deps are missing, no model file is configured,
   * or the model/tokenizer cannot be loaded. `turnDetector: undefined` keeps
   * the plain VAD-silence endpointing, so the agent never crashes on startup.
   *
   * An out-of-range `threshold` (or non-positive `maxTokens`) still throws:
   * that is a configuration bug, not a provisioning gap. Mirror of the Python
   * `NamoTurnDetector.maybe_load`.
   */
  static async maybeLoad(
    options: NamoTurnDetectorOptions = {},
  ): Promise<NamoTurnDetector | undefined> {
    const threshold = options.threshold ?? DEFAULT_NAMO_THRESHOLD;
    if (!(threshold >= 0 && threshold <= 1)) {
      throw new Error('threshold must be within [0.0, 1.0]');
    }
    const maxTokens = options.maxTokens ?? DEFAULT_NAMO_MAX_TOKENS;
    if (!(maxTokens > 0)) {
      throw new Error('maxTokens must be positive');
    }
    try {
      return await NamoTurnDetector.load(options);
    } catch (err) {
      getLogger().warn(
        'Semantic turn detection unavailable — falling back to plain ' +
          `VAD-silence endpointing: ${err instanceof Error ? err.message : String(err)}`,
      );
      return undefined;
    }
  }

  /**
   * Internal factory used by tests — bypasses onnxruntime-node / transformers
   * loading by injecting a session + tokenizer directly.
   * @internal
   */
  static fromSession(
    runtime: OnnxRuntime,
    session: OnnxInferenceSession,
    tokenizer: NamoTokenizer,
    options: { threshold?: number; maxTokens?: number } = {},
  ): NamoTurnDetector {
    const threshold = options.threshold ?? DEFAULT_NAMO_THRESHOLD;
    if (!(threshold >= 0 && threshold <= 1)) {
      throw new Error('threshold must be within [0.0, 1.0]');
    }
    const maxTokens = options.maxTokens ?? DEFAULT_NAMO_MAX_TOKENS;
    if (!(maxTokens > 0)) {
      throw new Error('maxTokens must be positive');
    }
    return new NamoTurnDetector(runtime, session, tokenizer, threshold, maxTokens);
  }

  /** Identifier of the underlying model (`namo-turn-detector-v1`). */
  get model(): string {
    return 'namo-turn-detector-v1';
  }

  /** Identifier of the runtime backend (`ONNX`). */
  get provider(): string {
    return 'ONNX';
  }

  /** Rolling-transcript token budget consumed per prediction. */
  get maxTokens(): number {
    return this.maxTokensValue;
  }

  /** End-of-turn probability at/above which the turn is complete. */
  get threshold(): number {
    return this.thresholdValue;
  }

  /**
   * End-of-turn probability for the rolling conversation transcript.
   *
   * @param _pcm16Window Ignored. NAMO is a text model — it scores the
   *   transcript, not the audio. Accepted for contract parity with
   *   audio-native detectors.
   * @param transcript The rolling conversation text (last few turns plus the
   *   caller's current in-flight utterance) assembled by the pipeline
   *   handler. Truncated to {@link maxTokens} tokens.
   * @returns Probability in `[0, 1]` that the turn is COMPLETE. Returns 0
   *   (turn incomplete) for an empty transcript — nothing to score yet, so
   *   the handler keeps holding.
   */
  async predict(_pcm16Window: Buffer, transcript?: string): Promise<number> {
    if (this.closed || this.session === null || this.tokenizer === null) {
      throw new Error('NamoTurnDetector is closed');
    }
    const text = (transcript ?? '').trim();
    if (!text) return 0;

    const { inputIds, attentionMask } = this.tokenizer.encode(text, this.maxTokensValue);
    const seqLen = inputIds.length;
    const { Tensor } = this.runtime;
    const feeds = {
      input_ids: new Tensor('int64', inputIds, [1, seqLen]),
      attention_mask: new Tensor('int64', attentionMask, [1, seqLen]),
    };
    const results = await this.session.run(feeds);
    const first = Object.values(results)[0] as OnnxTensor | undefined;
    return completionProbability(first?.data);
  }

  /** Release the ONNX session + tokenizer. Idempotent. */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    // onnxruntime-node sessions are garbage-collected; drop the refs.
    this.session = null;
    this.tokenizer = null;
  }
}
