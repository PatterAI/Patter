/**
 * Background-audio mixer for the Patter TypeScript SDK. Patter routes
 * outbound PCM through the pipeline stream handler, so this module exposes
 * a ``start / mix / stop`` API that does no I/O of its own. See
 * {@link BackgroundAudioPlayer} for the public class.
 *
 * Notes:
 *
 *  - PCM mixing is a ~40-line pure-JavaScript routine operating on
 *    ``Buffer`` (see :func:`mixPcm` below). Clipping is done against the
 *    int16 range.
 *  - ``.ogg`` decoding is not done in this module. Node does not bundle a
 *    Vorbis decoder and shipping a native one would triple the SDK size.
 *    Instead, callers supply a :class:`RawPcmSource` (pre-decoded int16
 *    mono LE PCM at a known sample rate) OR a :class:`DecodedSource` via a
 *    user-supplied decoder. The Python SDK ships the bundled ``.ogg``
 *    clips and their decoder; the TS package exposes the raw files next to
 *    this module for users who wire up their own decoder.
 *
 * Attribution for the bundled audio clips themselves is preserved in
 * ``src/resources/audio/NOTICE``.
 */

import { promises as fs } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

import type { BackgroundAudioPlayer as IBackgroundAudioPlayer } from '../types';

// ---------------------------------------------------------------------------
// Builtin clip names (match Python ``BuiltinAudioClip``).
// ---------------------------------------------------------------------------

/** Names of the .ogg clips bundled with the SDK under ``resources/audio/``. */
export const BuiltinAudioClip = {
  CITY_AMBIENCE: 'city-ambience.ogg',
  FOREST_AMBIENCE: 'forest-ambience.ogg',
  OFFICE_AMBIENCE: 'office-ambience.ogg',
  CROWDED_ROOM: 'crowded-room.ogg',
  KEYBOARD_TYPING: 'keyboard-typing.ogg',
  KEYBOARD_TYPING2: 'keyboard-typing2.ogg',
  HOLD_MUSIC: 'hold_music.ogg',
} as const;

/** Filename of one of the bundled clips (e.g. ``"city-ambience.ogg"``). */
export type BuiltinAudioClipName = (typeof BuiltinAudioClip)[keyof typeof BuiltinAudioClip];

/** Resolve a bundled clip name to its absolute path on disk. */
export function builtinClipPath(clip: BuiltinAudioClipName): string {
  // Resolve relative to this compiled file's location so the path works in
  // both CJS (``__dirname``) and ESM builds.  ``import.meta.url`` exists in
  // ESM; CJS builds of this module fall back to ``__dirname``.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const meta: any = typeof import.meta !== 'undefined' ? import.meta : undefined;
  const here =
    meta?.url
      ? path.dirname(fileURLToPath(meta.url as string))
      : typeof __dirname !== 'undefined'
        ? __dirname
        : process.cwd();
  return path.resolve(here, '..', 'resources', 'audio', clip);
}

/** Raw int16 mono LE PCM already decoded into memory. */
export interface RawPcmSource {
  readonly kind: 'pcm';
  readonly pcm: Buffer;
  readonly sampleRate: number;
  readonly volume?: number;
  readonly probability?: number;
}

/** File on disk that a user-supplied decoder will turn into raw PCM. */
export interface FilePcmSource {
  readonly kind: 'file';
  readonly path: string;
  readonly decode: (p: string) => Promise<{ pcm: Buffer; sampleRate: number }>;
  readonly volume?: number;
  readonly probability?: number;
}

/** One of the bundled clips — requires a ``decode`` function at start() time. */
export interface BuiltinPcmSource {
  readonly kind: 'builtin';
  readonly clip: BuiltinAudioClipName;
  readonly decode: (p: string) => Promise<{ pcm: Buffer; sampleRate: number }>;
  readonly volume?: number;
  readonly probability?: number;
}

/** Tagged union of every input shape accepted by the player. */
export type AudioSource = RawPcmSource | FilePcmSource | BuiltinPcmSource;

/** A source plus optional probability weight + volume for list-style players. */
export interface AudioConfig {
  readonly source: AudioSource;
  /** Probability weight used when ``BackgroundAudioPlayer`` receives a list. */
  readonly probability?: number;
  /** Master volume [0, 1] applied on top of the per-source ``volume``. */
  readonly volume?: number;
}

/** Constructor options for {@link BackgroundAudioPlayer}. */
export interface BackgroundAudioOptions {
  /** Overall mix ratio [0, 1].  Defaults to 0.1 (typical hold-music ratio). */
  readonly volume?: number;
  /** When true the source restarts on exhaustion. */
  readonly loop?: boolean;
}

// ---------------------------------------------------------------------------
// Pure PCM helpers (equivalent of ``patter.services.pcm_mixer``).
// ---------------------------------------------------------------------------

const INT16_MIN = -32768;
const INT16_MAX = 32767;

/** Clip *v* into the int16 range without wraparound. */
function clipInt16(v: number): number {
  if (v < INT16_MIN) return INT16_MIN;
  if (v > INT16_MAX) return INT16_MAX;
  return v | 0;
}

/**
 * Return ``agent + bg * ratio`` as a new Buffer of the same length as
 * ``agent``.  Background is zero-padded or truncated to match.
 */
export function mixPcm(agent: Buffer, bg: Buffer, ratio: number): Buffer {
  if ((agent.length & 1) !== 0) {
    throw new Error(`agent PCM must be a whole number of 16-bit samples (${agent.length} bytes)`);
  }
  if ((bg.length & 1) !== 0) {
    throw new Error(`bg PCM must be a whole number of 16-bit samples (${bg.length} bytes)`);
  }
  if (agent.length === 0) return Buffer.alloc(0);
  if (bg.length === 0 || ratio === 0) return Buffer.from(agent);

  const n = agent.length >> 1;
  const bgSamples = bg.length >> 1;
  const out = Buffer.allocUnsafe(agent.length);

  for (let i = 0; i < n; i++) {
    const a = agent.readInt16LE(i * 2);
    const b = i < bgSamples ? bg.readInt16LE(i * 2) : 0;
    out.writeInt16LE(clipInt16(Math.round(a + b * ratio)), i * 2);
  }
  return out;
}

/**
 * Linear-interpolation resample from ``srcSr`` to ``dstSr``.  Input and
 * output are mono int16 LE PCM buffers.  Used for low-fidelity background
 * audio (hold music at attenuated volume); not suitable for wideband
 * program audio.
 */
export function resamplePcm(src: Buffer, srcSr: number, dstSr: number): Buffer {
  if (srcSr === dstSr) return Buffer.from(src);
  const srcSamples = src.length >> 1;
  if (srcSamples === 0) return Buffer.alloc(0);

  const ratio = dstSr / srcSr;
  const dstSamples = Math.floor(srcSamples * ratio);
  if (dstSamples <= 0) return Buffer.alloc(0);

  const out = Buffer.allocUnsafe(dstSamples * 2);
  for (let i = 0; i < dstSamples; i++) {
    // Corresponding position in source space.
    const pos = (i * (srcSamples - 1)) / (dstSamples - 1 || 1);
    const lo = Math.floor(pos);
    const hi = Math.min(lo + 1, srcSamples - 1);
    const frac = pos - lo;
    const a = src.readInt16LE(lo * 2);
    const b = src.readInt16LE(hi * 2);
    out.writeInt16LE(clipInt16(Math.round(a + (b - a) * frac)), i * 2);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Probability-weighted selection.
// ---------------------------------------------------------------------------

/** Probability-weighted random pick from a list of {@link AudioConfig}. */
export function selectSoundFromList(sounds: readonly AudioConfig[]): AudioConfig | null {
  const total = sounds.reduce((acc, s) => acc + (s.probability ?? 1), 0);
  if (total <= 0) return null;

  if (total < 1.0 && Math.random() > total) return null;

  const normalize = total <= 1.0 ? 1.0 : total;
  const r = Math.random() * Math.min(total, 1.0);
  let cumulative = 0;
  for (const sound of sounds) {
    const p = sound.probability ?? 1;
    if (p <= 0) continue;
    cumulative += p / normalize;
    if (r <= cumulative) return sound;
  }
  return sounds[sounds.length - 1] ?? null;
}

// ---------------------------------------------------------------------------
// BackgroundAudioPlayer
// ---------------------------------------------------------------------------

/**
 * Mix a background audio clip into an outbound PCM stream.
 *
 * Accepts a single :class:`AudioSource`, a single :class:`AudioConfig`, or a
 * list of :class:`AudioConfig` (in which case one is picked via
 * probability-weighted random selection).  Call ``start()`` before any
 * ``mix()`` and ``stop()`` to release decoded PCM.
 */
export class BackgroundAudioPlayer implements IBackgroundAudioPlayer {
  private readonly source: AudioSource | AudioConfig | readonly AudioConfig[];
  private readonly volume: number;
  private readonly loop: boolean;

  private started = false;
  private pcm: Buffer | null = null;
  private sourceSr = 0;
  private position = 0;
  private readonly resampleCache = new Map<number, Buffer>();

  constructor(
    source: AudioSource | AudioConfig | readonly AudioConfig[],
    opts: BackgroundAudioOptions = {},
  ) {
    const volume = opts.volume ?? 0.1;
    if (volume < 0 || volume > 1) {
      throw new Error(`volume must be in [0, 1], got ${volume}`);
    }
    this.source = source;
    this.volume = volume;
    this.loop = opts.loop ?? false;
  }

  /**
   * Decode the configured source and arm the mixer.  Subsequent calls are
   * no-ops while the player is active.
   */
  async start(): Promise<void> {
    if (this.started) return;

    const resolved = this.resolveSource();
    if (resolved === null) {
      // Silence variant picked — mark started but leave pcm null so mix()
      // becomes a no-op.
      this.started = true;
      this.pcm = Buffer.alloc(0);
      this.sourceSr = 16000;
      return;
    }

    const [source, sourceVolume] = resolved;
    const { pcm, sampleRate } = await this.decodeSource(source);
    this.pcm = sourceVolume !== 1 ? this.applyGain(pcm, sourceVolume) : pcm;
    this.sourceSr = sampleRate;
    this.position = 0;
    this.started = true;
  }

  /**
   * Return a mix of ``agentPcm`` with the next background chunk.  The result
   * is always exactly ``agentPcm.length`` bytes long.  Returns a copy of
   * ``agentPcm`` when the player is not started, when ``volume == 0``, or
   * when the source has been exhausted and ``loop`` is false.
   */
  async mix(agentPcm: Buffer, sampleRate: number): Promise<Buffer> {
    if (!this.started || this.pcm === null || this.pcm.length === 0) {
      return Buffer.from(agentPcm);
    }
    if (this.volume === 0) return Buffer.from(agentPcm);

    const needed = agentPcm.length >> 1;
    if (needed === 0) return Buffer.from(agentPcm);

    const bg = this.resampleTo(sampleRate);
    if (bg.length === 0) return Buffer.from(agentPcm);

    const bgSamples = bg.length >> 1;
    const chunk = Buffer.allocUnsafe(agentPcm.length);

    if (this.loop) {
      for (let i = 0; i < needed; i++) {
        const srcIdx = (this.position + i) % bgSamples;
        chunk.writeInt16LE(bg.readInt16LE(srcIdx * 2), i * 2);
      }
      this.position = (this.position + needed) % bgSamples;
    } else {
      const remaining = bgSamples - this.position;
      if (remaining <= 0) {
        // Exhausted.
        this.pcm = Buffer.alloc(0);
        return Buffer.from(agentPcm);
      }
      const take = Math.min(remaining, needed);
      for (let i = 0; i < take; i++) {
        chunk.writeInt16LE(bg.readInt16LE((this.position + i) * 2), i * 2);
      }
      if (take < needed) chunk.fill(0, take * 2);
      this.position += take;
      if (this.position >= bgSamples) this.pcm = Buffer.alloc(0);
    }

    return mixPcm(agentPcm, chunk, this.volume);
  }

  /** Release all cached PCM and reset the player. */
  async stop(): Promise<void> {
    this.started = false;
    this.pcm = null;
    this.position = 0;
    this.sourceSr = 0;
    this.resampleCache.clear();
  }

  // -------------------------------------------------------------------------
  // Internals
  // -------------------------------------------------------------------------

  private resolveSource(): [AudioSource, number] | null {
    if (Array.isArray(this.source)) {
      const picked = selectSoundFromList(this.source as readonly AudioConfig[]);
      if (picked === null) return null;
      return [picked.source, picked.volume ?? 1.0];
    }
    if (isAudioConfig(this.source)) {
      return [this.source.source, this.source.volume ?? 1.0];
    }
    return [this.source as AudioSource, 1.0];
  }

  private async decodeSource(
    source: AudioSource,
  ): Promise<{ pcm: Buffer; sampleRate: number }> {
    switch (source.kind) {
      case 'pcm':
        return { pcm: source.pcm, sampleRate: source.sampleRate };
      case 'file':
        return source.decode(source.path);
      case 'builtin': {
        const p = builtinClipPath(source.clip);
        // Sanity-check that the bundled file is actually an .ogg container.
        const header = await fs.readFile(p, { flag: 'r' }).then((buf) => buf.subarray(0, 4));
        if (header.toString('ascii') !== 'OggS') {
          throw new Error(`Bundled clip ${source.clip} is not a valid Ogg file`);
        }
        return source.decode(p);
      }
    }
  }

  private applyGain(pcm: Buffer, gain: number): Buffer {
    if (gain === 1) return pcm;
    const n = pcm.length >> 1;
    const out = Buffer.allocUnsafe(pcm.length);
    for (let i = 0; i < n; i++) {
      out.writeInt16LE(clipInt16(Math.round(pcm.readInt16LE(i * 2) * gain)), i * 2);
    }
    return out;
  }

  private resampleTo(dstSr: number): Buffer {
    if (this.pcm === null) return Buffer.alloc(0);
    if (dstSr === this.sourceSr) return this.pcm;
    const cached = this.resampleCache.get(dstSr);
    if (cached) return cached;
    const resampled = resamplePcm(this.pcm, this.sourceSr, dstSr);
    this.resampleCache.set(dstSr, resampled);
    return resampled;
  }
}

function isAudioConfig(value: unknown): value is AudioConfig {
  return (
    typeof value === 'object' &&
    value !== null &&
    'source' in value &&
    typeof (value as AudioConfig).source === 'object'
  );
}
