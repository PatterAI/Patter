/**
 * Carrier-neutral local call recording — interleaved stereo WAV on disk.
 *
 * `LocalCallRecorder` taps the audio that already flows through the SDK's
 * per-call `StreamHandler` and writes a synchronized two-channel recording:
 *
 * - **left channel  = caller** (inbound, post mulaw→PCM16 decode)
 * - **right channel = agent**  (outbound TTS / Realtime / ConvAI audio)
 *
 * Unlike the carrier-side `recording: true` option (Twilio Recordings API,
 * Telnyx `record_start`, Plivo Record API) this works on ANY carrier because
 * nothing leaves the process: the recorder is fed at the transport tap points
 * and appends PCM to `recording.wav` incrementally. Enable via
 * `Patter.serve({ localRecording: true })` (or a directory string). Parity
 * with Python `getpatter/audio/call_recorder.py`.
 *
 * ## Format
 *
 * 16-bit little-endian PCM, 16 kHz, 2 channels. A placeholder RIFF header is
 * written at open with zero sizes; {@link LocalCallRecorder.close} patches
 * the `RIFF` and `data` chunk sizes so the file is parseable by any WAV
 * reader. The handler teardown paths (`handleStop` / `handleWsClose` →
 * `fireCallEnd`) call `close()`, so a truncated call still yields a valid
 * file.
 *
 * ## Time alignment (documented approach)
 *
 * The recorder is **caller-clocked**. On PSTN the inbound media stream is a
 * continuous realtime sequence of ~20 ms frames (silence included), so the
 * caller channel is used as the wall clock:
 *
 * - Each caller chunk advances the file by exactly its own duration. The
 *   right channel for that span is popped from a bounded agent FIFO and
 *   zero-padded (silence) when the agent has nothing buffered — i.e. the
 *   lagging channel is padded with silence whenever the other channel writes.
 * - Agent audio is appended to the FIFO on arrival and drained at the caller
 *   clock rate. TTS providers push audio faster than realtime (multi-second
 *   bursts); draining at the caller rate mirrors the carrier's own playout
 *   buffer, so agent audio lands on the timeline roughly where the caller
 *   actually heard it (frame-granularity ≈ the inbound chunk size, ~20 ms).
 *
 * Alignment is therefore duration-based, not wall-clock-based: cross-channel
 * skew is bounded by the carrier playout backlog.
 *
 * ## Memory profile (allocation-light, no per-frame disk I/O)
 *
 * Interleaved frames accumulate in a 64 KiB write buffer (~1 s of stereo
 * audio) that is flushed with a single `writeSync` when full and on
 * `close()` — the 20 ms hot path never issues per-frame disk writes.
 * Nothing accumulates for the call duration. Internal state is bounded: at
 * most one odd carry byte on each PCM path, a fixed-state resampler per
 * channel, the bounded write buffer, and an agent FIFO capped at
 * {@link AGENT_BACKLOG_CAP_S} seconds (~1.9 MB) whose overflow is
 * force-flushed against left-channel silence.
 *
 * Any I/O error disables the recorder for the rest of the call (logged
 * once) — recording must never take down a live phone call.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import { getLogger } from '../logger';
import { mulawToPcm16, PcmCarry, StatefulResampler } from './transcoding';

/** Output sample rate (both channels) — the SDK's internal PCM16 rate. */
export const RECORDING_SAMPLE_RATE = 16000;

/**
 * Maximum buffered agent audio (seconds) awaiting caller-clock drain. The
 * agent FIFO mirrors the carrier playout buffer; 60 s covers even very long
 * single-turn monologues. Overflow is force-flushed (silence on the caller
 * channel), bounding memory at ~1.9 MB per recorded call.
 */
export const AGENT_BACKLOG_CAP_S = 60;

const BYTES_PER_SAMPLE = 2;
const CHANNELS = 2;

/**
 * Userspace write-buffer size. A 20 ms stereo frame is 1 280 bytes, so
 * 64 KiB batches ~1 s of audio per `writeSync` — the hot path never issues
 * per-frame disk I/O. Mirrors the Python recorder's `buffering=` setting.
 */
const WRITE_BUFFER_BYTES = 64 * 1024;

/** Wire encodings the taps may feed; everything is decoded to PCM16 16 kHz. */
export type RecorderEncoding = 'pcm16_16k' | 'mulaw_8k' | 'pcm16_8k' | 'pcm16_24k';

const ENCODINGS: readonly string[] = ['pcm16_16k', 'mulaw_8k', 'pcm16_8k', 'pcm16_24k'];

function buildWavHeader(dataSize: number): Buffer {
  const header = Buffer.alloc(44);
  header.write('RIFF', 0, 'ascii');
  header.writeUInt32LE(36 + dataSize, 4);
  header.write('WAVE', 8, 'ascii');
  header.write('fmt ', 12, 'ascii');
  header.writeUInt32LE(16, 16); // PCM fmt chunk size
  header.writeUInt16LE(1, 20); // audio format: PCM
  header.writeUInt16LE(CHANNELS, 22);
  header.writeUInt32LE(RECORDING_SAMPLE_RATE, 24);
  header.writeUInt32LE(RECORDING_SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE, 28);
  header.writeUInt16LE(CHANNELS * BYTES_PER_SAMPLE, 32);
  header.writeUInt16LE(BYTES_PER_SAMPLE * 8, 34);
  header.write('data', 36, 'ascii');
  header.writeUInt32LE(dataSize, 40);
  return header;
}

/** Interleave two equal-length mono PCM16 buffers into stereo frames. */
function interleave(left: Buffer, right: Buffer): Buffer {
  const out = Buffer.alloc(left.length + right.length);
  const samples = left.length / BYTES_PER_SAMPLE;
  for (let i = 0; i < samples; i++) {
    const inOff = i * 2;
    const outOff = i * 4;
    out[outOff] = left[inOff];
    out[outOff + 1] = left[inOff + 1];
    out[outOff + 2] = right[inOff];
    out[outOff + 3] = right[inOff + 1];
  }
  return out;
}

/**
 * Per-channel decode pipeline: any supported encoding → PCM16 @ 16 kHz.
 * Owns the channel's stateful resampler (filter state preserved across
 * chunks) and an odd-byte carry for the pass-through path.
 */
class ChannelDecoder {
  private encoding: RecorderEncoding | null = null;
  private resampler: StatefulResampler | null = null;
  private carry: PcmCarry | null = null;

  decode(data: Buffer, encoding: RecorderEncoding): Buffer {
    if (!ENCODINGS.includes(encoding)) {
      // Mirrors the Python recorder's explicit ValueError — the caller's
      // try/catch turns this into a one-shot "recorder disabled" warning.
      throw new Error(
        `Unsupported recording encoding ${JSON.stringify(encoding)}; expected one of ${ENCODINGS.join(', ')}`,
      );
    }
    if (this.encoding !== encoding) this.init(encoding);
    if (encoding === 'pcm16_16k') {
      return this.carry!.push(data);
    }
    if (encoding === 'mulaw_8k') {
      return this.resampler!.process(mulawToPcm16(data));
    }
    // pcm16_8k / pcm16_24k — StatefulResampler aligns odd bytes itself.
    return this.resampler!.process(data);
  }

  private init(encoding: RecorderEncoding): void {
    this.encoding = encoding;
    this.carry = new PcmCarry();
    this.resampler = null;
    if (encoding === 'mulaw_8k' || encoding === 'pcm16_8k') {
      this.resampler = new StatefulResampler({ srcRate: 8000, dstRate: RECORDING_SAMPLE_RATE });
    } else if (encoding === 'pcm16_24k') {
      this.resampler = new StatefulResampler({ srcRate: 24000, dstRate: RECORDING_SAMPLE_RATE });
    }
  }
}

/**
 * Incremental stereo WAV writer for one call (left=caller, right=agent).
 *
 * The file is opened (and the placeholder header written) eagerly so
 * permission errors surface at call start, not mid-call. All `add*` methods
 * are synchronous and cheap (one decode + one buffered append per ~20 ms
 * frame; disk writes happen in 64 KiB batches); errors never propagate —
 * the recorder disables itself and logs a single warning.
 */
export class LocalCallRecorder {
  private readonly filePath: string;
  private fd: number;
  private dataBytes = 0;
  private isClosed = false;
  private broken = false;
  private readonly callerDecoder = new ChannelDecoder();
  private readonly agentDecoder = new ChannelDecoder();
  /** Bounded FIFO of decoded agent PCM16@16k awaiting caller-clock drain. */
  private agentBacklog: Buffer[] = [];
  private agentBacklogBytes = 0;
  /** Test hook: shrink to exercise the overflow path without 60 s of audio. */
  maxBacklogBytes = AGENT_BACKLOG_CAP_S * RECORDING_SAMPLE_RATE * BYTES_PER_SAMPLE;
  private callerSampleCount = 0;
  private agentSampleCount = 0;
  /** Interleaved stereo bytes awaiting the next batched disk write. */
  private pendingChunks: Buffer[] = [];
  private pendingBytes = 0;
  /** Stereo data bytes already flushed to disk (file offset = 44 + this). */
  private flushedBytes = 0;

  constructor(filePath: string) {
    this.filePath = filePath;
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    this.fd = fs.openSync(filePath, 'w');
    // Placeholder header (zero sizes) — patched by close().
    fs.writeSync(this.fd, buildWavHeader(0), 0, 44, 0);
  }

  /** Target WAV path. */
  get path(): string {
    return this.filePath;
  }

  /** True once `close()` has run (or the recorder broke). */
  get closed(): boolean {
    return this.isClosed;
  }

  /**
   * Stereo frames appended so far (the 64 KiB write buffer flushes roughly
   * once per second of audio and always on `close()`).
   */
  get framesWritten(): number {
    return this.dataBytes / (CHANNELS * BYTES_PER_SAMPLE);
  }

  /** Real caller samples ingested (excludes padding). */
  get callerSamples(): number {
    return this.callerSampleCount;
  }

  /** Real agent samples ingested (excludes padding). */
  get agentSamples(): number {
    return this.agentSampleCount;
  }

  /** Recorded duration so far (committed stereo frames / 16 kHz). */
  get durationSeconds(): number {
    return this.framesWritten / RECORDING_SAMPLE_RATE;
  }

  /**
   * Record a caller-side chunk; advances the file by its duration. The
   * right channel for the span is drained from the agent FIFO and
   * zero-padded (silence) when the agent is not speaking.
   */
  addCallerAudio(data: Buffer, encoding: RecorderEncoding = 'pcm16_16k'): void {
    if (this.isClosed || this.broken || data.length === 0) return;
    try {
      const pcm = this.callerDecoder.decode(data, encoding);
      if (pcm.length === 0) return;
      this.callerSampleCount += pcm.length / BYTES_PER_SAMPLE;
      const right = this.popAgentBacklog(pcm.length);
      this.writeFrames(pcm, right);
    } catch (err) {
      this.markBroken(err);
    }
  }

  /** Buffer an agent-side chunk; it drains at the caller clock rate. */
  addAgentAudio(data: Buffer, encoding: RecorderEncoding = 'pcm16_16k'): void {
    if (this.isClosed || this.broken || data.length === 0) return;
    try {
      const pcm = this.agentDecoder.decode(data, encoding);
      if (pcm.length === 0) return;
      this.agentSampleCount += pcm.length / BYTES_PER_SAMPLE;
      this.agentBacklog.push(pcm);
      this.agentBacklogBytes += pcm.length;
      let overflow = this.agentBacklogBytes - this.maxBacklogBytes;
      if (overflow > 0) {
        // Bound memory: commit the oldest excess now, paired with silence
        // on the caller channel (degrades alignment only in the
        // pathological >60 s-monologue case).
        if (overflow % BYTES_PER_SAMPLE !== 0) overflow += 1;
        const excess = this.popAgentBacklog(overflow);
        this.writeFrames(Buffer.alloc(excess.length), excess);
      }
    } catch (err) {
      this.markBroken(err);
    }
  }

  /**
   * Drain the agent FIFO, patch the WAV header, and close the file.
   *
   * Idempotent and exception-safe — callable from every teardown path
   * (carrier `stop`, abnormal WS close, `fireCallEnd`). Returns the
   * recording path when a playable file exists, else `null`.
   */
  close(): string | null {
    if (this.isClosed) return this.broken ? null : this.filePath;
    this.isClosed = true;
    if (this.broken) {
      // The recorder already tore itself down mid-call (markBroken closed
      // the file descriptor) — nothing left to finalize.
      return null;
    }
    try {
      if (this.agentBacklogBytes > 0) {
        // Remaining agent audio never had caller frames to clock it out
        // (e.g. hangup mid-monologue) — commit it against silence.
        const tail = this.popAgentBacklog(this.agentBacklogBytes);
        this.writeFrames(Buffer.alloc(tail.length), tail, true);
      }
      this.flushPending();
      const sizes = Buffer.alloc(4);
      sizes.writeUInt32LE(36 + this.dataBytes, 0);
      fs.writeSync(this.fd, sizes, 0, 4, 4);
      sizes.writeUInt32LE(this.dataBytes, 0);
      fs.writeSync(this.fd, sizes, 0, 4, 40);
    } catch (err) {
      getLogger().warn(`Local recording finalize failed (${this.filePath}): ${String(err)}`);
      this.broken = true;
    } finally {
      try {
        fs.closeSync(this.fd);
      } catch {
        /* already closing */
      }
    }
    if (this.broken) return null;
    getLogger().info(
      `Local recording saved: ${this.filePath} (${this.durationSeconds.toFixed(1)}s)`,
    );
    return this.filePath;
  }

  /** Pop up to `numBytes` from the agent FIFO, zero-padded to size. */
  private popAgentBacklog(numBytes: number): Buffer {
    if (this.agentBacklogBytes === 0) return Buffer.alloc(numBytes);
    const out = Buffer.alloc(numBytes); // zero-filled → implicit padding
    let copied = 0;
    while (copied < numBytes && this.agentBacklog.length > 0) {
      const head = this.agentBacklog[0];
      const take = Math.min(head.length, numBytes - copied);
      head.copy(out, copied, 0, take);
      copied += take;
      this.agentBacklogBytes -= take;
      if (take === head.length) {
        this.agentBacklog.shift();
      } else {
        this.agentBacklog[0] = head.subarray(take);
      }
    }
    return out;
  }

  /**
   * Interleave equal-length mono buffers and append to the write buffer.
   * The buffer is flushed to disk in 64 KiB batches (and on `close()`),
   * so the per-frame cost is one `Buffer.alloc` + array push — no syscall.
   */
  private writeFrames(left: Buffer, right: Buffer, force = false): void {
    if ((this.isClosed && !force) || this.broken || left.length === 0) return;
    const stereo = interleave(left, right);
    this.pendingChunks.push(stereo);
    this.pendingBytes += stereo.length;
    this.dataBytes += stereo.length;
    if (this.pendingBytes >= WRITE_BUFFER_BYTES) {
      this.flushPending();
    }
  }

  /** Write all buffered stereo bytes in one `writeSync` call. */
  private flushPending(): void {
    if (this.pendingBytes === 0) return;
    const batch =
      this.pendingChunks.length === 1
        ? this.pendingChunks[0]
        : Buffer.concat(this.pendingChunks, this.pendingBytes);
    this.pendingChunks = [];
    this.pendingBytes = 0;
    fs.writeSync(this.fd, batch, 0, batch.length, 44 + this.flushedBytes);
    this.flushedBytes += batch.length;
  }

  private markBroken(err: unknown): void {
    if (this.broken) return;
    this.broken = true;
    // Drop anything still buffered — the file is no longer being finalized.
    this.pendingChunks = [];
    this.pendingBytes = 0;
    getLogger().warn(
      `Local recording disabled for the rest of the call (${this.filePath}): ${String(err)}`,
    );
    try {
      fs.closeSync(this.fd);
    } catch {
      /* best effort */
    }
  }
}
