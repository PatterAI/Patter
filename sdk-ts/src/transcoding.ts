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
 * Downsample 16 kHz PCM16 to 8 kHz by taking every 2nd sample.
 *
 * Output length = input length / 2.
 */
export function resample16kTo8k(pcm16k: Buffer): Buffer {
  if (pcm16k.length === 0) return Buffer.alloc(0);

  const sampleCount = Math.floor(pcm16k.length / 2);
  const outSamples = Math.floor(sampleCount / 2);
  const out = Buffer.alloc(outSamples * 2);

  for (let i = 0; i < outSamples; i++) {
    const sample = pcm16k.readInt16LE(i * 2 * 2); // every 2nd sample
    out.writeInt16LE(sample, i * 2);
  }

  return out;
}

/**
 * Downsample 24 kHz PCM16 to 16 kHz by taking 2 of every 3 samples.
 *
 * Matches the Python backend approach: for every group of 3 input samples,
 * output the 1st and 2nd, skip the 3rd.
 *
 * Output length = floor(inputSamples * 2 / 3) * 2 bytes.
 */
export function resample24kTo16k(pcm24k: Buffer): Buffer {
  if (pcm24k.length === 0) return Buffer.alloc(0);

  const sampleCount = Math.floor(pcm24k.length / 2);
  const outSamples = Math.floor(sampleCount * 2 / 3);
  const out = Buffer.alloc(outSamples * 2);

  let outIdx = 0;
  for (let i = 0; i < sampleCount && outIdx < outSamples; i++) {
    // Skip every 3rd sample (index 2, 5, 8, ...)
    if (i % 3 === 2) continue;
    out.writeInt16LE(pcm24k.readInt16LE(i * 2), outIdx * 2);
    outIdx++;
  }

  return out;
}
