/**
 * Wall-clock outbound frame pacer for the carrier media socket.
 *
 * The outbound path is otherwise event-driven: audio is written to the carrier
 * WebSocket the instant TTS/realtime produces it, so a stream arrives in bursts
 * and, after a pause, the buffered backlog is flushed as fast as the socket
 * accepts it. This module provides the pieces to pace it on a fixed 20 ms grid:
 *
 *  - `mulawSilenceFrame` / `pcm16SilenceFrame` — one precomputed silence frame
 *    per codec, so an empty queue never stalls the clock and the first tick pays
 *    no encode cost (no cold-start latency).
 *  - `OutboundFramePacer` — accumulates arbitrary enqueued audio, re-slices it
 *    into exact 20 ms frames, and runs a drift-corrected wall-clock loop that
 *    emits a real frame when one is queued and the silence frame when it is not.
 *
 * The loop timing derives from a monotonic clock deadline that advances by a
 * fixed frame period every tick — never from the duration of the work — so
 * scheduler jitter cannot accumulate. After an oversleep it emits a *bounded*
 * number of catch-up frames rather than an unbounded burst.
 *
 * Frame sizing is codec-relative: mu-law 8 kHz is 8 bytes/ms (160 B/frame),
 * PCM16 16 kHz is 32 bytes/ms (640 B/frame). The caller picks `frameBytes` and
 * the matching `silenceFrame` for the format it enqueues.
 */

// Standard RTP/G.711 telephony framing: 20 ms per packet (RFC 3551) — the
// latency-vs-header-overhead sweet spot every carrier already expects.
export const FRAME_MS = 20;

// mu-law encodes digital zero as 0xFF; 8 kHz * 20 ms = 160 samples * 1 byte.
const MULAW_SILENCE_FRAME = Buffer.alloc(160, 0xff);

/** One 20 ms frame of mu-law (G.711 PCMU) silence — 160 bytes of `0xFF`. */
export function mulawSilenceFrame(): Buffer {
  return MULAW_SILENCE_FRAME;
}

/** One 20 ms frame of PCM16-LE silence (all zero bytes) at `sampleRate`. */
export function pcm16SilenceFrame(sampleRate: number): Buffer {
  return Buffer.alloc((sampleRate * 2 * FRAME_MS) / 1000, 0);
}

export interface OutboundFramePacerOptions {
  frameBytes: number;
  silenceFrame: Buffer;
  /** Async callback invoked with each paced frame. */
  sendFrame?: (frame: Buffer) => Promise<void>;
  /** Monotonic clock in seconds (default `performance.now() / 1000`). */
  clock?: () => number;
  /** Sleep for `seconds` (default `setTimeout`). Injectable for tests. */
  sleep?: (seconds: number) => Promise<void>;
  /** Max frames emitted back-to-back to catch up after an oversleep. */
  maxCatchupFrames?: number;
}

/**
 * Re-frames enqueued audio into fixed-size frames and paces them onto a
 * wall-clock grid, emitting the silence frame whenever the queue runs dry.
 * Mirrors Python `OutboundFramePacer`.
 */
export class OutboundFramePacer {
  private readonly frameBytes: number;
  private readonly silence: Buffer;
  private readonly sendFrame?: (frame: Buffer) => Promise<void>;
  private readonly clock: () => number;
  private readonly sleep: (seconds: number) => Promise<void>;
  private readonly maxCatchupFrames: number;
  private readonly framePeriodS = FRAME_MS / 1000;
  private buf: Buffer = Buffer.alloc(0);
  private running = false;

  constructor(opts: OutboundFramePacerOptions) {
    if (opts.frameBytes <= 0) {
      throw new Error('frameBytes must be positive');
    }
    this.frameBytes = opts.frameBytes;
    this.silence = opts.silenceFrame;
    this.sendFrame = opts.sendFrame;
    this.clock = opts.clock ?? (() => performance.now() / 1000);
    this.sleep =
      opts.sleep ?? ((s: number) => new Promise((r) => setTimeout(r, Math.max(0, s) * 1000)));
    this.maxCatchupFrames = Math.max(1, opts.maxCatchupFrames ?? 5);
  }

  /** Bytes buffered but not yet framed out. */
  get pendingBytes(): number {
    return this.buf.length;
  }

  /** Append audio to the outbound buffer (any length; re-framed on tick). */
  enqueue(data: Buffer): void {
    if (data.length > 0) {
      this.buf = this.buf.length === 0 ? Buffer.from(data) : Buffer.concat([this.buf, data]);
    }
  }

  /**
   * Return the next frame: a full real frame when one is buffered, the
   * remaining partial buffer padded to a full frame with the silence tail when
   * only a sub-frame remains, or the silence frame when empty. This is the
   * `AudioSampleProvider.NextSample()` contract — always exactly one frame, so
   * the write loop never stalls.
   */
  nextFrame(): Buffer {
    const n = this.buf.length;
    if (n === 0) {
      return this.silence;
    }
    if (n >= this.frameBytes) {
      const frame = this.buf.subarray(0, this.frameBytes);
      this.buf = this.buf.subarray(this.frameBytes);
      return frame;
    }
    // Sub-frame remainder: pad up to a whole frame with the silence tail so
    // framing stays aligned, and drain the buffer.
    const frame = Buffer.concat([this.buf, this.silence.subarray(n)]);
    this.buf = Buffer.alloc(0);
    return frame;
  }

  /** Signal `run` to exit after the current tick. */
  stop(): void {
    this.running = false;
  }

  /**
   * Drive the pace loop until `stop` (or `maxFrames` in tests). Deadline-based:
   * `nextDeadline` advances by exactly one frame period per tick, independent of
   * send duration, so drift cannot accumulate. After an oversleep, up to
   * `maxCatchupFrames` frames are emitted back-to-back; being further behind
   * re-anchors the deadline to now instead of bursting the whole backlog.
   */
  async run(opts?: { maxFrames?: number }): Promise<void> {
    const maxFrames = opts?.maxFrames;
    this.running = true;
    let nextDeadline = this.clock();
    let count = 0;
    try {
      while (this.running && (maxFrames === undefined || count < maxFrames)) {
        const frame = this.nextFrame();
        if (this.sendFrame) {
          await this.sendFrame(frame);
        }
        count += 1;
        nextDeadline += this.framePeriodS;
        const delay = nextDeadline - this.clock();
        if (delay > 0) {
          await this.sleep(delay);
          continue;
        }
        const framesBehind = Math.floor(-delay / this.framePeriodS);
        if (framesBehind > this.maxCatchupFrames) {
          nextDeadline = this.clock();
        }
      }
    } finally {
      this.running = false;
    }
  }
}
