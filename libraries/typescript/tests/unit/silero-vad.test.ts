/**
 * Unit tests for SileroVAD (TypeScript port).
 *
 * Mocks `onnxruntime-node` entirely via `SileroVAD.fromOnnxModel` so the
 * real ONNX model and native addon are never loaded. Tests exercise the
 * streaming state machine deterministically with scripted probabilities.
 */

import { describe, expect, it } from 'vitest';
import {
  SileroVAD,
  type OnnxRuntime,
  type OnnxTensor,
  type OnnxInferenceSession,
} from '../../src/providers/silero-vad';

// ---------------------------------------------------------------------------
// Fake onnxruntime for tests
// ---------------------------------------------------------------------------

class FakeTensor implements OnnxTensor {
  constructor(
    readonly type: 'float32' | 'int64',
    readonly data: Float32Array | BigInt64Array,
    readonly dims: readonly number[],
  ) {}
}

interface FakeRuntime extends OnnxRuntime {
  calls: Array<Record<string, OnnxTensor>>;
}

function buildFake(probs: number[]): { runtime: FakeRuntime; session: OnnxInferenceSession } {
  const calls: Array<Record<string, OnnxTensor>> = [];
  let i = 0;

  const session: OnnxInferenceSession = {
    async run(feeds: Record<string, OnnxTensor>) {
      calls.push(feeds);
      const p = i < probs.length ? probs[i++] : (probs[probs.length - 1] ?? 0);
      return {
        output: new FakeTensor('float32', Float32Array.from([p]), [1, 1]),
        stateN: new FakeTensor('float32', new Float32Array(2 * 1 * 128), [2, 1, 128]),
      };
    },
  };

  const runtime: FakeRuntime = {
    // Not exercised — fromOnnxModel bypasses InferenceSession.create.
    InferenceSession: { create: async () => session },
    Tensor: FakeTensor as unknown as OnnxRuntime['Tensor'],
    calls,
  };

  return { runtime, session };
}

function buildVad(opts: {
  probs: number[];
  sampleRate?: 8000 | 16000;
  minSpeechDuration?: number;
  minSilenceDuration?: number;
  activationThreshold?: number;
}): { vad: SileroVAD; runtime: FakeRuntime } {
  const sampleRate = opts.sampleRate ?? 16000;
  const activationThreshold = opts.activationThreshold ?? 0.5;
  const { runtime, session } = buildFake(opts.probs);
  const vad = SileroVAD.fromOnnxModel(runtime, session, {
    minSpeechDuration: opts.minSpeechDuration ?? 0.032,
    minSilenceDuration: opts.minSilenceDuration ?? 0.064,
    prefixPaddingDuration: 0,
    activationThreshold,
    deactivationThreshold: Math.max(activationThreshold - 0.15, 0.01),
    sampleRate,
  });
  return { vad, runtime };
}

function silencePcm(numSamples: number): Buffer {
  return Buffer.alloc(numSamples * 2);
}

function sinePcm(numSamples: number, sampleRate: number, freqHz = 440): Buffer {
  const buf = Buffer.alloc(numSamples * 2);
  for (let i = 0; i < numSamples; i++) {
    const sample = Math.sin((2 * Math.PI * freqHz * i) / sampleRate) * 0.5;
    const int16 = Math.round(sample * 32767);
    buf.writeInt16LE(int16, i * 2);
  }
  return buf;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('SileroVAD', () => {
  it('initializes with 16 kHz defaults', () => {
    const { vad } = buildVad({ probs: [], sampleRate: 16000 });
    expect(vad.sampleRate).toBe(16000);
  });

  it('initializes with 8 kHz defaults', () => {
    const { vad } = buildVad({ probs: [], sampleRate: 8000 });
    expect(vad.sampleRate).toBe(8000);
  });

  it('returns null and runs no inference for a sub-window silence chunk', async () => {
    const { vad, runtime } = buildVad({ probs: [0] });
    const pcm = silencePcm(256); // < 512 window @ 16 kHz
    const event = await vad.processFrame(pcm, 16000);
    expect(event).toBeNull();
    expect(runtime.calls).toHaveLength(0);
  });

  it('runs inference but returns null on a full window of silence (mocked onnxruntime)', async () => {
    const { vad, runtime } = buildVad({ probs: [0.02, 0.02] });
    const event = await vad.processFrame(silencePcm(512), 16000);
    expect(event).toBeNull();
    expect(runtime.calls).toHaveLength(1);
    // Input tensor is float32 with shape [1, context_size + window_size_samples] (64 + 512 @ 16 kHz).
    const input = runtime.calls[0]!.input;
    expect(input.data).toBeInstanceOf(Float32Array);
    expect(input.dims).toEqual([1, 64 + 512]);
  });

  it('emits speech_start above the activation threshold (mocked onnxruntime)', async () => {
    const { vad } = buildVad({
      probs: [0.95, 0.95, 0.95, 0.95],
      minSpeechDuration: 0.032,
    });
    const event = await vad.processFrame(sinePcm(512, 16000), 16000);
    expect(event).not.toBeNull();
    expect(event!.type).toBe('speech_start');
    expect(event!.confidence).toBeGreaterThan(0.5);
    expect(event!.durationMs).toBeGreaterThanOrEqual(32);
  });

  it('emits speech_end after the silence gate (mocked onnxruntime)', async () => {
    const { vad } = buildVad({
      probs: [0.9, 0.9, 0.02, 0.02, 0.02],
      minSpeechDuration: 0.032,
      minSilenceDuration: 0.064,
    });
    const speech = sinePcm(512 * 2, 16000);
    const silence = silencePcm(512 * 3);
    const start = await vad.processFrame(speech, 16000);
    const end = await vad.processFrame(silence, 16000);
    expect(start?.type).toBe('speech_start');
    expect(end?.type).toBe('speech_end');
  });

  it('returns null for an empty chunk without invoking the session', async () => {
    const { vad, runtime } = buildVad({ probs: [0] });
    const event = await vad.processFrame(Buffer.alloc(0), 16000);
    expect(event).toBeNull();
    expect(runtime.calls).toHaveLength(0);
  });

  it('rejects a mismatched sample rate', async () => {
    const { vad } = buildVad({ probs: [0] });
    await expect(vad.processFrame(silencePcm(512), 8000)).rejects.toThrow(/sampleRate/);
  });

  it('close() is idempotent', async () => {
    const { vad } = buildVad({ probs: [0] });
    await vad.close();
    await expect(vad.close()).resolves.toBeUndefined();
  });

  it('processFrame() after close() throws', async () => {
    const { vad } = buildVad({ probs: [0] });
    await vad.close();
    await expect(vad.processFrame(silencePcm(512), 16000)).rejects.toThrow(/closed/);
  });
});
