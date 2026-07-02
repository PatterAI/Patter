/**
 * Unit tests for NamoTurnDetector (NAMO Turn Detector v1, text-based EOU).
 *
 * Mirrors the Python `test_namo_turn_detector.py` so cross-SDK behaviour stays
 * in lockstep. The NAMO Turn Detector v1 (VideoSDK, Apache-2.0) is a TEXT
 * model: it scores the rolling conversation transcript, not the audio window.
 * All tests inject a fake onnxruntime session + fake Hugging Face tokenizer
 * (via `NamoTurnDetector.fromSession`) — no model file, no native addon, no
 * network. The onnxruntime / transformers loaders (the external boundaries)
 * are mocked so the missing-dependency install errors are exercised
 * deterministically even though both optional deps are present in CI.
 */

import { describe, expect, it, vi } from 'vitest';
import { getLogger, setLogger, type Logger } from '../../src/logger';
import * as sileroVadModule from '../../src/providers/silero-vad';
import type {
  OnnxInferenceSession,
  OnnxRuntime,
  OnnxTensor,
} from '../../src/providers/silero-vad';

// Wrap the ONNX runtime loader in a passthrough spy so a test can simulate
// `onnxruntime-node` being missing.
vi.mock('../../src/providers/silero-vad', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../src/providers/silero-vad')>();
  return { ...mod, loadOnnxRuntime: vi.fn(mod.loadOnnxRuntime) };
});

// Force `@huggingface/transformers` to fail to import so `loadNamoTokenizer`
// exercises its install-error path deterministically. Only the load()/maybeLoad
// tokenizer-loading tests reach this import; the predict/surface tests inject a
// fake tokenizer via `fromSession`.
vi.mock('@huggingface/transformers', () => {
  throw new Error("Cannot find module '@huggingface/transformers'");
});

import {
  DEFAULT_NAMO_MAX_TOKENS,
  DEFAULT_NAMO_THRESHOLD,
  NAMO_MODEL_ENV_VAR,
  NAMO_REVISION_ENV_VAR,
  NAMO_TOKENIZER_ENV_VAR,
  NamoTurnDetector,
  completionProbability,
  loadNamoTokenizer,
  resolveNamoModelPath,
  resolveNamoRevision,
  resolveNamoTokenizerPath,
  type NamoTokenizer,
} from '../../src/providers/namo-turn-detector';

// ---------------------------------------------------------------------------
// Fakes
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

function buildFake(logitsRows: number[][]): { runtime: OnnxRuntime; session: FakeSession } {
  let i = 0;
  const session: FakeSession = {
    calls: [],
    async run(feeds: Record<string, OnnxTensor>) {
      session.calls.push(feeds);
      const row = i < logitsRows.length ? logitsRows[i++] : (logitsRows[logitsRows.length - 1] ?? [0, 0]);
      return { logits: new FakeTensor('float32', Float32Array.from(row), [1, row.length]) };
    },
  };
  const runtime: OnnxRuntime = {
    InferenceSession: { create: async () => session },
    Tensor: FakeTensor as unknown as OnnxRuntime['Tensor'],
  };
  return { runtime, session };
}

/** Deterministic tokenizer: one id per char (+ CLS/SEP), honours truncation. */
class FakeTokenizer implements NamoTokenizer {
  readonly calls: Array<{ text: string; maxTokens: number }> = [];

  encode(text: string, maxTokens: number): { inputIds: BigInt64Array; attentionMask: BigInt64Array } {
    this.calls.push({ text, maxTokens });
    let ids = [101, ...Array.from(text, (c) => (c.charCodeAt(0) % 900) + 1), 102];
    if (ids.length > maxTokens) ids = ids.slice(0, maxTokens);
    const inputIds = BigInt64Array.from(ids, (v) => BigInt(v));
    const attentionMask = BigInt64Array.from(ids, () => 1n);
    return { inputIds, attentionMask };
  }
}

function buildDetector(
  opts: { logitsRows?: number[][]; threshold?: number; maxTokens?: number } = {},
): { detector: NamoTurnDetector; session: FakeSession; tokenizer: FakeTokenizer } {
  const { runtime, session } = buildFake(opts.logitsRows ?? [[0, 5]]);
  const tokenizer = new FakeTokenizer();
  const detector = NamoTurnDetector.fromSession(runtime, session, tokenizer, {
    threshold: opts.threshold,
    maxTokens: opts.maxTokens,
  });
  return { detector, session, tokenizer };
}

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

// ---------------------------------------------------------------------------
// Model / tokenizer path resolution
// ---------------------------------------------------------------------------

describe('NamoTurnDetector — path resolution', () => {
  it('rejects with a download hint when neither modelPath nor env is set', () => {
    const prev = process.env[NAMO_MODEL_ENV_VAR];
    delete process.env[NAMO_MODEL_ENV_VAR];
    try {
      expect(() => resolveNamoModelPath()).toThrow(/no model file configured/);
    } finally {
      if (prev !== undefined) process.env[NAMO_MODEL_ENV_VAR] = prev;
    }
  });

  it('rejects when the model file does not exist', () => {
    expect(() => resolveNamoModelPath('/nonexistent/model.onnx')).toThrow(/not found/);
  });

  it('resolves the env-var path when set', () => {
    const prev = process.env[NAMO_MODEL_ENV_VAR];
    process.env[NAMO_MODEL_ENV_VAR] = 'package.json';
    try {
      expect(resolveNamoModelPath()).toMatch(/package\.json$/);
    } finally {
      if (prev !== undefined) process.env[NAMO_MODEL_ENV_VAR] = prev;
      else delete process.env[NAMO_MODEL_ENV_VAR];
    }
  });

  it('explicit modelPath wins over the env var', () => {
    const prev = process.env[NAMO_MODEL_ENV_VAR];
    process.env[NAMO_MODEL_ENV_VAR] = '/nonexistent/env.onnx';
    try {
      expect(resolveNamoModelPath('package.json')).toMatch(/package\.json$/);
    } finally {
      if (prev !== undefined) process.env[NAMO_MODEL_ENV_VAR] = prev;
      else delete process.env[NAMO_MODEL_ENV_VAR];
    }
  });

  it('rejects when the model path is a directory', () => {
    expect(() => resolveNamoModelPath('.')).toThrow(/not a file/);
  });

  it('defaults the tokenizer path to the model directory', () => {
    const prev = process.env[NAMO_TOKENIZER_ENV_VAR];
    delete process.env[NAMO_TOKENIZER_ENV_VAR];
    try {
      expect(resolveNamoTokenizerPath(undefined, '/models/namo/model.onnx')).toBe('/models/namo');
    } finally {
      if (prev !== undefined) process.env[NAMO_TOKENIZER_ENV_VAR] = prev;
    }
  });

  it('honours the tokenizer env var, and an explicit path wins over it', () => {
    const prev = process.env[NAMO_TOKENIZER_ENV_VAR];
    process.env[NAMO_TOKENIZER_ENV_VAR] = '/env/tok';
    try {
      expect(resolveNamoTokenizerPath(undefined, '/models/namo/model.onnx')).toBe('/env/tok');
      expect(resolveNamoTokenizerPath('/explicit', '/models/namo/model.onnx')).toBe('/explicit');
    } finally {
      if (prev !== undefined) process.env[NAMO_TOKENIZER_ENV_VAR] = prev;
      else delete process.env[NAMO_TOKENIZER_ENV_VAR];
    }
  });
});

// ---------------------------------------------------------------------------
// Revision resolution — Hugging Face Hub download pinning
// ---------------------------------------------------------------------------

describe('NamoTurnDetector — revision resolution', () => {
  it('defaults to undefined without an option or env var', () => {
    const prev = process.env[NAMO_REVISION_ENV_VAR];
    delete process.env[NAMO_REVISION_ENV_VAR];
    try {
      expect(resolveNamoRevision(undefined)).toBeUndefined();
    } finally {
      if (prev !== undefined) process.env[NAMO_REVISION_ENV_VAR] = prev;
    }
  });

  it('reads the env var when no explicit option is given', () => {
    const prev = process.env[NAMO_REVISION_ENV_VAR];
    process.env[NAMO_REVISION_ENV_VAR] = 'abc1234';
    try {
      expect(resolveNamoRevision(undefined)).toBe('abc1234');
    } finally {
      if (prev !== undefined) process.env[NAMO_REVISION_ENV_VAR] = prev;
      else delete process.env[NAMO_REVISION_ENV_VAR];
    }
  });

  it('an explicit option wins over the env var', () => {
    const prev = process.env[NAMO_REVISION_ENV_VAR];
    process.env[NAMO_REVISION_ENV_VAR] = 'env-revision';
    try {
      expect(resolveNamoRevision('explicit-revision')).toBe('explicit-revision');
    } finally {
      if (prev !== undefined) process.env[NAMO_REVISION_ENV_VAR] = prev;
      else delete process.env[NAMO_REVISION_ENV_VAR];
    }
  });

  it('threads the resolved revision into AutoTokenizer.from_pretrained', async () => {
    // The module-level mock throws to exercise the missing-dependency path
    // elsewhere in this file; override it for this test only with a fake
    // that records its call args, then reset back afterwards.
    const fromPretrained = vi.fn(async () => (_text: string) => ({ input_ids: [1n] }));
    vi.resetModules();
    vi.doMock('@huggingface/transformers', () => ({
      AutoTokenizer: { from_pretrained: fromPretrained },
    }));
    try {
      const fresh = await import('../../src/providers/namo-turn-detector');
      await fresh.loadNamoTokenizer('org/some-repo', 'deadbeef');
      expect(fromPretrained).toHaveBeenCalledWith('org/some-repo', { revision: 'deadbeef' });

      await fresh.loadNamoTokenizer('/local/tokenizer/dir');
      expect(fromPretrained).toHaveBeenLastCalledWith('/local/tokenizer/dir', {
        revision: undefined,
      });
    } finally {
      // Re-establish the file-level "module missing" mock (rather than
      // vi.doUnmock, which would leave later dynamic imports resolving the
      // real, optionally-installed package) so the tests below still
      // exercise the missing-dependency install-error path deterministically.
      vi.doMock('@huggingface/transformers', () => {
        throw new Error("Cannot find module '@huggingface/transformers'");
      });
      vi.resetModules();
    }
  });
});

// ---------------------------------------------------------------------------
// Optional-dependency handling → clear install error (test d)
// ---------------------------------------------------------------------------

describe('NamoTurnDetector — optional dependency errors', () => {
  it('surfaces a clear install error when onnxruntime-node is missing', async () => {
    vi.mocked(sileroVadModule.loadOnnxRuntime).mockRejectedValueOnce(
      new Error('NamoTurnDetector requires the "onnxruntime-node" package — it is not installed.'),
    );
    // Model path resolves (package.json exists); the runtime loader fails.
    await expect(NamoTurnDetector.load({ modelPath: 'package.json' })).rejects.toThrow(
      /onnxruntime-node/,
    );
  });

  it('surfaces a clear install error when @huggingface/transformers is missing', async () => {
    await expect(loadNamoTokenizer('/any/tokenizer/dir')).rejects.toThrow(
      /@huggingface\/transformers/,
    );
  });

  it('load() surfaces the missing-transformers error (runtime OK, tokenizer fails)', async () => {
    const { runtime } = buildFake([[0, 1]]);
    vi.mocked(sileroVadModule.loadOnnxRuntime).mockResolvedValueOnce(runtime);
    await expect(NamoTurnDetector.load({ modelPath: 'package.json' })).rejects.toThrow(
      /@huggingface\/transformers/,
    );
  });
});

// ---------------------------------------------------------------------------
// maybeLoad — degrade (warn once + undefined) instead of throwing
// ---------------------------------------------------------------------------

describe('NamoTurnDetector.maybeLoad', () => {
  it('resolves undefined with a single warning when no model is configured', async () => {
    const prev = process.env[NAMO_MODEL_ENV_VAR];
    delete process.env[NAMO_MODEL_ENV_VAR];
    try {
      const { warn } = await withSpyLogger(async () => {
        expect(await NamoTurnDetector.maybeLoad()).toBeUndefined();
      });
      expect(warn).toHaveBeenCalledTimes(1);
      expect(String(warn.mock.calls[0][0])).toMatch(/falling back to plain VAD-silence/);
    } finally {
      if (prev !== undefined) process.env[NAMO_MODEL_ENV_VAR] = prev;
    }
  });

  it('still rejects an out-of-range threshold (config bug, not provisioning gap)', async () => {
    await expect(NamoTurnDetector.maybeLoad({ threshold: 1.5 })).rejects.toThrow(/threshold/);
  });
});

// ---------------------------------------------------------------------------
// completionProbability — logits → probability
// ---------------------------------------------------------------------------

describe('completionProbability', () => {
  it('soft-maxes a 2-class head and takes the COMPLETE class', () => {
    expect(completionProbability(Float32Array.from([0, 5]))).toBeGreaterThan(0.99);
    expect(completionProbability(Float32Array.from([5, 0]))).toBeLessThan(0.01);
  });

  it('passes a single-logit head through a sigmoid', () => {
    expect(completionProbability(Float32Array.from([0]))).toBeCloseTo(0.5, 6);
    expect(completionProbability(Float32Array.from([10]))).toBeGreaterThan(0.99);
    expect(completionProbability(Float32Array.from([-10]))).toBeLessThan(0.01);
  });

  it('returns 0 for empty / undefined output', () => {
    expect(completionProbability(undefined)).toBe(0);
    expect(completionProbability(Float32Array.from([]))).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// predict() — text scoring through a stubbed ONNX session (tests a, b)
// ---------------------------------------------------------------------------

describe('NamoTurnDetector.predict', () => {
  it('scores a complete transcript at or above the threshold', async () => {
    const { detector, session, tokenizer } = buildDetector({ logitsRows: [[0, 6]] });
    const p = await detector.predict(Buffer.alloc(0), 'What time is the meeting tomorrow?');
    expect(p).toBeGreaterThanOrEqual(detector.threshold);
    expect(session.calls).toHaveLength(1);
    expect(tokenizer.calls).toHaveLength(1);
  });

  it('scores a trailing-conjunction transcript below the threshold', async () => {
    const { detector, session } = buildDetector({ logitsRows: [[6, 0]] });
    const p = await detector.predict(Buffer.alloc(0), 'I would like to order a pizza and');
    expect(p).toBeLessThan(detector.threshold);
    expect(session.calls).toHaveLength(1);
  });

  it('short-circuits an empty transcript to 0 (never consults the model)', async () => {
    const { detector, session, tokenizer } = buildDetector({ logitsRows: [[0, 9]] });
    expect(await detector.predict(Buffer.alloc(640), undefined)).toBe(0);
    expect(await detector.predict(Buffer.alloc(640), '   ')).toBe(0);
    expect(session.calls).toHaveLength(0);
    expect(tokenizer.calls).toHaveLength(0);
  });

  it('feeds input_ids + attention_mask as [1, n] int64 tensors', async () => {
    const { detector, session } = buildDetector({ logitsRows: [[0, 3]] });
    await detector.predict(Buffer.alloc(0), 'hello there');
    const feeds = session.calls[0];
    expect(Object.keys(feeds).sort()).toEqual(['attention_mask', 'input_ids']);
    expect(feeds.input_ids.type).toBe('int64');
    expect(feeds.attention_mask.type).toBe('int64');
    expect(feeds.input_ids.data).toBeInstanceOf(BigInt64Array);
    expect(feeds.input_ids.dims[0]).toBe(1);
    expect(feeds.attention_mask.dims).toEqual(feeds.input_ids.dims);
  });

  it('truncates the transcript to maxTokens', async () => {
    const { detector, session, tokenizer } = buildDetector({ logitsRows: [[0, 1]], maxTokens: 8 });
    await detector.predict(Buffer.alloc(0), 'a'.repeat(200));
    expect(tokenizer.calls[0].maxTokens).toBe(8);
    expect(session.calls[0].input_ids.dims[1]).toBe(8);
  });

  it('ignores the audio window (same score with or without audio)', async () => {
    const withAudio = buildDetector({ logitsRows: [[0, 5]] });
    const noAudio = buildDetector({ logitsRows: [[0, 5]] });
    const p1 = await withAudio.detector.predict(Buffer.alloc(3200, 0x11), 'are we done here?');
    const p2 = await noAudio.detector.predict(Buffer.alloc(0), 'are we done here?');
    expect(p1).toBeCloseTo(p2, 9);
  });
});

// ---------------------------------------------------------------------------
// Surface / lifecycle
// ---------------------------------------------------------------------------

describe('NamoTurnDetector surface', () => {
  it('exposes provider tags + configured threshold / max tokens', () => {
    const { detector } = buildDetector({ threshold: 0.62, maxTokens: 64 });
    expect(detector.model).toBe('namo-turn-detector-v1');
    expect(detector.provider).toBe('ONNX');
    expect(detector.threshold).toBe(0.62);
    expect(detector.maxTokens).toBe(64);
  });

  it('defaults the threshold and token budget', () => {
    const { detector } = buildDetector();
    expect(detector.threshold).toBe(DEFAULT_NAMO_THRESHOLD);
    expect(detector.threshold).toBe(0.5);
    expect(detector.maxTokens).toBe(DEFAULT_NAMO_MAX_TOKENS);
  });

  it('fromSession rejects an out-of-range threshold', () => {
    const { runtime, session } = buildFake([[0, 1]]);
    expect(() =>
      NamoTurnDetector.fromSession(runtime, session, new FakeTokenizer(), { threshold: 2 }),
    ).toThrow(/threshold/);
  });

  it('close() is idempotent and predict() after close throws', async () => {
    const { detector } = buildDetector();
    await detector.close();
    await detector.close(); // must not throw
    await expect(detector.predict(Buffer.alloc(0), 'anything')).rejects.toThrow(/closed/);
  });
});
