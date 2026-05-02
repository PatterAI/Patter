/**
 * Silero VAD provider.
 *
 * Acoustic voice activity detection backed by the Silero ONNX model. Buffers
 * incoming int16 LE PCM frames, runs inference on fixed-size windows
 * (256 samples at 8 kHz, 512 at 16 kHz), applies an exponential probability
 * filter, and emits VADEvent transitions (speech_start / speech_end).
 *
 * Notes:
 *   - Input is raw PCM `Buffer` (int16 LE, mono) via
 *     `processFrame(pcmChunk, sampleRate)`.
 *   - onnxruntime-node is loaded lazily as an optional dependency.
 *   - Emits `VADEvent` (Patter protocol).
 */

import { createRequire } from 'node:module';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { VADEvent, VADProvider } from '../types';

const SUPPORTED_SAMPLE_RATES = [8000, 16000] as const;
/** Sample rates supported by the bundled Silero ONNX model (8 kHz or 16 kHz). */
export type SileroSampleRate = (typeof SUPPORTED_SAMPLE_RATES)[number];

// Resolve __dirname in a way that works for both the CJS (dist/index.js)
// and the ESM (dist/index.mjs) bundles tsup emits. Top-level ``__dirname``
// is undefined in ESM, and ``import.meta`` is undefined in CJS — pick one.
function resolveModuleDir(): string {
  // CJS path: __dirname is a per-module binding, not a global. Detect via
  // typeof inside a function that is only evaluated at runtime; bundlers
  // preserve the reference in CJS output.
  try {
    // eslint-disable-next-line no-new-func
    const cjsDir = new Function("return typeof __dirname !== 'undefined' ? __dirname : null")();
    if (typeof cjsDir === 'string') return cjsDir;
  } catch { /* fall through */ }
  // ESM path
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const url = (import.meta as { url?: string }).url;
    if (url) return path.dirname(fileURLToPath(url));
  } catch { /* fall through */ }
  return process.cwd();
}

const MODULE_DIR = resolveModuleDir();
function resolveDefaultModelPath(): string {
  // tsup ships resources/ alongside the bundled dist/index.{js,mjs}, so the
  // ONNX model lives at MODULE_DIR/resources/silero_vad.onnx in published
  // builds. When developing from source (src/providers/...), the model
  // lives one level up in src/resources/ instead. Try both.
  const candidates = [
    path.join(MODULE_DIR, 'resources', 'silero_vad.onnx'),
    path.join(MODULE_DIR, '..', 'resources', 'silero_vad.onnx'),
  ];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return candidates[0];
}
const DEFAULT_MODEL_PATH = resolveDefaultModelPath();

/** Options accepted by {@link SileroVAD.load}. */
export interface SileroVADOptions {
  minSpeechDuration?: number;
  minSilenceDuration?: number;
  prefixPaddingDuration?: number;
  activationThreshold?: number;
  deactivationThreshold?: number;
  sampleRate?: SileroSampleRate;
  forceCpu?: boolean;
  onnxFilePath?: string;
}

/**
 * Minimal structural type for the subset of `onnxruntime-node` we depend on.
 * Declared locally so consumers don't need the package installed at build time.
 */
/** Minimal subset of `onnxruntime-node`'s `InferenceSession` that Silero needs. */
export interface OnnxInferenceSession {
  run(
    feeds: Record<string, OnnxTensor>,
  ): Promise<Record<string, OnnxTensor>>;
}

/** Minimal subset of an `onnxruntime-node` tensor used by Silero inference. */
export interface OnnxTensor {
  readonly data: Float32Array | BigInt64Array;
  readonly dims: readonly number[];
}

/** Minimal `onnxruntime-node` module surface accepted by {@link SileroVAD}. */
export interface OnnxRuntime {
  InferenceSession: {
    create(
      pathOrBuffer: string | Uint8Array,
      options?: Record<string, unknown>,
    ): Promise<OnnxInferenceSession>;
  };
  Tensor: new (
    type: 'float32' | 'int64',
    data: Float32Array | BigInt64Array,
    dims: readonly number[],
  ) => OnnxTensor;
}

/**
 * Heuristic to distinguish three failure modes for surface-up to the user:
 *
 * 1. **Module not found** — the package isn't installed at all. Install with
 *    ``npm install onnxruntime-node@~1.18.0``.
 * 2. **Native binding mismatch** — the package is installed but the prebuilt
 *    ``.node`` for the host platform/arch is missing or corrupt (typical on
 *    macOS x86_64 + post-1.18 versions where the bundled bin layout drifted).
 *    Pin to ``~1.18.0`` or rebuild from source.
 * 3. **API drift** — the installed version exposes a different internal
 *    surface than the SDK was tested against (1.24+: ``listSupportedBackends``
 *    moved/removed). Pin to ``~1.18.0``.
 */
function classifyOnnxError(err: unknown): 'missing' | 'binding' | 'api-drift' | 'unknown' {
  const msg = (err as Error)?.message ?? String(err);
  if (/Cannot find module ['"]?onnxruntime-node['"]?$/m.test(msg)) return 'missing';
  if (/onnxruntime_binding\.node|napi-v\d/.test(msg)) return 'binding';
  if (/listSupportedBackends|backend_\d/.test(msg)) return 'api-drift';
  return 'unknown';
}

async function loadOnnxRuntime(): Promise<OnnxRuntime> {
  let firstErr: unknown;
  // 1. Plain dynamic import — works when onnxruntime-node is hoisted to a
  //    node_modules folder Node's resolver can find from the running script.
  try {
    // onnxruntime-node is an optional peer dep; resolved dynamically so the
    // SDK keeps installing on systems that don't need server-side VAD.
    // @ts-ignore — types may be absent when the optional dep is not installed
    const mod = await import('onnxruntime-node');
    return mod as unknown as OnnxRuntime;
  } catch (e) {
    firstErr = e;
  }
  // 2. Fallback: createRequire from the user's cwd. Catches the case where
  //    onnxruntime-node is installed in the project root but the SDK lives
  //    elsewhere (e.g. file: linked workspaces).
  try {
    const req = createRequire(path.join(process.cwd(), 'package.json'));
    return req('onnxruntime-node') as OnnxRuntime;
  } catch (secondErr) {
    // Classify each failure separately — they may be different (the import
    // could hit API drift while the require hits a missing native binding).
    const importClass = classifyOnnxError(firstErr);
    const requireClass = classifyOnnxError(secondErr);
    const original = (firstErr as Error)?.message ?? String(firstErr);
    const detail = (secondErr as Error)?.message ?? String(secondErr);

    let header: string;
    let remedy: string;
    if (importClass === 'missing' && requireClass === 'missing') {
      header = 'SileroVAD requires the "onnxruntime-node" package — it is not installed.';
      remedy = '  Install:  npm install onnxruntime-node@~1.18.0\n\n' +
        '  (~210 MB. Only needed when you actually use SileroVAD in pipeline mode.)';
    } else if (importClass === 'api-drift' || requireClass === 'api-drift') {
      header = 'SileroVAD found onnxruntime-node but the installed version uses an API the SDK does not support.';
      remedy = '  Patter is currently tested against onnxruntime-node 1.18.x.\n\n' +
        '  Fix:  npm install onnxruntime-node@~1.18.0\n\n' +
        '  Versions 1.24+ removed `listSupportedBackends` from the public surface — track\n' +
        '  https://github.com/PatterAI/Patter/issues for the SDK update that targets 1.24.';
    } else if (importClass === 'binding' || requireClass === 'binding') {
      header = 'SileroVAD found onnxruntime-node but the native binding for this platform is missing.';
      remedy = '  Common cause on macOS x86_64: the prebuilt bin/ layout drifted between releases.\n\n' +
        '  Fix:  npm install onnxruntime-node@~1.18.0\n\n' +
        '  Or rebuild from source:  npm rebuild onnxruntime-node';
    } else {
      header = 'SileroVAD requires the "onnxruntime-node" package, which could not be resolved.';
      remedy = '  Install:  npm install onnxruntime-node@~1.18.0\n\n' +
        '  This is an optional peer dependency of getpatter (~210 MB).';
    }

    const err = new Error(
      `\n${header}\n\n${remedy}\n\n` +
        `  import() failed:     ${original}\n` +
        `  cwd-require failed:  ${detail}\n`,
    );
    // Attach the underlying causes so users running with --stack-trace-limit
    // / debuggers can still drill into the real ONNX error chain.
    (err as Error & { cause?: unknown }).cause = secondErr ?? firstErr;
    throw err;
  }
}

/** Exponential smoothing filter. */
class ExpFilter {
  private filtered: number | null = null;

  constructor(private readonly alpha: number) {
    if (!(alpha > 0 && alpha <= 1)) {
      throw new Error('alpha must be in (0, 1].');
    }
  }

  apply(exp: number, sample: number): number {
    if (this.filtered === null) {
      this.filtered = sample;
    } else {
      const a = Math.pow(this.alpha, exp);
      this.filtered = a * this.filtered + (1 - a) * sample;
    }
    return this.filtered;
  }

  reset(): void {
    this.filtered = null;
  }
}

/**
 * Stateful single-window wrapper for the Silero VAD ONNX model.
 * Maintains the RNN hidden state and rolling context buffer across calls.
 */
class OnnxModel {
  readonly sampleRate: SileroSampleRate;
  readonly windowSizeSamples: number;
  readonly contextSize: number;

  private context: Float32Array;
  private rnnState: Float32Array;
  private inputBuffer: Float32Array;
  private readonly sampleRateTensor: BigInt64Array;

  constructor(
    private readonly runtime: OnnxRuntime,
    private readonly session: OnnxInferenceSession,
    sampleRate: SileroSampleRate,
  ) {
    if (!SUPPORTED_SAMPLE_RATES.includes(sampleRate)) {
      throw new Error('Silero VAD only supports 8KHz and 16KHz sample rates');
    }
    this.sampleRate = sampleRate;
    this.windowSizeSamples = sampleRate === 8000 ? 256 : 512;
    this.contextSize = sampleRate === 8000 ? 32 : 64;

    this.context = new Float32Array(this.contextSize);
    this.rnnState = new Float32Array(2 * 1 * 128);
    this.inputBuffer = new Float32Array(this.contextSize + this.windowSizeSamples);
    this.sampleRateTensor = BigInt64Array.from([BigInt(sampleRate)]);
  }

  async run(window: Float32Array): Promise<number> {
    if (window.length !== this.windowSizeSamples) {
      throw new Error(
        `window must have exactly ${this.windowSizeSamples} samples, got ${window.length}`,
      );
    }

    // Compose [context | window] into the input buffer.
    this.inputBuffer.set(this.context, 0);
    this.inputBuffer.set(window, this.contextSize);

    const { Tensor } = this.runtime;
    const feeds = {
      input: new Tensor('float32', this.inputBuffer, [1, this.inputBuffer.length]),
      state: new Tensor('float32', this.rnnState, [2, 1, 128]),
      sr: new Tensor('int64', this.sampleRateTensor, []),
    };

    const results = await this.session.run(feeds);
    const outputKey = Object.keys(results).find((k) => k !== 'stateN') ?? 'output';
    const stateKey = 'stateN' in results ? 'stateN' : Object.keys(results).find((k) => k !== outputKey);
    const out = results[outputKey];
    const newState = stateKey ? results[stateKey] : undefined;

    if (newState && newState.data instanceof Float32Array) {
      this.rnnState = Float32Array.from(newState.data);
    }

    // Update rolling context with the tail of the combined input.
    this.context = this.inputBuffer.slice(-this.contextSize);

    const data = out.data as Float32Array;
    return data[0] ?? 0;
  }
}

/**
 * Silero-based `VADProvider`. Load via `SileroVAD.load()`:
 *
 *     const vad = await SileroVAD.load({ sampleRate: 16000 });
 *     const evt = await vad.processFrame(pcm, 16000);
 *     if (evt && evt.type === 'speech_start') { ... }
 *     await vad.close();
 */
export class SileroVAD implements VADProvider {
  private pending: Float32Array = new Float32Array(0);
  private expFilter = new ExpFilter(0.35);
  private pubSpeaking = false;
  private speechThresholdDuration = 0;
  private silenceThresholdDuration = 0;
  private closed = false;

  private constructor(
    private readonly model: OnnxModel,
    private readonly opts: Required<Omit<SileroVADOptions, 'onnxFilePath' | 'forceCpu'>>,
  ) {}

  /**
   * Load the Silero VAD model.
   * Throws if `onnxruntime-node` is not installed.
   */
  static async load(options: SileroVADOptions = {}): Promise<SileroVAD> {
    const sampleRate = (options.sampleRate ?? 16000) as SileroSampleRate;
    if (!SUPPORTED_SAMPLE_RATES.includes(sampleRate)) {
      throw new Error('Silero VAD only supports 8KHz and 16KHz sample rates');
    }

    const activationThreshold = options.activationThreshold ?? 0.5;
    const deactivationThreshold =
      options.deactivationThreshold ?? Math.max(activationThreshold - 0.15, 0.01);
    if (deactivationThreshold <= 0) {
      throw new Error('deactivationThreshold must be greater than 0');
    }

    const runtime = await loadOnnxRuntime();
    const modelPath = options.onnxFilePath ?? DEFAULT_MODEL_PATH;
    const session = await runtime.InferenceSession.create(modelPath, {
      interOpNumThreads: 1,
      intraOpNumThreads: 1,
      executionMode: 'sequential',
      executionProviders: options.forceCpu === false ? undefined : ['cpu'],
    });

    const model = new OnnxModel(runtime, session, sampleRate);
    return new SileroVAD(model, {
      minSpeechDuration: options.minSpeechDuration ?? 0.05,
      minSilenceDuration: options.minSilenceDuration ?? 0.55,
      prefixPaddingDuration: options.prefixPaddingDuration ?? 0.5,
      activationThreshold,
      deactivationThreshold,
      sampleRate,
    });
  }

  /**
   * Internal factory used by tests — bypasses onnxruntime-node loading.
   * @internal
   */
  static fromOnnxModel(
    runtime: OnnxRuntime,
    session: OnnxInferenceSession,
    options: Required<Omit<SileroVADOptions, 'onnxFilePath' | 'forceCpu'>>,
  ): SileroVAD {
    const model = new OnnxModel(runtime, session, options.sampleRate);
    return new SileroVAD(model, options);
  }

  /** Sample rate (Hz) the underlying ONNX model was loaded with. */
  get sampleRate(): SileroSampleRate {
    return this.opts.sampleRate;
  }

  /**
   * Number of int16 PCM samples that must be provided per call to
   * processFrame for the model to run one inference window.
   *
   * Constraint (Silero ONNX spec):
   *   - 16 000 Hz → 512 samples (32 ms)
   *   -  8 000 Hz → 256 samples (32 ms)
   *
   * Callers that feed raw audio in fixed-size chunks (e.g. WebSocket frames)
   * should buffer incoming audio until at least numFramesRequired() int16
   * samples are available before calling processFrame.  The provider
   * internally buffers partial windows so smaller chunks are also safe, but
   * passing exactly one window per call minimises heap allocation.
   */
  numFramesRequired(): number {
    return this.opts.sampleRate === 8000 ? 256 : 512;
  }


  /** Run VAD on a PCM16 chunk; returns a transition event or null if no change. */
  async processFrame(pcmChunk: Buffer, sampleRate: number): Promise<VADEvent | null> {
    if (this.closed) {
      throw new Error('SileroVAD is closed');
    }
    if (sampleRate !== this.opts.sampleRate) {
      throw new Error(
        `input sampleRate ${sampleRate} does not match model sampleRate ${this.opts.sampleRate}; resampling is not implemented in the Patter port`,
      );
    }
    if (pcmChunk.length === 0) {
      return null;
    }

    // int16 LE PCM -> Float32Array in [-1.0, 1.0]
    const numSamples = Math.floor(pcmChunk.length / 2);
    if (numSamples === 0) {
      return null;
    }
    const samples = new Float32Array(numSamples);
    for (let i = 0; i < numSamples; i++) {
      samples[i] = pcmChunk.readInt16LE(i * 2) / 32767;
    }

    // Append to pending buffer
    const merged = new Float32Array(this.pending.length + samples.length);
    merged.set(this.pending, 0);
    merged.set(samples, this.pending.length);
    this.pending = merged;

    const windowSize = this.model.windowSizeSamples;
    let event: VADEvent | null = null;

    while (this.pending.length >= windowSize) {
      const window = this.pending.slice(0, windowSize);
      this.pending = this.pending.slice(windowSize);

      const rawP = await this.model.run(window);
      const p = this.expFilter.apply(1.0, rawP);

      const windowDuration = windowSize / this.opts.sampleRate;
      const transition = this.advanceState(p, windowDuration);
      if (transition !== null) {
        event = transition;  // overwrite — last event wins
      }
    }

    return event;
  }

  private advanceState(p: number, windowDuration: number): VADEvent | null {
    const opts = this.opts;
    if (p >= opts.activationThreshold || (this.pubSpeaking && p > opts.deactivationThreshold)) {
      this.speechThresholdDuration += windowDuration;
      this.silenceThresholdDuration = 0;

      if (!this.pubSpeaking) {
        if (this.speechThresholdDuration >= opts.minSpeechDuration) {
          this.pubSpeaking = true;
          return {
            type: 'speech_start',
            confidence: p,
            durationMs: this.speechThresholdDuration * 1000,
          };
        }
      }
    } else {
      this.silenceThresholdDuration += windowDuration;
      this.speechThresholdDuration = 0;

      if (
        this.pubSpeaking &&
        this.silenceThresholdDuration >= opts.minSilenceDuration
      ) {
        this.pubSpeaking = false;
        return {
          type: 'speech_end',
          confidence: p,
          durationMs: this.silenceThresholdDuration * 1000,
        };
      }
    }
    return null;
  }

  /** Mark the VAD as closed; subsequent processFrame calls throw. */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    // onnxruntime-node sessions are garbage-collected; no explicit release API.
  }
}
