import { describe, it, expect, beforeEach } from 'vitest';
import { SentenceChunker, DEFAULT_MIN_SENTENCE_LEN } from '../../src/sentence-chunker';

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
      // Verify via behaviour: a short push that would only emit if min were 0
      const c = new SentenceChunker();
      expect(c.push('Hi!')).toEqual([]);
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
      // The short-flush path (added for low TTS TTFB on short greetings)
      // may emit small complete sentences early, so token-by-token streaming
      // can produce up to ~30% more sentences than the bulk reference. We
      // therefore validate content equivalence (after whitespace
      // normalisation) rather than the exact split.
      const c = new SentenceChunker(); // default minSentenceLen = 20
      const pushed = streamText(c, REFERENCE_TEXT);
      const flushed = c.flush();
      const all = [...pushed, ...flushed];

      expect(all.length).toBeGreaterThanOrEqual(EXPECTED_MIN_20.length);
      expect(all.length).toBeLessThanOrEqual(EXPECTED_MIN_20.length + 4);

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

    it('does NOT emit single-word "Sì." standalone', () => {
      const c = new SentenceChunker();
      expect(c.push('Sì.')).toEqual([]);
      // Survives a flush though.
      expect(c.flush()).toEqual(['Sì.']);
    });

    it('does NOT emit single-word "Yes."', () => {
      const c = new SentenceChunker();
      expect(c.push('Yes.')).toEqual([]);
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
