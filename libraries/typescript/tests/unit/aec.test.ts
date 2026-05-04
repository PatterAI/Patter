/**
 * Unit tests for {@link NlmsEchoCanceller}.
 *
 * Tests use synthetic audio (deterministic sine + noise mixture) to
 * verify convergence and double-talk preservation without depending on a
 * real acoustic environment.
 */

import { describe, it, expect } from 'vitest';
import { NlmsEchoCanceller } from '../../src/audio/aec';

const SR = 16000;

/** Deterministic Mulberry32-style PRNG for reproducible synthetic audio. */
function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function voicelike(numSamples: number): Float32Array {
  const out = new Float32Array(numSamples);
  const r = rng(0xa1c);
  for (let i = 0; i < numSamples; i++) {
    const t = i / SR;
    out[i] =
      0.3 * Math.sin(2 * Math.PI * 220 * t) +
      0.2 * Math.sin(2 * Math.PI * 440 * t) +
      0.05 * (r() * 2 - 1);
  }
  return out;
}

function toInt16Buf(arr: Float32Array): Buffer {
  const b = Buffer.alloc(arr.length * 2);
  for (let i = 0; i < arr.length; i++) {
    let v = Math.round(arr[i] * 32000);
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    b.writeInt16LE(v, i * 2);
  }
  return b;
}

function fromInt16Buf(buf: Buffer): Float32Array {
  const out = new Float32Array(buf.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = buf.readInt16LE(i * 2) / 32000;
  }
  return out;
}

function makeEcho(
  far: Float32Array,
  delaySamples = 1280,
  gain = 0.5,
): Float32Array {
  const out = new Float32Array(far.length);
  for (let i = delaySamples; i < far.length; i++) {
    out[i] = gain * far[i - delaySamples];
  }
  // Crude low-pass — 8-tap moving average — mimics smearing of a real
  // room/handset path.
  const lp = new Float32Array(out.length);
  for (let i = 0; i < out.length; i++) {
    let s = 0;
    let n = 0;
    for (let k = 0; k < 8 && i - k >= 0; k++, n++) s += out[i - k];
    lp[i] = s / n;
  }
  return lp;
}

describe('[unit] NlmsEchoCanceller', () => {
  it('rejects unsupported sample rate', () => {
    // @ts-expect-error invalid input by design
    expect(() => new NlmsEchoCanceller({ sampleRate: 44100 })).toThrow(
      /8000 Hz or 16000 Hz/,
    );
  });

  it('rejects too few taps', () => {
    expect(() => new NlmsEchoCanceller({ filterTaps: 32 })).toThrow(
      /filterTaps must be/,
    );
  });

  it('rejects invalid step size', () => {
    expect(() => new NlmsEchoCanceller({ stepSize: 0 })).toThrow(/stepSize/);
  });

  it('passes near-end through until far-end buffer fills', () => {
    const aec = new NlmsEchoCanceller({ filterTaps: 512 });
    const probe = toInt16Buf(voicelike(1024));
    const out = aec.processNearEnd(probe);
    expect(out.equals(probe)).toBe(true);
  });

  it('converges to at least 10 dB ERLE after 1 s of training', () => {
    const aec = new NlmsEchoCanceller({ filterTaps: 512, stepSize: 0.2 });
    const far = voicelike(SR);
    const echo = makeEcho(far);
    aec.pushFarEnd(toInt16Buf(far));
    const cleaned = fromInt16Buf(aec.processNearEnd(toInt16Buf(echo)));

    const tailStart = Math.floor(0.7 * SR);
    let inPwr = 0;
    let outPwr = 0;
    for (let i = tailStart; i < SR; i++) {
      inPwr += echo[i] * echo[i];
      outPwr += cleaned[i] * cleaned[i];
    }
    inPwr /= SR - tailStart;
    outPwr /= SR - tailStart;
    const erleDb = 10 * Math.log10(inPwr / Math.max(outPwr, 1e-10));
    expect(erleDb).toBeGreaterThanOrEqual(10.0);
  });

  it('preserves near-end speech during double-talk (Geigel)', () => {
    const aec = new NlmsEchoCanceller({ filterTaps: 512, stepSize: 0.2 });
    const far = voicelike(SR);
    const echo = makeEcho(far);

    // Phase 1 — train on echo-only audio for 0.7 s.
    const nTrain = Math.floor(0.7 * SR);
    aec.pushFarEnd(toInt16Buf(far.subarray(0, nTrain)));
    aec.processNearEnd(toInt16Buf(echo.subarray(0, nTrain)));

    // Phase 2 — caller speaks (different frequency than far-end).
    const tailLen = SR - nTrain;
    const nearSpeech = new Float32Array(tailLen);
    for (let i = 0; i < tailLen; i++) {
      nearSpeech[i] = 0.4 * Math.sin((2 * Math.PI * 330 * i) / SR);
    }
    const combined = new Float32Array(tailLen);
    for (let i = 0; i < tailLen; i++) {
      combined[i] = echo[nTrain + i] + nearSpeech[i];
    }

    aec.pushFarEnd(toInt16Buf(far.subarray(nTrain)));
    const cleaned = fromInt16Buf(aec.processNearEnd(toInt16Buf(combined)));

    let speechPwr = 0;
    let cleanedPwr = 0;
    for (let i = 0; i < tailLen; i++) {
      speechPwr += nearSpeech[i] * nearSpeech[i];
      cleanedPwr += cleaned[i] * cleaned[i];
    }
    expect(cleanedPwr).toBeGreaterThanOrEqual(0.5 * speechPwr);
    expect(aec.doubleTalkFrames).toBeGreaterThanOrEqual(1);
  });

  it('reset() clears filter state', () => {
    const aec = new NlmsEchoCanceller({ filterTaps: 512, stepSize: 0.2 });
    const far = voicelike(SR / 2);
    aec.pushFarEnd(toInt16Buf(far));
    aec.processNearEnd(toInt16Buf(makeEcho(far)));
    expect(aec.framesProcessed).toBeGreaterThanOrEqual(1);

    aec.reset();
    expect(aec.framesProcessed).toBe(0);
    expect(aec.doubleTalkFrames).toBe(0);

    const probe = toInt16Buf(voicelike(1024));
    const out = aec.processNearEnd(probe);
    expect(out.equals(probe)).toBe(true);
  });

  it('handles empty buffer input as no-op', () => {
    const aec = new NlmsEchoCanceller({ filterTaps: 512 });
    aec.pushFarEnd(Buffer.alloc(0));
    const out = aec.processNearEnd(Buffer.alloc(0));
    expect(out.length).toBe(0);
  });

  it('default warmup converges to ≥10 dB ERLE within the first 250 ms', () => {
    // Regression guard for the cellular-call slow-convergence bug
    // observed on 0.6.0 with 2048 taps + constant step: a real call
    // showed 8–12 s convergence and the user's first turn was lost.
    // The default config (512 taps + 5× warmup step for 0.5 s) must
    // hit ≥10 dB ERLE in the first 250 ms output window.
    const r = rng(42);
    const far = new Float32Array(SR);
    for (let i = 0; i < SR; i++) {
      const t = i / SR;
      far[i] =
        0.4 * Math.sin(2 * Math.PI * 220 * t) +
        0.3 * Math.sin(2 * Math.PI * 440 * t) +
        0.2 * Math.sin(2 * Math.PI * 880 * t) +
        0.15 * (r() * 2 - 1);
    }
    const echo = makeEcho(far, Math.floor(0.05 * SR));

    const aec = new NlmsEchoCanceller(); // defaults
    const frame = 320;
    const outChunks: Float32Array[] = [];
    for (let i = 0; i < far.length - frame; i += frame) {
      aec.pushFarEnd(toInt16Buf(far.subarray(i, i + frame)));
      outChunks.push(
        fromInt16Buf(aec.processNearEnd(toInt16Buf(echo.subarray(i, i + frame)))),
      );
    }
    const totalLen = outChunks.reduce((s, c) => s + c.length, 0);
    const out = new Float32Array(totalLen);
    let off = 0;
    for (const c of outChunks) {
      out.set(c, off);
      off += c.length;
    }

    const first = (250 * SR) / 1000;
    let inPwr = 0;
    let outPwr = 0;
    for (let i = 0; i < first; i++) {
      inPwr += echo[i] * echo[i];
      outPwr += out[i] * out[i];
    }
    const erle = 10 * Math.log10(inPwr / Math.max(outPwr, 1e-10));
    expect(erle).toBeGreaterThanOrEqual(10.0);
  });
});
