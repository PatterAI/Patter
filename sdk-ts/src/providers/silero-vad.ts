/**
 * Silero VAD provider (TypeScript port).
 *
 * Acoustic voice activity detection backed by the Silero ONNX model. Buffers
 * incoming int16 LE PCM frames, runs inference on fixed-size windows
 * (256 samples at 8 kHz, 512 at 16 kHz), applies an exponential probability
 * filter, and emits VADEvent transitions (speech_start / speech_end).
 *
 * Ported from LiveKit Agents (Apache 2.0):
 *   https://github.com/livekit/agents
 * Sources:
 *   - livekit-plugins/livekit-plugins-silero/livekit/plugins/silero/vad.py
 *   - livekit-plugins/livekit-plugins-silero/livekit/plugins/silero/onnx_model.py
 *
 * Adaptations for Patter:
 *   - Input is raw PCM `Buffer` (int16 LE, mono) via
 *     `processFrame(pcmChunk, sampleRate)`, not `livekit.rtc.AudioFrame`.
 *   - onnxruntime-node is loaded lazily as an optional dependency.
 *   - Emits `VADEvent` (Patter protocol) instead of LiveKit event types.
 */

// Copyright 2023 LiveKit, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import { createRequire } from 'node:module';
import * as path from 'node:path';
import type { VADEvent, VADProvider } from '../types';

const SUPPORTED_SAMPLE_RATES = [8000, 16000] as const;
export type SileroSampleRate = (typeof SUPPORTED_SAMPLE_RATES)[number];

const DEFAULT_MODEL_PATH = path.join(__dirname, '..', 'resources', 'silero_vad.onnx');

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
export interface OnnxInferenceSession {
  run(
    feeds: Record<string, OnnxTensor>,
  ): Promise<Record<string, OnnxTensor>>;
}

export interface OnnxTensor {
  readonly data: Float32Array | BigInt64Array;
  readonly dims: readonly number[];
}

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

function loadOnnxRuntime(): OnnxRuntime {
  try {
    // Use createRequire so bundlers don't try to resolve onnxruntime-node at build time.
    const req = createRequire(__filename);
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    return req('onnxruntime-node') as OnnxRuntime;
  } catch {
    throw new Error(
      '\nSileroVAD requires the "onnxruntime-node" package, which is not installed.\n\n' +
        '  Install:  npm install onnxruntime-node\n\n' +
        'This is an optional peer dependency of getpatter (~210 MB) — it is only\n' +
        'needed when you use SileroVAD in pipeline mode. If you do not need VAD\n' +
        '(e.g. you use OpenAI Realtime or ElevenLabs ConvAI), remove SileroVAD\n' +
        'from your agent configuration.\n',
    );
  }
}

/** Exponential smoothing filter (ported from livekit.agents.utils.ExpFilter). */
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
   * Load the Silero VAD model. Defaults match the LiveKit Silero plugin.
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

    const runtime = loadOnnxRuntime();
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

  get sampleRate(): SileroSampleRate {
    return this.opts.sampleRate;
  }

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
      if (transition !== null && event === null) {
        event = transition;
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

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    // onnxruntime-node sessions are garbage-collected; no explicit release API.
  }
}
