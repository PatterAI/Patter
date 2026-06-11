/**
 * Smart-turn v3 semantic turn detector (ONNX).
 *
 * Audio-native end-of-utterance model from the pipecat-ai project
 * (https://github.com/pipecat-ai/smart-turn, Apache-2.0 — ~8 M params,
 * <100 ms CPU inference). Unlike a VAD — which only knows *whether* the
 * caller is producing sound — smart-turn looks at the prosody of the last
 * few seconds of speech and predicts whether the caller has *finished
 * their turn* or is merely pausing mid-sentence ("My phone number is…").
 *
 * Wiring: pass an instance as `agent.turnDetector`. The pipeline stream
 * handler then defers the STT finalize that normally fires on a VAD
 * `speech_end` until the model agrees the turn is complete (probability
 * ≥ {@link SmartTurnDetector.threshold}), holding for at most
 * `agent.maxSemanticHoldMs` (default 1200 ms) so a turn can never hang.
 *
 * Model file: the ONNX weights are NOT bundled with the SDK (~30 MB).
 * Download a `smart-turn-v3*.onnx` file from
 * https://huggingface.co/pipecat-ai/smart-turn-v3 and point the SDK at it
 * via the `PATTER_SMART_TURN_MODEL` environment variable or the
 * `modelPath` option of {@link SmartTurnDetector.load}.
 *
 * Preprocessing (matches `pipecat-ai/smart-turn` `inference.py` exactly —
 * the v3 ONNX graph takes Whisper log-mel features, not a raw waveform):
 *
 *  1. int16 LE PCM → float in [-1, 1] (÷ 32768).
 *  2. Keep the LAST 8 s of 16 kHz audio; left-pad with zeros to exactly
 *     128 000 samples so the speech sits at the END of the window
 *     (`truncate_audio_to_last_n_seconds`).
 *  3. Zero-mean / unit-variance normalize the full padded window
 *     (`WhisperFeatureExtractor(..., do_normalize=True)`).
 *  4. Whisper log-mel: 400-point Hann STFT (hop 160, reflect-padded,
 *     centered), 80 Slaney-scale mel filters, `log10` with 1e-10 floor,
 *     clamp to `max - 8`, scale `(x + 4) / 4`, drop the trailing frame
 *     → `(80, 800)` float32.
 *  5. `session.run({ input_features })` — the graph applies the sigmoid
 *     internally and returns the end-of-turn probability directly.
 *     `probability > 0.5` ⇒ turn complete.
 *
 * `onnxruntime-node` is loaded lazily as an optional dependency (same
 * pattern as `providers/silero-vad.ts`). The feature extraction is pure
 * TypeScript (mixed-radix FFT) and yields to the event loop periodically
 * so a per-turn prediction never blocks inbound audio handling.
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

/** Env var consulted by {@link SmartTurnDetector.load} when no `modelPath` is given. */
export const SMART_TURN_MODEL_ENV_VAR = 'PATTER_SMART_TURN_MODEL';

/** Smart-turn v3 input contract — 16 kHz mono, up to 8 s of context. */
export const SMART_TURN_SAMPLE_RATE = 16000;
export const SMART_TURN_MAX_SECONDS = 8;
export const SMART_TURN_MAX_SAMPLES = SMART_TURN_SAMPLE_RATE * SMART_TURN_MAX_SECONDS; // 128 000

/**
 * Default decision threshold per the smart-turn v3 docs
 * (`prediction = 1 if probability > 0.5 else 0`).
 */
export const DEFAULT_SMART_TURN_THRESHOLD = 0.5;

// Whisper feature-extractor constants (WhisperFeatureExtractor defaults).
const N_FFT = 400;
const HOP_LENGTH = 160;
const N_MELS = 80;
const N_FRAMES = 800; // 8 s × 100 frames/s after dropping the trailing frame
const MEL_FLOOR = 1e-10;
const NORM_EPS = 1e-7;

const DOWNLOAD_HINT =
  'Download a smart-turn-v3 ONNX file from ' +
  'https://huggingface.co/pipecat-ai/smart-turn-v3 and either set the ' +
  `${SMART_TURN_MODEL_ENV_VAR} environment variable to its path or pass ` +
  'modelPath to SmartTurnDetector.load(). The model is not bundled with ' +
  'the SDK (~30 MB).';

/** Options accepted by {@link SmartTurnDetector.load}. */
export interface SmartTurnDetectorOptions {
  /**
   * End-of-turn probability at/above which the turn is considered
   * complete. Default 0.5 per the smart-turn v3 reference.
   */
  readonly threshold?: number;
  /**
   * Path to the `smart-turn-v3*.onnx` file. Falls back to the
   * `PATTER_SMART_TURN_MODEL` environment variable when omitted.
   */
  readonly modelPath?: string;
  /** Restrict ONNX Runtime to the CPU execution provider (default true). */
  readonly forceCpu?: boolean;
}

/**
 * Resolve the smart-turn ONNX file from the option or the env var.
 * Throws with download instructions when unset or missing.
 * @internal
 */
export function resolveSmartTurnModelPath(modelPath?: string): string {
  let resolved = modelPath;
  if (!resolved) {
    resolved = (process.env[SMART_TURN_MODEL_ENV_VAR] ?? '').trim();
    if (!resolved) {
      throw new Error(
        `SmartTurnDetector has no model file configured. ${DOWNLOAD_HINT}`,
      );
    }
  }
  if (!fs.existsSync(resolved)) {
    throw new Error(`Smart-turn model file not found: ${resolved}. ${DOWNLOAD_HINT}`);
  }
  if (!fs.statSync(resolved).isFile()) {
    throw new Error(`Smart-turn model path is not a file: ${resolved}. ${DOWNLOAD_HINT}`);
  }
  return path.resolve(resolved);
}

// ---------------------------------------------------------------------------
// Whisper log-mel feature extraction (TS port of WhisperFeatureExtractor —
// verified against `transformers.WhisperFeatureExtractor(chunk_length=8)`
// via the Python twin, getpatter/providers/smart_turn.py)
// ---------------------------------------------------------------------------

/** Hertz → mel on the Slaney scale (linear < 1 kHz, log above). */
function hertzToMelSlaney(freq: number): number {
  const minLogHertz = 1000.0;
  const minLogMel = 15.0;
  const logstep = 27.0 / Math.log(6.4);
  if (freq >= minLogHertz) {
    return minLogMel + Math.log(freq / minLogHertz) * logstep;
  }
  return (3.0 * freq) / 200.0;
}

/** Mel → Hertz, inverse of {@link hertzToMelSlaney}. */
function melToHertzSlaney(mels: number): number {
  const minLogHertz = 1000.0;
  const minLogMel = 15.0;
  const logstep = Math.log(6.4) / 27.0;
  if (mels >= minLogMel) {
    return minLogHertz * Math.exp(logstep * (mels - minLogMel));
  }
  return (200.0 * mels) / 3.0;
}

interface SparseMelFilter {
  /** First FFT bin with non-zero weight. */
  readonly startBin: number;
  /** Triangular filter weights for bins `startBin .. startBin+len-1`. */
  readonly weights: Float64Array;
}

let melFilterbankCache: SparseMelFilter[] | null = null;

/**
 * 80-filter Slaney-normalized triangular mel filterbank over 201 FFT bins
 * (`mel_filter_bank(norm="slaney", mel_scale="slaney")`), stored sparsely
 * (each triangle covers only a handful of bins). Cached after first build.
 */
function melFilterbank(): SparseMelFilter[] {
  if (melFilterbankCache) return melFilterbankCache;

  const numBins = 1 + N_FFT / 2; // 201
  const fftFreqs = new Float64Array(numBins);
  for (let k = 0; k < numBins; k++) {
    fftFreqs[k] = (k * (SMART_TURN_SAMPLE_RATE / 2)) / (numBins - 1);
  }

  const melMin = hertzToMelSlaney(0.0);
  const melMax = hertzToMelSlaney(SMART_TURN_SAMPLE_RATE / 2);
  const filterFreqs = new Float64Array(N_MELS + 2);
  for (let i = 0; i < N_MELS + 2; i++) {
    filterFreqs[i] = melToHertzSlaney(melMin + ((melMax - melMin) * i) / (N_MELS + 1));
  }

  const filters: SparseMelFilter[] = [];
  for (let m = 0; m < N_MELS; m++) {
    const lower = filterFreqs[m];
    const center = filterFreqs[m + 1];
    const upper = filterFreqs[m + 2];
    const enorm = 2.0 / (upper - lower); // Slaney energy normalization
    const dense = new Float64Array(numBins);
    let startBin = -1;
    let endBin = -1;
    for (let k = 0; k < numBins; k++) {
      const down = (fftFreqs[k] - lower) / (center - lower);
      const up = (upper - fftFreqs[k]) / (upper - center);
      const w = Math.max(0, Math.min(down, up)) * enorm;
      dense[k] = w;
      if (w > 0) {
        if (startBin === -1) startBin = k;
        endBin = k;
      }
    }
    if (startBin === -1) {
      filters.push({ startBin: 0, weights: new Float64Array(0) });
    } else {
      filters.push({ startBin, weights: dense.slice(startBin, endBin + 1) });
    }
  }
  melFilterbankCache = filters;
  return filters;
}

let hannWindowCache: Float64Array | null = null;

/** Periodic 400-point Hann window (`window_function(400, "hann")`). */
function hannWindow(): Float64Array {
  if (!hannWindowCache) {
    const w = new Float64Array(N_FFT);
    for (let n = 0; n < N_FFT; n++) {
      w[n] = 0.5 - 0.5 * Math.cos((2 * Math.PI * n) / N_FFT);
    }
    hannWindowCache = w;
  }
  return hannWindowCache;
}

// 25-point naive DFT twiddle tables (cos/sin of -2πjk/25) — the odd base
// case of the mixed-radix FFT below. 400 = 2^4 × 25.
let dft25Cos: Float64Array | null = null;
let dft25Sin: Float64Array | null = null;

function dft25Tables(): { cos: Float64Array; sin: Float64Array } {
  if (!dft25Cos || !dft25Sin) {
    dft25Cos = new Float64Array(25 * 25);
    dft25Sin = new Float64Array(25 * 25);
    for (let k = 0; k < 25; k++) {
      for (let j = 0; j < 25; j++) {
        const angle = (-2 * Math.PI * k * j) / 25;
        dft25Cos[k * 25 + j] = Math.cos(angle);
        dft25Sin[k * 25 + j] = Math.sin(angle);
      }
    }
  }
  return { cos: dft25Cos, sin: dft25Sin };
}

// Per-size butterfly twiddle tables (cos/sin of -2πk/n for k < n/2) and
// reusable scratch buffers. The FFT runs once per STFT frame (801 frames
// per prediction) — recomputing trig in the butterfly loop dominated the
// profile, and per-call scratch allocation churned the GC.
const fftTwiddleCos = new Map<number, Float64Array>();
const fftTwiddleSin = new Map<number, Float64Array>();
const fftScratch = new Map<number, [Float64Array, Float64Array, Float64Array, Float64Array]>();

function fftTables(n: number): { cos: Float64Array; sin: Float64Array } {
  let cos = fftTwiddleCos.get(n);
  let sin = fftTwiddleSin.get(n);
  if (!cos || !sin) {
    const half = n / 2;
    cos = new Float64Array(half);
    sin = new Float64Array(half);
    for (let k = 0; k < half; k++) {
      const angle = (-2 * Math.PI * k) / n;
      cos[k] = Math.cos(angle);
      sin[k] = Math.sin(angle);
    }
    fftTwiddleCos.set(n, cos);
    fftTwiddleSin.set(n, sin);
  }
  return { cos, sin };
}

function fftScratchFor(n: number): [Float64Array, Float64Array, Float64Array, Float64Array] {
  let bufs = fftScratch.get(n);
  if (!bufs) {
    bufs = [
      new Float64Array(n),
      new Float64Array(n),
      new Float64Array(n),
      new Float64Array(n),
    ];
    fftScratch.set(n, bufs);
  }
  return bufs;
}

// Dedicated output scratch for the 25-point base case. MUST be distinct from
// `fftScratchFor(25)`: an n=50 parent stores its even/odd halves in the
// size-25 scratch and then runs the base case ON those very arrays — if the
// base case also wrote its output through `fftScratchFor(25)` it would (a)
// alias its own input (out[k] clobbers in[k] while later k's still read it)
// and (b) let the odd-half DFT overwrite the parent's stored even-half
// results before the butterfly combines them.
const dft25OutRe = new Float64Array(25);
const dft25OutIm = new Float64Array(25);

/**
 * In-place complex FFT for n = 2^a × 25 (Cooley-Tukey radix-2 recursion
 * with a table-driven 25-point naive DFT base case). Whisper's STFT uses
 * n_fft = 400, which is not a power of two, so a plain radix-2 FFT cannot
 * be used without changing the bin frequencies. Single-threaded by
 * design: scratch buffers are module-level and reused across calls (safe
 * — Node executes the synchronous frame loop without interleaving).
 */
function fftComplex(re: Float64Array, im: Float64Array): void {
  const n = re.length;
  if (n === 25) {
    const { cos, sin } = dft25Tables();
    for (let k = 0; k < 25; k++) {
      let sumRe = 0;
      let sumIm = 0;
      const row = k * 25;
      for (let j = 0; j < 25; j++) {
        const c = cos[row + j];
        const s = sin[row + j];
        sumRe += re[j] * c - im[j] * s;
        sumIm += re[j] * s + im[j] * c;
      }
      dft25OutRe[k] = sumRe;
      dft25OutIm[k] = sumIm;
    }
    re.set(dft25OutRe);
    im.set(dft25OutIm);
    return;
  }
  if (n === 1) return;
  const half = n / 2;
  // Recursion uses scratch keyed by the CHILD size; the child's own
  // recursion uses the next size down and the 25-point base case writes
  // through its dedicated `dft25Out*` buffers, so levels never alias. The
  // child data is fully consumed back into re/im before this frame returns.
  const [evenRe, evenIm, oddRe, oddIm] = fftScratchFor(half);
  for (let i = 0; i < half; i++) {
    evenRe[i] = re[2 * i];
    evenIm[i] = im[2 * i];
    oddRe[i] = re[2 * i + 1];
    oddIm[i] = im[2 * i + 1];
  }
  fftComplex(evenRe.subarray(0, half), evenIm.subarray(0, half));
  fftComplex(oddRe.subarray(0, half), oddIm.subarray(0, half));
  const { cos, sin } = fftTables(n);
  for (let k = 0; k < half; k++) {
    const wr = cos[k];
    const wi = sin[k];
    const tr = wr * oddRe[k] - wi * oddIm[k];
    const ti = wr * oddIm[k] + wi * oddRe[k];
    re[k] = evenRe[k] + tr;
    im[k] = evenIm[k] + ti;
    re[k + half] = evenRe[k] - tr;
    im[k + half] = evenIm[k] - ti;
  }
}

/**
 * Truncate/left-pad `samples` to exactly 8 s and normalize.
 *
 * Mirrors smart-turn's `truncate_audio_to_last_n_seconds` (keep the END
 * of the audio, pad zeros at the BEGINNING) followed by the feature
 * extractor's `do_normalize=True` zero-mean / unit-variance pass over the
 * full padded window. Returns a Float64Array of length 128 000.
 * @internal — exported for tests.
 */
export function prepareInputWindow(samples: ArrayLike<number>): Float64Array {
  const out = new Float64Array(SMART_TURN_MAX_SAMPLES);
  const n = samples.length;
  if (n >= SMART_TURN_MAX_SAMPLES) {
    const offset = n - SMART_TURN_MAX_SAMPLES;
    for (let i = 0; i < SMART_TURN_MAX_SAMPLES; i++) out[i] = samples[offset + i];
  } else {
    const padding = SMART_TURN_MAX_SAMPLES - n;
    for (let i = 0; i < n; i++) out[padding + i] = samples[i];
  }

  let mean = 0;
  for (let i = 0; i < SMART_TURN_MAX_SAMPLES; i++) mean += out[i];
  mean /= SMART_TURN_MAX_SAMPLES;
  let variance = 0;
  for (let i = 0; i < SMART_TURN_MAX_SAMPLES; i++) {
    const d = out[i] - mean;
    variance += d * d;
  }
  variance /= SMART_TURN_MAX_SAMPLES;
  const scale = 1 / Math.sqrt(variance + NORM_EPS);
  for (let i = 0; i < SMART_TURN_MAX_SAMPLES; i++) out[i] = (out[i] - mean) * scale;
  return out;
}

/**
 * Whisper log-mel features for a prepared 8 s window.
 *
 * TS port of `WhisperFeatureExtractor._np_extract_fbank_features` (the
 * preprocessing smart-turn v3 runs before the ONNX graph): reflect-padded
 * centered STFT (n_fft 400, hop 160, periodic Hann), power spectrum,
 * Slaney mel projection, `log10` with a 1e-10 floor, clamp to `max - 8`,
 * scale `(x + 4) / 4`, drop the trailing frame.
 *
 * Returns a row-major Float32Array of shape `(80, 800)` — index
 * `[mel * 800 + frame]` — ready to wrap in a `[1, 80, 800]` ONNX tensor.
 * Yields to the event loop every 128 frames so a per-turn prediction
 * cannot starve inbound audio handling.
 * @internal — exported for tests.
 */
export async function computeWhisperLogMelFeatures(
  window: Float64Array,
): Promise<Float32Array> {
  if (window.length !== SMART_TURN_MAX_SAMPLES) {
    throw new Error(
      `expected ${SMART_TURN_MAX_SAMPLES} samples, got ${window.length}; ` +
        'run prepareInputWindow() first',
    );
  }

  const half = N_FFT / 2; // 200 — center padding
  const paddedLen = SMART_TURN_MAX_SAMPLES + N_FFT; // reflect-pad both sides
  const numBins = 1 + N_FFT / 2; // 201

  // Reflect padding (numpy `mode="reflect"`: edge sample not repeated).
  const padded = new Float64Array(paddedLen);
  for (let i = 0; i < half; i++) padded[i] = window[half - i];
  padded.set(window, half);
  for (let i = 0; i < half; i++) {
    padded[half + SMART_TURN_MAX_SAMPLES + i] = window[SMART_TURN_MAX_SAMPLES - 2 - i];
  }

  const hann = hannWindow();
  const filters = melFilterbank();
  const totalFrames = 1 + Math.floor((paddedLen - N_FFT) / HOP_LENGTH); // 801

  // log-mel over the RETAINED frames only (Whisper drops the last frame
  // BEFORE the dynamic-range clamp, so the max is computed on 800 frames).
  const logSpec = new Float64Array(N_MELS * N_FRAMES);
  const re = new Float64Array(N_FFT);
  const im = new Float64Array(N_FFT);
  const power = new Float64Array(numBins);
  let maxLog = -Infinity;

  for (let t = 0; t < totalFrames - 1; t++) {
    const start = t * HOP_LENGTH;
    for (let j = 0; j < N_FFT; j++) {
      re[j] = padded[start + j] * hann[j];
      im[j] = 0;
    }
    fftComplex(re, im);
    for (let k = 0; k < numBins; k++) {
      power[k] = re[k] * re[k] + im[k] * im[k];
    }
    for (let m = 0; m < N_MELS; m++) {
      const { startBin, weights } = filters[m];
      let acc = 0;
      for (let j = 0; j < weights.length; j++) {
        acc += power[startBin + j] * weights[j];
      }
      const v = Math.log10(Math.max(acc, MEL_FLOOR));
      logSpec[m * N_FRAMES + t] = v;
      if (v > maxLog) maxLog = v;
    }
    // Keep the event loop responsive — ~6 yields per 8 s window.
    if ((t & 127) === 127) {
      await new Promise<void>((resolve) => setImmediate(resolve));
    }
  }

  const floor = maxLog - 8.0;
  const out = new Float32Array(N_MELS * N_FRAMES);
  for (let i = 0; i < logSpec.length; i++) {
    out[i] = (Math.max(logSpec[i], floor) + 4.0) / 4.0;
  }
  return out;
}

/** int16 LE PCM bytes → smart-turn `input_features` data (80 × 800). @internal */
export async function featuresFromPcm16(pcm16Window: Buffer): Promise<Float32Array> {
  const numSamples = Math.floor(pcm16Window.length / 2);
  const samples = new Float64Array(numSamples);
  for (let i = 0; i < numSamples; i++) {
    // ÷32768 matches smart-turn / pipecat int16→float conversion. The
    // exact scale is irrelevant after zero-mean/unit-variance
    // normalization, but kept identical for parity with the reference.
    samples[i] = pcm16Window.readInt16LE(i * 2) / 32768;
  }
  return computeWhisperLogMelFeatures(prepareInputWindow(samples));
}

/**
 * Semantic end-of-utterance detector backed by smart-turn v3 (ONNX).
 *
 * Load via {@link SmartTurnDetector.load} (throws with actionable
 * instructions when the optional deps or the model file are missing) or
 * {@link SmartTurnDetector.maybeLoad} (warns once and resolves `undefined`
 * instead, so the agent degrades to plain VAD-silence endpointing rather
 * than crashing):
 *
 * ```ts
 * const detector = await SmartTurnDetector.load();   // PATTER_SMART_TURN_MODEL
 * // or: await SmartTurnDetector.load({ modelPath: '…/smart-turn-v3.0.onnx' });
 * // or: await SmartTurnDetector.maybeLoad();        // undefined when unprovisioned
 *
 * const agent = phone.agent({ ..., turnDetector: detector });
 * ```
 *
 * {@link predict} takes the most recent window of mono int16 LE PCM at
 * 16 kHz (up to 8 s — longer windows are truncated to the last 8 s,
 * shorter ones are left-padded) and returns the probability in `[0, 1]`
 * that the caller has finished their turn. The pipeline handler compares
 * it against {@link threshold} (default 0.5).
 *
 * The model is stateless (no streaming RNN state), so a single instance
 * may be shared across concurrent calls.
 */
export class SmartTurnDetector implements TurnDetectorProvider {
  private closed = false;

  private constructor(
    private readonly runtime: OnnxRuntime,
    private session: OnnxInferenceSession | null,
    private readonly thresholdValue: number,
  ) {}

  /**
   * Load the smart-turn v3 ONNX model and return a ready detector.
   * Throws with download instructions when no model file is configured
   * (see {@link SMART_TURN_MODEL_ENV_VAR}), and with install instructions
   * when `onnxruntime-node` is missing.
   */
  static async load(options: SmartTurnDetectorOptions = {}): Promise<SmartTurnDetector> {
    const threshold = options.threshold ?? DEFAULT_SMART_TURN_THRESHOLD;
    if (!(threshold >= 0 && threshold <= 1)) {
      throw new Error('threshold must be within [0.0, 1.0]');
    }
    // Resolve the model path BEFORE touching onnxruntime so a missing
    // model file surfaces the actionable download hint, not a native-dep
    // resolution error.
    const modelPath = resolveSmartTurnModelPath(options.modelPath);
    const runtime = await loadOnnxRuntime('SmartTurnDetector');
    const session = await runtime.InferenceSession.create(modelPath, {
      interOpNumThreads: 1,
      intraOpNumThreads: 1,
      executionMode: 'sequential',
      graphOptimizationLevel: 'all',
      executionProviders: options.forceCpu === false ? undefined : ['cpu'],
    });
    return new SmartTurnDetector(runtime, session, threshold);
  }

  /**
   * Like {@link load}, but degrade instead of throw.
   *
   * Resolves to `undefined` — after a single clear warning — when semantic
   * turn detection is not provisioned: the optional `onnxruntime-node`
   * dependency is missing, no model file is configured, or the configured
   * file cannot be loaded. Intended for deployments where the detector is
   * a soft upgrade:
   *
   * ```ts
   * const agent = phone.agent({
   *   ...,
   *   turnDetector: await SmartTurnDetector.maybeLoad(),
   * });
   * ```
   *
   * `turnDetector: undefined` keeps the plain VAD-silence endpointing, so
   * the agent starts (and the call behaves) exactly as if the feature were
   * never enabled — it never crashes the app.
   *
   * An out-of-range `threshold` still throws: that is a configuration bug,
   * not a provisioning gap. Mirror of the Python
   * `SmartTurnDetector.maybe_load`.
   */
  static async maybeLoad(
    options: SmartTurnDetectorOptions = {},
  ): Promise<SmartTurnDetector | undefined> {
    const threshold = options.threshold ?? DEFAULT_SMART_TURN_THRESHOLD;
    if (!(threshold >= 0 && threshold <= 1)) {
      throw new Error('threshold must be within [0.0, 1.0]');
    }
    try {
      return await SmartTurnDetector.load(options);
    } catch (err) {
      getLogger().warn(
        'Semantic turn detection unavailable — falling back to plain ' +
          `VAD-silence endpointing: ${err instanceof Error ? err.message : String(err)}`,
      );
      return undefined;
    }
  }

  /**
   * Internal factory used by tests — bypasses onnxruntime-node loading.
   * @internal
   */
  static fromOnnxSession(
    runtime: OnnxRuntime,
    session: OnnxInferenceSession,
    options: { threshold?: number } = {},
  ): SmartTurnDetector {
    return new SmartTurnDetector(
      runtime,
      session,
      options.threshold ?? DEFAULT_SMART_TURN_THRESHOLD,
    );
  }

  /** Identifier of the underlying model (`smart-turn-v3`). */
  get model(): string {
    return 'smart-turn-v3';
  }

  /** Identifier of the runtime backend (`ONNX`). */
  get provider(): string {
    return 'ONNX';
  }

  /** Input sample rate the model expects (16 000 Hz). */
  get sampleRate(): number {
    return SMART_TURN_SAMPLE_RATE;
  }

  /** Maximum audio context the model consumes per prediction (8 s). */
  get maxWindowSeconds(): number {
    return SMART_TURN_MAX_SECONDS;
  }

  /** End-of-turn probability at/above which the turn is complete. */
  get threshold(): number {
    return this.thresholdValue;
  }

  /**
   * End-of-turn probability for the given recent-audio window.
   *
   * @param pcm16Window Mono int16 little-endian PCM at 16 kHz — ideally
   *   the full audio of the caller's current turn, up to 8 s (the
   *   handler keeps a rolling 8 s buffer). Longer input is truncated to
   *   the most recent 8 s; shorter input is left-padded with silence,
   *   matching the reference preprocessing exactly.
   * @returns Probability in `[0, 1]` that the turn is COMPLETE (the
   *   graph applies the sigmoid internally). Returns 0 for an empty
   *   window.
   */
  async predict(pcm16Window: Buffer): Promise<number> {
    if (this.closed || this.session === null) {
      throw new Error('SmartTurnDetector is closed');
    }
    if (pcm16Window.length < 2) {
      return 0;
    }
    const features = await featuresFromPcm16(pcm16Window);
    const { Tensor } = this.runtime;
    const feeds = {
      input_features: new Tensor('float32', features, [1, N_MELS, N_FRAMES]),
    };
    const results = await this.session.run(feeds);
    const first = Object.values(results)[0] as OnnxTensor | undefined;
    const data = first?.data as Float32Array | undefined;
    const probability = data?.[0] ?? 0;
    // Defensive clamp — the graph already emits a sigmoid in (0, 1).
    return Math.min(1, Math.max(0, probability));
  }

  /** Release the ONNX session. Idempotent. */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    // onnxruntime-node sessions are garbage-collected; drop the ref.
    this.session = null;
  }
}
