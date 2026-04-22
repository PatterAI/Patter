"""
Sentence chunker for streaming TTS in pipeline mode.

Accumulates streaming LLM tokens and yields complete sentences.
Uses regex-based marker replacement for robust sentence boundary
detection, handling abbreviations, acronyms, decimals, websites,
ellipsis, and CJK punctuation.

Algorithm adapted from LiveKit Agents (Apache 2.0):
https://github.com/livekit/agents
"""

from __future__ import annotations

import re

# Default minimum sentence length before emitting.
# Fragments shorter than this are merged with the next sentence.
DEFAULT_MIN_SENTENCE_LEN = 20


def _split_sentences(
    text: str,
    *,
    min_sentence_len: int = DEFAULT_MIN_SENTENCE_LEN,
) -> list[tuple[str, int, int]]:
    """Split text into sentences using regex marker replacement.

    Returns a list of (sentence, start_pos, end_pos) tuples.
    The text must not contain literal ``<prd>`` or ``<stop>`` substrings.
    """
    alphabets = r"([A-Za-z])"
    prefixes = r"(Mr|St|Mrs|Ms|Dr)[.]"
    suffixes = r"(Inc|Ltd|Jr|Sr|Co)"
    starters = (
        r"(Mr|Mrs|Ms|Dr|Prof|Capt|Cpt|Lt|He\s|She\s|It\s|They\s|Their\s|"
        r"Our\s|We\s|But\s|However\s|That\s|This\s|Wherever)"
    )
    acronyms = r"([A-Z][.][A-Z][.](?:[A-Z][.])?)"
    websites = r"[.](com|net|org|io|gov|edu|me)"
    digits = r"([0-9])"
    multiple_dots = r"\.{2,}"

    text = text.replace("\n", " ")

    text = re.sub(prefixes, r"\1<prd>", text)
    text = re.sub(websites, r"<prd>\1", text)
    text = re.sub(digits + r"[.]" + digits, r"\1<prd>\2", text)
    text = re.sub(multiple_dots, lambda m: "<prd>" * len(m.group(0)), text)

    if "Ph.D" in text:
        text = text.replace("Ph.D.", "Ph<prd>D<prd>")

    text = re.sub(r"\s" + alphabets + r"[.] ", r" \1<prd> ", text)
    text = re.sub(acronyms + r" " + starters, r"\1<stop> \2", text)
    text = re.sub(
        alphabets + r"[.]" + alphabets + r"[.]" + alphabets + r"[.]",
        r"\1<prd>\2<prd>\3<prd>",
        text,
    )
    text = re.sub(alphabets + r"[.]" + alphabets + r"[.]", r"\1<prd>\2<prd>", text)
    text = re.sub(r" " + suffixes + r"[.] " + starters, r" \1<stop> \2", text)
    text = re.sub(r" " + suffixes + r"[.]", r" \1<prd>", text)
    text = re.sub(r" " + alphabets + r"[.]", r" \1<prd>", text)

    # Mark sentence-ending punctuation (including CJK)
    text = re.sub(r'([.!?\u3002\uff01\uff1f])(["\u201d])', r"\1\2<stop>", text)
    text = re.sub(r'([.!?\u3002\uff01\uff1f])(?!["\u201d])', r"\1<stop>", text)

    # Restore periods
    text = text.replace("<prd>", ".")

    splitted = text.split("<stop>")
    text = text.replace("<stop>", "")

    sentences: list[tuple[str, int, int]] = []
    buff = ""
    start_pos = 0
    end_pos = 0

    for match in splitted:
        sentence = match.strip()
        if not sentence:
            continue

        buff += " " + sentence
        end_pos += len(match)

        if len(buff) > min_sentence_len:
            sentences.append((buff.lstrip(), start_pos, end_pos))
            start_pos = end_pos
            buff = ""

    if buff:
        sentences.append((buff.lstrip(), start_pos, len(text) - 1))

    return sentences


class SentenceChunker:
    """Accumulates streaming tokens and yields complete sentences.

    Usage::

        chunker = SentenceChunker()
        for token in llm_stream:
            for sentence in chunker.push(token):
                await tts.synthesize(sentence)
        for sentence in chunker.flush():
            await tts.synthesize(sentence)
    """

    def __init__(self, *, min_sentence_len: int = DEFAULT_MIN_SENTENCE_LEN) -> None:
        self._buffer = ""
        self._min_sentence_len = min_sentence_len

    def push(self, token: str) -> list[str]:
        """Feed a token. Returns zero or more complete sentences."""
        self._buffer += token

        if len(self._buffer) < self._min_sentence_len:
            return []

        sentences = _split_sentences(
            self._buffer, min_sentence_len=self._min_sentence_len
        )

        if len(sentences) <= 1:
            return []

        # Emit all sentences except the last (which may be incomplete)
        result: list[str] = []
        for sent_text, _, _ in sentences[:-1]:
            if sent_text.strip():
                result.append(sent_text.strip())

        # Keep the last (potentially incomplete) sentence in the buffer
        last_text = sentences[-1][0] if sentences else ""
        self._buffer = last_text

        return result

    def flush(self) -> list[str]:
        """Flush remaining buffer as final sentence(s). Call at end of stream."""
        remaining = self._buffer.strip()
        self._buffer = ""

        if not remaining:
            return []

        return [remaining]

    def reset(self) -> None:
        """Discard buffered text. Call on interrupt."""
        self._buffer = ""
