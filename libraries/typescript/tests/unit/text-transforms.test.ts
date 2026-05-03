/**
 * Unit tests for built-in text transforms (filterMarkdown, filterEmoji, filterForTTS).
 *
 * Covers:
 *  1. filterMarkdown — header stripping
 *  2. filterMarkdown — bold and italic
 *  3. filterMarkdown — inline code and fenced code blocks
 *  4. filterMarkdown — links and images
 *  5. filterMarkdown — list markers (ordered + unordered)
 *  6. filterMarkdown — block quotes
 *  7. filterMarkdown — strikethrough
 *  8. filterMarkdown — horizontal rules
 *  9. filterMarkdown — HTML tags
 * 10. filterMarkdown — plain text passes through unchanged
 * 11. filterMarkdown — empty string
 * 12. filterMarkdown — combined markdown
 * 13. filterEmoji — removes common emoji
 * 14. filterEmoji — preserves non-emoji Unicode (CJK, accented chars)
 * 15. filterEmoji — empty string
 * 16. filterEmoji — text without emoji passes through unchanged
 * 17. filterForTTS — combined markdown + emoji removal
 */

import { describe, it, expect } from 'vitest';
import { filterMarkdown, filterEmoji, filterForTTS } from '../../src/text-transforms';

// ---------------------------------------------------------------------------
// filterMarkdown
// ---------------------------------------------------------------------------

describe('filterMarkdown', () => {
  it('strips h1 headers', () => {
    expect(filterMarkdown('# Hello World')).toBe('Hello World');
  });

  it('strips h2 headers', () => {
    expect(filterMarkdown('## Section Title')).toBe('Section Title');
  });

  it('strips h3 through h6 headers', () => {
    expect(filterMarkdown('### H3')).toBe('H3');
    expect(filterMarkdown('#### H4')).toBe('H4');
    expect(filterMarkdown('###### H6')).toBe('H6');
  });

  it('strips bold (**text**)', () => {
    expect(filterMarkdown('This is **bold** text')).toBe('This is bold text');
  });

  it('strips bold (__text__)', () => {
    expect(filterMarkdown('This is __bold__ text')).toBe('This is bold text');
  });

  it('strips italic (*text*)', () => {
    expect(filterMarkdown('This is *italic* text')).toBe('This is italic text');
  });

  it('strips italic (_text_)', () => {
    expect(filterMarkdown('This is _italic_ text')).toBe('This is italic text');
  });

  it('strips inline code', () => {
    expect(filterMarkdown('Use the `console.log` function')).toBe('Use the console.log function');
  });

  it('strips fenced code blocks', () => {
    const input = '```javascript\nconst x = 1;\n```';
    expect(filterMarkdown(input)).toBe('const x = 1;');
  });

  it('strips fenced code blocks without language', () => {
    const input = '```\nhello\n```';
    expect(filterMarkdown(input)).toBe('hello');
  });

  it('strips links [text](url)', () => {
    expect(filterMarkdown('Visit [Google](https://google.com) now')).toBe('Visit Google now');
  });

  it('strips images ![alt](url)', () => {
    expect(filterMarkdown('![Logo](https://example.com/logo.png)')).toBe('Logo');
  });

  it('strips strikethrough (~~text~~)', () => {
    expect(filterMarkdown('This is ~~deleted~~ text')).toBe('This is deleted text');
  });

  it('strips unordered list markers (dash)', () => {
    const input = '- Item one\n- Item two';
    expect(filterMarkdown(input)).toBe('Item one\nItem two');
  });

  it('strips unordered list markers (asterisk)', () => {
    const input = '* Item one\n* Item two';
    expect(filterMarkdown(input)).toBe('Item one\nItem two');
  });

  it('strips ordered list markers', () => {
    const input = '1. First\n2. Second\n3. Third';
    expect(filterMarkdown(input)).toBe('First\nSecond\nThird');
  });

  it('strips block quotes', () => {
    expect(filterMarkdown('> This is a quote')).toBe('This is a quote');
  });

  it('strips horizontal rules (---)', () => {
    const input = 'Above\n---\nBelow';
    expect(filterMarkdown(input)).toBe('Above\n\nBelow');
  });

  it('strips horizontal rules (***)', () => {
    const input = 'Above\n***\nBelow';
    expect(filterMarkdown(input)).toBe('Above\n\nBelow');
  });

  it('strips HTML tags', () => {
    expect(filterMarkdown('Hello <b>world</b>')).toBe('Hello world');
  });

  it('strips self-closing HTML tags', () => {
    expect(filterMarkdown('Line<br/>break')).toBe('Linebreak');
  });

  it('returns plain text unchanged', () => {
    const plain = 'Hello, this is a normal sentence.';
    expect(filterMarkdown(plain)).toBe(plain);
  });

  it('returns empty string for empty input', () => {
    expect(filterMarkdown('')).toBe('');
  });

  it('handles combined markdown', () => {
    const input = [
      '# Welcome',
      '',
      'This is **bold** and *italic* text.',
      '',
      '- Item one',
      '- Item two',
      '',
      '> A quote',
      '',
      'Visit [here](https://example.com).',
    ].join('\n');

    const expected = [
      'Welcome',
      '',
      'This is bold and italic text.',
      '',
      'Item one',
      'Item two',
      '',
      'A quote',
      '',
      'Visit here.',
    ].join('\n');

    expect(filterMarkdown(input)).toBe(expected);
  });
});

// ---------------------------------------------------------------------------
// filterEmoji
// ---------------------------------------------------------------------------

describe('filterEmoji', () => {
  it('removes common emoticons', () => {
    expect(filterEmoji('Hello! \u{1F600}')).toBe('Hello!');
  });

  it('removes multiple emoji', () => {
    expect(filterEmoji('\u{1F680} Launch \u{1F31F} now \u{1F389}')).toBe('Launch now');
  });

  it('removes misc symbols (U+2600 block)', () => {
    expect(filterEmoji('Sunny \u{2600} day')).toBe('Sunny day');
  });

  it('removes dingbats (U+2700 block)', () => {
    expect(filterEmoji('Check \u{2714} done')).toBe('Check done');
  });

  it('preserves CJK characters', () => {
    expect(filterEmoji('\u4F60\u597D')).toBe('\u4F60\u597D');
  });

  it('preserves accented characters', () => {
    expect(filterEmoji('caf\u00E9 r\u00E9sum\u00E9')).toBe('caf\u00E9 r\u00E9sum\u00E9');
  });

  it('preserves normal punctuation', () => {
    expect(filterEmoji('Hello, world! How are you?')).toBe('Hello, world! How are you?');
  });

  it('returns text without emoji unchanged', () => {
    const plain = 'No emoji here.';
    expect(filterEmoji(plain)).toBe(plain);
  });

  it('returns empty string for empty input', () => {
    expect(filterEmoji('')).toBe('');
  });

  it('handles text that is only emoji', () => {
    expect(filterEmoji('\u{1F600}\u{1F601}\u{1F602}')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// filterForTTS
// ---------------------------------------------------------------------------

describe('filterForTTS', () => {
  it('removes both markdown and emoji', () => {
    const input = '## Hello \u{1F44B}\n\nThis is **great** \u{1F389}!';
    const result = filterForTTS(input);
    expect(result).toBe('Hello\n\nThis is great !');
  });

  it('returns plain text unchanged', () => {
    const plain = 'Just a normal sentence without formatting.';
    expect(filterForTTS(plain)).toBe(plain);
  });

  it('returns empty string for empty input', () => {
    expect(filterForTTS('')).toBe('');
  });
});
