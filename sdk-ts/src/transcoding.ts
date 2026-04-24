/**
 * Audio transcoding utilities for Patter TypeScript SDK.
 *
 * Pure TypeScript implementation — no native dependencies required.
 * Handles mulaw (G.711) encoding/decoding and PCM16 resampling for
 * telephony audio pipelines (Twilio mulaw 8kHz, Telnyx 16kHz PCM,
 * OpenAI TTS 24kHz PCM).
 */

// ---------- ITU-T G.711 mu-law tables ----------

/**
 * Lookup table: mu-law encoded byte -> signed 16-bit PCM value.
 * Generated from the standard ITU-T G.711 algorithm.
 */
const MULAW_TO_PCM16_TABLE: Int16Array = (() => {
  const table = new Int16Array(256);
  for (let i = 0; i < 256; i++) {
    const mu = ~i & 0xff;
    const sign = mu & 0x80 ? -1 : 1;
    const exponent = (mu >> 4) & 0x07;
    const mantissa = mu & 0x0f;
    const magnitude = ((mantissa << 1) | 0x21) << (exponent + 2);
    table[i] = sign * (magnitude - 0x84);
  }
  return table;
})();

/**
 * Lookup table: signed 16-bit PCM value (shifted to 0..65535) -> mu-law byte.
 * Built using the standard compression algorithm for fast encoding.
 */
const PCM16_TO_MULAW_TABLE: Uint8Array = (() => {
  const BIAS = 0x84;
  const CLIP = 32635;
  const table = new Uint8Array(65536);

  for (let i = 0; i < 65536; i++) {
    // Convert unsigned index to signed 16-bit
    let sample = i >= 32768 ? i - 65536 : i;

    const sign = sample < 0 ? 0x80 : 0;
    if (sample < 0) sample = -sample;
    if (sample > CLIP) sample = CLIP;
    sample += BIAS;

    let exponent = 7;
    const exponentMask = 0x4000;
    for (let shift = exponentMask; shift > 0 && (sample & shift) === 0; shift >>= 1) {
      exponent--;
    }

    const mantissa = (sample >> (exponent + 3)) & 0x0f;
    const mulaw = ~(sign | (exponent << 4) | mantissa) & 0xff;
    table[i] = mulaw;
  }

  return table;
})();

/**
 * Decode mu-law 8-bit audio to signed 16-bit little-endian PCM.
 *
 * Each input byte produces one 16-bit sample (2 bytes), so the output
 * buffer is exactly twice the length of the input.
 */
export function mulawToPcm16(mulawData: Buffer): Buffer {
  const out = Buffer.alloc(mulawData.length * 2);
  for (let i = 0; i < mulawData.length; i++) {
    out.writeInt16LE(MULAW_TO_PCM16_TABLE[mulawData[i]], i * 2);
  }
  return out;
}

/**
 * Encode signed 16-bit little-endian PCM to mu-law 8-bit audio.
 *
 * Each pair of input bytes (one 16-bit sample) produces one output byte.
 * If the input length is odd, the trailing byte is ignored.
 */
export function pcm16ToMulaw(pcmData: Buffer): Buffer {
  const sampleCount = Math.floor(pcmData.length / 2);
  const out = Buffer.alloc(sampleCount);
  for (let i = 0; i < sampleCount; i++) {
    const sample = pcmData.readInt16LE(i * 2);
    // Shift signed value to unsigned index (0..65535)
    out[i] = PCM16_TO_MULAW_TABLE[(sample + 65536) & 0xffff];
  }
  return out;
}

/**
 * Upsample 8 kHz PCM16 to 16 kHz using linear interpolation.
 *
 * For each pair of consecutive samples (s[n], s[n+1]) the output
 * contains s[n] followed by (s[n] + s[n+1]) / 2. The last sample
 * is duplicated to fill the final position.
 *
 * Output length = input length * 2.
 */
export function resample8kTo16k(pcm8k: Buffer): Buffer {
  if (pcm8k.length === 0) return Buffer.alloc(0);

  const sampleCount = Math.floor(pcm8k.length / 2);
  const out = Buffer.alloc(sampleCount * 2 * 2); // double the samples, 2 bytes each

  for (let i = 0; i < sampleCount; i++) {
    const current = pcm8k.readInt16LE(i * 2);
    const next = i + 1 < sampleCount ? pcm8k.readInt16LE((i + 1) * 2) : current;
    const interpolated = Math.round((current + next) / 2);

    out.writeInt16LE(current, i * 4);
    out.writeInt16LE(interpolated, i * 4 + 2);
  }

  return out;
}

/**
 * Downsample 16 kHz PCM16 to 8 kHz with anti-aliasing.
 *
 * Uses a 5-tap binomial low-pass FIR filter ([1, 4, 6, 4, 1] / 16) applied
 * to every pair of input samples before decimating by 2. The filter has
 * cutoff around Fs/4, which is enough to suppress content between 4 kHz
 * and 8 kHz in the input so it doesn't alias into the 0–4 kHz output band.
 *
 * A naive 2:1 decimation (``y[i] = x[2i]``) folds every frequency between
 * 4 kHz and 8 kHz back on top of the signal as continuous hiss — very
 * audible with TTS voice where sibilant consonants (/s/, /f/, /sh/) carry
 * a lot of energy above 4 kHz. Note: the Python SDK uses ``audioop.ratecv``
 * which does NOT apply an anti-alias filter on downsample (it linearly
 * interpolates between nearest input samples, which at an integer stride
 * of 2 reduces to naive decimation). This 5-tap FIR is therefore the
 * better-behaved of the two SDK implementations, though it still has ~8 dB
 * droop at 3.4 kHz.
 *
 * Chunk-boundary behaviour: samples outside the buffer are clamped to the
 * nearest edge sample, not zero-padded. This avoids a click at the start
 * of every chunk when the filter reaches before index 0.
 *
 * Output length = input length / 2.
 */
export function resample16kTo8k(pcm16k: Buffer): Buffer {
  if (pcm16k.length === 0) return Buffer.alloc(0);

  const sampleCount = Math.floor(pcm16k.length / 2);
  const outSamples = Math.floor(sampleCount / 2);
  const out = Buffer.alloc(outSamples * 2);

  const edge0 = sampleCount > 0 ? pcm16k.readInt16LE(0) : 0;
  const edgeN = sampleCount > 0 ? pcm16k.readInt16LE((sampleCount - 1) * 2) : 0;

  for (let i = 0; i < outSamples; i++) {
    const center = i * 2;
    const sM2 = center - 2 >= 0 ? pcm16k.readInt16LE((center - 2) * 2) : edge0;
    const sM1 = center - 1 >= 0 ? pcm16k.readInt16LE((center - 1) * 2) : edge0;
    const s0 = pcm16k.readInt16LE(center * 2);
    const sP1 = center + 1 < sampleCount ? pcm16k.readInt16LE((center + 1) * 2) : edgeN;
    const sP2 = center + 2 < sampleCount ? pcm16k.readInt16LE((center + 2) * 2) : edgeN;

    // Binomial 5-tap low-pass [1, 4, 6, 4, 1] / 16 then decimate by 2.
    const filtered = (sM2 + 4 * sM1 + 6 * s0 + 4 * sP1 + sP2 + 8) >> 4;
    out.writeInt16LE(Math.max(-32768, Math.min(32767, filtered)), i * 2);
  }

  return out;
}

/**
 * Downsample 24 kHz PCM16 to 16 kHz with linear interpolation.
 *
 * For a 3:2 ratio, each output sample is a weighted blend of the two
 * neighbouring input samples rather than a raw pick-every-third. This
 * eliminates aliasing of content between 8 kHz and 12 kHz into the
 * output's 0–8 kHz band that would otherwise sound like background hiss.
 *
 * Output length = floor(inputSamples * 2 / 3) * 2 bytes.
 */
export function resample24kTo16k(pcm24k: Buffer): Buffer {
  if (pcm24k.length === 0) return Buffer.alloc(0);

  const sampleCount = Math.floor(pcm24k.length / 2);
  const outSamples = Math.floor(sampleCount * 2 / 3);
  const out = Buffer.alloc(outSamples * 2);

  for (let i = 0; i < outSamples; i++) {
    // Map output index i to fractional input position: pos = i * 3 / 2.
    const pos = i * 1.5;
    const idx = Math.floor(pos);
    const frac = pos - idx;
    const s0 = pcm24k.readInt16LE(idx * 2);
    const s1 = idx + 1 < sampleCount ? pcm24k.readInt16LE((idx + 1) * 2) : s0;
    const interp = Math.round(s0 + (s1 - s0) * frac);
    out.writeInt16LE(Math.max(-32768, Math.min(32767, interp)), i * 2);
  }

  return out;
}
