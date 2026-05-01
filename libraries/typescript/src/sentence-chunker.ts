/**
 * Sentence chunker for streaming TTS in pipeline mode.
 *
 * Accumulates streaming LLM tokens and yields complete sentences.
 * Uses regex-based marker replacement for robust sentence boundary
 * detection, handling abbreviations, acronyms, decimals, websites,
 * ellipsis, and CJK punctuation.
 */

/** Default minimum sentence length before emitting. */
export const DEFAULT_MIN_SENTENCE_LEN = 20;

/**
 * Minimum word count for emitting a "short" sentence (one whose total length
 * is below `minSentenceLen`) as soon as a terminator is seen. Default is 1:
 * a single-word reply ("Yes.", "Done.") flushes immediately on the
 * terminator so TTS can speak it without waiting for `flush()`. Acronym
 * and decimal guards in `maybeShortFlush` still block dangerous cases
 * ("U.S.", "f(x) = 2."). Bumping this to 2+ keeps single-word utterances
 * buffered until `flush()` is called by the caller.
 */
export const DEFAULT_MIN_WORDS_FOR_SHORT_FLUSH = 1;

// ---------------------------------------------------------------------------
// Per-language honorific / abbreviation prefixes.
//
// Each entry is the ALPHA prefix (no trailing period) — the regex framework
// in `splitSentences` appends the `[.]` itself. We merge all language lists
// into a single regex alternation so the chunker handles mixed-language text
// correctly out of the box (this is the behaviour shipped since the SDK
// introduced sentence chunking; per-language constants are an organisational
// refactor that also lets callers verify per-language coverage in tests).
//
// Single-letter honorifics (French "M.", "A.") are deliberately omitted —
// they are handled by the existing `\\s + alphabets + [.] ` rule which
// preserves any single-letter-period sequence.
// ---------------------------------------------------------------------------

/** English honorifics (NLTK Punkt training set + common military/civic). */
export const HONORIFICS_EN: readonly string[] = [
  'Mr', 'St', 'Mrs', 'Ms', 'Dr', 'Prof',
  'Gen', 'Sen', 'Rep', 'Lt', 'Cpt', 'Capt', 'Col', 'Cmdr', 'Adm',
];

/**
 * Italian honorifics. Compound abbreviations like "Sig.ra" / "Dott.ssa" /
 * "Prof.ssa" are handled implicitly: the prefix regex matches the leading
 * word ("Sig", "Dott", "Prof") and the trailing letters after the period
 * are preserved as part of the same token by the marker-replacement pass.
 */
export const HONORIFICS_IT: readonly string[] = [
  'Sig', 'Sgr', 'Dott', 'Prof', 'Avv', 'Ing', 'Geom', 'Rag',
  'Arch', 'On', 'Egr', 'Spett', 'Gent', 'Ill',
];

/** Spanish honorifics. */
export const HONORIFICS_ES: readonly string[] = [
  'Sr', 'Sra', 'Sres', 'Sras', 'Srta', 'Srtas',
  'Dr', 'Dra', 'Dres', 'Lic', 'Licda', 'Ing', 'Prof', 'Profa',
  'Arq', 'Mtro', 'Mtra',
];

/** German honorifics. */
export const HONORIFICS_DE: readonly string[] = [
  'Hr', 'Fr', 'Frl', 'Dr', 'Prof', 'Dipl', 'Mag',
];

/** French honorifics. */
export const HONORIFICS_FR: readonly string[] = [
  'Mme', 'Mmes', 'Mlle', 'Mlles', 'MM', 'Dr', 'Pr', 'Mgr', 'Me',
];

/** Portuguese honorifics (European + Brazilian). */
export const HONORIFICS_PT: readonly string[] = [
  'Sr', 'Sra', 'Srs', 'Sras', 'Srta', 'Srtas',
  'Dr', 'Dra', 'Eng', 'Enga', 'Prof', 'Profa',
];

/** Mapping for callers who want to know which language ships which list. */
export const HONORIFICS_BY_LANGUAGE: Readonly<Record<string, readonly string[]>> = {
  en: HONORIFICS_EN,
  it: HONORIFICS_IT,
  es: HONORIFICS_ES,
  de: HONORIFICS_DE,
  fr: HONORIFICS_FR,
  pt: HONORIFICS_PT,
};

/**
 * Union of every language list, sorted longest-first so regex alternation
 * prefers the most specific match (e.g. "Sras" before "Sr").
 */
export const HONORIFICS_ALL: readonly string[] = Array.from(
  new Set(Object.values(HONORIFICS_BY_LANGUAGE).flat()),
).sort((a, b) => b.length - a.length || a.localeCompare(b));

/**
 * Sentence-terminating characters. Includes Latin (`. ! ?`), full-width CJK
 * (`。 ！ ？`), Japanese half-width (`｡`), full-width semicolon (`；`),
 * full-width period (`．`), Western ellipsis (`…`), and ASCII semicolon (`;`).
 */
const SENTENCE_TERMINATORS = '.!?…;。！？；．｡';

/**
 * Unambiguous non-Latin sentence terminators — punctuation that cannot also
 * appear in numbers, abbreviations, or URLs in the script's typical usage.
 * Hindi/Devanagari (। ॥), Arabic (؟ ؛ ۔ ؏), Armenian (։), Ethiopic (። ፧),
 * Khmer (។ ៕), Burmese (။), Tibetan (༎ ༏).
 */
const UNAMBIGUOUS_NON_LATIN_TERMINATORS = '।॥؟؛۔؏։፧።។៕။༎༏';

/** Pre-built regex character class covering all terminators (escaped). */
const TERMINATOR_REGEX_CLASS = Array.from(
  new Set(SENTENCE_TERMINATORS + UNAMBIGUOUS_NON_LATIN_TERMINATORS),
)
  .map((c) => c.replace(/[\\^$.|?*+()[\]{}]/g, '\\$&'))
  .sort()
  .join('');

/**
 * "Soft" punctuation marks that terminate a clause but not a full sentence.
 * Candidates for the optional aggressive first-clause flush only. Includes
 * em-dash (U+2014) and en-dash (U+2013); excludes ":" (often used as
 * "Name: …" in LLM output) and ";" (rare in conversational speech).
 */
const SOFT_TERMINATORS = ',—–';

/**
 * Default minimum buffer length before the aggressive first-clause flush is
 * allowed to fire. Below ~40 chars TTS prosody suffers; ElevenLabs internally
 * buffers up to 120 chars by default (`chunk_length_schedule`), so very
 * short fragments are merged regardless of what we send.
 */
export const DEFAULT_AGGRESSIVE_FIRST_MIN_LEN = 40;

/**
 * Currency symbols that, when present near a comma, indicate the comma is a
 * decimal/thousands separator and must not be treated as a clause boundary.
 */
const CURRENCY_SYMBOLS = '$€£¥₹₩';

/**
 * Pre-built regex alternation for honorific prefixes (longest-first so that
 * "Sras" matches before "Sr"). Built once at module load.
 */
const HONORIFICS_REGEX_ALT = HONORIFICS_ALL.map((p) =>
  p.replace(/[\\^$.|?*+()[\]{}]/g, '\\$&'),
).join('|');

/** Honorifics as a Set for O(1) word-membership tests in the short-flush path. */
const HONORIFICS_SET = new Set<string>(HONORIFICS_ALL);

/**
 * Split text into sentences using regex marker replacement.
 *
 * Returns an array of [sentence, startPos, endPos] tuples.
 * The input text must not contain literal `<prd>` or `<stop>` substrings.
 */
function splitSentences(
  text: string,
  minSentenceLen: number = DEFAULT_MIN_SENTENCE_LEN,
): Array<[string, number, number]> {
  const alphabets = '([A-Za-z])';
  // Title/honorific prefixes that take a trailing period. Sourced from the
  // union of every language list in `HONORIFICS_BY_LANGUAGE` (en / it / es /
  // de / fr / pt). The period after these is preserved (treated as part of
  // the word, not as sentence end).
  const prefixes = `(${HONORIFICS_REGEX_ALT})[.]`;
  // Suffix-style abbreviations. EN additions from NLTK Punkt:
  // vs, etc, No, Vol, pp, cf, ca, op, plus address-style abbrevs Mt, Hwy,
  // Rt, Pl, Ave, Blvd, Sq.
  const suffixes =
    '(Inc|Ltd|Jr|Sr|Co|ecc|cit|cap|sez|art|pag|fig|tab|cfr|vol|ed|' +
    'vs|etc|No|Vol|pp|cf|ca|op|Mt|Hwy|Rt|Pl|Ave|Blvd|Sq)';
  const starters =
    '(Mr|Mrs|Ms|Dr|Prof|Capt|Cpt|Lt|He\\s|She\\s|It\\s|They\\s|Their\\s|' +
    'Our\\s|We\\s|But\\s|However\\s|That\\s|This\\s|Wherever)';
  const acronyms = '([A-Z][.][A-Z][.](?:[A-Z][.])?)';
  const websites = '[.](com|net|org|io|gov|edu|me)';
  const digits = '([0-9])';
  const multipleDots = '\\.{2,}';

  text = text.replace(/\n/g, ' ');

  text = text.replace(new RegExp(prefixes, 'g'), '$1<prd>');
  text = text.replace(new RegExp(websites, 'g'), '<prd>$1');
  text = text.replace(new RegExp(digits + '[.]' + digits, 'g'), '$1<prd>$2');
  text = text.replace(new RegExp(multipleDots, 'g'), (m) => '<prd>'.repeat(m.length));

  if (text.includes('Ph.D')) {
    text = text.replace(/Ph\.D\./g, 'Ph<prd>D<prd>');
  }

  text = text.replace(new RegExp('\\s' + alphabets + '[.] ', 'g'), ' $1<prd> ');
  text = text.replace(new RegExp(acronyms + ' ' + starters, 'g'), '$1<stop> $2');
  text = text.replace(
    new RegExp(alphabets + '[.]' + alphabets + '[.]' + alphabets + '[.]', 'g'),
    '$1<prd>$2<prd>$3<prd>',
  );
  text = text.replace(
    new RegExp(alphabets + '[.]' + alphabets + '[.]', 'g'),
    '$1<prd>$2<prd>',
  );
  // Preserve the period of the suffix abbreviation when it precedes a starter,
  // e.g. "Patter Inc. He left" → keep "Inc." in the emitted sentence.
  text = text.replace(new RegExp(' ' + suffixes + '[.] ' + starters, 'g'), ' $1.<stop> $2');
  text = text.replace(new RegExp(' ' + suffixes + '[.]', 'g'), ' $1<prd>');
  text = text.replace(new RegExp(' ' + alphabets + '[.]', 'g'), ' $1<prd>');

  // Mark sentence-ending punctuation (Latin + CJK + non-Latin scripts).
  text = text.replace(
    new RegExp(`([${TERMINATOR_REGEX_CLASS}])(["\u201d])`, 'g'),
    '$1$2<stop>',
  );
  text = text.replace(
    new RegExp(`([${TERMINATOR_REGEX_CLASS}])(?!["\u201d])`, 'g'),
    '$1<stop>',
  );

  // Restore periods
  text = text.replace(/<prd>/g, '.');

  const splitted = text.split('<stop>');
  text = text.replace(/<stop>/g, '');

  const sentences: Array<[string, number, number]> = [];
  let buff = '';
  let startPos = 0;
  let endPos = 0;

  for (const match of splitted) {
    const sentence = match.trim();
    if (!sentence) continue;

    buff += ' ' + sentence;
    endPos += match.length;

    if (buff.length > minSentenceLen) {
      sentences.push([buff.trimStart(), startPos, endPos]);
      startPos = endPos;
      buff = '';
    }
  }

  if (buff) {
    sentences.push([buff.trimStart(), startPos, text.length - 1]);
  }

  return sentences;
}

/**
 * Accumulates streaming tokens and yields complete sentences.
 *
 * @example
 * ```typescript
 * const chunker = new SentenceChunker();
 * for await (const token of llmStream) {
 *   for (const sentence of chunker.push(token)) {
 *     await tts.synthesizeStream(sentence);
 *   }
 * }
 * for (const sentence of chunker.flush()) {
 *   await tts.synthesizeStream(sentence);
 * }
 * ```
 */
export class SentenceChunker {
  private buffer = '';
  private readonly minSentenceLen: number;
  private readonly minWordsForShortFlush: number;
  private readonly aggressiveFirstMinLen: number;
  private readonly aggressiveFirstFlush: boolean;
  private readonly language: string;
  private isFirstFlush = true;

  constructor(options?: {
    minSentenceLen?: number;
    minWordsForShortFlush?: number;
    /**
     * When true, the chunker emits the first clause of each response on a
     * soft punctuation boundary (",", em-dash, en-dash) once
     * `aggressiveFirstMinLen` characters accumulate. Saves 200-500 ms TTFA
     * on the first sentence of each turn. Subsequent sentences fall through
     * to the standard sentence-boundary path. Default: false.
     */
    aggressiveFirstFlush?: boolean;
    aggressiveFirstMinLen?: number;
    /**
     * BCP-47-ish language tag. Italian uses comma as decimal separator
     * (3,14) and dot as thousands (1.000) — both invert the English
     * convention — so aggressive comma flush is hard-disabled when language
     * starts with "it" regardless of `aggressiveFirstFlush`. Default: "en".
     */
    language?: string;
  }) {
    this.minSentenceLen = options?.minSentenceLen ?? DEFAULT_MIN_SENTENCE_LEN;
    this.minWordsForShortFlush =
      options?.minWordsForShortFlush ?? DEFAULT_MIN_WORDS_FOR_SHORT_FLUSH;
    this.aggressiveFirstMinLen =
      options?.aggressiveFirstMinLen ?? DEFAULT_AGGRESSIVE_FIRST_MIN_LEN;
    this.language = (options?.language ?? 'en').toLowerCase();
    this.aggressiveFirstFlush =
      (options?.aggressiveFirstFlush ?? false) && !this.language.startsWith('it');
  }

  /**
   * Feed a token. Returns zero or more complete sentences.
   *
   * Two emission paths:
   * - **Standard path** — when the buffer is at least `minSentenceLen`
   *   characters long and the regex tokenizer reports more than one
   *   sentence, all but the last (potentially incomplete) are emitted.
   * - **Short-flush path** — when the buffer is shorter than `minSentenceLen`
   *   but ends with a sentence terminator AND has at least
   *   `minWordsForShortFlush` whitespace-separated words (default 1 — a
   *   single-word reply like `"Yes."` flushes immediately for low TTS
   *   TTFB). Acronym ("U.S.") and decimal ("f(x) = 2.") guards still block
   *   dangerous cases. Bump `minWordsForShortFlush` to 2+ to keep
   *   single-word utterances buffered until `flush()`.
   */
  push(token: string): string[] {
    this.buffer += token;

    // Aggressive first-clause flush: when enabled, emit the first clause of
    // the response on a soft punctuation boundary (",", em/en-dash) as soon
    // as enough characters accumulate. Saves 200-500 ms TTFA on the first
    // sentence of each turn. Subsequent sentences fall through to the
    // standard sentence-boundary path.
    if (this.aggressiveFirstFlush && this.isFirstFlush) {
      const flushed = this.maybeAggressiveFirstFlush();
      if (flushed !== null) {
        this.isFirstFlush = false;
        return [flushed];
      }
    }

    if (this.buffer.length < this.minSentenceLen) {
      return this.maybeShortFlush();
    }

    const sentences = splitSentences(this.buffer, this.minSentenceLen);

    if (sentences.length <= 1) {
      return [];
    }

    // Emit all sentences except the last (which may be incomplete)
    const result: string[] = [];
    for (let i = 0; i < sentences.length - 1; i++) {
      const text = sentences[i][0].trim();
      if (text) result.push(text);
    }

    // Keep the last (potentially incomplete) sentence in the buffer
    this.buffer = sentences[sentences.length - 1]?.[0] ?? '';

    return result;
  }

  /**
   * Emit the buffer when it's a short, complete single-sentence utterance.
   *
   * A buffer qualifies when **all** of these hold:
   * 1. Last non-whitespace char is a sentence terminator.
   * 2. Word count is at least `minWordsForShortFlush` (default 1 —
   *    single-word replies like `"Yes."` flush immediately).
   * 3. The buffer contains exactly one terminator (the trailing one).
   *    Multiple terminators mean we may be mid-stream of a longer merged
   *    utterance like `"Hey! Hi! Hello! This is a sentence."` — let the
   *    standard path keep merging.
   * 4. The char immediately before the terminator is NOT a digit (avoids
   *    decimal mid-stream like `"f(x) = x * 2."` flushing before `54`).
   * 5. The trailing word is NOT a short ASCII all-caps acronym of 1-3 chars
   *    (`"U."` / `"U.S."` / `"USA."`).
   * 6. The trailing word is NOT a known honorific from any of the
   *    per-language `HONORIFICS_*` constants (`"Mr."`, `"Sr."`, `"Dr."`,
   *    `"Hr."`, `"Mme."`, ...).
   */
  private maybeShortFlush(): string[] {
    const stripped = this.buffer.replace(/\s+$/, '');
    if (!stripped) return [];
    const last = stripped[stripped.length - 1];
    if (!SENTENCE_TERMINATORS.includes(last)) return [];

    // Only one terminator in the entire buffer (the trailing one).
    let terminatorCount = 0;
    for (const c of stripped) {
      if (SENTENCE_TERMINATORS.includes(c)) terminatorCount++;
    }
    if (terminatorCount !== 1) return [];

    const wordCount = stripped.split(/\s+/).filter((w) => w.length > 0).length;
    if (wordCount < this.minWordsForShortFlush) return [];

    // Don't flush on potential decimals.
    if (stripped.length >= 2) {
      const prev = stripped[stripped.length - 2];
      if (/\d/.test(prev)) return [];
      // Don't flush on short all-caps acronyms ("U.", "US.", "USA.") — these
      // are likely abbreviation periods, not sentence ends. Only block if
      // the trailing word is **purely uppercase ASCII** AND **at most 3
      // chars** (matches U/US/USA/NATO patterns; longer all-caps words
      // like RAMESH or SPEAKING are real sentences).
      const terminator = stripped[stripped.length - 1];
      if (terminator === '.') {
        const stripTerm = stripped.replace(
          new RegExp(`[${TERMINATOR_REGEX_CLASS}]+$`),
          '',
        );
        const tokens = stripTerm.split(/\s+/).filter((w) => w.length > 0);
        const lastWord = tokens.length > 0 ? tokens[tokens.length - 1] : '';
        if (/^[A-Z]{1,3}$/.test(lastWord)) return [];
        // Don't flush when trailing word is a known honorific — name almost
        // certainly follows ("Mr. Theo", "Sr. García", "Hr. Müller").
        if (HONORIFICS_SET.has(lastWord)) return [];
      }
    }

    this.buffer = '';
    return [stripped];
  }

  /**
   * Try to flush the first clause of the response on a soft punctuation
   * boundary (comma / em-dash / en-dash) to minimise TTFA.
   *
   * Returns the flushed clause text (with terminator) or `null` if no safe
   * boundary is found. All of these guards must pass:
   *
   * 1. **Min length** — buffer ≥ `aggressiveFirstMinLen` (default 40).
   * 2. **Trailing terminator** — last non-whitespace char in `SOFT_TERMINATORS`.
   * 3. **Decimal/thousands guard** — refuse if comma is between two digits
   *    or surrounded by digit-thousands grouping.
   * 4. **Currency guard** — refuse if a currency symbol appears in the
   *    preceding 8 characters.
   * 5. **Balanced delimiter** — refuse if open parens/brackets/braces or
   *    unmatched double-quotes still pending.
   * 6. **Ellipsis** — refuse if buffer ends with `...` or `…`.
   * 7. **Sub-token ambiguity** — only fire when at least one trailing char
   *    after the terminator has arrived.
   */
  private maybeAggressiveFirstFlush(): string | null {
    const rstripped = this.buffer.replace(/\s+$/, '');
    if (rstripped.length < this.aggressiveFirstMinLen) return null;

    const lastChar = rstripped[rstripped.length - 1] ?? '';
    if (!SOFT_TERMINATORS.includes(lastChar)) return null;

    const pos = rstripped.length - 1;

    // Sub-token ambiguity: require at least one char after the terminator.
    if (pos + 1 >= this.buffer.length) return null;
    const nextChar = this.buffer[pos + 1] ?? '';

    // Decimal/thousands guard for comma: refuse if surrounded by digits.
    if (lastChar === ',') {
      const prevChar = pos >= 1 ? rstripped[pos - 1] ?? '' : '';
      if (/\d/.test(prevChar) && /\d/.test(nextChar)) return null;
      // Conservative thousands-separator pattern: digit-comma followed by a
      // window containing another comma in a digit context ⇒ skip.
      const tail = rstripped.slice(Math.max(0, pos - 6), pos);
      if (
        /\d/.test(prevChar) &&
        tail.includes(',') &&
        /\d/.test(tail)
      ) {
        return null;
      }
    }

    // Currency guard.
    const snippet = rstripped.slice(Math.max(0, pos - 8), pos);
    for (const c of CURRENCY_SYMBOLS) {
      if (snippet.includes(c)) return null;
    }

    // Balanced delimiter guard.
    const opens = (rstripped.match(/[([{]/g) ?? []).length;
    const closes = (rstripped.match(/[)\]}]/g) ?? []).length;
    if (opens > closes) return null;
    const dquoteCount = (rstripped.match(/"/g) ?? []).length;
    if (dquoteCount % 2 !== 0) return null;

    // Ellipsis guard.
    if (rstripped.endsWith('...') || rstripped.endsWith('…')) return null;

    // Comma-before-quote guard (orphan fragment).
    if (lastChar === ',' && nextChar === '"') return null;

    // All guards passed. Emit the clause and trim the buffer.
    const flushed = rstripped;
    this.buffer = this.buffer.slice(rstripped.length).replace(/^\s+/, '');
    return flushed;
  }

  /** Flush remaining buffer as final sentence(s). Call at end of stream. */
  flush(): string[] {
    const remaining = this.buffer.trim();
    this.buffer = '';
    this.isFirstFlush = true;

    if (!remaining) return [];

    return [remaining];
  }

  /** Discard buffered text. Call on interrupt. */
  reset(): void {
    this.buffer = '';
    this.isFirstFlush = true;
  }
}
