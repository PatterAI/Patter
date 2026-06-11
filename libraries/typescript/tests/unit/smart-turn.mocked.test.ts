/**
 * Unit tests for SmartTurnDetector (smart-turn v3 semantic turn detector).
 *
 * Mirrors the Python `test_smart_turn.py` so cross-SDK behaviour stays in
 * lockstep. All tests stub `onnxruntime-node` — via
 * `SmartTurnDetector.fromOnnxSession` or a passthrough spy on the runtime
 * loader — no model file, no native addon, no network. The Whisper log-mel
 * preprocessing is exercised against its documented invariants plus
 * reference values produced by the Python twin
 * (`getpatter/providers/smart_turn.py`), which was itself verified against
 * `transformers.WhisperFeatureExtractor(chunk_length=8)` to ~1e-6.
 */

import { describe, expect, it, vi } from 'vitest';
import { getLogger, setLogger, type Logger } from '../../src/logger';
import * as sileroVadModule from '../../src/providers/silero-vad';
import type {
  OnnxInferenceSession,
  OnnxRuntime,
  OnnxTensor,
} from '../../src/providers/silero-vad';

// Wrap the runtime loader (the external boundary) in a passthrough spy so a
// single test can simulate `onnxruntime-node` being missing. Every other
// test either fails model-path resolution first or bypasses the loader via
// `SmartTurnDetector.fromOnnxSession`, so the passthrough never engages.
vi.mock('../../src/providers/silero-vad', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../src/providers/silero-vad')>();
  return { ...mod, loadOnnxRuntime: vi.fn(mod.loadOnnxRuntime) };
});
import {
  DEFAULT_SMART_TURN_THRESHOLD,
  SMART_TURN_MAX_SAMPLES,
  SMART_TURN_MODEL_ENV_VAR,
  SmartTurnDetector,
  computeWhisperLogMelFeatures,
  featuresFromPcm16,
  prepareInputWindow,
  resolveSmartTurnModelPath,
} from '../../src/providers/smart-turn';

// ---------------------------------------------------------------------------
// Fake onnxruntime
// ---------------------------------------------------------------------------

class FakeTensor implements OnnxTensor {
  constructor(
    readonly type: 'float32' | 'int64',
    readonly data: Float32Array | BigInt64Array,
    readonly dims: readonly number[],
  ) {}
}

interface FakeSession extends OnnxInferenceSession {
  calls: Array<Record<string, OnnxTensor>>;
}

function buildFake(probs: number[]): { runtime: OnnxRuntime; session: FakeSession } {
  let i = 0;
  const session: FakeSession = {
    calls: [],
    async run(feeds: Record<string, OnnxTensor>) {
      session.calls.push(feeds);
      const p = i < probs.length ? probs[i++] : (probs[probs.length - 1] ?? 0);
      return {
        logits: new FakeTensor('float32', Float32Array.from([p]), [1, 1]),
      };
    },
  };
  const runtime: OnnxRuntime = {
    InferenceSession: { create: async () => session },
    Tensor: FakeTensor as unknown as OnnxRuntime['Tensor'],
  };
  return { runtime, session };
}

function buildDetector(opts: { probs: number[]; threshold?: number } = { probs: [0.9] }): {
  detector: SmartTurnDetector;
  session: FakeSession;
} {
  const { runtime, session } = buildFake(opts.probs);
  const detector = SmartTurnDetector.fromOnnxSession(runtime, session, {
    threshold: opts.threshold,
  });
  return { detector, session };
}

function sinePcm(numSamples: number, sampleRate = 16000, freqHz = 440): Buffer {
  const buf = Buffer.alloc(numSamples * 2);
  for (let i = 0; i < numSamples; i++) {
    // Math.trunc mirrors numpy's `.astype(np.int16)` truncation so the
    // reference-value test below sees byte-identical input to Python.
    const int16 = Math.trunc(Math.sin((2 * Math.PI * freqHz * i) / sampleRate) * 0.5 * 32767);
    buf.writeInt16LE(int16, i * 2);
  }
  return buf;
}

// ---------------------------------------------------------------------------
// Model-path resolution (no onnxruntime needed — resolution happens first)
// ---------------------------------------------------------------------------

describe('SmartTurnDetector.load — model resolution', () => {
  it('rejects with a download hint when neither modelPath nor env var is set', async () => {
    const prev = process.env[SMART_TURN_MODEL_ENV_VAR];
    delete process.env[SMART_TURN_MODEL_ENV_VAR];
    try {
      await expect(SmartTurnDetector.load()).rejects.toThrow(
        /huggingface\.co\/pipecat-ai\/smart-turn-v3/,
      );
    } finally {
      if (prev !== undefined) process.env[SMART_TURN_MODEL_ENV_VAR] = prev;
    }
  });

  it('rejects with a clear error when the model file does not exist', async () => {
    await expect(
      SmartTurnDetector.load({ modelPath: '/nonexistent/smart-turn-v3.0.onnx' }),
    ).rejects.toThrow(/not found.*huggingface\.co/s);
  });

  it('resolves the env-var path when set', () => {
    const prev = process.env[SMART_TURN_MODEL_ENV_VAR];
    // package.json definitely exists — resolution only checks existence.
    process.env[SMART_TURN_MODEL_ENV_VAR] = 'package.json';
    try {
      expect(resolveSmartTurnModelPath()).toMatch(/package\.json$/);
    } finally {
      if (prev !== undefined) process.env[SMART_TURN_MODEL_ENV_VAR] = prev;
      else delete process.env[SMART_TURN_MODEL_ENV_VAR];
    }
  });

  it('explicit modelPath wins over the env var', () => {
    const prev = process.env[SMART_TURN_MODEL_ENV_VAR];
    process.env[SMART_TURN_MODEL_ENV_VAR] = '/nonexistent/env.onnx';
    try {
      expect(resolveSmartTurnModelPath('package.json')).toMatch(/package\.json$/);
    } finally {
      if (prev !== undefined) process.env[SMART_TURN_MODEL_ENV_VAR] = prev;
      else delete process.env[SMART_TURN_MODEL_ENV_VAR];
    }
  });

  it('rejects an out-of-range threshold before touching the model', async () => {
    await expect(SmartTurnDetector.load({ threshold: 1.5 })).rejects.toThrow(/threshold/);
  });
});

// ---------------------------------------------------------------------------
// maybeLoad — degrade (warn once + undefined) instead of throwing
// ---------------------------------------------------------------------------

describe('SmartTurnDetector.maybeLoad', () => {
  /** Run `fn` with a spy logger installed; returns the spy. */
  async function withSpyLogger(fn: () => Promise<void>): Promise<{ warn: ReturnType<typeof vi.fn> }> {
    const warn = vi.fn();
    const spy: Logger = { info: vi.fn(), warn, error: vi.fn(), debug: vi.fn() };
    const original = getLogger();
    setLogger(spy);
    try {
      await fn();
    } finally {
      setLogger(original);
    }
    return { warn };
  }

  it('resolves undefined with a single warning when no model is configured', async () => {
    const prev = process.env[SMART_TURN_MODEL_ENV_VAR];
    delete process.env[SMART_TURN_MODEL_ENV_VAR];
    try {
      const { warn } = await withSpyLogger(async () => {
        expect(await SmartTurnDetector.maybeLoad()).toBeUndefined();
      });
      expect(warn).toHaveBeenCalledTimes(1);
      const message = String(warn.mock.calls[0][0]);
      expect(message).toMatch(/falling back to plain VAD-silence/);
      expect(message).toMatch(/huggingface\.co\/pipecat-ai\/smart-turn-v3/);
    } finally {
      if (prev !== undefined) process.env[SMART_TURN_MODEL_ENV_VAR] = prev;
    }
  });

  it('resolves undefined when the model file does not exist', async () => {
    const { warn } = await withSpyLogger(async () => {
      expect(
        await SmartTurnDetector.maybeLoad({ modelPath: '/nonexistent/smart-turn-v3.0.onnx' }),
      ).toBeUndefined();
    });
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it('still rejects an out-of-range threshold (config bug, not provisioning gap)', async () => {
    await expect(SmartTurnDetector.maybeLoad({ threshold: -0.1 })).rejects.toThrow(/threshold/);
  });

  it('resolves undefined when onnxruntime-node is missing (model file present)', async () => {
    // Model path resolves (package.json exists) — the failure comes from
    // the runtime loader, exactly as on a box without the optional dep.
    vi.mocked(sileroVadModule.loadOnnxRuntime).mockRejectedValueOnce(
      new Error(
        'SmartTurnDetector requires the "onnxruntime-node" package — it is not installed.',
      ),
    );
    const { warn } = await withSpyLogger(async () => {
      expect(
        await SmartTurnDetector.maybeLoad({ modelPath: 'package.json' }),
      ).toBeUndefined();
    });
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toMatch(/onnxruntime-node/);
  });
});

// ---------------------------------------------------------------------------
// Whisper log-mel preprocessing invariants (smart-turn v3 input contract)
// ---------------------------------------------------------------------------

describe('Whisper log-mel feature extraction', () => {
  it('produces (80, 800) float32 features', async () => {
    const feats = await computeWhisperLogMelFeatures(
      prepareInputWindow(new Float64Array(16000)),
    );
    expect(feats).toBeInstanceOf(Float32Array);
    expect(feats.length).toBe(80 * 800);
  });

  it('maps all-zero audio to the uniform clamp floor (-1.5)', async () => {
    const feats = await computeWhisperLogMelFeatures(
      prepareInputWindow(new Float64Array(8000)),
    );
    for (let i = 0; i < feats.length; i++) {
      expect(feats[i]).toBeCloseTo(-1.5, 5);
    }
  });

  it('left-pads short audio so speech sits at the END of the window', async () => {
    // 1 s of tone in an 8 s window → energy concentrated in the LAST frames
    // (smart-turn pads at the beginning per truncate_audio_to_last_n_seconds).
    const tone = new Float64Array(16000);
    for (let i = 0; i < tone.length; i++) {
      tone[i] = Math.sin((2 * Math.PI * 440 * i) / 16000) * 0.5;
    }
    const feats = await computeWhisperLogMelFeatures(prepareInputWindow(tone));
    let tail = 0;
    let head = 0;
    for (let m = 0; m < 80; m++) {
      for (let t = 0; t < 100; t++) {
        head += feats[m * 800 + t];
        tail += feats[m * 800 + (700 + t)];
      }
    }
    expect(tail).toBeGreaterThan(head);
  });

  it('keeps only the most recent 8 seconds of longer audio', async () => {
    // A 10 s buffer whose only tone lives in the FIRST 2 s is truncated to
    // the trailing 8 s of silence → features collapse to the uniform floor.
    const audio = new Float64Array(160000);
    for (let i = 0; i < 32000; i++) {
      audio[i] = Math.sin((2 * Math.PI * 440 * i) / 16000) * 0.5;
    }
    const feats = await computeWhisperLogMelFeatures(prepareInputWindow(audio));
    for (let i = 0; i < feats.length; i += 997) {
      expect(feats[i]).toBeCloseTo(-1.5, 5);
    }
  });

  it('normalizes the padded window to zero mean / unit variance', () => {
    const samples = new Float64Array(SMART_TURN_MAX_SAMPLES);
    for (let i = 0; i < samples.length; i++) {
      samples[i] = Math.sin(i * 0.37) * 3 + 5;
    }
    const window = prepareInputWindow(samples);
    let mean = 0;
    for (const v of window) mean += v;
    mean /= window.length;
    let variance = 0;
    for (const v of window) variance += (v - mean) * (v - mean);
    variance /= window.length;
    expect(Math.abs(mean)).toBeLessThan(1e-9);
    expect(Math.abs(variance - 1)).toBeLessThan(1e-3); // 1e-7 eps shifts it slightly
  });

  it('rejects an unprepared window', async () => {
    await expect(computeWhisperLogMelFeatures(new Float64Array(1000))).rejects.toThrow(
      /prepareInputWindow/,
    );
  });

  it('matches the Python reference implementation on a deterministic vector', async () => {
    // Reference values produced by getpatter/providers/smart_turn.py
    // (itself verified against transformers.WhisperFeatureExtractor to
    // ~1e-6) for 1 s of 440 Hz half-amplitude int16 sine.
    const feats = await featuresFromPcm16(sinePcm(16000));
    const at = (m: number, t: number) => feats[m * 800 + t];
    expect(at(0, 0)).toBeCloseTo(-0.110252, 3); // padding region = clamp floor
    expect(at(0, 799)).toBeCloseTo(0.929245, 3);
    expect(at(10, 750)).toBeCloseTo(1.800282, 3); // 440 Hz lands in mel ~10
    expect(at(79, 799)).toBeCloseTo(-0.110252, 3);
    let mean = 0;
    let max = -Infinity;
    let min = Infinity;
    for (const v of feats) {
      mean += v;
      if (v > max) max = v;
      if (v < min) min = v;
    }
    mean /= feats.length;
    expect(mean).toBeCloseTo(-0.096046, 3);
    expect(max).toBeCloseTo(1.889748, 3);
    expect(min).toBeCloseTo(-0.110252, 3);
  });
});

// ---------------------------------------------------------------------------
// predict() — stubbed ONNX session
// ---------------------------------------------------------------------------

describe('SmartTurnDetector.predict', () => {
  it('returns the session probability', async () => {
    const { detector, session } = buildDetector({ probs: [0.83] });
    const p = await detector.predict(sinePcm(16000));
    expect(p).toBeCloseTo(0.83, 5);
    expect(session.calls).toHaveLength(1);
  });

  it('feeds input_features as a [1, 80, 800] float32 tensor', async () => {
    const { detector, session } = buildDetector({ probs: [0.4] });
    await detector.predict(sinePcm(32000));
    const feeds = session.calls[0];
    expect(Object.keys(feeds)).toEqual(['input_features']);
    expect(feeds.input_features.dims).toEqual([1, 80, 800]);
    expect(feeds.input_features.data).toBeInstanceOf(Float32Array);
    expect(feeds.input_features.data.length).toBe(80 * 800);
  });

  it('short-circuits an empty window to probability 0', async () => {
    const { detector, session } = buildDetector({ probs: [0.99] });
    expect(await detector.predict(Buffer.alloc(0))).toBe(0);
    expect(session.calls).toHaveLength(0);
  });

  it('clamps out-of-range outputs', async () => {
    const { detector } = buildDetector({ probs: [1.7] });
    expect(await detector.predict(sinePcm(1600))).toBe(1);
  });

  it('accepts more than 8 s of audio (truncates, does not reject)', async () => {
    const { detector, session } = buildDetector({ probs: [0.6] });
    const p = await detector.predict(sinePcm(16000 * 10));
    expect(p).toBeCloseTo(0.6, 5);
    expect(session.calls[0].input_features.dims).toEqual([1, 80, 800]);
  });
});

// ---------------------------------------------------------------------------
// Surface / lifecycle
// ---------------------------------------------------------------------------

describe('SmartTurnDetector surface', () => {
  it('exposes the provider tags and 16 kHz / 8 s contract', () => {
    const { detector } = buildDetector({ probs: [0], threshold: 0.65 });
    expect(detector.model).toBe('smart-turn-v3');
    expect(detector.provider).toBe('ONNX');
    expect(detector.sampleRate).toBe(16000);
    expect(detector.maxWindowSeconds).toBe(8);
    expect(detector.threshold).toBe(0.65);
  });

  it('defaults the threshold to the smart-turn reference value (0.5)', () => {
    const { detector } = buildDetector({ probs: [0] });
    expect(detector.threshold).toBe(DEFAULT_SMART_TURN_THRESHOLD);
    expect(detector.threshold).toBe(0.5);
  });

  it('close() is idempotent and predict() after close throws', async () => {
    const { detector } = buildDetector({ probs: [0] });
    await detector.close();
    await detector.close(); // must not throw
    await expect(detector.predict(sinePcm(1600))).rejects.toThrow(/closed/);
  });
});
