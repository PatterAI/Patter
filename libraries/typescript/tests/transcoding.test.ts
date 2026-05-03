import { describe, it, expect } from "vitest";
import {
  mulawToPcm16,
  pcm16ToMulaw,
  resample8kTo16k,
  resample16kTo8k,
  resample24kTo16k,
} from "../src/audio/transcoding";

describe("mulawToPcm16", () => {
  it("decodes silence (mulaw 0xFF) to near-zero PCM", () => {
    const mulaw = Buffer.from([0xff]);
    const pcm = mulawToPcm16(mulaw);
    expect(pcm.length).toBe(2);
    // mulaw 0xFF is the standard silence value, should decode to 0
    expect(pcm.readInt16LE(0)).toBe(0);
  });

  it("doubles the buffer length", () => {
    const mulaw = Buffer.from([0x00, 0x80, 0xff, 0x7f]);
    const pcm = mulawToPcm16(mulaw);
    expect(pcm.length).toBe(mulaw.length * 2);
  });

  it("produces valid signed 16-bit values", () => {
    const mulaw = Buffer.alloc(256);
    for (let i = 0; i < 256; i++) mulaw[i] = i;
    const pcm = mulawToPcm16(mulaw);
    for (let i = 0; i < 256; i++) {
      const sample = pcm.readInt16LE(i * 2);
      expect(sample).toBeGreaterThanOrEqual(-32768);
      expect(sample).toBeLessThanOrEqual(32767);
    }
  });
});

describe("pcm16ToMulaw", () => {
  it("encodes silence (PCM 0) to mulaw 0xFF", () => {
    const pcm = Buffer.alloc(2);
    pcm.writeInt16LE(0, 0);
    const mulaw = pcm16ToMulaw(pcm);
    expect(mulaw.length).toBe(1);
    expect(mulaw[0]).toBe(0xff);
  });

  it("halves the buffer length", () => {
    const pcm = Buffer.alloc(8);
    const mulaw = pcm16ToMulaw(pcm);
    expect(mulaw.length).toBe(4);
  });

  it("ignores trailing odd byte", () => {
    const pcm = Buffer.alloc(5); // 2 full samples + 1 trailing byte
    const mulaw = pcm16ToMulaw(pcm);
    expect(mulaw.length).toBe(2);
  });
});

describe("mulaw round-trip", () => {
  it("round-trips all 256 mulaw values within tolerance", () => {
    // mulaw encoding is lossy, but decoding then re-encoding should
    // produce the same mulaw byte for all 256 possible values.
    // Exception: mulaw 0x7F and 0xFF both decode to PCM 0 (silence),
    // so 0x7F round-trips to 0xFF. This is correct G.711 behavior.
    const original = Buffer.alloc(256);
    for (let i = 0; i < 256; i++) original[i] = i;

    const pcm = mulawToPcm16(original);
    const roundTripped = pcm16ToMulaw(pcm);

    for (let i = 0; i < 256; i++) {
      if (i === 0x7f) {
        // Both 0x7F and 0xFF map to PCM 0; canonical encoding is 0xFF
        expect(roundTripped[i]).toBe(0xff);
      } else {
        expect(roundTripped[i]).toBe(original[i]);
      }
    }
  });

  it("round-trips known audio values", () => {
    // Positive peak, negative peak, mid-range
    const testValues = [0x00, 0x80, 0x40, 0xc0, 0x10, 0x90];
    const mulaw = Buffer.from(testValues);
    const pcm = mulawToPcm16(mulaw);
    const result = pcm16ToMulaw(pcm);

    for (let i = 0; i < testValues.length; i++) {
      expect(result[i]).toBe(testValues[i]);
    }
  });
});

describe("resample8kTo16k", () => {
  it("returns empty buffer for empty input", () => {
    expect(resample8kTo16k(Buffer.alloc(0)).length).toBe(0);
  });

  it("doubles the number of samples", () => {
    // 4 samples = 8 bytes -> 8 samples = 16 bytes
    const input = Buffer.alloc(8);
    input.writeInt16LE(100, 0);
    input.writeInt16LE(200, 2);
    input.writeInt16LE(300, 4);
    input.writeInt16LE(400, 6);

    const output = resample8kTo16k(input);
    expect(output.length).toBe(16);
  });

  it("interpolates between consecutive samples", () => {
    const input = Buffer.alloc(4); // 2 samples
    input.writeInt16LE(0, 0);
    input.writeInt16LE(1000, 2);

    const output = resample8kTo16k(input);
    // Sample 0: original 0
    expect(output.readInt16LE(0)).toBe(0);
    // Sample 1: interpolated (0 + 1000) / 2 = 500
    expect(output.readInt16LE(2)).toBe(500);
    // Sample 2: original 1000
    expect(output.readInt16LE(4)).toBe(1000);
    // Sample 3: interpolated (1000 + 1000) / 2 = 1000 (last sample duplicated)
    expect(output.readInt16LE(6)).toBe(1000);
  });
});

describe("resample16kTo8k", () => {
  it("returns empty buffer for empty input", () => {
    expect(resample16kTo8k(Buffer.alloc(0)).length).toBe(0);
  });

  it("halves the number of samples", () => {
    // 8 samples = 16 bytes -> 4 samples = 8 bytes
    const input = Buffer.alloc(16);
    const output = resample16kTo8k(input);
    expect(output.length).toBe(8);
  });

  it("applies anti-aliasing low-pass before decimating by 2", () => {
    // 5-tap binomial filter [1, 4, 6, 4, 1] / 16 with edge-clamping.
    // Input samples [100, 200, 300, 400] at 16 kHz produce 2 filtered samples at 8 kHz.
    const input = Buffer.alloc(8);
    input.writeInt16LE(100, 0);
    input.writeInt16LE(200, 2);
    input.writeInt16LE(300, 4);
    input.writeInt16LE(400, 6);

    const output = resample16kTo8k(input);
    expect(output.length).toBe(4);
    // Output[0] at center=0 with edge-clamped s_m1, s_m2 = 100:
    // (100 + 4*100 + 6*100 + 4*200 + 300 + 8) >> 4 = 2208 >> 4 = 138
    expect(output.readInt16LE(0)).toBe(138);
    // Output[1] at center=2 with edge-clamped s_p2 = 400:
    // (100 + 4*200 + 6*300 + 4*400 + 400 + 8) >> 4 = 4708 >> 4 = 294
    expect(output.readInt16LE(2)).toBe(294);
  });

  it("passes DC through unchanged (no aliasing on a constant signal)", () => {
    // Filter is normalised (coefficients sum to 16, then >> 4) so a constant
    // input must come out as the same constant.
    const input = Buffer.alloc(20);
    for (let i = 0; i < 10; i++) input.writeInt16LE(5000, i * 2);
    const output = resample16kTo8k(input);
    expect(output.length).toBe(10);
    for (let i = 0; i < 5; i++) {
      // Allow +/- 1 LSB for rounding with (sum + 8) >> 4.
      const v = output.readInt16LE(i * 2);
      expect(Math.abs(v - 5000)).toBeLessThanOrEqual(1);
    }
  });
});

describe("resample24kTo16k", () => {
  it("returns empty buffer for empty input", () => {
    expect(resample24kTo16k(Buffer.alloc(0)).length).toBe(0);
  });

  it("linearly interpolates 3:2 ratio instead of raw picking", () => {
    // 6 samples at 24 kHz -> 4 samples at 16 kHz.
    // Output index i samples input at fractional position i * 1.5.
    const input = Buffer.alloc(12);
    input.writeInt16LE(10, 0);
    input.writeInt16LE(20, 2);
    input.writeInt16LE(30, 4);
    input.writeInt16LE(40, 6);
    input.writeInt16LE(50, 8);
    input.writeInt16LE(60, 10);

    const output = resample24kTo16k(input);
    expect(output.length).toBe(8);
    // i=0 -> pos=0.0 -> s[0] = 10
    expect(output.readInt16LE(0)).toBe(10);
    // i=1 -> pos=1.5 -> 0.5*(s[1]+s[2]) = 25
    expect(output.readInt16LE(2)).toBe(25);
    // i=2 -> pos=3.0 -> s[3] = 40
    expect(output.readInt16LE(4)).toBe(40);
    // i=3 -> pos=4.5 -> 0.5*(s[4]+s[5]) = 55
    expect(output.readInt16LE(6)).toBe(55);
  });

  it("produces correct output length for various input sizes", () => {
    // 3 samples -> 2 output
    expect(resample24kTo16k(Buffer.alloc(6)).length).toBe(4);
    // 9 samples -> 6 output
    expect(resample24kTo16k(Buffer.alloc(18)).length).toBe(12);
    // 12 samples -> 8 output
    expect(resample24kTo16k(Buffer.alloc(24)).length).toBe(16);
  });

  it("handles non-multiple-of-3 sample counts", () => {
    // 4 samples -> floor(4 * 2/3) = 2 output samples
    const input = Buffer.alloc(8);
    input.writeInt16LE(100, 0);
    input.writeInt16LE(200, 2);
    input.writeInt16LE(300, 4);
    input.writeInt16LE(400, 6);

    const output = resample24kTo16k(input);
    expect(output.length).toBe(4);
    // i=0 -> pos=0.0 -> s[0] = 100
    expect(output.readInt16LE(0)).toBe(100);
    // i=1 -> pos=1.5 -> 0.5*(s[1]+s[2]) = 250
    expect(output.readInt16LE(2)).toBe(250);
  });
});
