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

# Minimum word count for emitting a "short" sentence (one whose total length
# is below ``min_sentence_len``) as soon as a terminator is seen. This avoids
# emitting standalone single-word utterances ("Sì.", "Yes.") while still
# letting short greetings ("Hi there!") flush immediately for low TTS TTFB.
DEFAULT_MIN_WORDS_FOR_SHORT_FLUSH = 2

# Sentence-terminating characters (Latin + CJK).
_SENTENCE_TERMINATORS = ".!?。！？"


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

    def __init__(
        self,
        *,
        min_sentence_len: int = DEFAULT_MIN_SENTENCE_LEN,
        min_words_for_short_flush: int = DEFAULT_MIN_WORDS_FOR_SHORT_FLUSH,
    ) -> None:
        self._buffer = ""
        self._min_sentence_len = min_sentence_len
        self._min_words_for_short_flush = min_words_for_short_flush

    def push(self, token: str) -> list[str]:
        """Feed a token. Returns zero or more complete sentences.

        Two emission paths:

        * **Standard path** — when the buffer is at least ``min_sentence_len``
          characters long and the regex tokenizer reports more than one
          sentence, all but the last (potentially incomplete) sentence are
          emitted. This is the LiveKit-derived behaviour.
        * **Short-flush path** — when the buffer is shorter than
          ``min_sentence_len`` but ends with a sentence terminator AND the
          preceding text has at least ``min_words_for_short_flush`` words
          (default 2), emit it immediately. This drops TTS TTFB on short
          greetings like ``"Hi there!"`` while keeping single-word
          utterances (``"Sì."``) buffered until ``flush()``.
        """
        self._buffer += token

        if len(self._buffer) < self._min_sentence_len:
            return self._maybe_short_flush()

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

    def _maybe_short_flush(self) -> list[str]:
        """Emit the buffer when it's a short, complete single-sentence utterance.

        A buffer qualifies when **all** of these hold:

        1. Last non-whitespace char is a sentence terminator.
        2. Word count is at least ``min_words_for_short_flush`` (default 2 —
           keeps single-word "Sì." / "Yes." buffered until ``flush()``).
        3. The buffer contains exactly one terminator (the trailing one).
           Multiple terminators mean we may be mid-stream of a longer merged
           utterance like ``"Hey! Hi! Hello! This is a sentence."`` — let
           the standard path keep merging.
        4. The char immediately before the terminator is **not** a digit
           (avoids decimal mid-stream like ``"f(x) = x * 2."`` flushing
           before the ``54`` arrives).
        5. The char immediately before the terminator is **not** an
           uppercase letter (avoids acronym patterns like ``"U.S."`` /
           ``"U."`` flushing prematurely).

        Together these gates preserve the LiveKit-derived merging behaviour
        of the standard path while letting genuine short greetings flush
        immediately for low TTS TTFB.
        """
        stripped = self._buffer.rstrip()
        if not stripped or stripped[-1] not in _SENTENCE_TERMINATORS:
            return []

        # Only one terminator in the entire buffer (the trailing one).
        if sum(1 for c in stripped if c in _SENTENCE_TERMINATORS) != 1:
            return []

        # Word count: ``"Hi there!".split()`` -> 2.
        word_count = len(stripped.split())
        if word_count < self._min_words_for_short_flush:
            return []

        # Don't flush on potential decimals or acronyms.
        if len(stripped) >= 2:
            prev = stripped[-2]
            if prev.isdigit() or (prev.isascii() and prev.isupper()):
                return []

        self._buffer = ""
        return [stripped]

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
