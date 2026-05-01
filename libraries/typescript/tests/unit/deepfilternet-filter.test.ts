/*
 * Unit tests for the DeepFilterNet OSS AudioFilter (TypeScript port).
 *
 * MOCK: no real inference. These tests verify that without a model path (or
 * without onnxruntime-node installed) the filter deliberately passes audio
 * through unchanged rather than fabricating enhanced output. Real-model
 * behaviour is out of scope for CI and must be validated manually.
 */
import { describe, it, expect, vi } from 'vitest';
import { DeepFilterNetFilter } from '../../src/providers/deepfilternet-filter';

function pcm16Buffer(samples: number, fill: number = 0): Buffer {
  const buf = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i += 1) {
    buf.writeInt16LE(fill, i * 2);
  }
  return buf;
}

describe('DeepFilterNetFilter (TS)', () => {
  it('passes audio through unchanged when no modelPath is provided', async () => {
    const filter = new DeepFilterNetFilter({ silenceWarnings: true });
    const input = pcm16Buffer(160, 1234);
    const output = await filter.process(input, 16000);
    expect(output.equals(input)).toBe(true);
    await filter.close();
  });

  it('returns empty buffer for empty input', async () => {
    const filter = new DeepFilterNetFilter({ silenceWarnings: true });
    const output = await filter.process(Buffer.alloc(0), 16000);
    expect(output.length).toBe(0);
    await filter.close();
  });

  it('rejects process() after close()', async () => {
    const filter = new DeepFilterNetFilter({ silenceWarnings: true });
    await filter.close();
    await expect(filter.process(pcm16Buffer(16, 0), 16000)).rejects.toThrow(/closed/);
  });

  it('warns exactly once when model is missing', async () => {
    const warn = vi.fn();
    const { setLogger } = await import('../../src/logger');
    setLogger({
      info: () => {},
      debug: () => {},
      error: () => {},
      warn,
    });
    const filter = new DeepFilterNetFilter();
    await filter.process(pcm16Buffer(16, 0), 16000);
    await filter.process(pcm16Buffer(16, 0), 16000);
    expect(warn).toHaveBeenCalledTimes(1);
    await filter.close();
  });
});
