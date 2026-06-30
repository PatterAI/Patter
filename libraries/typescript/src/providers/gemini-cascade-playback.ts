/**
 * Gemini Cascade playback state machine.
 *
 * Manages the output audio pipeline for {@link GeminiCascadeAdapter}:
 * - IDLE       — no audio playing; waiting for barge-in or turn-end
 * - FILLER     — draining a pre-synthesised filler clip
 * - SPEAKING   — draining TTS audio frames
 * - INTERRUPTED — barge-in detected; dropping buffered frames
 *
 * All outbound audio (filler, TTS, silence padding) passes through ONE
 * timestamp-corrected ~20 ms drive loop. The loop compensates for clock
 * drift by emitting 0, 1, or 2 frames per tick (capped at 2 to bound jitter).
 *
 * Filler clips are pre-synthesised at engine start and cached as
 * 8 kHz mulaw 160-byte-frame arrays. The adapter owns clip synthesis;
 * this module only owns the playback state machine.
 */

import { getLogger } from '../logger';

/** One mulaw frame expected by the carrier playout scheduler: 20 ms @ 8 kHz. */
const MULAW_FRAME_BYTES = 160;
/** Drive loop tick interval in milliseconds. */
const TICK_MS = 20;
/** Maximum frames emitted per tick to bound burst jitter. */
const MAX_FRAMES_PER_TICK = 2;
/** Silence byte for mulaw (G.711 encoding of zero). */
const MULAW_SILENCE = 0xff;
/** ~40 ms silence after barge-in before resuming (2 frames). */
const POST_BARGE_IN_SILENCE_FRAMES = 2;

/** Playback state. */
export type PlaybackState = 'IDLE' | 'FILLER' | 'SPEAKING' | 'INTERRUPTED';

/** Callback invoked per 160-byte mulaw frame that should be sent to the carrier. */
export type FrameEmitter = (frame: Buffer) => void;

/** Options for the playback machine. */
export interface CascadePlaybackOptions {
  /** Called with each 160-byte mulaw frame to send to the carrier. */
  onFrame: FrameEmitter;
}

/**
 * Playback state machine for GeminiCascadeAdapter.
 *
 * Call {@link start} once after constructing; call {@link stop} at session close.
 * Feed TTS output via {@link enqueueTtsFrames}. Signal filler selection via
 * {@link startFiller}. Signal barge-in via {@link interrupt}.
 */
export class CascadePlaybackMachine {
  private state: PlaybackState = 'IDLE';
  /** Filler frames queued for draining (pre-synthesised clip). */
  private fillerQueue: Buffer[] = [];
  /** TTS frames queued for draining (from Gemini TTS leg). */
  private ttsQueue: Buffer[] = [];
  /** Whether TTS turn is complete (flush remaining + return to IDLE). */
  private ttsComplete = false;
  /** Silence frames to emit after barge-in before returning to IDLE. */
  private postBargeInSilenceRemaining = 0;
  /** Drive loop timer handle. */
  private ticker: ReturnType<typeof setInterval> | null = null;
  /** Timestamp of the previous tick for drift compensation. */
  private lastTickAt = 0;
  /** Accumulated fractional frame deficit from drift. */
  private frameDebt = 0;
  private readonly onFrame: FrameEmitter;

  constructor(opts: CascadePlaybackOptions) {
    this.onFrame = opts.onFrame;
  }

  /** Current playback state (read-only). */
  get currentState(): PlaybackState {
    return this.state;
  }

  /** Start the ~20 ms drive loop. Must be called once at session open. */
  start(): void {
    if (this.ticker !== null) return;
    this.lastTickAt = Date.now();
    this.frameDebt = 0;
    this.ticker = setInterval(() => this.tick(), TICK_MS);
  }

  /** Stop the drive loop and reset all state. Call at session close. */
  stop(): void {
    if (this.ticker !== null) {
      clearInterval(this.ticker);
      this.ticker = null;
    }
    this.state = 'IDLE';
    this.fillerQueue = [];
    this.ttsQueue = [];
    this.ttsComplete = false;
    this.postBargeInSilenceRemaining = 0;
    this.frameDebt = 0;
  }

  /**
   * Begin draining a filler clip.
   *
   * Only transitions if the machine is currently IDLE — otherwise the
   * caller already received audio (TTS in SPEAKING, or still FILLER).
   */
  startFiller(frames: Buffer[]): void {
    if (this.state !== 'IDLE') return;
    this.fillerQueue = [...frames];
    this.ttsQueue = [];
    this.ttsComplete = false;
    this.state = 'FILLER';
    getLogger().debug('Gemini Cascade playback: IDLE -> FILLER');
  }

  /**
   * Enqueue TTS frames for playback.
   *
   * If we are in FILLER state, transitions to SPEAKING at the next frame
   * boundary (the tick loop handles the crossfade — it finishes the current
   * 160-byte frame first before switching queues).
   */
  enqueueTtsFrames(frames: Buffer[], turnComplete: boolean): void {
    for (const f of frames) this.ttsQueue.push(f);
    if (turnComplete) this.ttsComplete = true;

    if (this.state === 'FILLER' && frames.length > 0) {
      // Drain filler in the tick loop; it will crossfade at next frame boundary.
    } else if (this.state === 'IDLE' && frames.length > 0) {
      this.state = 'SPEAKING';
      getLogger().debug('Gemini Cascade playback: IDLE -> SPEAKING');
    }
  }

  /**
   * Signal turn complete without new frames (flush tail of TTS turn).
   * If already SPEAKING this will drain remaining ttsQueue then go IDLE.
   */
  markTtsComplete(): void {
    this.ttsComplete = true;
    if (this.state === 'IDLE') {
      // Nothing to drain — already done.
    }
  }

  /**
   * Barge-in detected. Drop queued frames and emit ~40 ms silence,
   * then return to IDLE. Transitions from any state.
   */
  interrupt(): void {
    if (this.state === 'INTERRUPTED') return;
    getLogger().debug(`Gemini Cascade playback: ${this.state} -> INTERRUPTED`);
    this.state = 'INTERRUPTED';
    this.fillerQueue = [];
    this.ttsQueue = [];
    this.ttsComplete = false;
    this.postBargeInSilenceRemaining = POST_BARGE_IN_SILENCE_FRAMES;
  }

  /** Reset to IDLE without emitting silence (new turn started). */
  resetToIdle(): void {
    this.state = 'IDLE';
    this.fillerQueue = [];
    this.ttsQueue = [];
    this.ttsComplete = false;
    this.postBargeInSilenceRemaining = 0;
    getLogger().debug('Gemini Cascade playback: -> IDLE');
  }

  // ---------------------------------------------------------------------------
  // Drive loop
  // ---------------------------------------------------------------------------

  private tick(): void {
    const now = Date.now();
    const elapsed = now - this.lastTickAt;
    this.lastTickAt = now;

    // Compute how many frames we should emit this tick based on elapsed time.
    // frameDebt accumulates fractional frames across ticks to avoid drift.
    const idealFrames = elapsed / TICK_MS + this.frameDebt;
    const framesToEmit = Math.min(Math.round(idealFrames), MAX_FRAMES_PER_TICK);
    this.frameDebt = idealFrames - framesToEmit;

    for (let i = 0; i < framesToEmit; i++) {
      this.emitOneFrame();
    }
  }

  private emitOneFrame(): void {
    switch (this.state) {
      case 'IDLE':
        return; // Nothing to emit.

      case 'INTERRUPTED':
        if (this.postBargeInSilenceRemaining > 0) {
          this.onFrame(Buffer.alloc(MULAW_FRAME_BYTES, MULAW_SILENCE));
          this.postBargeInSilenceRemaining--;
        } else {
          this.state = 'IDLE';
          getLogger().debug('Gemini Cascade playback: INTERRUPTED -> IDLE');
        }
        return;

      case 'FILLER': {
        // If TTS frames are available, crossfade at current frame boundary
        // by switching to SPEAKING before draining the next filler frame.
        if (this.ttsQueue.length > 0) {
          this.state = 'SPEAKING';
          getLogger().debug('Gemini Cascade playback: FILLER -> SPEAKING (TTS ready)');
          this.emitOneTtsFrame();
          return;
        }
        if (this.fillerQueue.length > 0) {
          this.onFrame(this.fillerQueue.shift()!);
          return;
        }
        // Filler exhausted, TTS not yet ready — emit silence (never loop filler).
        this.onFrame(Buffer.alloc(MULAW_FRAME_BYTES, MULAW_SILENCE));
        return;
      }

      case 'SPEAKING':
        this.emitOneTtsFrame();
        return;
    }
  }

  private emitOneTtsFrame(): void {
    if (this.ttsQueue.length > 0) {
      this.onFrame(this.ttsQueue.shift()!);
      return;
    }
    if (this.ttsComplete) {
      this.state = 'IDLE';
      this.ttsComplete = false;
      getLogger().debug('Gemini Cascade playback: SPEAKING -> IDLE (TTS turn complete)');
      return;
    }
    // TTS stalled mid-turn — emit silence to maintain timing.
    this.onFrame(Buffer.alloc(MULAW_FRAME_BYTES, MULAW_SILENCE));
  }
}

// ---------------------------------------------------------------------------
// Chunked TTS text splitter
// ---------------------------------------------------------------------------

/** Abbreviations that contain a period but should NOT trigger a split. */
const ABBREV_BLOCKLIST = new Set([
  'dr.', 'mr.', 'mrs.', 'ms.', 'vs.', 'approx.', 'est.',
]);

/**
 * Split accumulated text into TTS chunks.
 *
 * Priority order:
 * 1. Sentence boundary `/[.!?]\s/` (guards: digit-before, abbrev, lowercase-after)
 * 2. Clause boundary `/[,;:]\s+(?=[A-Z])/`
 * 3. Conjunction boundary (only if buffer >= 60 chars)
 * 4. Length fallback: split before position 120
 *
 * Min chunk: 15 chars / 3 words (unless `flush` is true).
 *
 * @returns [chunk, remainder] or [null, text] if no split point found yet.
 */
export function splitTtsChunk(
  text: string,
  flush: boolean,
): [string | null, string] {
  if (flush && text.trim().length > 0) return [text, ''];

  const minLen = 15;
  const wordCount = text.trim().split(/\s+/).length;
  if (text.length < minLen || wordCount < 3) return [null, text];

  // 1. Sentence boundary
  const sentMatch = findSentenceBoundary(text);
  if (sentMatch !== -1) {
    return [text.slice(0, sentMatch), text.slice(sentMatch)];
  }

  // 2. Clause boundary: [,;:] followed by whitespace then uppercase
  const clauseRe = /[,;:]\s+(?=[A-Z])/g;
  let clauseMatch: RegExpExecArray | null;
  while ((clauseMatch = clauseRe.exec(text)) !== null) {
    const pos = clauseMatch.index + 1; // after the punctuation
    if (pos >= minLen) return [text.slice(0, pos), text.slice(pos)];
  }

  // 3. Conjunction boundary (only if long enough)
  if (text.length >= 60) {
    const conjRe = /\s+(and|but|so|because|however|therefore|although)\s+/gi;
    let conjMatch: RegExpExecArray | null;
    while ((conjMatch = conjRe.exec(text)) !== null) {
      const pos = conjMatch.index;
      if (pos >= minLen) return [text.slice(0, pos), text.slice(pos)];
    }
  }

  // 4. Length fallback: split before position 120
  if (text.length >= 120) {
    const splitPos = text.lastIndexOf(' ', 119);
    if (splitPos > minLen) return [text.slice(0, splitPos), text.slice(splitPos + 1)];
    return [text.slice(0, 120), text.slice(120)];
  }

  return [null, text];
}

/** Find the first valid sentence boundary index in `text`. Returns -1 if none. */
function findSentenceBoundary(text: string): number {
  const sentRe = /[.!?]\s/g;
  let m: RegExpExecArray | null;
  while ((m = sentRe.exec(text)) !== null) {
    const pos = m.index;
    const ch = text[pos];
    // Guard: digit before period (e.g. "3.5 times")
    if (ch === '.' && pos > 0 && /\d/.test(text[pos - 1])) continue;
    // Guard: lowercase after period (abbreviation mid-sentence)
    if (ch === '.' && pos + 2 < text.length && /[a-z]/.test(text[pos + 2])) continue;
    // Guard: known abbreviation blocklist
    if (ch === '.') {
      const before = text.slice(Math.max(0, pos - 5), pos + 1).toLowerCase();
      if (ABBREV_BLOCKLIST.has(before.split(/\s+/).pop() ?? '')) continue;
    }
    const splitAt = pos + 1; // include the punctuation, split before the space
    if (splitAt >= 15) return splitAt;
  }
  return -1;
}

// ---------------------------------------------------------------------------
// Filler selection
// ---------------------------------------------------------------------------

/** Names of the pre-synthesised filler clips. */
export type FillerKey =
  | 'mhm'
  | 'got_it'
  | 'sure_one_sec'
  | 'let_me_check'
  | 'let_me_think'
  | 'okay_moment';

/** The round-robin cursor is module-level state (simple, not shared across calls). */
let _rrCursor = 0;
const RR_FILLERS: FillerKey[] = ['sure_one_sec', 'got_it'];

/**
 * Select a filler clip key based on the last ~30 tokens of input text.
 *
 * Rules (in priority order):
 * 1. Question word at any position → 'let_me_check'
 * 2. >60 chars or 2+ clause boundaries → 'let_me_think'
 * 3. Urgency token → 'okay_moment'
 * 4. Closure token → 'got_it'
 * 5. <15 chars → 'mhm'
 * 6. Default: round-robin 'sure_one_sec' / 'got_it'
 */
export function selectFiller(recentInputText: string): FillerKey {
  const text = recentInputText.toLowerCase().trim();
  if (text.length === 0) return 'mhm';

  const QUESTION_RE = /\b(what|where|when|who|how|why|which|can you|could you|do you|is there|are there)\b/;
  if (QUESTION_RE.test(text)) return 'let_me_check';

  const clauseBoundaries = (text.match(/[.!?,;:]/g) ?? []).length;
  if (text.length > 60 || clauseBoundaries >= 2) return 'let_me_think';

  const URGENCY_RE = /\b(urgent|asap|right away|immediately|emergency|as soon as)\b/;
  if (URGENCY_RE.test(text)) return 'okay_moment';

  const CLOSURE_RE = /\b(thanks|thank you|got it|understood|perfect|great|sounds good|okay|alright)\b/;
  if (CLOSURE_RE.test(text)) return 'got_it';

  if (text.length < 15) return 'mhm';

  // Round-robin fallback
  const chosen = RR_FILLERS[_rrCursor % RR_FILLERS.length];
  _rrCursor++;
  return chosen;
}
