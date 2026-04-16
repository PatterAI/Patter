/**
 * Built-in text transforms for cleaning LLM output before TTS synthesis.
 *
 * These functions strip markdown formatting and emoji characters so that TTS
 * engines produce natural-sounding speech rather than reading aloud syntax
 * like "asterisk asterisk bold asterisk asterisk" or Unicode pictographs.
 */

/**
 * Remove markdown formatting from text, preserving the readable content.
 *
 * Handles: headers, bold, italic, code blocks/inline, links, images,
 * strikethrough, list markers, block quotes, horizontal rules, HTML tags.
 */
export function filterMarkdown(text: string): string {
  let result = text;

  // Fenced code blocks: ```lang\ncode\n``` → code
  result = result.replace(/```[\s\S]*?```/g, (match) => {
    const inner = match.replace(/^```[^\n]*\n?/, '').replace(/\n?```$/, '');
    return inner;
  });

  // Inline code: `code` → code
  result = result.replace(/`([^`]+)`/g, '$1');

  // Images: ![alt](url) → alt
  result = result.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');

  // Links: [text](url) → text
  result = result.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');

  // Strikethrough: ~~text~~ → text
  result = result.replace(/~~(.*?)~~/g, '$1');

  // Headers: # text → text (at start of line)
  result = result.replace(/^#{1,6}\s+/gm, '');

  // Horizontal rules: ---, ***, ___ (standalone line) — must come before bold/italic
  result = result.replace(/^[-*_]{3,}\s*$/gm, '');

  // Bold: **text** or __text__ → text (must come before italic)
  result = result.replace(/\*\*(.+?)\*\*/g, '$1');
  result = result.replace(/__(.+?)__/g, '$1');

  // Italic: *text* or _text_ → text
  result = result.replace(/\*(.+?)\*/g, '$1');
  result = result.replace(/(?<!\w)_(.+?)_(?!\w)/g, '$1');

  // Block quotes: > text → text (at start of line)
  result = result.replace(/^>\s+/gm, '');

  // Unordered list markers: - item, * item → item (at start of line)
  result = result.replace(/^[-*]\s+/gm, '');

  // Ordered list markers: 1. item → item (at start of line)
  result = result.replace(/^\d+\.\s+/gm, '');

  // HTML tags: <tag> or </tag> → empty
  result = result.replace(/<\/?[^>]+(>|$)/g, '');

  // Collapse multiple blank lines into one
  result = result.replace(/\n{3,}/g, '\n\n');

  return result.trim();
}

/**
 * Remove emoji characters from text, preserving normal text, punctuation,
 * and non-emoji Unicode (CJK, accented characters, etc.).
 */
export function filterEmoji(text: string): string {
  // Comprehensive emoji removal regex covering major Unicode emoji blocks:
  //   U+1F600-U+1F64F  Emoticons
  //   U+1F300-U+1F5FF  Misc Symbols and Pictographs
  //   U+1F680-U+1F6FF  Transport and Map Symbols
  //   U+1F1E0-U+1F1FF  Regional Indicator Symbols (Flags)
  //   U+1F900-U+1F9FF  Supplemental Symbols and Pictographs
  //   U+1FA00-U+1FA6F  Chess Symbols
  //   U+1FA70-U+1FAFF  Symbols and Pictographs Extended-A
  //   U+2600-U+26FF    Misc Symbols
  //   U+2700-U+27BF    Dingbats
  //   U+FE00-U+FE0F    Variation Selectors
  //   U+200D           Zero Width Joiner
  //   U+20E3           Combining Enclosing Keycap
  //   U+E0020-U+E007F  Tags (flag sequences)
  const emojiPattern =
    /[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{1F900}-\u{1F9FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{FE00}-\u{FE0F}\u{200D}\u{20E3}\u{E0020}-\u{E007F}]/gu;

  return text
    .replace(emojiPattern, '')
    .replace(/ {2,}/g, ' ')
    .replace(/ +$/gm, '')
    .trim();
}

/**
 * Combined filter: strip markdown formatting and emoji from text.
 *
 * Intended as a convenience for the most common TTS pre-processing use case.
 */
export function filterForTTS(text: string): string {
  return filterEmoji(filterMarkdown(text));
}
