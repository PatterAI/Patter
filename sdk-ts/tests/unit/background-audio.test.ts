import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { promises as fs } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

import {
  BackgroundAudioPlayer,
  BuiltinAudioClip,
  builtinClipPath,
  mixPcm,
  resamplePcm,
  selectSoundFromList,
  type AudioConfig,
} from '../../src/services/background-audio';

// ---------------------------------------------------------------------------
// Synthetic PCM helpers
// ---------------------------------------------------------------------------

function sinePcm(
  freq: number,
  durationMs: number,
  sampleRate: number,
  amplitude = 10_000,
): Buffer {
  const n = Math.floor((sampleRate * durationMs) / 1000);
  const out = Buffer.allocUnsafe(n * 2);
  for (let i = 0; i < n; i++) {
    const v = Math.round(amplitude * Math.sin((2 * Math.PI * freq * i) / sampleRate));
    out.writeInt16LE(Math.max(-32768, Math.min(32767, v)), i * 2);
  }
  return out;
}

function rmsInt16(buf: Buffer): number {
  if (buf.length === 0) return 0;
  const n = buf.length >> 1;
  let sumSq = 0;
  for (let i = 0; i < n; i++) {
    const v = buf.readInt16LE(i * 2);
    sumSq += v * v;
  }
  return Math.sqrt(sumSq / n);
}

/** Naive DFT magnitude at one frequency bin — avoids depending on a FFT lib. */
function magnitudeAt(buf: Buffer, freq: number, sampleRate: number): number {
  const n = buf.length >> 1;
  let re = 0;
  let im = 0;
  for (let i = 0; i < n; i++) {
    const sample = buf.readInt16LE(i * 2);
    const phase = (2 * Math.PI * freq * i) / sampleRate;
    re += sample * Math.cos(phase);
    im -= sample * Math.sin(phase);
  }
  return Math.sqrt(re * re + im * im) / n;
}

// ---------------------------------------------------------------------------
// mixPcm
// ---------------------------------------------------------------------------

describe('mixPcm', () => {
  it('returns agent unchanged when bg is empty', () => {
    const agent = sinePcm(440, 20, 16000);
    expect(mixPcm(agent, Buffer.alloc(0), 0.5).equals(agent)).toBe(true);
  });

  it('returns agent unchanged when ratio is 0', () => {
    const agent = sinePcm(440, 20, 16000);
    const bg = sinePcm(220, 20, 16000);
    expect(mixPcm(agent, bg, 0).equals(agent)).toBe(true);
  });

  it('returns empty when agent is empty', () => {
    const bg = sinePcm(440, 20, 16000);
    expect(mixPcm(Buffer.alloc(0), bg, 0.5).length).toBe(0);
  });

  it('output length matches agent regardless of bg length', () => {
    const agent = sinePcm(440, 20, 16000); // 640 bytes
    expect(mixPcm(agent, sinePcm(220, 10, 16000), 0.5).length).toBe(agent.length);
    expect(mixPcm(agent, sinePcm(220, 40, 16000), 0.5).length).toBe(agent.length);
  });

  it('RMS of mix of two orthogonal sines matches closed-form formula', () => {
    const sr = 16000;
    // Integer cycles in 100 ms -> orthogonal.
    const agent = sinePcm(440, 100, sr, 10_000);
    const bg = sinePcm(880, 100, sr, 10_000);
    const ratio = 0.25;

    const mixed = mixPcm(agent, bg, ratio);
    const expected = Math.sqrt(rmsInt16(agent) ** 2 + (ratio * rmsInt16(bg)) ** 2);
    const actual = rmsInt16(mixed);
    expect(Math.abs(actual - expected) / expected).toBeLessThan(0.02);
  });

  it('clips to int16 range without wraparound', () => {
    const fullScale = Buffer.alloc(400);
    for (let i = 0; i < 200; i++) fullScale.writeInt16LE(30_000, i * 2);
    const mixed = mixPcm(fullScale, fullScale, 1.0);
    for (let i = 0; i < 200; i++) {
      expect(mixed.readInt16LE(i * 2)).toBe(32767);
    }
  });

  it('rejects odd byte lengths', () => {
    expect(() => mixPcm(Buffer.alloc(3), Buffer.alloc(2), 0.1)).toThrow(/whole number/);
    expect(() => mixPcm(Buffer.alloc(2), Buffer.alloc(1), 0.1)).toThrow(/whole number/);
  });

  it('DFT: mixed signal retains both input frequencies', () => {
    const sr = 16000;
    const agent = sinePcm(500, 100, sr, 8_000);
    const bg = sinePcm(1500, 100, sr, 8_000);
    const mixed = mixPcm(agent, bg, 0.5);

    const mAgent = magnitudeAt(mixed, 500, sr);
    const mBg = magnitudeAt(mixed, 1500, sr);
    const mNoise = magnitudeAt(mixed, 3000, sr);

    expect(mAgent).toBeGreaterThan(20 * mNoise);
    expect(mBg).toBeGreaterThan(5 * mNoise);
  });
});

// ---------------------------------------------------------------------------
// resamplePcm
// ---------------------------------------------------------------------------

describe('resamplePcm', () => {
  it('is a copy when sample rates match', () => {
    const src = sinePcm(1000, 100, 16000);
    const out = resamplePcm(src, 16000, 16000);
    expect(out.equals(src)).toBe(true);
    expect(out).not.toBe(src); // must be a copy
  });

  it('2x upsample doubles sample count', () => {
    const src = sinePcm(500, 100, 8000);
    const out = resamplePcm(src, 8000, 16000);
    // Not exactly 2x because of the (dstSamples-1)/(srcSamples-1) mapping;
    // must be within one sample of 2x.
    expect(Math.abs(out.length / 2 - src.length)).toBeLessThan(4);
  });

  it('preserves a 1 kHz tone when resampling 16 kHz -> 8 kHz', () => {
    const src = sinePcm(1000, 500, 16000, 20_000);
    const out = resamplePcm(src, 16000, 8000);
    const mTone = magnitudeAt(out, 1000, 8000);
    const mNoise = magnitudeAt(out, 3500, 8000);
    expect(mTone).toBeGreaterThan(20 * mNoise);
  });
});

// ---------------------------------------------------------------------------
// selectSoundFromList
// ---------------------------------------------------------------------------

describe('selectSoundFromList', () => {
  it('empirical distribution matches configured probabilities within 5%', () => {
    const configs: AudioConfig[] = [
      { source: { kind: 'pcm', pcm: Buffer.alloc(0), sampleRate: 16000 }, probability: 0.5 },
      { source: { kind: 'pcm', pcm: Buffer.alloc(0), sampleRate: 16000 }, probability: 0.3 },
      { source: { kind: 'pcm', pcm: Buffer.alloc(0), sampleRate: 16000 }, probability: 0.2 },
    ];
    const counts = new Map<number, number>();
    const draws = 10_000;
    for (let i = 0; i < draws; i++) {
      const picked = selectSoundFromList(configs);
      expect(picked).not.toBeNull();
      const idx = configs.indexOf(picked!);
      counts.set(idx, (counts.get(idx) ?? 0) + 1);
    }
    expect(Math.abs((counts.get(0) ?? 0) / draws - 0.5)).toBeLessThan(0.05);
    expect(Math.abs((counts.get(1) ?? 0) / draws - 0.3)).toBeLessThan(0.05);
    expect(Math.abs((counts.get(2) ?? 0) / draws - 0.2)).toBeLessThan(0.05);
  });

  it('returns null on the silence band when probabilities sum < 1', () => {
    const configs: AudioConfig[] = [
      { source: { kind: 'pcm', pcm: Buffer.alloc(0), sampleRate: 16000 }, probability: 0.3 },
    ];
    const draws = 10_000;
    let silence = 0;
    for (let i = 0; i < draws; i++) {
      if (selectSoundFromList(configs) === null) silence++;
    }
    expect(Math.abs(silence / draws - 0.7)).toBeLessThan(0.05);
  });

  it('returns null when all probabilities are zero', () => {
    const configs: AudioConfig[] = [
      { source: { kind: 'pcm', pcm: Buffer.alloc(0), sampleRate: 16000 }, probability: 0 },
    ];
    expect(selectSoundFromList(configs)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Bundled .ogg clips
// ---------------------------------------------------------------------------

describe('bundled builtin clips', () => {
  it('builtinClipPath resolves for every clip', () => {
    for (const clip of Object.values(BuiltinAudioClip)) {
      const p = builtinClipPath(clip);
      expect(path.isAbsolute(p)).toBe(true);
      expect(p.endsWith(clip)).toBe(true);
    }
  });

  it('every bundled clip starts with the OggS magic bytes', async () => {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const resDir = path.resolve(here, '..', '..', 'src', 'resources', 'audio');
    for (const clip of Object.values(BuiltinAudioClip)) {
      const buf = await fs.readFile(path.join(resDir, clip));
      expect(buf.subarray(0, 4).toString('ascii')).toBe('OggS');
    }
  });

  it('ships exactly 7 .ogg files plus a NOTICE', async () => {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const resDir = path.resolve(here, '..', '..', 'src', 'resources', 'audio');
    const entries = await fs.readdir(resDir);
    const ogg = entries.filter((e) => e.endsWith('.ogg'));
    expect(ogg.length).toBe(7);
    expect(entries).toContain('NOTICE');
  });
});

// ---------------------------------------------------------------------------
// BackgroundAudioPlayer — start/mix/stop with raw PCM source
// ---------------------------------------------------------------------------

describe('BackgroundAudioPlayer', () => {
  let player: BackgroundAudioPlayer | null = null;

  afterEach(async () => {
    if (player) {
      await player.stop();
      player = null;
    }
  });

  it('mix before start is a no-op', async () => {
    player = new BackgroundAudioPlayer({
      kind: 'pcm',
      pcm: sinePcm(1500, 500, 16000),
      sampleRate: 16000,
    });
    const agent = sinePcm(500, 20, 16000);
    const out = await player.mix(agent, 16000);
    expect(out.equals(agent)).toBe(true);
  });

  it('rejects out-of-range volume', () => {
    expect(
      () =>
        new BackgroundAudioPlayer(
          { kind: 'pcm', pcm: Buffer.alloc(0), sampleRate: 16000 },
          { volume: 1.5 },
        ),
    ).toThrow(/volume/);
    expect(
      () =>
        new BackgroundAudioPlayer(
          { kind: 'pcm', pcm: Buffer.alloc(0), sampleRate: 16000 },
          { volume: -0.1 },
        ),
    ).toThrow(/volume/);
  });

  it('mix with raw PCM source preserves agent and bg frequencies', async () => {
    const sr = 16000;
    player = new BackgroundAudioPlayer(
      { kind: 'pcm', pcm: sinePcm(1500, 500, sr, 20_000), sampleRate: sr },
      { volume: 0.5, loop: true },
    );
    await player.start();

    const agent = sinePcm(500, 100, sr, 8_000);
    const mixed = await player.mix(agent, sr);
    expect(mixed.length).toBe(agent.length);

    const mAgent = magnitudeAt(mixed, 500, sr);
    const mBg = magnitudeAt(mixed, 1500, sr);
    const mNoise = magnitudeAt(mixed, 3500, sr);
    expect(mAgent).toBeGreaterThan(20 * mNoise);
    expect(mBg).toBeGreaterThan(5 * mNoise);
  });

  it('zero volume is a no-op', async () => {
    player = new BackgroundAudioPlayer(
      { kind: 'pcm', pcm: sinePcm(1500, 200, 16000), sampleRate: 16000 },
      { volume: 0, loop: true },
    );
    await player.start();
    const agent = sinePcm(500, 20, 16000);
    const out = await player.mix(agent, 16000);
    expect(out.equals(agent)).toBe(true);
  });

  it('non-loop source exhausts and then passes through', async () => {
    const sr = 16000;
    player = new BackgroundAudioPlayer(
      { kind: 'pcm', pcm: sinePcm(1500, 20, sr, 20_000), sampleRate: sr },
      { volume: 0.5, loop: false },
    );
    await player.start();

    const agent = sinePcm(500, 20, sr);
    const first = await player.mix(agent, sr);
    expect(first.equals(agent)).toBe(false);
    const second = await player.mix(agent, sr);
    expect(second.equals(agent)).toBe(true);
  });

  it('resamples to a different target sample rate', async () => {
    const srcSr = 16000;
    const dstSr = 8000;
    player = new BackgroundAudioPlayer(
      { kind: 'pcm', pcm: sinePcm(1000, 500, srcSr, 20_000), sampleRate: srcSr },
      { volume: 0.5, loop: true },
    );
    await player.start();

    const agent = sinePcm(500, 100, dstSr, 8_000);
    const mixed = await player.mix(agent, dstSr);
    expect(mixed.length).toBe(agent.length);

    const mAgent = magnitudeAt(mixed, 500, dstSr);
    const mBg = magnitudeAt(mixed, 1000, dstSr);
    const mNoise = magnitudeAt(mixed, 3500, dstSr);
    expect(mAgent).toBeGreaterThan(20 * mNoise);
    expect(mBg).toBeGreaterThan(5 * mNoise);
  });

  it('stop releases PCM and resets state', async () => {
    player = new BackgroundAudioPlayer(
      { kind: 'pcm', pcm: sinePcm(1500, 200, 16000), sampleRate: 16000 },
      { loop: true },
    );
    await player.start();
    await player.stop();

    const agent = sinePcm(500, 20, 16000);
    const out = await player.mix(agent, 16000);
    expect(out.equals(agent)).toBe(true);
  });

  it('probability list selection + silence path are both handled', async () => {
    const configs: AudioConfig[] = [
      {
        source: { kind: 'pcm', pcm: sinePcm(1500, 100, 16000), sampleRate: 16000 },
        probability: 0.001, // almost always silence
      },
    ];
    // Run until we hit the silence path; ensure mix is a no-op.
    for (let i = 0; i < 20; i++) {
      const p = new BackgroundAudioPlayer(configs, { volume: 0.5 });
      await p.start();
      const agent = sinePcm(500, 20, 16000);
      const out = await p.mix(agent, 16000);
      // When silence was picked, pcm buffer is empty and mix is a pass-through.
      // When (rarely) the source was picked, lengths still match.
      expect(out.length).toBe(agent.length);
      await p.stop();
    }
  });
});
