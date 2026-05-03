/**
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
 * Known limitation: each ``process()`` call runs the model on a single
 * isolated chunk, which can introduce per-chunk discontinuities at
 * sample-rate-conversion boundaries. The stateful resamplers in
 * ``transcoding.ts`` carry phase across calls but the DFN inference itself
 * does not — when DeepFilterNet3 ships a stable community ONNX export with
 * a streaming-friendly graph, swap to that and remove this caveat.
 */
import { getLogger } from '../logger';
import { StatefulResampler } from '../audio/transcoding';
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
  // ``onnxruntime-node`` is an optional peer dependency. The variable
  // module-name cast avoids a TS2307 hard build-time requirement when the
  // peer dep is not installed (same pattern as gemini-live.ts).
  try {
    const specifier = 'onnxruntime-node' as string;
    const mod = await import(specifier as string);
    return mod as OnnxRuntimeModule;
  } catch {
    return null;
  }
}

/**
 * Convert a PCM16 Buffer to a Float32Array for ONNX inference.
 * Kept as a thin helper — resampling is now handled by StatefulResampler.
 */

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
  // Fix 5: stateful resamplers for src_sr↔48k conversions so chunk-boundary
  // samples are not discarded. Lazy-created and torn down on rate change.
  private _resamplerSrcRate: number | null = null;
  private _upsamplerInst: StatefulResampler | null = null;
  private _downsamplerInst: StatefulResampler | null = null;

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

  /** Run noise suppression on a PCM16 chunk; pass-through when no model is loaded. */
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
      // Fix 5: use stateful resamplers so samples spanning chunk boundaries
      // are not silently discarded (stateless np.interp-style had this bug).
      if (this._resamplerSrcRate !== sampleRate) {
        // Rate changed or first call — create fresh instances.
        this._resamplerSrcRate = sampleRate;
        this._upsamplerInst = new StatefulResampler({ srcRate: sampleRate, dstRate: DEEPFILTERNET_SR });
        this._downsamplerInst = new StatefulResampler({ srcRate: DEEPFILTERNET_SR, dstRate: sampleRate });
      }

      const samples = pcm16ToFloat32(pcmChunk);
      // Up-sample to 48 kHz using stateful resampler (PCM16 → Float32 → Resampler)
      const pcm16Up = this._upsamplerInst!.process(float32ToPcm16(new Float32Array(samples)));
      const upsampled = pcm16ToFloat32(pcm16Up);

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
      // Down-sample back to src_sr using stateful resampler
      const pcm16Enhanced = float32ToPcm16(enhanced);
      const pcm16Restored = this._downsamplerInst!.process(pcm16Enhanced);
      return pcm16Restored;
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : String(error);
      log().error(`DeepFilterNetFilter.process failed: ${message}`);
      return pcmChunk;
    }
  }

  /** Flush resamplers, release the ONNX session, and mark the filter closed. */
  async close(): Promise<void> {
    // Fix 5: flush stateful resamplers so tail samples are not clipped.
    try { this._upsamplerInst?.flush(); } catch { /* best effort */ }
    try { this._downsamplerInst?.flush(); } catch { /* best effort */ }
    this._upsamplerInst = null;
    this._downsamplerInst = null;

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
