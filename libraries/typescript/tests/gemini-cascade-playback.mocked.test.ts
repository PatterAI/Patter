/**
 * [mocked] GeminiCascade — playback state machine, TTS splitter, filler selection.
 *
 * Mocked boundary: ONLY @google/genai (TTS + Live SDK). Everything inward is
 * real: CascadePlaybackMachine, splitTtsChunk, selectFiller, and the
 * GeminiCascadeAdapter message-handling path.
 *
 * Coverage:
 *  1. splitTtsChunk — sentence/clause/conjunction/length splits + guards
 *     (digit-before, abbreviation, 'Dr. Smith', '$3.5 million')
 *  2. CascadePlaybackMachine state transitions
 *     IDLE -> FILLER -> SPEAKING, SPEAKING/FILLER -> INTERRUPTED -> IDLE,
 *     filler-exhaust -> silence (no filler loop), IDLE -> SPEAKING direct
 *  3. selectFiller — question / long / urgency / closure / short / round-robin
 *  4. GeminiCascadeAdapter: turns dropped before setupComplete (regression)
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  CascadePlaybackMachine,
  splitTtsChunk,
  selectFiller,
} from '../src/providers/gemini-cascade-playback.js';
import type { CascadePlaybackOptions } from '../src/providers/gemini-cascade-playback.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a fresh machine with a captured frame list. */
function makeMachine(opts?: Partial<CascadePlaybackOptions>): {
  machine: CascadePlaybackMachine;
  frames: Buffer[];
} {
  const frames: Buffer[] = [];
  const machine = new CascadePlaybackMachine({
    onFrame: (f) => frames.push(f),
    ...opts,
  });
  return { machine, frames };
}

/** A single 160-byte mulaw frame. */
function frame(byte = 0x00): Buffer {
  return Buffer.alloc(160, byte);
}

/** Drive the machine's private tick() directly N times without starting the timer. */
function tickN(machine: CascadePlaybackMachine, n: number): void {
  const tick = (machine as unknown as { tick: () => void }).tick.bind(machine);
  // Override timing fields so every tick sees exactly 20 ms elapsed (1 frame ideal).
  const m = machine as unknown as { lastTickAt: number; frameDebt: number };
  for (let i = 0; i < n; i++) {
    m.lastTickAt = Date.now() - 20;
    tick();
  }
}

// ---------------------------------------------------------------------------
// 1. splitTtsChunk
// ---------------------------------------------------------------------------

describe('[unit] splitTtsChunk — sentence boundary', () => {
  it('splits on sentence-ending period followed by space', () => {
    // splitAt must be >= 15: "This is done." is 13 chars (splitAt=13 < 15),
    // so we need the split position to land at or after index 15.
    // "We are finished here." = 21 chars, splitAt = 21 >= 15.
    const text = 'We are finished here. Now we proceed to next step.';
    const [chunk, rest] = splitTtsChunk(text, false);
    expect(chunk).toBe('We are finished here.');
    expect(rest).toBe(' Now we proceed to next step.');
  });

  it('splits on exclamation mark followed by space', () => {
    // "This is done now!" = 17 chars, splitAt = 17 >= 15.
    const text = 'This is done now! We can move on to the next task.';
    const [chunk, rest] = splitTtsChunk(text, false);
    expect(chunk).toBe('This is done now!');
    expect(rest).toBe(' We can move on to the next task.');
  });

  it('splits on question mark followed by space', () => {
    // "Are you available?" = 18 chars, splitAt = 18 >= 15.
    const text = 'Are you available? Let me know if you need help.';
    const [chunk, rest] = splitTtsChunk(text, false);
    expect(chunk).toBe('Are you available?');
    expect(rest).toBe(' Let me know if you need help.');
  });

  it('guards: does NOT split on digit-before period ($3.5 million)', () => {
    // "$3.5 million" — the digit before the period means it is a decimal, not EOS.
    const text = 'The deal was worth $3.5 million dollars and more cash.';
    const [chunk] = splitTtsChunk(text, false);
    // No sentence boundary before position 15 so we fall through to clause/conjunction
    // or length. Crucially, the split must NOT occur at "3.5 ".
    if (chunk !== null) {
      expect(chunk).not.toMatch(/\$3\.5$/);
    }
  });

  it('guards: does NOT split on "Dr. Smith" (abbreviation)', () => {
    const text = 'Please call Dr. Smith tomorrow morning about the results.';
    const [chunk] = splitTtsChunk(text, false);
    // If the abbreviation guard fires correctly, no split at "Dr."
    if (chunk !== null) {
      expect(chunk).not.toBe('Please call Dr.');
    }
  });

  it('guards: does NOT split when next char after period is lowercase (mid-sentence)', () => {
    // "e.g. we found" — lowercase 'w' after the period means mid-sentence.
    const text = 'This is useful e.g. we found three issues that need fixing today.';
    const [chunk] = splitTtsChunk(text, false);
    // Should not split at "e.g." because next char is lowercase 'w'.
    if (chunk !== null) {
      expect(chunk).not.toMatch(/e\.g\.$/);
    }
  });

  it('guards: known abbreviation vs. in the blocklist is not split', () => {
    const text = 'We tested this vs. the baseline and got better results overall.';
    const [chunk] = splitTtsChunk(text, false);
    if (chunk !== null) {
      expect(chunk).not.toBe('We tested this vs.');
    }
  });

  it('returns [null, text] when text is shorter than minLen (15 chars)', () => {
    const [chunk, rest] = splitTtsChunk('Hi there.', false);
    expect(chunk).toBeNull();
    expect(rest).toBe('Hi there.');
  });

  it('returns [null, text] when word count < 3', () => {
    const [chunk, rest] = splitTtsChunk('Okay. Yes.', false);
    expect(chunk).toBeNull();
    expect(rest).toBe('Okay. Yes.');
  });
});

describe('[unit] splitTtsChunk — clause boundary', () => {
  it('splits on comma followed by whitespace then uppercase letter', () => {
    // Clause split: ", W..." after enough leading chars.
    const text = 'If you need help, We can assist you right now.';
    const [chunk, rest] = splitTtsChunk(text, false);
    expect(chunk).not.toBeNull();
    // The chunk must end with a comma and the rest start with whitespace+uppercase.
    expect(chunk).toMatch(/,$/);
    expect(rest?.trimStart()).toMatch(/^[A-Z]/);
  });

  it('splits on semicolon followed by whitespace then uppercase letter', () => {
    const text = 'The first step is done; The second step follows right after.';
    const [chunk, rest] = splitTtsChunk(text, false);
    expect(chunk).not.toBeNull();
    expect(chunk).toMatch(/;$/);
    expect(rest?.trimStart()).toMatch(/^[A-Z]/);
  });

  it('splits on colon followed by whitespace then uppercase letter', () => {
    const text = 'Here is the plan: We will proceed with the next action.';
    const [chunk, rest] = splitTtsChunk(text, false);
    expect(chunk).not.toBeNull();
    expect(chunk).toMatch(/:$/);
  });

  it('does NOT split on clause boundary when chunk would be shorter than minLen', () => {
    // "Hi, No" — far too short; the clause split should not fire.
    const [chunk, rest] = splitTtsChunk('Hi, No more text.', false);
    // clause at pos 2 is < 15 chars so it must be skipped.
    if (chunk !== null) {
      expect(chunk.length).toBeGreaterThanOrEqual(15);
    } else {
      expect(rest).toBe('Hi, No more text.');
    }
  });
});

describe('[unit] splitTtsChunk — conjunction boundary', () => {
  it('splits on "and" when buffer >= 60 chars', () => {
    // Exactly 60+ chars so conjunction logic activates.
    const text = 'We have finished the first part of the analysis and we need to continue the work.';
    expect(text.length).toBeGreaterThanOrEqual(60);
    const [chunk, rest] = splitTtsChunk(text, false);
    expect(chunk).not.toBeNull();
    // Chunk ends before " and" — split is at the space before "and".
    expect(chunk).not.toMatch(/\band\b/);
    expect(rest).toMatch(/^\s+(and|but|so|because|however|therefore|although)\s+/i);
  });

  it('does NOT split on conjunction when buffer < 60 chars', () => {
    const text = 'Go left and turn right.';
    expect(text.length).toBeLessThan(60);
    const [chunk] = splitTtsChunk(text, false);
    // Must not split on "and" — either null or a non-conjunction split.
    if (chunk !== null) {
      // Any split that occurred must not be at the conjunction boundary
      expect(chunk).not.toBe('Go left');
    }
  });
});

describe('[unit] splitTtsChunk — length fallback', () => {
  it('splits before position 120 when text is very long with no other boundary', () => {
    // 150 chars, no punctuation — pure length fallback.
    const text = Array(15).fill('longword').join(' ') + ' extra'; // well over 120 chars, no punct
    expect(text.length).toBeGreaterThan(120);
    const [chunk, rest] = splitTtsChunk(text, false);
    expect(chunk).not.toBeNull();
    expect(chunk!.length).toBeLessThanOrEqual(120);
    expect(rest).toBeDefined();
  });
});

describe('[unit] splitTtsChunk — flush mode', () => {
  it('flush=true returns the entire remaining text as one chunk', () => {
    const text = 'This is some remaining text';
    const [chunk, rest] = splitTtsChunk(text, true);
    expect(chunk).toBe(text);
    expect(rest).toBe('');
  });

  it('flush=true on empty/whitespace-only text returns [null, text]', () => {
    const [chunk] = splitTtsChunk('   ', true);
    expect(chunk).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. CascadePlaybackMachine state transitions
// ---------------------------------------------------------------------------

describe('[unit] CascadePlaybackMachine — IDLE -> FILLER', () => {
  it('transitions from IDLE to FILLER when startFiller is called with frames', () => {
    const { machine } = makeMachine();
    expect(machine.currentState).toBe('IDLE');
    machine.startFiller([frame(0x01), frame(0x02)]);
    expect(machine.currentState).toBe('FILLER');
  });

  it('emits filler frames during FILLER state ticks', () => {
    const { machine, frames } = makeMachine();
    machine.startFiller([frame(0xAA), frame(0xBB)]);
    tickN(machine, 2);
    expect(frames).toHaveLength(2);
    expect(frames[0].every((b) => b === 0xAA)).toBe(true);
    expect(frames[1].every((b) => b === 0xBB)).toBe(true);
  });

  it('does NOT transition to FILLER when already in FILLER (no-op)', () => {
    const { machine } = makeMachine();
    machine.startFiller([frame(0x01)]);
    expect(machine.currentState).toBe('FILLER');
    machine.startFiller([frame(0x02)]);
    // State stays FILLER, second call is a no-op because state !== IDLE.
    expect(machine.currentState).toBe('FILLER');
  });

  it('does NOT transition to FILLER when in SPEAKING (no-op)', () => {
    const { machine } = makeMachine();
    machine.enqueueTtsFrames([frame()], false);
    expect(machine.currentState).toBe('SPEAKING');
    machine.startFiller([frame(0x01)]);
    expect(machine.currentState).toBe('SPEAKING');
  });
});

describe('[unit] CascadePlaybackMachine — FILLER -> SPEAKING', () => {
  it('crossfades FILLER -> SPEAKING when TTS frames arrive while in FILLER', () => {
    const { machine } = makeMachine();
    machine.startFiller([frame(0xAA), frame(0xAA)]);
    machine.enqueueTtsFrames([frame(0xBB)], false);
    // Crossfade happens at next tick.
    tickN(machine, 1);
    expect(machine.currentState).toBe('SPEAKING');
  });

  it('emits TTS frame (not filler frame) after crossfade at the tick boundary', () => {
    const { machine, frames } = makeMachine();
    machine.startFiller([frame(0xAA), frame(0xAA), frame(0xAA)]);
    // Consume first two filler frames.
    tickN(machine, 2);
    // Now enqueue TTS — next tick should crossfade and emit TTS frame.
    machine.enqueueTtsFrames([frame(0xBB)], false);
    tickN(machine, 1);
    expect(machine.currentState).toBe('SPEAKING');
    const ttsFrame = frames[frames.length - 1];
    expect(ttsFrame.every((b) => b === 0xBB)).toBe(true);
  });
});

describe('[unit] CascadePlaybackMachine — IDLE -> SPEAKING (direct)', () => {
  it('transitions IDLE -> SPEAKING when enqueueTtsFrames is called in IDLE', () => {
    const { machine } = makeMachine();
    machine.enqueueTtsFrames([frame()], false);
    expect(machine.currentState).toBe('SPEAKING');
  });

  it('returns to IDLE after markTtsComplete and all frames drained', () => {
    const { machine } = makeMachine();
    machine.enqueueTtsFrames([frame(0x01)], false);
    machine.markTtsComplete();
    tickN(machine, 1); // drain the one frame
    tickN(machine, 1); // ttsQueue empty + ttsComplete -> IDLE
    expect(machine.currentState).toBe('IDLE');
  });

  it('emits silence while SPEAKING and TTS is stalled mid-turn (not complete)', () => {
    const { machine, frames } = makeMachine();
    machine.enqueueTtsFrames([frame(0x01)], false);
    tickN(machine, 1); // drain the one frame
    // Queue empty but ttsComplete=false: stalled mid-turn -> silence.
    tickN(machine, 1);
    expect(machine.currentState).toBe('SPEAKING'); // still SPEAKING
    const silenceFrame = frames[frames.length - 1];
    expect(silenceFrame.every((b) => b === 0xff)).toBe(true); // MULAW_SILENCE
  });
});

describe('[unit] CascadePlaybackMachine — barge-in (INTERRUPTED)', () => {
  it('transitions any state to INTERRUPTED on interrupt()', () => {
    const { machine } = makeMachine();
    machine.enqueueTtsFrames([frame()], false);
    expect(machine.currentState).toBe('SPEAKING');
    machine.interrupt();
    expect(machine.currentState).toBe('INTERRUPTED');
  });

  it('interrupt() from FILLER drops filler queue and transitions to INTERRUPTED', () => {
    const { machine } = makeMachine();
    machine.startFiller([frame(0xAA), frame(0xAA), frame(0xAA)]);
    machine.interrupt();
    expect(machine.currentState).toBe('INTERRUPTED');
  });

  it('emits ~40 ms of silence (2 frames) then returns to IDLE', () => {
    const { machine, frames } = makeMachine();
    machine.enqueueTtsFrames([frame()], false);
    machine.interrupt();
    tickN(machine, 2); // emit the 2 silence frames
    expect(frames.filter((f) => f.every((b) => b === 0xff))).toHaveLength(2);
    tickN(machine, 1); // one more tick — silence exhausted -> IDLE
    expect(machine.currentState).toBe('IDLE');
  });

  it('second interrupt() while already INTERRUPTED is a no-op (guard)', () => {
    const { machine } = makeMachine();
    machine.interrupt();
    expect(machine.currentState).toBe('INTERRUPTED');
    machine.interrupt();
    expect(machine.currentState).toBe('INTERRUPTED');
    // postBargeInSilenceRemaining must not be doubled.
    const m = machine as unknown as { postBargeInSilenceRemaining: number };
    expect(m.postBargeInSilenceRemaining).toBe(2);
  });
});

describe('[unit] CascadePlaybackMachine — filler exhaust -> silence (no loop)', () => {
  it('emits silence (not filler) when filler is exhausted and TTS is not yet ready', () => {
    const { machine, frames } = makeMachine();
    machine.startFiller([frame(0xAA)]); // single filler frame
    tickN(machine, 1); // drain the one filler frame
    // Filler exhausted, TTS not queued yet — must emit silence, NOT loop filler.
    tickN(machine, 3);
    // The last 3 frames must all be silence (0xff), not 0xAA (filler byte).
    const afterFiller = frames.slice(1); // skip the filler frame itself
    for (const f of afterFiller) {
      expect(f.every((b) => b === 0xff)).toBe(true);
    }
    // Machine remains in FILLER (waiting for TTS).
    expect(machine.currentState).toBe('FILLER');
  });

  it('filler loop is never re-triggered — only one pass through filler frames', () => {
    const { machine, frames } = makeMachine();
    const fillerFrames = [frame(0xAA), frame(0xAA)];
    machine.startFiller(fillerFrames);
    tickN(machine, 10); // many ticks past the end of filler
    const fillerByteFrames = frames.filter((f) => f.every((b) => b === 0xAA));
    // Exactly 2 filler frames emitted, never replayed.
    expect(fillerByteFrames).toHaveLength(2);
  });
});

describe('[unit] CascadePlaybackMachine — stop() resets all state', () => {
  it('stop() returns machine to IDLE and clears queues', () => {
    const { machine } = makeMachine();
    machine.enqueueTtsFrames([frame(), frame()], false);
    machine.interrupt();
    machine.stop();
    expect(machine.currentState).toBe('IDLE');
    const m = machine as unknown as {
      fillerQueue: Buffer[];
      ttsQueue: Buffer[];
      ttsComplete: boolean;
      postBargeInSilenceRemaining: number;
    };
    expect(m.fillerQueue).toHaveLength(0);
    expect(m.ttsQueue).toHaveLength(0);
    expect(m.ttsComplete).toBe(false);
    expect(m.postBargeInSilenceRemaining).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 3. selectFiller
// ---------------------------------------------------------------------------

describe('[unit] selectFiller — rule priority', () => {
  it('returns "let_me_check" for a question-word input (highest priority)', () => {
    expect(selectFiller('What time does the office close?')).toBe('let_me_check');
    expect(selectFiller('How do I reset my password?')).toBe('let_me_check');
    expect(selectFiller('Can you tell me the price?')).toBe('let_me_check');
    expect(selectFiller('Where is the nearest branch?')).toBe('let_me_check');
  });

  it('returns "let_me_check" regardless of question word position in sentence', () => {
    // "what" appears mid-sentence.
    expect(selectFiller('I need to know what the total is.')).toBe('let_me_check');
  });

  it('returns "let_me_think" for long input (>60 chars) with no question', () => {
    const longText = 'I have a pretty complicated situation and I need your help to sort it out.';
    expect(longText.length).toBeGreaterThan(60);
    expect(selectFiller(longText)).toBe('let_me_think');
  });

  it('returns "let_me_think" for input with >= 2 clause boundaries', () => {
    // 2 commas = 2 clause boundaries.
    const text = 'Yes, I agree, this is fine.';
    const count = (text.match(/[.!?,;:]/g) ?? []).length;
    expect(count).toBeGreaterThanOrEqual(2);
    expect(selectFiller(text)).toBe('let_me_think');
  });

  it('returns "okay_moment" for urgency token', () => {
    expect(selectFiller('This is urgent please respond.')).toBe('okay_moment');
    expect(selectFiller('Handle this asap.')).toBe('okay_moment');
    expect(selectFiller('I need this right away.')).toBe('okay_moment');
  });

  it('returns "got_it" for closure token', () => {
    expect(selectFiller('Thanks for the help!')).toBe('got_it');
    // "Alright" is in CLOSURE_RE; one punctuation mark -> clauseBoundaries=1 < 2
    expect(selectFiller('Alright understood')).toBe('got_it');
    // "great" is in CLOSURE_RE; short text, 0 clause boundaries
    expect(selectFiller('That is great')).toBe('got_it');
  });

  it('returns "mhm" for input shorter than 15 chars (no question / no urgency / no closure)', () => {
    // "Yep" and "Sure" are not in CLOSURE_RE, URGENCY_RE, or QUESTION_RE and are < 15 chars.
    expect(selectFiller('Yep')).toBe('mhm');
    expect(selectFiller('Mm hmm')).toBe('mhm');
  });

  it('returns "mhm" for empty input', () => {
    expect(selectFiller('')).toBe('mhm');
  });

  it('round-robins between "sure_one_sec" and "got_it" for neutral medium-length input', () => {
    // Use a text that hits none of the priority rules: no question word, no long, no urgency, no closure.
    const neutral = 'I will call you back in a minute later.';
    expect(neutral.length).toBeGreaterThanOrEqual(15);
    expect(neutral.length).toBeLessThanOrEqual(60);

    // Drain the round-robin to a known starting position first.
    // Because _rrCursor is module-level, we call selectFiller enough times to
    // see both values regardless of starting cursor position.
    const results = new Set<string>();
    for (let i = 0; i < 10; i++) {
      results.add(selectFiller(neutral));
    }
    expect(results.has('sure_one_sec')).toBe(true);
    expect(results.has('got_it')).toBe(true);
    // Must only alternate between those two values.
    for (const r of results) {
      expect(['sure_one_sec', 'got_it']).toContain(r);
    }
  });

  it('question-word rule takes priority over length rule', () => {
    // Long (>60 chars) AND contains a question word — question wins.
    const text = 'Can you please tell me all the details about the pricing plan and the discounts available?';
    expect(text.length).toBeGreaterThan(60);
    expect(selectFiller(text)).toBe('let_me_check');
  });

  it('question-word rule takes priority over urgency rule', () => {
    // Contains both "what" and "urgent" — question wins.
    expect(selectFiller('What is the most urgent thing to do right now?')).toBe('let_me_check');
  });
});

// ---------------------------------------------------------------------------
// 4. GeminiCascadeAdapter — setupComplete gates turn dispatch (regression)
// ---------------------------------------------------------------------------

describe('[mocked] GeminiCascadeAdapter — setupComplete gates turn dispatch', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('sendText before connect() resolves is silently dropped (no session yet)', async () => {
    // Mock @google/genai: connect() never fires setupComplete so we get the timeout.
    // We test that sendText() before _ready resolves does not throw and does not
    // attempt to send on a null session.
    vi.doMock('@google/genai', () => ({
      GoogleGenAI: vi.fn().mockImplementation(() => ({
        live: {
          connect: vi.fn(async (args: { callbacks?: { onopen?: () => void } }) => {
            args.callbacks?.onopen?.();
            // setupComplete deliberately withheld — simulates pre-setup race.
            return {
              sendRealtimeInput: vi.fn(),
              sendClientContent: vi.fn(),
              sendToolResponse: vi.fn(),
              close: vi.fn(),
            };
          }),
          models: { generateContentStream: vi.fn(async () => (async function* () {})()) },
        },
      })),
    }));

    const { GeminiCascadeAdapter } = await import('../src/providers/gemini-cascade.js');
    const adapter = new GeminiCascadeAdapter('test-key');

    // Do NOT await connect() — it will block waiting for setupComplete.
    const connectPromise = adapter.connect();

    // sendText before the session is ready: should not throw.
    await expect(adapter.sendText('hello')).resolves.toBeUndefined();

    // Clean up: close without waiting for ready.
    await adapter.close();
    await connectPromise.catch(() => {
      // Expected: CONNECT_TIMEOUT_MS fires and throws. That's fine for this test.
    });
  }, 12000);

  it('connect() waits for setupComplete before resolving (prod silence regression)', async () => {
    // REGRESSION (prod 2026-06-18): resolving on onopen let the firstMessage be
    // sent before the Gemini session was configured — the server silently dropped
    // it and the agent never spoke. connect() MUST block until setupComplete.
    interface Cbs {
      onopen?: () => void;
      onmessage?: (msg: unknown) => void;
    }

    let capturedCbs: Cbs | null = null;

    const fakeSession = {
      sendRealtimeInput: vi.fn(),
      sendClientContent: vi.fn(),
      sendToolResponse: vi.fn(),
      close: vi.fn(),
    };

    vi.doMock('@google/genai', () => ({
      GoogleGenAI: vi.fn().mockImplementation(() => ({
        live: {
          connect: vi.fn(async (args: { callbacks?: Cbs }) => {
            capturedCbs = args.callbacks ?? null;
            args.callbacks?.onopen?.(); // socket open — NOT yet setup
            return fakeSession;
          }),
        },
        models: {
          generateContentStream: vi.fn(async () => (async function* () {})()),
        },
      })),
    }));

    const { GeminiCascadeAdapter } = await import('../src/providers/gemini-cascade.js');
    const adapter = new GeminiCascadeAdapter('test-key');

    let resolved = false;
    const connectPromise = adapter.connect().then(() => {
      resolved = true;
    });

    // onopen has fired; setupComplete has NOT — connect() must still be pending.
    await new Promise<void>((r) => setTimeout(r, 50));
    expect(resolved).toBe(false);
    expect(capturedCbs).not.toBeNull();

    // Server fires setupComplete — connect() must now resolve.
    capturedCbs!.onmessage!({ setupComplete: {} });
    // Also resolve the preSynthClips TTS calls (return empty streams).
    await connectPromise.catch(() => {});

    // Give the clips + warmup a chance to settle.
    await new Promise<void>((r) => setTimeout(r, 100));
    expect(resolved).toBe(true);

    await adapter.close();
  }, 15000);
});
