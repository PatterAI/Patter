/**
 * Sentence chunker for streaming TTS in pipeline mode.
 *
 * Accumulates streaming LLM tokens and yields complete sentences.
 * Uses regex-based marker replacement for robust sentence boundary
 * detection, handling abbreviations, acronyms, decimals, websites,
 * ellipsis, and CJK punctuation.
 *
 * Algorithm adapted from LiveKit Agents (Apache 2.0):
 * https://github.com/livekit/agents
 */

/** Default minimum sentence length before emitting. */
export const DEFAULT_MIN_SENTENCE_LEN = 20;

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
  const prefixes = '(Mr|St|Mrs|Ms|Dr)[.]';
  const suffixes = '(Inc|Ltd|Jr|Sr|Co)';
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
  text = text.replace(new RegExp(' ' + suffixes + '[.] ' + starters, 'g'), ' $1<stop> $2');
  text = text.replace(new RegExp(' ' + suffixes + '[.]', 'g'), ' $1<prd>');
  text = text.replace(new RegExp(' ' + alphabets + '[.]', 'g'), ' $1<prd>');

  // Mark sentence-ending punctuation (including CJK)
  text = text.replace(/([.!?\u3002\uff01\uff1f])(["\u201d])/g, '$1$2<stop>');
  text = text.replace(/([.!?\u3002\uff01\uff1f])(?!["\u201d])/g, '$1<stop>');

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

  constructor(options?: { minSentenceLen?: number }) {
    this.minSentenceLen = options?.minSentenceLen ?? DEFAULT_MIN_SENTENCE_LEN;
  }

  /** Feed a token. Returns zero or more complete sentences. */
  push(token: string): string[] {
    this.buffer += token;

    if (this.buffer.length < this.minSentenceLen) {
      return [];
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

  /** Flush remaining buffer as final sentence(s). Call at end of stream. */
  flush(): string[] {
    const remaining = this.buffer.trim();
    this.buffer = '';

    if (!remaining) return [];

    return [remaining];
  }

  /** Discard buffered text. Call on interrupt. */
  reset(): void {
    this.buffer = '';
  }
}
