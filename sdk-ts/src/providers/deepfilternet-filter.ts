/*
 * Copyright 2025 PatterAI
 *
 * Licensed under the MIT License.  See LICENSE at the repository root.
 *
 * DeepFilterNet open-source AudioFilter for the Patter TypeScript SDK.
 *
 * NOTE: DeepFilterNet does not ship an official ONNX export for Node.js at
 * time of writing.  This wrapper targets ``onnxruntime-node`` so it can load
 * a user-supplied ``deepfilternet.onnx`` file when one is available.  When
 * no ONNX model is provided, the filter falls back to a pass-through with a
 * one-time warning — we deliberately do not fake enhancement, so tests or
 * runtime audio quality metrics remain truthful.
 *
 * TODO: when DeepFilterNet3 has a stable community ONNX export, ship a
 * default loader that downloads the model on first use.
 */
import { getLogger } from '../logger';
import type { AudioFilter } from '../types';

// Resolve the logger lazily so tests that swap it via ``setLogger`` after
// import still capture our warnings/errors.
function log() {
  return getLogger();
}

// DeepFilterNet3 operates at 48 kHz natively.
const DEEPFILTERNET_SR = 48000;

/** Options accepted by {@link DeepFilterNetFilter}. */
export interface DeepFilterNetOptions {
  /** Absolute path to a DeepFilterNet ONNX model.  If omitted, the filter
   *  logs a warning and becomes a pass-through. */
  modelPath?: string;
  /** When true, disable the pass-through warning (used by tests). */
  silenceWarnings?: boolean;
}

// ``onnxruntime-node`` is declared as a peer/optional dependency; the module
// is typed loosely to avoid a hard dependency in the default SDK install.
type OnnxSession = {
  readonly inputNames: readonly string[];
  readonly outputNames: readonly string[];
  run(feeds: Record<string, unknown>): Promise<Record<string, unknown>>;
  release?(): Promise<void> | void;
};

type OnnxRuntimeModule = {
  InferenceSession: {
    create(path: string): Promise<OnnxSession>;
  };
  Tensor: new (type: string, data: Float32Array, dims: readonly number[]) => unknown;
};

async function loadOnnxRuntime(): Promise<OnnxRuntimeModule | null> {
  // ``onnxruntime-node`` is an optional peer dependency; loaded via a
  // dynamic expression so TypeScript does not require it at build time.
  try {
    const moduleName = 'onnxruntime-node';
    const dynamicImport = new Function('m', 'return import(m)') as (m: string) => Promise<unknown>;
    const mod = await dynamicImport(moduleName);
    return mod as OnnxRuntimeModule;
  } catch {
    return null;
  }
}

/** Linear-interpolation resampler (mono, Float32).  Good enough for the
 *  narrow-band / wide-band conversions DeepFilterNet needs around telephony
 *  audio. */
function resample(samples: Float32Array, srcSr: number, dstSr: number): Float32Array {
  if (srcSr === dstSr || samples.length === 0) {
    return samples;
  }
  const dstLen = Math.max(1, Math.round((samples.length * dstSr) / srcSr));
  const out = new Float32Array(dstLen);
  const ratio = (samples.length - 1) / Math.max(1, dstLen - 1);
  for (let i = 0; i < dstLen; i += 1) {
    const srcIdx = i * ratio;
    const lo = Math.floor(srcIdx);
    const hi = Math.min(samples.length - 1, lo + 1);
    const frac = srcIdx - lo;
    out[i] = samples[lo] * (1 - frac) + samples[hi] * frac;
  }
  return out;
}

function pcm16ToFloat32(pcm: Buffer): Float32Array {
  const view = new Int16Array(pcm.buffer, pcm.byteOffset, Math.floor(pcm.byteLength / 2));
  const out = new Float32Array(view.length);
  for (let i = 0; i < view.length; i += 1) {
    out[i] = view[i] / 32768;
  }
  return out;
}

function float32ToPcm16(samples: Float32Array): Buffer {
  const out = Buffer.alloc(samples.length * 2);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    out.writeInt16LE(Math.round(clamped * 32767), i * 2);
  }
  return out;
}

/** OSS noise-suppression filter backed by a DeepFilterNet ONNX model. */
export class DeepFilterNetFilter implements AudioFilter {
  private readonly modelPath: string | undefined;
  private readonly silenceWarnings: boolean;
  private session: OnnxSession | null = null;
  private ort: OnnxRuntimeModule | null = null;
  private warned = false;
  private closed = false;

  constructor(options: DeepFilterNetOptions = {}) {
    this.modelPath = options.modelPath;
    this.silenceWarnings = options.silenceWarnings === true;
  }

  private async ensureSession(): Promise<OnnxSession | null> {
    if (this.session !== null) {
      return this.session;
    }
    if (!this.modelPath) {
      if (!this.warned && !this.silenceWarnings) {
        log().warn(
          'DeepFilterNetFilter: no modelPath provided; audio will pass ' +
            'through unmodified. Provide a DeepFilterNet ONNX model to enable ' +
            'noise suppression.',
        );
        this.warned = true;
      }
      return null;
    }
    if (this.ort === null) {
      this.ort = await loadOnnxRuntime();
    }
    if (this.ort === null) {
      if (!this.warned && !this.silenceWarnings) {
        log().warn(
          'DeepFilterNetFilter: onnxruntime-node is not installed; audio ' +
            'will pass through unmodified. Run `npm install onnxruntime-node` ' +
            'to enable noise suppression.',
        );
        this.warned = true;
      }
      return null;
    }
    try {
      this.session = await this.ort.InferenceSession.create(this.modelPath);
      return this.session;
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : String(error);
      log().error(`DeepFilterNetFilter: failed to load model: ${message}`);
      this.warned = true;
      return null;
    }
  }

  async process(pcmChunk: Buffer, sampleRate: number): Promise<Buffer> {
    if (this.closed) {
      throw new Error('DeepFilterNetFilter is closed');
    }
    if (pcmChunk.length === 0) {
      return pcmChunk;
    }
    const session = await this.ensureSession();
    if (session === null || this.ort === null) {
      // No model/runtime available — pass-through. Never fabricate enhanced
      // audio; tests rely on this being detectably a no-op.
      return pcmChunk;
    }

    try {
      const samples = pcm16ToFloat32(pcmChunk);
      const upsampled = resample(samples, sampleRate, DEEPFILTERNET_SR);
      const inputName = session.inputNames[0];
      const outputName = session.outputNames[0];
      const tensor = new this.ort.Tensor('float32', upsampled, [1, upsampled.length]);
      const feeds: Record<string, unknown> = { [inputName]: tensor };
      const results = await session.run(feeds);
      const output = results[outputName] as { data?: Float32Array } | undefined;
      if (!output || !output.data) {
        return pcmChunk;
      }
      const enhanced = output.data instanceof Float32Array ? output.data : new Float32Array(output.data);
      const restored = resample(enhanced, DEEPFILTERNET_SR, sampleRate);
      return float32ToPcm16(restored);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : String(error);
      log().error(`DeepFilterNetFilter.process failed: ${message}`);
      return pcmChunk;
    }
  }

  async close(): Promise<void> {
    if (this.session !== null && typeof this.session.release === 'function') {
      try {
        await this.session.release();
      } catch (error: unknown) {
        const message = error instanceof Error ? error.message : String(error);
        log().warn(`DeepFilterNetFilter.close: release failed: ${message}`);
      }
    }
    this.session = null;
    this.closed = true;
  }
}
