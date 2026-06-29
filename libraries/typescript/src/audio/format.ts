/**
 * Audio-format contract — the single source of truth for "what rate/encoding
 * does this stage emit, and what does the carrier wire require".
 *
 * Why this exists: the outbound pipeline sender used to ASSUME every TTS
 * provider emits PCM16 @ 16 kHz and ran a hardcoded 16 kHz → 8 kHz decimator
 * before μ-law encoding. A provider configured for any other rate (e.g.
 * Cartesia at 8 kHz) was decimated AGAIN and played back at ~2x pitch
 * (chipmunk audio). The fix is to make every TTS provider DECLARE its output
 * format and the sender derive the resample ratio from the declared rate.
 *
 * One {@link AudioFormat} describes a single stage; {@link CARRIER_WIRE_FORMAT}
 * pins what each telephony carrier's media stream actually carries.
 */

import type { CarrierKind } from '../types';

/** Sample encoding of a PCM/telephony audio stream. */
export type AudioEncoding = 'pcm_s16le' | 'mulaw' | 'alaw';

/**
 * Declarative description of an audio stream: its encoding and sample rate.
 *
 * A TTS provider returns this from ``sourceAudioFormat()`` so the pipeline
 * knows exactly what it emits. The carrier layer exposes the same shape via
 * {@link CARRIER_WIRE_FORMAT} for the wire it requires. The pipeline resamples
 * / transcodes from the former to the latter automatically.
 */
export interface AudioFormat {
  /** Sample encoding. ``pcm_s16le`` = linear PCM16 LE; ``mulaw`` / ``alaw`` = G.711. */
  readonly encoding: AudioEncoding;
  /** Sample rate in Hz of the emitted (or required) stream. */
  readonly sampleRate: number;
}

/** Structural contract a TTS provider may implement to declare its output. */
export interface AudioFormatSource {
  /** Return the encoding + sample rate this provider emits per chunk. */
  sourceAudioFormat(): AudioFormat;
}

/**
 * Wire format every supported carrier's bidirectional media stream carries.
 *
 * All three carriers run G.711 μ-law @ 8 kHz on the SDK's negotiated media
 * stream: Twilio always; Telnyx because ``streaming_start`` pins
 * ``stream_bidirectional_codec=PCMU`` @ 8 kHz; Plivo because the answer XML
 * pins the μ-law content type. This is the ONE place that knowledge lives —
 * the sender reads it instead of hardcoding 8 kHz / μ-law in the encode path.
 */
export const CARRIER_WIRE_FORMAT: Readonly<Record<CarrierKind, AudioFormat>> = {
  twilio: { encoding: 'mulaw', sampleRate: 8000 },
  telnyx: { encoding: 'mulaw', sampleRate: 8000 },
  plivo: { encoding: 'mulaw', sampleRate: 8000 },
};

/** Fallback assumed for providers that declare nothing — the pre-fix default. */
export const LEGACY_PIPELINE_TTS_FORMAT: AudioFormat = {
  encoding: 'pcm_s16le',
  sampleRate: 16000,
};

/**
 * Parse a legacy wire-format string (ElevenLabs ``output_format`` style) into
 * an {@link AudioFormat}. Returns ``null`` for unrecognised strings (e.g. mp3)
 * so the caller can fall back.
 *
 * Recognised: ``ulaw_8000`` / ``mulaw_8000``, ``alaw_8000``, ``pcm_<rate>``,
 * and the bare ``pcm16_<rate>`` form.
 */
export function parseWireFormatString(s: string): AudioFormat | null {
  const lower = s.toLowerCase();
  if (lower === 'ulaw_8000' || lower === 'mulaw_8000') {
    return { encoding: 'mulaw', sampleRate: 8000 };
  }
  if (lower === 'alaw_8000') {
    return { encoding: 'alaw', sampleRate: 8000 };
  }
  const pcm = /^pcm(?:16)?_(\d{4,6})$/.exec(lower);
  if (pcm) {
    const rate = Number(pcm[1]);
    if (Number.isFinite(rate) && rate > 0) {
      return { encoding: 'pcm_s16le', sampleRate: rate };
    }
  }
  return null;
}

/** True when two formats are byte-compatible (same encoding + rate) → passthrough. */
export function formatsMatch(a: AudioFormat, b: AudioFormat): boolean {
  return a.encoding === b.encoding && a.sampleRate === b.sampleRate;
}

/**
 * Resolve the audio format a TTS adapter emits, in priority order:
 *  1. ``sourceAudioFormat()`` — the uniform contract (preferred).
 *  2. ``outputFormat`` string — ElevenLabs-style declaration.
 *  3. ``sampleRate`` number — PCM providers (Cartesia/OpenAI/LMNT/Rime/Inworld).
 *  4. {@link LEGACY_PIPELINE_TTS_FORMAT} — preserves pre-fix behaviour for
 *     any custom adapter that declares nothing.
 *
 * The fallback is what makes this change fully backward compatible: an adapter
 * that exposes none of the above behaves exactly as before (PCM16 @ 16 kHz).
 */
export function resolveTtsSourceFormat(tts: unknown): AudioFormat {
  if (tts && typeof tts === 'object') {
    const obj = tts as {
      sourceAudioFormat?: () => AudioFormat;
      outputFormat?: unknown;
      sampleRate?: unknown;
    };
    if (typeof obj.sourceAudioFormat === 'function') {
      const f = obj.sourceAudioFormat();
      if (f && typeof f.sampleRate === 'number' && f.sampleRate > 0) return f;
    }
    if (typeof obj.outputFormat === 'string') {
      const parsed = parseWireFormatString(obj.outputFormat);
      if (parsed) return parsed;
    }
    if (typeof obj.sampleRate === 'number' && obj.sampleRate > 0) {
      return { encoding: 'pcm_s16le', sampleRate: obj.sampleRate };
    }
  }
  return LEGACY_PIPELINE_TTS_FORMAT;
}
