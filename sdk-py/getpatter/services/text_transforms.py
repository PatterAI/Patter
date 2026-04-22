"""Built-in text transforms for cleaning LLM output before TTS synthesis.

These functions strip markdown formatting and emoji characters so that TTS
engines produce natural-sounding speech rather than reading aloud syntax
like "asterisk asterisk bold asterisk asterisk" or Unicode pictographs.
"""

from __future__ import annotations

import re


def filter_markdown(text: str) -> str:
    """Remove markdown formatting from *text*, preserving readable content.

    Handles: headers, bold, italic, code blocks/inline, links, images,
    strikethrough, list markers, block quotes, horizontal rules, HTML tags.
    """
    result = text

    # Fenced code blocks: ```lang\ncode\n``` -> code
    def _strip_fence(m: re.Match) -> str:
        inner = re.sub(r"^```[^\n]*\n?", "", m.group(0))
        inner = re.sub(r"\n?```$", "", inner)
        return inner

    result = re.sub(r"```[\s\S]*?```", _strip_fence, result)

    # Inline code: `code` -> code
    result = re.sub(r"`([^`]+)`", r"\1", result)

    # Images: ![alt](url) -> alt
    result = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", result)

    # Links: [text](url) -> text
    result = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", result)

    # Strikethrough: ~~text~~ -> text
    result = re.sub(r"~~(.*?)~~", r"\1", result)

    # Headers: # text -> text (at start of line)
    result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)

    # Horizontal rules: ---, ***, ___ (standalone line) -- must come before bold/italic
    result = re.sub(r"^[-*_]{3,}\s*$", "", result, flags=re.MULTILINE)

    # Bold: **text** or __text__ -> text (must come before italic)
    result = re.sub(r"\*\*(.+?)\*\*", r"\1", result)
    result = re.sub(r"__(.+?)__", r"\1", result)

    # Italic: *text* or _text_ -> text
    result = re.sub(r"\*(.+?)\*", r"\1", result)
    result = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", result)

    # Block quotes: > text -> text (at start of line)
    result = re.sub(r"^>\s+", "", result, flags=re.MULTILINE)

    # Unordered list markers: - item, * item -> item (at start of line)
    result = re.sub(r"^[-*]\s+", "", result, flags=re.MULTILINE)

    # Ordered list markers: 1. item -> item (at start of line)
    result = re.sub(r"^\d+\.\s+", "", result, flags=re.MULTILINE)

    # HTML tags: <tag> or </tag> -> empty
    result = re.sub(r"</?[^>]+(>|$)", "", result)

    # Collapse multiple blank lines into one
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result.strip()


# Precompiled emoji removal pattern covering major Unicode emoji blocks.
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # Emoticons
    "\U0001F300-\U0001F5FF"  # Misc Symbols and Pictographs
    "\U0001F680-\U0001F6FF"  # Transport and Map Symbols
    "\U0001F1E0-\U0001F1FF"  # Regional Indicator Symbols (Flags)
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
    "\U0001FA00-\U0001FA6F"  # Chess Symbols
    "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
    "\u2600-\u26FF"  # Misc Symbols
    "\u2700-\u27BF"  # Dingbats
    "\uFE00-\uFE0F"  # Variation Selectors
    "\u200D"  # Zero Width Joiner
    "\u20E3"  # Combining Enclosing Keycap
    "\U000E0020-\U000E007F"  # Tags (flag sequences)
    "]+",
    flags=re.UNICODE,
)


def filter_emoji(text: str) -> str:
    """Remove emoji characters from *text*.

    Preserves normal text, punctuation, and non-emoji Unicode (CJK, accented
    characters, etc.).
    """
    result = _EMOJI_PATTERN.sub("", text)
    # Collapse leftover double-spaces from removed emoji
    result = re.sub(r" {2,}", " ", result)
    # Trim trailing spaces per line (emoji at end of line leaves a space)
    result = re.sub(r" +$", "", result, flags=re.MULTILINE)
    return result.strip()


def filter_for_tts(text: str) -> str:
    """Combined filter: strip markdown formatting and emoji from text.

    Intended as a convenience for the most common TTS pre-processing use case.
    """
    return filter_emoji(filter_markdown(text))
