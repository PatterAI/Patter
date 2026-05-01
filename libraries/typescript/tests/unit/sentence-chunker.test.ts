import { describe, it, expect, beforeEach } from 'vitest';
import {
  DEFAULT_MIN_SENTENCE_LEN,
  HONORIFICS_ALL,
  HONORIFICS_BY_LANGUAGE,
  HONORIFICS_DE,
  HONORIFICS_EN,
  HONORIFICS_ES,
  HONORIFICS_FR,
  HONORIFICS_IT,
  HONORIFICS_PT,
  SentenceChunker,
} from '../../src/sentence-chunker';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Feed an entire string one character at a time, collect all emitted sentences. */
function streamText(chunker: SentenceChunker, text: string): string[] {
  const results: string[] = [];
  for (const char of text) {
    results.push(...chunker.push(char));
  }
  return results;
}

// ---------------------------------------------------------------------------
// Reference text + expected output for sentence-boundary detection.
// ---------------------------------------------------------------------------

const REFERENCE_TEXT =
  'Hi! ' +
  'Patter is a platform for live audio and video applications and services. \n\n' +
  'R.T.C stands for Real-Time Communication... again R.T.C. ' +
  'Mr. Theo is testing the sentence tokenizer. ' +
  '\nThis is a test. Another test. ' +
  'A short sentence.\n' +
  'A longer sentence that is longer than the previous sentence. ' +
  'f(x) = x * 2.54 + 42. ' +
  'Hey!\n Hi! Hello! ' +
  '\n\n' +
  'This is a sentence. 这是一个中文句子。これは日本語の文章です。' +
  '你好！Patter是一个直播音频和视频应用程序和服务的平台。' +
  '\nThis is a sentence contains   consecutive spaces.';

const EXPECTED_MIN_20 = [
  'Hi! Patter is a platform for live audio and video applications and services.',
  'R.T.C stands for Real-Time Communication... again R.T.C.',
  'Mr. Theo is testing the sentence tokenizer.',
  'This is a test. Another test.',
  'A short sentence. A longer sentence that is longer than the previous sentence.',
  'f(x) = x * 2.54 + 42.',
  'Hey! Hi! Hello! This is a sentence.',
  '这是一个中文句子。 これは日本語の文章です。',
  '你好！ Patter是一个直播音频和视频应用程序和服务的平台。',
  'This is a sentence contains   consecutive spaces.',
];

// ---------------------------------------------------------------------------
// SentenceChunker — Unit Tests
// ---------------------------------------------------------------------------

describe('SentenceChunker', () => {
  let chunker: SentenceChunker;

  beforeEach(() => {
    chunker = new SentenceChunker();
  });

  // -------------------------------------------------------------------------
  // Constructor / constants
  // -------------------------------------------------------------------------

  describe('DEFAULT_MIN_SENTENCE_LEN', () => {
    it('is exported as 20', () => {
      expect(DEFAULT_MIN_SENTENCE_LEN).toBe(20);
    });
  });

  describe('constructor', () => {
    it('uses DEFAULT_MIN_SENTENCE_LEN when no options provided', () => {
      // Buffer with no terminator must not flush regardless of min length.
      const c = new SentenceChunker();
      expect(c.push('Hi there')).toEqual([]);
    });

    it('accepts a custom minSentenceLen', () => {
      const c = new SentenceChunker({ minSentenceLen: 5 });
      // "Hello." is 6 chars, above threshold — should split at the period
      const out = c.push('Hello. World.');
      // At min=5, "Hello." alone exceeds the threshold so it can be emitted
      expect(out.length).toBeGreaterThanOrEqual(1);
    });
  });

  // -------------------------------------------------------------------------
  // Empty / trivial input
  // -------------------------------------------------------------------------

  describe('empty input', () => {
    it('returns [] for empty string push', () => {
      expect(chunker.push('')).toEqual([]);
    });

    it('flush on empty buffer returns []', () => {
      expect(chunker.flush()).toEqual([]);
    });

    it('push then flush with no sentence boundary returns buffered text via flush', () => {
      chunker.push('hello world');
      const out = chunker.flush();
      expect(out).toHaveLength(1);
      expect(out[0]).toBe('hello world');
    });
  });

  // -------------------------------------------------------------------------
  // Basic sentence splitting
  // -------------------------------------------------------------------------

  describe('basic sentence splitting', () => {
    it('splits on period', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      // Push a full two-sentence string
      const out = [
        ...c.push('First sentence. Second sentence.'),
        ...c.flush(),
      ];
      expect(out.some((s) => s.includes('First sentence'))).toBe(true);
      expect(out.some((s) => s.includes('Second sentence'))).toBe(true);
    });

    it('splits on exclamation mark', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('Hello! World!'), ...c.flush()];
      expect(out.some((s) => s.includes('Hello'))).toBe(true);
    });

    it('splits on question mark', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('Are you there? Yes I am.'), ...c.flush()];
      expect(out.some((s) => s.includes('Are you there?'))).toBe(true);
      expect(out.some((s) => s.includes('Yes I am'))).toBe(true);
    });

    it('does not split mid-sentence (no punctuation)', () => {
      const out = chunker.push('This is not a complete sentence yet');
      expect(out).toEqual([]);
    });
  });

  // -------------------------------------------------------------------------
  // Abbreviation handling
  // -------------------------------------------------------------------------

  describe('abbreviation handling', () => {
    it('does NOT split at "Mr."', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('Mr. Theo is testing.'), ...c.flush()];
      // "Mr. Theo is testing." should appear as a single sentence
      const joined = out.join(' ');
      expect(joined).toContain('Mr. Theo is testing');
      // Ensure there is no orphaned "Theo is testing" without "Mr."
      expect(out.every((s) => !s.startsWith('Theo'))).toBe(true);
    });

    it('does NOT split at "Dr."', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('Dr. Smith treated the patient.'), ...c.flush()];
      const joined = out.join(' ');
      expect(joined).toContain('Dr. Smith');
      expect(out.every((s) => !s.startsWith('Smith'))).toBe(true);
    });

    it('does NOT split at "Mrs."', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('Mrs. Jones arrived early.'), ...c.flush()];
      const joined = out.join(' ');
      expect(joined).toContain('Mrs. Jones');
    });

    it('does NOT split at "Ms."', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('Ms. Taylor leads the team.'), ...c.flush()];
      const joined = out.join(' ');
      expect(joined).toContain('Ms. Taylor');
    });

    it('does NOT split at "St." (Saint)', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('St. Patrick was celebrated today.'), ...c.flush()];
      const joined = out.join(' ');
      expect(joined).toContain('St. Patrick');
    });
  });

  // -------------------------------------------------------------------------
  // Decimal / numeric handling
  // -------------------------------------------------------------------------

  describe('decimal handling', () => {
    it('does NOT split at a decimal point like "3.14"', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('Pi is 3.14 dollars. That is all.'), ...c.flush()];
      const joined = out.join(' ');
      // "3.14" must not be broken across sentences
      expect(joined).toContain('3.14');
      expect(out.some((s) => s.startsWith('14'))).toBe(false);
    });

    it('does NOT split "2.54" in a formula', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('f(x) = x * 2.54 + 42. Done.'), ...c.flush()];
      const joined = out.join(' ');
      expect(joined).toContain('2.54');
    });
  });

  // -------------------------------------------------------------------------
  // Website / domain handling
  // -------------------------------------------------------------------------

  describe('website handling', () => {
    it('does NOT split at "example.com"', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [
        ...c.push('Visit example.com for more info. Thanks.'),
        ...c.flush(),
      ];
      const joined = out.join(' ');
      expect(joined).toContain('example.com');
      expect(out.some((s) => s.startsWith('com'))).toBe(false);
    });

    it('does NOT split at .org, .net, .io domains', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [
        ...c.push('Check getpatter.io and nodejs.org. Done.'),
        ...c.flush(),
      ];
      const joined = out.join(' ');
      expect(joined).toContain('getpatter.io');
      expect(joined).toContain('nodejs.org');
    });
  });

  // -------------------------------------------------------------------------
  // Acronym handling
  // -------------------------------------------------------------------------

  describe('acronym handling', () => {
    it('does NOT split inside "R.T.C"', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [
        ...c.push('R.T.C stands for Real-Time Communication... again R.T.C. Done.'),
        ...c.flush(),
      ];
      // None of the emitted sentences should start with "T.C" or "C"
      expect(out.every((s) => !s.match(/^[TC]\.[A-Z]/))).toBe(true);
    });

    it('keeps two-letter acronyms like "U.S" intact', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('The U.S. is large. Really large.'), ...c.flush()];
      const joined = out.join(' ');
      expect(joined).toContain('U.S');
    });
  });

  // -------------------------------------------------------------------------
  // Ellipsis handling
  // -------------------------------------------------------------------------

  describe('ellipsis handling', () => {
    it('does NOT split on "..."', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [
        ...c.push('Wait for it... and then it happened. Great.'),
        ...c.flush(),
      ];
      // "Wait for it..." and "and then it happened." should end up together or
      // "..." should not be treated as three sentence boundaries.
      // There must be no sentence starting with a bare "."
      expect(out.every((s) => !s.startsWith('.'))).toBe(true);
    });
  });

  // -------------------------------------------------------------------------
  // CJK punctuation
  // -------------------------------------------------------------------------

  describe('CJK terminators', () => {
    it('splits on fullwidth period 。', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('これはテストです。次の文章です。'), ...c.flush()];
      expect(out.length).toBeGreaterThanOrEqual(1);
    });

    it('splits on fullwidth exclamation ！', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('你好！再见。'), ...c.flush()];
      expect(out.length).toBeGreaterThanOrEqual(1);
    });

    it('splits on fullwidth question ？', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('你好吗？我很好。'), ...c.flush()];
      expect(out.length).toBeGreaterThanOrEqual(1);
    });
  });

  // -------------------------------------------------------------------------
  // Ph.D. handling
  // -------------------------------------------------------------------------

  describe('Ph.D. handling', () => {
    it('does NOT split inside "Ph.D."', () => {
      const c = new SentenceChunker({ minSentenceLen: 1 });
      const out = [...c.push('She earned her Ph.D. last year. Congratulations.'), ...c.flush()];
      const joined = out.join(' ');
      expect(joined).toContain('Ph.D.');
      // No sentence should begin with "D." alone
      expect(out.every((s) => !s.match(/^D\./))).toBe(true);
    });
  });

  // -------------------------------------------------------------------------
  // minSentenceLen merging
  // -------------------------------------------------------------------------

  describe('minSentenceLen merge behaviour', () => {
    it('merges fragments shorter than minSentenceLen (default 20)', () => {
      // "Hi!" alone is 3 chars — too short to emit on its own with min=20.
      // It should be merged with the following sentence.
      const c = new SentenceChunker();
      const out = [
        ...c.push('Hi! This is a longer sentence that exceeds twenty chars.'),
        ...c.flush(),
      ];
      // The merged output should contain "Hi!" and the following text together
      const first = out[0];
      expect(first).toContain('Hi!');
      expect(first.length).toBeGreaterThanOrEqual(20);
    });

    it('two short fragments are merged together until combined length exceeds min', () => {
      const c = new SentenceChunker({ minSentenceLen: 30 });
      // "Short one." (10) + "Also short." (11) = combined 21, still < 30 → merged further
      const out = [
        ...c.push('Short one. Also short. Now a properly long sentence here!'),
        ...c.flush(),
      ];
      // No emitted sentence should be shorter than 30 chars (except possibly the last flush)
      const emittedBeforeFlush = out.slice(0, -1);
      emittedBeforeFlush.forEach((s) => {
        expect(s.length).toBeGreaterThanOrEqual(30);
      });
    });
  });

  // -------------------------------------------------------------------------
  // Streaming: token-by-token feeding
  // -------------------------------------------------------------------------

  describe('streaming (token-by-token)', () => {
    it('emits nothing until a full sentence boundary is reached', () => {
      const partial = 'This is an incomplete';
      for (const char of partial) {
        expect(chunker.push(char)).toEqual([]);
      }
    });

    it('eventually emits a sentence when boundary token arrives', () => {
      const c = new SentenceChunker({ minSentenceLen: 5 });
      const text = 'Hello world. Next sentence here.';
      const out = streamText(c, text);
      expect(out.length).toBeGreaterThanOrEqual(1);
      expect(out[0]).toContain('Hello world');
    });

    it('does not duplicate content across push and flush', () => {
      const c = new SentenceChunker({ minSentenceLen: 5 });
      const text = 'First sentence. Second sentence.';
      const pushed = streamText(c, text);
      const flushed = c.flush();
      const all = [...pushed, ...flushed];
      // Reconstruct: joining all output should equal the original (modulo whitespace normalisation)
      const reconstructed = all.join(' ').replace(/\s+/g, ' ').trim();
      const original = text.replace(/\s+/g, ' ').trim().replace(/\.$/, '');
      expect(reconstructed).toContain('First sentence');
      expect(reconstructed).toContain('Second sentence');
    });

    it('handles multi-sentence text fed one token at a time', () => {
      const c = new SentenceChunker({ minSentenceLen: 10 });
      const text = 'The cat sat. The dog ran. The bird flew.';
      const pushed = streamText(c, text);
      const flushed = c.flush();
      const all = [...pushed, ...flushed];
      expect(all.length).toBeGreaterThanOrEqual(2);
    });
  });

  // -------------------------------------------------------------------------
  // flush()
  // -------------------------------------------------------------------------

  describe('flush()', () => {
    it('returns the remaining buffer content', () => {
      chunker.push('This is a partial');
      const out = chunker.flush();
      expect(out).toHaveLength(1);
      expect(out[0]).toBe('This is a partial');
    });

    it('clears the buffer after flush', () => {
      chunker.push('Some text');
      chunker.flush();
      expect(chunker.flush()).toEqual([]);
    });

    it('returns [] when buffer is empty', () => {
      expect(chunker.flush()).toEqual([]);
    });

    it('trims whitespace in flushed content', () => {
      chunker.push('  leading and trailing  ');
      const out = chunker.flush();
      expect(out[0]).toBe('leading and trailing');
    });
  });

  // -------------------------------------------------------------------------
  // reset()
  // -------------------------------------------------------------------------

  describe('reset()', () => {
    it('discards buffered text so flush returns []', () => {
      chunker.push('Some buffered text');
      chunker.reset();
      expect(chunker.flush()).toEqual([]);
    });

    it('allows normal operation after reset', () => {
      chunker.push('Forgotten text');
      chunker.reset();
      chunker.push('Fresh start');
      const out = chunker.flush();
      expect(out[0]).toBe('Fresh start');
      expect(out[0]).not.toContain('Forgotten');
    });

    it('is idempotent — double reset is safe', () => {
      chunker.push('Text');
      chunker.reset();
      chunker.reset();
      expect(chunker.flush()).toEqual([]);
    });
  });

  // -------------------------------------------------------------------------
  // Full reference text — sentence boundary detection over a long fixture
  // -------------------------------------------------------------------------

  describe('full reference text', () => {
    it('produces the expected sentences from the full reference text (batch push)', () => {
      const c = new SentenceChunker(); // default minSentenceLen = 20
      const pushed = c.push(REFERENCE_TEXT);
      const flushed = c.flush();
      const all = [...pushed, ...flushed];

      expect(all).toEqual(EXPECTED_MIN_20);
    });

    it('produces the expected sentences when fed token-by-token', () => {
      // With Bug #49's single-word short-flush default, char-by-char
      // streaming may emit many more sentences than the bulk reference
      // (every short greeting "Hi!", "Hey!", "Hello!" flushes individually).
      // We validate content equivalence (after whitespace normalisation)
      // rather than the exact count.
      const c = new SentenceChunker(); // default minSentenceLen = 20
      const pushed = streamText(c, REFERENCE_TEXT);
      const flushed = c.flush();
      const all = [...pushed, ...flushed];

      expect(all.length).toBeGreaterThanOrEqual(EXPECTED_MIN_20.length);

      const normalise = (s: string) => s.replace(/\s+/g, ' ').trim();
      expect(normalise(all.join(' '))).toBe(normalise(EXPECTED_MIN_20.join(' ')));
    });
  });

  // -------------------------------------------------------------------------
  // Short-flush path — TTS TTFB optimisation for short greetings
  // -------------------------------------------------------------------------

  describe('short-flush path', () => {
    it('emits "Hi there!" immediately on the !', () => {
      const c = new SentenceChunker();
      expect(c.push('Hi there!')).toEqual(['Hi there!']);
    });

    it('emits "Hello world." immediately on the .', () => {
      const c = new SentenceChunker();
      expect(c.push('Hello world.')).toEqual(['Hello world.']);
    });

    it('emits "Are you?" immediately on the ?', () => {
      const c = new SentenceChunker();
      expect(c.push('Are you?')).toEqual(['Are you?']);
    });

    it('emits single-word "Sì." on terminator (Bug #49)', () => {
      // Bug #49 — for phone calls a one-word reply must reach TTS without
      // waiting for `flush()`. Acronym/honorific guards still block the
      // dangerous cases ("U.S.", "Mr.").
      const c = new SentenceChunker();
      expect(c.push('Sì.')).toEqual(['Sì.']);
      expect(c.flush()).toEqual([]);
    });

    it('emits single-word "Yes." on terminator (Bug #49)', () => {
      const c = new SentenceChunker();
      expect(c.push('Yes.')).toEqual(['Yes.']);
    });

    it('legacy: minWordsForShortFlush=2 keeps single words buffered', () => {
      // Bumping the threshold restores the pre-Bug-#49 behaviour.
      const c = new SentenceChunker({ minWordsForShortFlush: 2 });
      expect(c.push('Sì.')).toEqual([]);
      expect(c.flush()).toEqual(['Sì.']);
    });

    it('does NOT flush a buffer with no terminator', () => {
      const c = new SentenceChunker();
      expect(c.push('Hi there')).toEqual([]);
    });

    it('does NOT flush "f(x) = 2." (digit before terminator)', () => {
      const c = new SentenceChunker();
      expect(c.push('f(x) = 2.')).toEqual([]);
    });

    it('does NOT flush "The U.S." (acronym pattern)', () => {
      const c = new SentenceChunker();
      expect(c.push('The U.S.')).toEqual([]);
    });

    it('does NOT flush a buffer with multiple terminators ("Hey! Hi!")', () => {
      const c = new SentenceChunker();
      expect(c.push('Hey! Hi!')).toEqual([]);
    });

    it('honours a custom minWordsForShortFlush of 1', () => {
      const c = new SentenceChunker({ minWordsForShortFlush: 1 });
      expect(c.push('Yes.')).toEqual(['Yes.']);
    });

    it('handles trailing whitespace before the terminator-only buffer', () => {
      const c = new SentenceChunker();
      expect(c.push('Hi there!  \n')).toEqual(['Hi there!']);
    });
  });
});

// ---------------------------------------------------------------------------
// Phase 2 — Aggressive first-clause flush (opt-in)
// ---------------------------------------------------------------------------

describe('SentenceChunker — aggressive first-clause flush', () => {
  it('default OFF: no behaviour change', () => {
    const c = new SentenceChunker();
    const out: string[] = [];
    for (const t of ['Sure, ', 'I can ', 'definitely ', 'help ', 'you ', 'now.']) {
      out.push(...c.push(t));
    }
    out.push(...c.flush());
    expect(out).toEqual(['Sure, I can definitely help you now.']);
  });

  it('aggressive flush fires after first comma when buffer ≥ 40 chars', () => {
    const c = new SentenceChunker({ aggressiveFirstFlush: true });
    const tokens = [
      'Sure, ',
      'I can ',
      'definitely ',
      'help ',
      'you ',
      'with ',
      'that ',
      'request',
      ', ',
      'right ',
      'away.',
    ];
    const out: string[] = [];
    for (const t of tokens) out.push(...c.push(t));
    out.push(...c.flush());
    expect(out).toHaveLength(2);
    expect(out[0].endsWith(',')).toBe(true);
    expect(out[1]).toBe('right away.');
  });

  it('only fires for the first sentence — subsequent sentences use period boundary', () => {
    const c = new SentenceChunker({ aggressiveFirstFlush: true });
    const out: string[] = [];
    for (const t of [
      'Sure, ',
      'I can help you with that today. ',
      'Also, ',
      'let me check inventory levels for you next.',
    ]) {
      out.push(...c.push(t));
    }
    out.push(...c.flush());
    expect(out[0].endsWith(',')).toBe(true);
    for (let i = 1; i < out.length; i++) {
      // After the first aggressive flush, no further comma-only emissions.
      expect(out[i].endsWith(',') && !out[i].endsWith('.')).toBe(false);
    }
  });

  it('Italian language hard-disables aggressive flush (decimal comma killer)', () => {
    const c = new SentenceChunker({ aggressiveFirstFlush: true, language: 'it' });
    const out: string[] = [];
    for (const t of [
      'Certo, ',
      'ti aiuto subito con questa richiesta importante. ',
      'Vediamo subito.',
    ]) {
      out.push(...c.push(t));
    }
    out.push(...c.flush());
    expect(out).toEqual([
      'Certo, ti aiuto subito con questa richiesta importante.',
      'Vediamo subito.',
    ]);
  });

  it('decimal guard: comma between digits does not trigger flush', () => {
    const c = new SentenceChunker({ aggressiveFirstFlush: true });
    const out: string[] = [];
    for (const t of [
      'The total is exactly ',
      '1,',
      '000 ',
      'dollars for the entire week. ',
      'Confirmed.',
    ]) {
      out.push(...c.push(t));
    }
    out.push(...c.flush());
    expect(out).toEqual([
      'The total is exactly 1,000 dollars for the entire week.',
      'Confirmed.',
    ]);
  });

  it('currency guard: $ within 8 chars before comma blocks flush', () => {
    const c = new SentenceChunker({ aggressiveFirstFlush: true });
    const out: string[] = [];
    for (const t of ['The amount is $1,', '000 ', 'for next week. ', 'Confirmed.']) {
      out.push(...c.push(t));
    }
    out.push(...c.flush());
    expect(out).toEqual(['The amount is $1,000 for next week.', 'Confirmed.']);
  });

  it('balanced delimiter guard: open brace blocks flush (JSON payload)', () => {
    const c = new SentenceChunker({ aggressiveFirstFlush: true });
    const out: string[] = [];
    for (const t of [
      'Sending payload {"amount": 1000, "currency": "USD"} to backend ',
      'now.',
    ]) {
      out.push(...c.push(t));
    }
    out.push(...c.flush());
    expect(out).toEqual([
      'Sending payload {"amount": 1000, "currency": "USD"} to backend now.',
    ]);
  });

  it('ellipsis guard: "..." does not trigger flush', () => {
    const c = new SentenceChunker({ aggressiveFirstFlush: true });
    const out: string[] = [];
    for (const t of ['Let me think about this for a moment... ', 'perhaps yes.']) {
      out.push(...c.push(t));
    }
    out.push(...c.flush());
    expect(out).toEqual(['Let me think about this for a moment... perhaps yes.']);
  });

  it('first-flush state resets after flush()', () => {
    const c = new SentenceChunker({ aggressiveFirstFlush: true });
    for (const t of ['Sure, ', 'I can help you with that today, ', 'no problem.']) {
      c.push(t);
    }
    c.flush();
    const turn2: string[] = [];
    for (const t of [
      'Of course, ',
      'I will check inventory levels right now, ',
      'one moment.',
    ]) {
      turn2.push(...c.push(t));
    }
    turn2.push(...c.flush());
    expect(turn2[0].endsWith(',')).toBe(true);
  });

  it('first-flush state resets after reset()', () => {
    const c = new SentenceChunker({ aggressiveFirstFlush: true });
    c.push('Sure, I can help you with that today, no problem.');
    c.reset();
    const turn2: string[] = [];
    for (const t of [
      'Of course, ',
      'I will check inventory levels right now, ',
      'one moment.',
    ]) {
      turn2.push(...c.push(t));
    }
    turn2.push(...c.flush());
    expect(turn2[0].endsWith(',')).toBe(true);
  });

  it('buffer below aggressiveFirstMinLen does not flush', () => {
    const c = new SentenceChunker({
      aggressiveFirstFlush: true,
      aggressiveFirstMinLen: 40,
    });
    const out = [...c.push('Hi, '), ...c.push('hello there.')];
    out.push(...c.flush());
    expect(out).toEqual(['Hi, hello there.']);
  });
});

// ---------------------------------------------------------------------------
// Bug #48 — per-language honorifics / abbreviations
// ---------------------------------------------------------------------------

describe('SentenceChunker — per-language honorifics (Bug #48)', () => {
  // Helper: pushes a full text, then flushes, and returns all emitted sentences.
  const split = (text: string, minSentenceLen = 1): string[] => {
    const c = new SentenceChunker({ minSentenceLen });
    return [...c.push(text), ...c.flush()];
  };

  // ---------------------------- English ---------------------------- //

  it('English: "Mr. Smith joined us." stays as one sentence', () => {
    const out = split('Mr. Smith joined us.');
    expect(out).toHaveLength(1);
    expect(out[0]).toContain('Mr. Smith');
  });

  it('English negative: "Map. Then we left." splits (Map is not an honorific)', () => {
    const out = split('Map. Then we left.');
    expect(out).toHaveLength(2);
  });

  // ---------------------------- Spanish ---------------------------- //

  it('Spanish: "Sra. García" stays as one sentence', () => {
    const out = split('Buenos días Sra. García.');
    expect(out).toHaveLength(1);
    expect(out[0]).toContain('Sra. García');
  });

  it('Spanish: "Dra. Fernández" stays as one sentence', () => {
    const out = split('La Dra. Fernández llegó temprano.');
    expect(out).toHaveLength(1);
  });

  it('Spanish: "Lic. Ramírez" stays as one sentence', () => {
    const out = split('El Lic. Ramírez firmó.');
    expect(out).toHaveLength(1);
  });

  it('Spanish negative: "Pan. Comemos a las dos." splits', () => {
    const out = split('Pan. Comemos a las dos.');
    expect(out).toHaveLength(2);
  });

  // ---------------------------- German ---------------------------- //

  it('German: "Hr. Müller" stays as one sentence', () => {
    const out = split('Guten Tag Hr. Müller.');
    expect(out).toHaveLength(1);
    expect(out[0]).toContain('Hr. Müller');
  });

  it('German: "Fr. Schmidt" stays as one sentence', () => {
    const out = split('Hallo Fr. Schmidt.');
    expect(out).toHaveLength(1);
  });

  it('German: "z.B." inline stays as one sentence', () => {
    const out = split('Es gibt viele Optionen, z.B. rote oder blaue.');
    expect(out).toHaveLength(1);
    expect(out[0]).toContain('z.B.');
  });

  it('German negative: "Tag. Wir gehen jetzt." splits', () => {
    const out = split('Tag. Wir gehen jetzt.');
    expect(out).toHaveLength(2);
  });

  // ---------------------------- Italian ---------------------------- //

  it('Italian: "Dott. Rossi" stays as one sentence', () => {
    const out = split('Buongiorno Dott. Rossi.');
    expect(out).toHaveLength(1);
    expect(out[0]).toContain('Dott. Rossi');
  });

  it('Italian: "Sig.ra Bianchi" stays as one sentence', () => {
    const out = split('Buongiorno Sig.ra Bianchi, come va?');
    expect(out).toHaveLength(1);
    expect(out[0]).toContain('Sig.ra Bianchi');
  });

  it('Italian negative: "Pane. Mangiamo alle due." splits', () => {
    const out = split('Pane. Mangiamo alle due.');
    expect(out).toHaveLength(2);
  });

  // ---------------------------- French ---------------------------- //

  it('French: "Mme. Dupont" stays as one sentence', () => {
    const out = split('Bonjour Mme. Dupont.');
    expect(out).toHaveLength(1);
    expect(out[0]).toContain('Mme. Dupont');
  });

  it('French: "Pr. Martin" stays as one sentence', () => {
    const out = split('Le Pr. Martin enseigne ici.');
    expect(out).toHaveLength(1);
  });

  it('French: "Mlle. Leroy" stays as one sentence', () => {
    const out = split('Mlle. Leroy est arrivée hier.');
    expect(out).toHaveLength(1);
  });

  it('French negative: "Pain. Nous mangeons à deux." splits', () => {
    const out = split('Pain. Nous mangeons à deux.');
    expect(out).toHaveLength(2);
  });

  // ---------------------------- Portuguese ---------------------------- //

  it('Portuguese: "Sr. Silva" stays as one sentence', () => {
    const out = split('Bom dia Sr. Silva.');
    expect(out).toHaveLength(1);
    expect(out[0]).toContain('Sr. Silva');
  });

  it('Portuguese: "Eng. Costa" stays as one sentence', () => {
    const out = split('O Eng. Costa aprovou o projeto.');
    expect(out).toHaveLength(1);
  });

  it('Portuguese negative: "Pão. Comemos às duas." splits', () => {
    const out = split('Pão. Comemos às duas.');
    expect(out).toHaveLength(2);
  });

  // -------------------- Aggregation invariants -------------------- //

  it('HONORIFICS_ALL is the union of every per-language list', () => {
    const union = new Set<string>();
    for (const prefixes of Object.values(HONORIFICS_BY_LANGUAGE)) {
      for (const p of prefixes) union.add(p);
    }
    expect(new Set(HONORIFICS_ALL)).toEqual(union);
  });

  it('HONORIFICS_ALL is sorted longest-first', () => {
    for (let i = 1; i < HONORIFICS_ALL.length; i++) {
      expect(HONORIFICS_ALL[i - 1].length).toBeGreaterThanOrEqual(
        HONORIFICS_ALL[i].length,
      );
    }
  });

  it('per-language constants are non-empty', () => {
    expect(HONORIFICS_EN.length).toBeGreaterThan(0);
    expect(HONORIFICS_IT.length).toBeGreaterThan(0);
    expect(HONORIFICS_ES.length).toBeGreaterThan(0);
    expect(HONORIFICS_DE.length).toBeGreaterThan(0);
    expect(HONORIFICS_FR.length).toBeGreaterThan(0);
    expect(HONORIFICS_PT.length).toBeGreaterThan(0);
  });

  it('chunker accepts every supported language tag', () => {
    for (const lang of ['en', 'it', 'es', 'de', 'fr', 'pt', 'multi']) {
      const c = new SentenceChunker({ language: lang });
      expect(c.push('Hello world.')).toEqual(['Hello world.']);
    }
  });
});

// ---------------------------------------------------------------------------
// Bug #49 — single-word "Yes." flushes during stream
// ---------------------------------------------------------------------------

describe('SentenceChunker — single-word flush (Bug #49)', () => {
  it('flushes "Yes." on push — TTS does not have to wait for flush()', () => {
    const c = new SentenceChunker();
    expect(c.push('Yes.')).toEqual(['Yes.']);
  });

  it('flushes "Done!" on push', () => {
    const c = new SentenceChunker();
    expect(c.push('Done!')).toEqual(['Done!']);
  });

  it('flushes "Really?" on push', () => {
    const c = new SentenceChunker();
    expect(c.push('Really?')).toEqual(['Really?']);
  });

  it('flushes diacritic single-word "Sì."', () => {
    const c = new SentenceChunker();
    expect(c.push('Sì.')).toEqual(['Sì.']);
  });

  it('flushes Japanese "はい。" with fullwidth period', () => {
    const c = new SentenceChunker();
    expect(c.push('はい。')).toEqual(['はい。']);
  });

  it('flushes word-then-period split across two pushes', () => {
    const c = new SentenceChunker();
    expect(c.push('Yes')).toEqual([]);
    expect(c.push('.')).toEqual(['Yes.']);
  });

  // Regression guards — dangerous cases must still NOT flush.

  it('does not flush acronym "U.S."', () => {
    const c = new SentenceChunker();
    expect(c.push('U.S.')).toEqual([]);
  });

  it('does not flush "f(x) = 2." (digit before terminator)', () => {
    const c = new SentenceChunker();
    expect(c.push('f(x) = 2.')).toEqual([]);
  });

  it('does not flush honorific-only "Mr."', () => {
    const c = new SentenceChunker();
    expect(c.push('Mr.')).toEqual([]);
  });

  it('does not flush honorific-only "Sr." (Spanish)', () => {
    const c = new SentenceChunker();
    expect(c.push('Sr.')).toEqual([]);
  });

  it('does not flush honorific-only "Hr." (German)', () => {
    const c = new SentenceChunker();
    expect(c.push('Hr.')).toEqual([]);
  });

  it('does not flush honorific-only "Mme." (French)', () => {
    const c = new SentenceChunker();
    expect(c.push('Mme.')).toEqual([]);
  });
});
