"""Built-in text transforms for cleaning LLM output before TTS synthesis.

These functions strip markdown formatting and emoji characters so that TTS
engines produce natural-sounding speech rather than reading aloud syntax
like "asterisk asterisk bold asterisk asterisk" or Unicode pictographs.
"""

from __future__ import annotations

import re

# Markdown patterns are precompiled once at module import time. filter_markdown
# is invoked per sentence on every LLM streaming turn, so paying compile cost
# upfront avoids ~14 re.compile invocations per sentence.
_FENCED_CODE = re.compile(r"```[\s\S]*?```")
_FENCE_OPEN = re.compile(r"^```[^\n]*\n?")
_FENCE_CLOSE = re.compile(r"\n?```$")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_STRIKE = re.compile(r"~~(.*?)~~")
_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_HRULE = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDER = re.compile(r"__(.+?)__")
_ITALIC_STAR = re.compile(r"\*(.+?)\*")
_ITALIC_UNDER = re.compile(r"(?<!\w)_(.+?)_(?!\w)")
_BLOCK_QUOTE = re.compile(r"^>\s+", re.MULTILINE)
_LIST_UNORDERED = re.compile(r"^[-*]\s+", re.MULTILINE)
_LIST_ORDERED = re.compile(r"^\d+\.\s+", re.MULTILINE)
_HTML_TAG = re.compile(r"</?[^>]+(>|$)")
_BLANK_LINES = re.compile(r"\n{3,}")


def filter_markdown(text: str) -> str:
    """Remove markdown formatting from *text*, preserving readable content.

    Handles: headers, bold, italic, code blocks/inline, links, images,
    strikethrough, list markers, block quotes, horizontal rules, HTML tags.
    """

    def _strip_fence(m: re.Match) -> str:
        inner = _FENCE_OPEN.sub("", m.group(0))
        inner = _FENCE_CLOSE.sub("", inner)
        return inner

    result = _FENCED_CODE.sub(_strip_fence, text)
    result = _INLINE_CODE.sub(r"\1", result)
    result = _IMAGE.sub(r"\1", result)
    result = _LINK.sub(r"\1", result)
    result = _STRIKE.sub(r"\1", result)
    result = _HEADER.sub("", result)
    # Horizontal rules must come before bold/italic so triple-dash dividers
    # are stripped before being misread as emphasis markers.
    result = _HRULE.sub("", result)
    result = _BOLD_STAR.sub(r"\1", result)
    result = _BOLD_UNDER.sub(r"\1", result)
    result = _ITALIC_STAR.sub(r"\1", result)
    result = _ITALIC_UNDER.sub(r"\1", result)
    result = _BLOCK_QUOTE.sub("", result)
    result = _LIST_UNORDERED.sub("", result)
    result = _LIST_ORDERED.sub("", result)
    result = _HTML_TAG.sub("", result)
    result = _BLANK_LINES.sub("\n\n", result)
    return result.strip()


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # Emoticons
    "\U0001f300-\U0001f5ff"  # Misc Symbols and Pictographs
    "\U0001f680-\U0001f6ff"  # Transport and Map Symbols
    "\U0001f1e0-\U0001f1ff"  # Regional Indicator Symbols (Flags)
    "\U0001f900-\U0001f9ff"  # Supplemental Symbols and Pictographs
    "\U0001fa00-\U0001fa6f"  # Chess Symbols
    "\U0001fa70-\U0001faff"  # Symbols and Pictographs Extended-A
    "☀-⛿"  # Misc Symbols
    "✀-➿"  # Dingbats
    "︀-️"  # Variation Selectors
    "‍"  # Zero Width Joiner
    "⃣"  # Combining Enclosing Keycap
    "\U000e0020-\U000e007f"  # Tags (flag sequences)
    "]+",
    flags=re.UNICODE,
)
_DOUBLE_SPACE = re.compile(r" {2,}")
_TRAILING_SPACE = re.compile(r" +$", re.MULTILINE)


def filter_emoji(text: str) -> str:
    """Remove emoji characters from *text*.

    Preserves normal text, punctuation, and non-emoji Unicode (CJK, accented
    characters, etc.).
    """
    result = _EMOJI_PATTERN.sub("", text)
    result = _DOUBLE_SPACE.sub(" ", result)
    result = _TRAILING_SPACE.sub("", result)
    return result.strip()


def filter_for_tts(text: str) -> str:
    """Combined filter: strip markdown formatting and emoji from text.

    Intended as a convenience for the most common TTS pre-processing use case.
    """
    return filter_emoji(filter_markdown(text))
