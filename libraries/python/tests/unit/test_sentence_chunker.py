"""Unit tests for getpatter.services.sentence_chunker — SentenceChunker and _split_sentences."""

from __future__ import annotations

import pytest

from getpatter.services.sentence_chunker import (
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
    _split_sentences,
)


# ---------------------------------------------------------------------------
# Full reference text + expected output for sentence-boundary detection.
# ---------------------------------------------------------------------------

TEXT = (
    "Hi! "
    "Patter is a platform for live audio and video applications and services. \n\n"
    "R.T.C stands for Real-Time Communication... again R.T.C. "
    "Mr. Theo is testing the sentence tokenizer. "
    "\nThis is a test. Another test. "
    "A short sentence.\n"
    "A longer sentence that is longer than the previous sentence. "
    "f(x) = x * 2.54 + 42. "
    "Hey!\n Hi! Hello! "
    "\n\n"
    "This is a sentence. 这是一个中文句子。これは日本語の文章です。"
    "你好！Patter是一个直播音频和视频应用程序和服务的平台。"
    "\nThis is a sentence contains   consecutive spaces."
)

EXPECTED_MIN_20 = [
    "Hi! Patter is a platform for live audio and video applications and services.",
    "R.T.C stands for Real-Time Communication... again R.T.C.",
    "Mr. Theo is testing the sentence tokenizer.",
    "This is a test. Another test.",
    "A short sentence. A longer sentence that is longer than the previous sentence.",
    "f(x) = x * 2.54 + 42.",
    "Hey! Hi! Hello! This is a sentence.",
    "这是一个中文句子。 これは日本語の文章です。",
    "你好！ Patter是一个直播音频和视频应用程序和服务的平台。",
    "This is a sentence contains   consecutive spaces.",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sentences_only(
    text: str, min_sentence_len: int = DEFAULT_MIN_SENTENCE_LEN
) -> list[str]:
    """Return only the sentence strings from _split_sentences."""
    return [s for s, _, _ in _split_sentences(text, min_sentence_len=min_sentence_len)]


def _chunker_all(
    tokens: list[str], min_sentence_len: int = DEFAULT_MIN_SENTENCE_LEN
) -> list[str]:
    """Feed all tokens through SentenceChunker and collect every sentence."""
    chunker = SentenceChunker(min_sentence_len=min_sentence_len)
    results: list[str] = []
    for token in tokens:
        results.extend(chunker.push(token))
    results.extend(chunker.flush())
    return results


# ===========================================================================
# _split_sentences — low-level function tests
# ===========================================================================


@pytest.mark.unit
class TestSplitSentencesBasic:
    """Basic sentence boundary detection."""

    def test_period_splits(self) -> None:
        result = _sentences_only("Hello world. How are you.", min_sentence_len=1)
        assert "Hello world." in result
        assert "How are you." in result

    def test_exclamation_splits(self) -> None:
        result = _sentences_only("Hello! World!", min_sentence_len=1)
        assert "Hello!" in result
        assert "World!" in result

    def test_question_mark_splits(self) -> None:
        result = _sentences_only("How are you? Fine.", min_sentence_len=1)
        assert "How are you?" in result

    def test_multiple_terminators(self) -> None:
        result = _sentences_only("A. B! C?", min_sentence_len=1)
        assert len(result) == 3

    def test_empty_string_returns_empty(self) -> None:
        assert _sentences_only("") == []

    def test_whitespace_only_returns_empty(self) -> None:
        assert _sentences_only("   ") == []
        assert _sentences_only("\n\n\n") == []


@pytest.mark.unit
class TestSplitSentencesAbbreviations:
    """Abbreviation handling — should NOT split on these periods."""

    def test_mr_not_split(self) -> None:
        result = _sentences_only("Mr. Theo is testing.", min_sentence_len=1)
        # "Mr. Theo is testing." must come out as a single sentence
        assert len(result) == 1
        assert "Mr. Theo is testing." in result[0]

    def test_dr_not_split(self) -> None:
        result = _sentences_only("Dr. Smith is here.", min_sentence_len=1)
        assert len(result) == 1
        assert "Dr. Smith is here." in result[0]

    def test_mrs_not_split(self) -> None:
        result = _sentences_only("Mrs. Jones replied.", min_sentence_len=1)
        assert len(result) == 1

    def test_ms_not_split(self) -> None:
        result = _sentences_only("Ms. Lee arrived.", min_sentence_len=1)
        assert len(result) == 1

    def test_phd_not_split(self) -> None:
        result = _sentences_only("She has a Ph.D. in physics.", min_sentence_len=1)
        assert len(result) == 1
        assert "Ph.D." in result[0]


@pytest.mark.unit
class TestSplitSentencesDecimals:
    """Decimal numbers should NOT split."""

    def test_decimal_not_split(self) -> None:
        result = _sentences_only("The value is 3.14 dollars.", min_sentence_len=1)
        assert len(result) == 1
        assert "3.14" in result[0]

    def test_decimal_in_expression(self) -> None:
        result = _sentences_only("f(x) = x * 2.54 + 42.", min_sentence_len=1)
        assert len(result) == 1
        assert "2.54" in result[0]

    def test_multiple_decimals(self) -> None:
        result = _sentences_only("Rates are 1.5 and 2.7 percent.", min_sentence_len=1)
        assert len(result) == 1


@pytest.mark.unit
class TestSplitSentencesWebsites:
    """Website TLDs should NOT split."""

    def test_com_not_split(self) -> None:
        result = _sentences_only("Visit example.com today.", min_sentence_len=1)
        assert len(result) == 1
        assert "example.com" in result[0]

    def test_org_not_split(self) -> None:
        result = _sentences_only("See ietf.org for details.", min_sentence_len=1)
        assert len(result) == 1

    def test_io_not_split(self) -> None:
        result = _sentences_only("Check getpatter.io out.", min_sentence_len=1)
        assert len(result) == 1


@pytest.mark.unit
class TestSplitSentencesAcronyms:
    """Acronym periods should NOT cause splits."""

    def test_rtc_acronym_not_split(self) -> None:
        text = "R.T.C stands for Real-Time Communication... again R.T.C."
        result = _sentences_only(text, min_sentence_len=1)
        # The whole thing is one sentence
        assert len(result) == 1
        assert "R.T.C" in result[0]

    def test_two_letter_acronym_not_split(self) -> None:
        result = _sentences_only("The U.S. is large.", min_sentence_len=1)
        assert len(result) == 1

    def test_three_letter_acronym_not_split(self) -> None:
        result = _sentences_only("U.S.A. leads the way.", min_sentence_len=1)
        assert len(result) == 1


@pytest.mark.unit
class TestSplitSentencesEllipsis:
    """Ellipsis (2+ dots) should NOT split as separate sentences."""

    def test_ellipsis_not_split(self) -> None:
        result = _sentences_only("Wait... really?", min_sentence_len=1)
        # "..." should not produce empty fragment sentences
        texts = " ".join(result)
        assert "..." in texts

    def test_ellipsis_in_longer_text(self) -> None:
        text = "R.T.C stands for Real-Time Communication... again R.T.C."
        result = _sentences_only(text, min_sentence_len=1)
        assert len(result) == 1


@pytest.mark.unit
class TestSplitSentencesCJK:
    """CJK terminators (。！？) should trigger sentence splits."""

    def test_chinese_period(self) -> None:
        result = _sentences_only("这是一个中文句子。这是另一个。", min_sentence_len=1)
        assert len(result) == 2

    def test_fullwidth_exclamation(self) -> None:
        result = _sentences_only("你好！再见！", min_sentence_len=1)
        assert len(result) == 2

    def test_fullwidth_question(self) -> None:
        result = _sentences_only("这是什么？那是什么？", min_sentence_len=1)
        assert len(result) == 2

    def test_mixed_latin_cjk(self) -> None:
        result = _sentences_only(
            "This is a sentence. 这是一个中文句子。", min_sentence_len=1
        )
        assert len(result) == 2


@pytest.mark.unit
class TestSplitSentencesMinLen:
    """min_sentence_len merges short fragments into the next sentence."""

    def test_short_fragments_merged(self) -> None:
        # "Hi!" is 3 chars — below min_sentence_len=20, merges with next
        result = _sentences_only("Hi! How are you today?", min_sentence_len=20)
        assert len(result) == 1
        joined = result[0]
        assert "Hi!" in joined
        assert "How are you today?" in joined

    def test_fragments_at_boundary(self) -> None:
        # Fragments exactly at min_sentence_len should pass through
        # "Exactly twenty chars." is 21 chars — emitted as its own sentence
        result = _sentences_only(
            "Exactly twenty chars. Next sentence here.", min_sentence_len=20
        )
        assert len(result) >= 1

    def test_min_len_zero_no_merging(self) -> None:
        # With min_sentence_len=0 every sentence stands alone
        result = _sentences_only("Hi! Hello. Bye!", min_sentence_len=0)
        assert len(result) == 3


# ===========================================================================
# SentenceChunker — public API tests
# ===========================================================================


@pytest.mark.unit
class TestSentenceChunkerPush:
    """SentenceChunker.push() accumulates tokens and emits complete sentences."""

    def test_short_tokens_no_emit(self) -> None:
        chunker = SentenceChunker()
        # None of these alone should trigger emission
        assert chunker.push("Hello") == []
        assert chunker.push(" world") == []

    def test_emits_on_sentence_boundary(self) -> None:
        chunker = SentenceChunker(min_sentence_len=1)
        result: list[str] = []
        for token in ["Hello world. ", "How are you?"]:
            result.extend(chunker.push(token))
        result.extend(chunker.flush())
        assert "Hello world." in result[0]

    def test_does_not_emit_last_incomplete(self) -> None:
        chunker = SentenceChunker(min_sentence_len=1)
        # After "Hello.", the second fragment "World" is still in the buffer
        sentences = chunker.push("Hello. World")
        assert len(sentences) == 1
        assert "Hello." in sentences[0]
        # "World" stays buffered
        assert chunker._buffer == "World"

    def test_empty_push_returns_empty(self) -> None:
        chunker = SentenceChunker()
        assert chunker.push("") == []

    def test_multiple_sentences_in_one_push(self) -> None:
        chunker = SentenceChunker(min_sentence_len=1)
        result = chunker.push("First. Second. Third. ")
        result.extend(chunker.flush())
        assert len(result) >= 2


@pytest.mark.unit
class TestSentenceChunkerFlush:
    """SentenceChunker.flush() returns and clears remaining buffer."""

    def test_flush_returns_remaining(self) -> None:
        chunker = SentenceChunker(min_sentence_len=1)
        chunker.push("Incomplete sentence")
        result = chunker.flush()
        assert result == ["Incomplete sentence"]

    def test_flush_clears_buffer(self) -> None:
        chunker = SentenceChunker()
        chunker.push("Something")
        chunker.flush()
        assert chunker._buffer == ""

    def test_flush_empty_buffer_returns_empty(self) -> None:
        chunker = SentenceChunker()
        assert chunker.flush() == []

    def test_flush_after_flush_is_empty(self) -> None:
        chunker = SentenceChunker()
        chunker.push("Hello")
        chunker.flush()
        assert chunker.flush() == []

    def test_flush_strips_whitespace(self) -> None:
        chunker = SentenceChunker()
        chunker.push("  trailing spaces  ")
        result = chunker.flush()
        assert result == ["trailing spaces"]


@pytest.mark.unit
class TestSentenceChunkerReset:
    """SentenceChunker.reset() discards the buffer without returning anything."""

    def test_reset_clears_buffer(self) -> None:
        chunker = SentenceChunker()
        chunker.push("Some text in the buffer")
        chunker.reset()
        assert chunker._buffer == ""

    def test_flush_after_reset_is_empty(self) -> None:
        chunker = SentenceChunker()
        chunker.push("Some text")
        chunker.reset()
        assert chunker.flush() == []

    def test_reset_allows_reuse(self) -> None:
        chunker = SentenceChunker(min_sentence_len=1)
        chunker.push("Old content.")
        chunker.reset()
        chunker.push("New sentence.")
        result = chunker.flush()
        assert result == ["New sentence."]
        assert "Old content" not in result[0]


@pytest.mark.unit
class TestSentenceChunkerStreaming:
    """Simulate token-by-token LLM streaming behaviour."""

    def test_single_char_tokens(self) -> None:
        text = "Hello world. How are you?"
        tokens = list(text)  # one char at a time
        result = _chunker_all(tokens, min_sentence_len=1)
        assert len(result) >= 2
        full = " ".join(result)
        assert "Hello world." in full
        assert "How are you?" in full

    def test_word_by_word_tokens(self) -> None:
        words = ["Hello ", "world. ", "How ", "are ", "you?"]
        result = _chunker_all(words, min_sentence_len=1)
        assert len(result) == 2

    def test_sentences_preserved_across_word_tokens(self) -> None:
        tokens = ["The quick brown fox. ", "Jumped over the lazy dog."]
        result = _chunker_all(tokens, min_sentence_len=1)
        assert any("The quick brown fox." in s for s in result)
        assert any("Jumped over the lazy dog." in s for s in result)

    def test_no_sentences_lost_in_streaming(self) -> None:
        # All tokens together should produce the same text as bulk
        full_text = "First sentence. Second sentence. Third sentence."
        # Bulk
        bulk = _chunker_all([full_text], min_sentence_len=1)
        # Streaming
        streaming = _chunker_all(list(full_text), min_sentence_len=1)
        assert "".join(bulk) == "".join(streaming)

    def test_chunker_stateful_across_pushes(self) -> None:
        chunker = SentenceChunker(min_sentence_len=1)
        # Build a sentence across many tiny pushes
        for ch in "Hello":
            chunker.push(ch)
        for ch in " world.":
            chunker.push(ch)
        # Nothing emitted yet because min_sentence_len=1 still needs a second sentence
        result = chunker.flush()
        assert "Hello world." in result[0]


@pytest.mark.unit
class TestSentenceChunkerEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_string_push(self) -> None:
        chunker = SentenceChunker()
        assert chunker.push("") == []

    def test_only_whitespace_push(self) -> None:
        chunker = SentenceChunker()
        assert chunker.push("   ") == []

    def test_newlines_in_buffer_preserved_by_flush(self) -> None:
        # flush() returns the raw buffer — newlines are not converted at that stage.
        # _split_sentences converts them internally, but flush() bypasses that path.
        chunker = SentenceChunker(min_sentence_len=1)
        chunker.push("Hello\nworld.")
        result = chunker.flush()
        assert result
        # The raw buffer text is returned; "Hello" and "world." are present
        assert "Hello" in result[0]
        assert "world." in result[0]

    def test_unicode_content(self) -> None:
        chunker = SentenceChunker(min_sentence_len=1)
        result = _chunker_all(["Café is nice. Merci!"], min_sentence_len=1)
        assert len(result) == 2

    def test_very_long_sentence(self) -> None:
        long_sentence = "word " * 1000 + "."
        result = _chunker_all([long_sentence], min_sentence_len=20)
        assert len(result) == 1
        assert result[0].endswith(".")

    def test_default_min_sentence_len(self) -> None:
        chunker = SentenceChunker()
        assert chunker._min_sentence_len == DEFAULT_MIN_SENTENCE_LEN

    def test_custom_min_sentence_len(self) -> None:
        chunker = SentenceChunker(min_sentence_len=5)
        assert chunker._min_sentence_len == 5

    def test_consecutive_spaces_preserved(self) -> None:
        result = _chunker_all(
            ["This is a sentence contains   consecutive spaces."], min_sentence_len=1
        )
        assert len(result) == 1
        assert "   " in result[0]


# ===========================================================================
# Short-flush path — TTS TTFB optimisation for short greetings
# ===========================================================================


@pytest.mark.unit
class TestSentenceChunkerShortFlush:
    """The short-flush path emits a complete short utterance as soon as a
    sentence terminator is seen — provided the buffer has at least N words
    (default 2) and is not a potential abbreviation/decimal continuation."""

    def test_short_greeting_emits_immediately(self) -> None:
        """``"Hi there!"`` is 9 chars (< default 20) — must flush right away."""
        chunker = SentenceChunker()
        result = chunker.push("Hi there!")
        assert result == ["Hi there!"]
        # Buffer is consumed.
        assert chunker._buffer == ""

    def test_two_word_period_greeting_emits(self) -> None:
        chunker = SentenceChunker()
        result = chunker.push("Hello world.")
        assert result == ["Hello world."]

    def test_two_word_question_emits(self) -> None:
        chunker = SentenceChunker()
        result = chunker.push("Are you?")
        assert result == ["Are you?"]

    def test_single_word_flushes_on_terminator(self) -> None:
        """Single-word utterances flush immediately on the terminator
        (Bug #49 — see DEFAULT_MIN_WORDS_FOR_SHORT_FLUSH=1).

        For phone calls a one-word LLM reply ("Sì.", "Yes.", "Done.") must
        reach TTS without waiting for the caller to invoke ``flush()``.
        """
        chunker = SentenceChunker()
        assert chunker.push("Sì.") == ["Sì."]
        assert chunker._buffer == ""

    def test_yes_alone_flushes_on_terminator(self) -> None:
        """Bug #49 — ``"Yes."`` is the entire reply and must flush now."""
        chunker = SentenceChunker()
        assert chunker.push("Yes.") == ["Yes."]
        assert chunker._buffer == ""

    def test_done_alone_flushes_on_terminator(self) -> None:
        """Bug #49 — ``"Done!"`` is the entire reply and must flush now."""
        chunker = SentenceChunker()
        assert chunker.push("Done!") == ["Done!"]

    def test_question_single_word_flushes(self) -> None:
        """Bug #49 — single-word question still flushes on ``?``."""
        chunker = SentenceChunker()
        assert chunker.push("Really?") == ["Really?"]

    def test_legacy_min_words_two_keeps_single_words_buffered(self) -> None:
        """Setting ``min_words_for_short_flush=2`` restores the pre-Bug-#49
        behaviour where single-word utterances stay buffered until ``flush()``.
        """
        chunker = SentenceChunker(min_words_for_short_flush=2)
        assert chunker.push("Sì.") == []
        assert chunker._buffer == "Sì."
        assert chunker.flush() == ["Sì."]

    def test_no_terminator_does_not_flush(self) -> None:
        chunker = SentenceChunker()
        assert chunker.push("Hi there") == []
        assert chunker._buffer == "Hi there"

    def test_decimal_does_not_flush_mid_stream(self) -> None:
        """``"f(x) = 2."`` must NOT flush — the next char might be a digit."""
        chunker = SentenceChunker()
        assert chunker.push("f(x) = 2.") == []

    def test_acronym_does_not_flush_mid_stream(self) -> None:
        """``"The U.S."`` must NOT flush — could be inside an acronym/sentence."""
        chunker = SentenceChunker()
        assert chunker.push("The U.S.") == []

    def test_multiple_terminators_does_not_flush(self) -> None:
        """``"Hey! Hi!"`` has two terminators — defer to the standard path."""
        chunker = SentenceChunker()
        assert chunker.push("Hey! Hi!") == []

    def test_min_words_for_short_flush_configurable(self) -> None:
        """Setting the threshold to 1 lets ``"Yes."`` flush immediately."""
        chunker = SentenceChunker(min_words_for_short_flush=1)
        assert chunker.push("Yes.") == ["Yes."]

    def test_min_words_for_short_flush_three_blocks_two_words(self) -> None:
        """Setting the threshold to 3 keeps two-word greetings buffered."""
        chunker = SentenceChunker(min_words_for_short_flush=3)
        assert chunker.push("Hi there!") == []
        assert chunker.push(" Goodbye.") == []
        # Now buffer is "Hi there! Goodbye." — first sentence emits.
        result = chunker.push(" Done.")
        # Should have emitted at least one sentence by now.
        assert any("Hi there!" in s for s in result + chunker.flush())

    def test_short_flush_does_not_break_on_trailing_whitespace(self) -> None:
        """``"Hi there!  \n"`` — terminator hidden after whitespace must still flush."""
        chunker = SentenceChunker()
        result = chunker.push("Hi there!  \n")
        assert result == ["Hi there!"]


# ===========================================================================
# Full reference text — sentence boundary detection over a long fixture
# ===========================================================================


@pytest.mark.unit
class TestFullReferenceText:
    """Full reference test exercising sentence boundary detection."""

    def test_full_text_bulk(self) -> None:
        """Feed entire TEXT at once and verify the output matches EXPECTED_MIN_20."""
        result = _chunker_all([TEXT], min_sentence_len=20)
        assert result == EXPECTED_MIN_20

    def test_full_text_word_by_word(self) -> None:
        """Feed TEXT word-by-word and verify content + count is plausible.

        When text is split into individual word tokens (word + trailing space),
        the SentenceChunker stores stripped fragments as the residual buffer.
        If a fragment ends with a period (e.g. "Mr.") and the next token
        starts with a letter (e.g. "Theo "), the concatenation produces
        "Mr.Theo " — the inter-word space is lost.  This is a documented
        boundary artefact of the regex-marker algorithm when operating on
        short partial buffers.

        With the short-flush path defaulting to ``min_words=1`` (Bug #49),
        streaming may emit single-word and short multi-word sentences ("Hi!",
        "Hey!", "This is a test.") as soon as their terminator arrives. Count
        may diverge significantly from the bulk reference; content equivalence
        is the strict check via ``content_markers``.
        """
        import re as _re

        normalised = _re.sub(r"\s+", " ", TEXT).strip()
        tokens = [w + " " for w in normalised.split(" ") if w]
        result = _chunker_all(tokens, min_sentence_len=20)
        # Streaming with single-word short-flush emits more sentences; the
        # important invariants are content preservation and no-loss.
        assert len(result) >= len(EXPECTED_MIN_20)
        joined = " ".join(result)
        # Content that must be present (spacing-independent checks)
        content_markers = [
            "Patter is a platform",
            "Real-Time Communication",
            "Theo is testing",  # "Mr." may be concatenated without space
            "is a test",
            "short sentence",
            "2.54",
            "Hey!",
            "这是一个中文句子",
            "你好",
        ]
        for marker in content_markers:
            assert marker in joined, f"Expected content not found: {marker!r}"

    def test_full_text_char_by_char(self) -> None:
        """Feed TEXT one character at a time — most demanding streaming scenario.

        With Bug #49's single-word short-flush default, the count may exceed
        ``EXPECTED_MIN_20`` significantly. We validate content equivalence
        (after whitespace normalisation) rather than the exact split.
        """
        result = _chunker_all(list(TEXT), min_sentence_len=20)
        assert len(result) >= len(EXPECTED_MIN_20)
        # Content must be equivalent to the bulk reference
        import re as _re

        normalise = lambda s: _re.sub(r"\s+", " ", s).strip()
        assert normalise(" ".join(result)) == normalise(" ".join(EXPECTED_MIN_20))

    def test_sentence_count(self) -> None:
        result = _chunker_all([TEXT], min_sentence_len=20)
        assert len(result) == len(EXPECTED_MIN_20)

    def test_no_empty_sentences(self) -> None:
        result = _chunker_all([TEXT], min_sentence_len=20)
        for sentence in result:
            assert sentence.strip() != ""

    def test_all_content_preserved(self) -> None:
        """Every word from the expected output appears in the actual output."""
        result = _chunker_all([TEXT], min_sentence_len=20)
        joined_result = " ".join(result)
        joined_expected = " ".join(EXPECTED_MIN_20)
        # Both joined forms should carry the same non-whitespace content
        import re

        normalise = lambda s: re.sub(r"\s+", " ", s).strip()
        assert normalise(joined_result) == normalise(joined_expected)


# ---------------------------------------------------------------------------
# Aggressive first-clause flush (opt-in)
# ---------------------------------------------------------------------------


class TestAggressiveFirstFlush:
    """Phase 2 opt-in feature: emit the first clause of the response on a soft
    punctuation boundary to minimise TTFA. All tests assume English; Italian
    is hard-disabled regardless of the flag.
    """

    def test_default_off_invariant(self) -> None:
        """Default constructor must not change behaviour."""
        c = SentenceChunker()
        out: list[str] = []
        for t in ["Sure, ", "I can ", "definitely ", "help ", "you ", "now."]:
            out.extend(c.push(t))
        out.extend(c.flush())
        assert out == ["Sure, I can definitely help you now."]

    def test_aggressive_flush_fires_after_first_comma(self) -> None:
        c = SentenceChunker(aggressive_first_flush=True)
        emitted_during_push: list[str] = []
        tokens = [
            "Sure, ",
            "I can ",
            "definitely ",
            "help ",
            "you ",
            "with ",
            "that ",
            "request",
            ", ",
            "right ",
            "away.",
        ]
        for t in tokens:
            emitted_during_push.extend(c.push(t))
        emitted_during_push.extend(c.flush())
        # Aggressive flush should produce TWO emissions: clause + final period.
        assert len(emitted_during_push) == 2
        assert emitted_during_push[0].endswith(",")
        assert emitted_during_push[1] == "right away."

    def test_aggressive_only_fires_for_first_sentence(self) -> None:
        """Subsequent sentences must use the standard period boundary, not soft punct."""
        c = SentenceChunker(aggressive_first_flush=True)
        emitted: list[str] = []
        for t in [
            "Sure, ",
            "I can help you with that today. ",
            "Also, ",
            "let me check inventory levels for you next.",
        ]:
            emitted.extend(c.push(t))
        emitted.extend(c.flush())
        # First emission is the aggressive comma flush, then standard sentence
        # boundaries take over — the second comma must NOT trigger an emission.
        assert emitted[0].endswith(",")
        assert all("," not in s or s.endswith(".") for s in emitted[1:])

    def test_aggressive_disabled_for_italian_language(self) -> None:
        c = SentenceChunker(aggressive_first_flush=True, language="it")
        out: list[str] = []
        for t in [
            "Certo, ",
            "ti aiuto subito con questa richiesta importante. ",
            "Vediamo subito.",
        ]:
            out.extend(c.push(t))
        out.extend(c.flush())
        # Italian disables the feature → standard sentence boundaries only.
        assert out == [
            "Certo, ti aiuto subito con questa richiesta importante.",
            "Vediamo subito.",
        ]

    def test_decimal_guard_english(self) -> None:
        """Comma between digits must not trigger flush (1,000 thousands)."""
        c = SentenceChunker(aggressive_first_flush=True)
        out: list[str] = []
        for t in [
            "The total is exactly ",
            "1,",
            "000 ",
            "dollars for the entire week. ",
            "Confirmed.",
        ]:
            out.extend(c.push(t))
        out.extend(c.flush())
        # No clause emission before the period; comma in 1,000 is protected.
        assert out == [
            "The total is exactly 1,000 dollars for the entire week.",
            "Confirmed.",
        ]

    def test_currency_guard(self) -> None:
        """Currency symbol within 8 chars before comma blocks flush."""
        c = SentenceChunker(aggressive_first_flush=True)
        out: list[str] = []
        for t in ["The amount is $1,", "000 ", "for next week. ", "Confirmed."]:
            out.extend(c.push(t))
        out.extend(c.flush())
        assert out == ["The amount is $1,000 for next week.", "Confirmed."]

    def test_balanced_delimiter_guard_json(self) -> None:
        """Open brace without close blocks flush — protects JSON payloads."""
        c = SentenceChunker(aggressive_first_flush=True)
        out: list[str] = []
        for t in [
            'Sending payload {"amount": 1000, "currency": "USD"} to backend ',
            "now.",
        ]:
            out.extend(c.push(t))
        out.extend(c.flush())
        assert out == [
            'Sending payload {"amount": 1000, "currency": "USD"} to backend now.'
        ]

    def test_ellipsis_guard(self) -> None:
        """Ellipsis must not trigger soft-flush even though "..." includes "."""
        c = SentenceChunker(aggressive_first_flush=True)
        out: list[str] = []
        for t in [
            "Let me think about this for a moment... ",
            "perhaps yes.",
        ]:
            out.extend(c.push(t))
        out.extend(c.flush())
        assert out == ["Let me think about this for a moment... perhaps yes."]

    def test_first_flush_resets_after_flush(self) -> None:
        """After a full flush(), the next response starts fresh."""
        c = SentenceChunker(aggressive_first_flush=True)
        # Turn 1
        for t in ["Sure, ", "I can help you with that today, ", "no problem."]:
            c.push(t)
        c.flush()
        # Turn 2 should also benefit from aggressive first flush
        emitted: list[str] = []
        for t in [
            "Of course, ",
            "I will check inventory levels right now, ",
            "one moment.",
        ]:
            emitted.extend(c.push(t))
        emitted.extend(c.flush())
        assert emitted[0].endswith(",")

    def test_first_flush_resets_after_reset(self) -> None:
        c = SentenceChunker(aggressive_first_flush=True)
        c.push("Sure, I can help you with that today, no problem.")
        c.reset()
        emitted: list[str] = []
        for t in [
            "Of course, ",
            "I will check inventory levels right now, ",
            "one moment.",
        ]:
            emitted.extend(c.push(t))
        emitted.extend(c.flush())
        assert emitted[0].endswith(",")

    def test_below_min_len_does_not_flush(self) -> None:
        """Buffer < aggressive_first_min_len must not trigger flush."""
        c = SentenceChunker(aggressive_first_flush=True, aggressive_first_min_len=40)
        out = c.push("Hi, ") + c.push("hello there.")
        out += c.flush()
        # "Hi, hello there." is 16 chars, well below 40 → no aggressive flush.
        assert out == ["Hi, hello there."]


# ===========================================================================
# Bug #48 — per-language honorifics / abbreviations
# ===========================================================================


@pytest.mark.unit
class TestPerLanguageHonorifics:
    """Honorifics from every supported language must NOT cause sentence
    splits when they appear mid-clause.

    The chunker merges every per-language list into a single regex (see
    ``HONORIFICS_ALL``), so language detection is not required at runtime —
    Spanish "Sr. García", German "Hr. Müller", and French "Mme. Dupont" all
    work regardless of the ``language`` constructor argument.

    Each language gets one positive case (honorific should NOT split) plus
    one negative case (a same-shape but unrelated word SHOULD split).
    """

    # ------------------------- English ------------------------- #

    def test_english_mr_does_not_split(self) -> None:
        result = _sentences_only("Mr. Smith joined us.", min_sentence_len=1)
        assert len(result) == 1
        assert "Mr. Smith" in result[0]

    def test_english_negative_unrelated_word_splits(self) -> None:
        # "Map. Then" — "Map" is not in the honorific list → should split.
        result = _sentences_only("Map. Then we left.", min_sentence_len=1)
        assert len(result) == 2

    # ------------------------- Spanish ------------------------- #

    def test_spanish_sra_does_not_split(self) -> None:
        result = _sentences_only("Buenos días Sra. García.", min_sentence_len=1)
        assert len(result) == 1
        assert "Sra. García" in result[0]

    def test_spanish_dra_does_not_split(self) -> None:
        result = _sentences_only(
            "La Dra. Fernández llegó temprano.", min_sentence_len=1
        )
        assert len(result) == 1

    def test_spanish_lic_does_not_split(self) -> None:
        result = _sentences_only("El Lic. Ramírez firmó.", min_sentence_len=1)
        assert len(result) == 1

    def test_spanish_negative_unrelated_word_splits(self) -> None:
        # "Pan." then "Comemos" — "Pan" not in honorific list → split.
        result = _sentences_only("Pan. Comemos a las dos.", min_sentence_len=1)
        assert len(result) == 2

    # ------------------------- German ------------------------- #

    def test_german_hr_does_not_split(self) -> None:
        result = _sentences_only("Guten Tag Hr. Müller.", min_sentence_len=1)
        assert len(result) == 1
        assert "Hr. Müller" in result[0]

    def test_german_fr_does_not_split(self) -> None:
        result = _sentences_only("Hallo Fr. Schmidt.", min_sentence_len=1)
        assert len(result) == 1

    def test_german_zb_inline_does_not_split(self) -> None:
        # "z.B." (zum Beispiel) — internal periods handled by the existing
        # multi-letter-period pattern.
        result = _sentences_only(
            "Es gibt viele Optionen, z.B. rote oder blaue.", min_sentence_len=1
        )
        assert len(result) == 1
        assert "z.B." in result[0]

    def test_german_negative_unrelated_word_splits(self) -> None:
        # "Tag." standalone is a sentence end.
        result = _sentences_only("Tag. Wir gehen jetzt.", min_sentence_len=1)
        assert len(result) == 2

    # ------------------------- Italian ------------------------- #

    def test_italian_dott_does_not_split(self) -> None:
        result = _sentences_only("Buongiorno Dott. Rossi.", min_sentence_len=1)
        assert len(result) == 1
        assert "Dott. Rossi" in result[0]

    def test_italian_sigra_does_not_split(self) -> None:
        # Compound "Sig.ra" — internal period handled by Sig prefix rule.
        result = _sentences_only(
            "Buongiorno Sig.ra Bianchi, come va?", min_sentence_len=1
        )
        assert len(result) == 1
        assert "Sig.ra Bianchi" in result[0]

    def test_italian_negative_unrelated_word_splits(self) -> None:
        result = _sentences_only("Pane. Mangiamo alle due.", min_sentence_len=1)
        assert len(result) == 2

    # ------------------------- French ------------------------- #

    def test_french_mme_does_not_split(self) -> None:
        result = _sentences_only("Bonjour Mme. Dupont.", min_sentence_len=1)
        assert len(result) == 1
        assert "Mme. Dupont" in result[0]

    def test_french_pr_does_not_split(self) -> None:
        result = _sentences_only("Le Pr. Martin enseigne ici.", min_sentence_len=1)
        assert len(result) == 1

    def test_french_mlle_does_not_split(self) -> None:
        result = _sentences_only("Mlle. Leroy est arrivée hier.", min_sentence_len=1)
        assert len(result) == 1

    def test_french_negative_unrelated_word_splits(self) -> None:
        result = _sentences_only("Pain. Nous mangeons à deux.", min_sentence_len=1)
        assert len(result) == 2

    # ------------------------- Portuguese ------------------------- #

    def test_portuguese_sr_does_not_split(self) -> None:
        result = _sentences_only("Bom dia Sr. Silva.", min_sentence_len=1)
        assert len(result) == 1
        assert "Sr. Silva" in result[0]

    def test_portuguese_eng_does_not_split(self) -> None:
        result = _sentences_only("O Eng. Costa aprovou o projeto.", min_sentence_len=1)
        assert len(result) == 1

    def test_portuguese_negative_unrelated_word_splits(self) -> None:
        result = _sentences_only("Pão. Comemos às duas.", min_sentence_len=1)
        assert len(result) == 2

    # ------------------------- Aggregation invariants ------------------------- #

    def test_honorifics_all_is_union_of_per_language_lists(self) -> None:
        union = set()
        for prefixes in HONORIFICS_BY_LANGUAGE.values():
            union.update(prefixes)
        assert set(HONORIFICS_ALL) == union

    def test_honorifics_all_sorted_longest_first(self) -> None:
        # Critical: longer entries must come first so regex alternation
        # prefers "Sras" over "Sr".
        for i in range(1, len(HONORIFICS_ALL)):
            assert len(HONORIFICS_ALL[i - 1]) >= len(HONORIFICS_ALL[i])

    def test_per_language_constants_are_tuples(self) -> None:
        # Public constants must be immutable per the project's immutability rule.
        for name, value in [
            ("HONORIFICS_EN", HONORIFICS_EN),
            ("HONORIFICS_IT", HONORIFICS_IT),
            ("HONORIFICS_ES", HONORIFICS_ES),
            ("HONORIFICS_DE", HONORIFICS_DE),
            ("HONORIFICS_FR", HONORIFICS_FR),
            ("HONORIFICS_PT", HONORIFICS_PT),
        ]:
            assert isinstance(value, tuple), f"{name} must be a tuple"

    def test_chunker_accepts_each_language_arg(self) -> None:
        # The ``language`` parameter is opt-in with a safe default; passing
        # any of the supported codes must not raise.
        for lang in ("en", "it", "es", "de", "fr", "pt", "multi"):
            chunker = SentenceChunker(language=lang)
            # Sanity: same default behaviour regardless of language.
            assert chunker.push("Hello world.") == ["Hello world."]


# ===========================================================================
# Bug #49 — single-word "Yes." flushes during stream
# ===========================================================================


@pytest.mark.unit
class TestSingleWordFlushBug49:
    """A one-word LLM reply ending in ``.``, ``!`` or ``?`` must reach TTS
    on the terminator without the caller having to invoke ``flush()`` first.

    The protective gates (digit-before-terminator, short-acronym, known
    honorific) still block the dangerous cases.
    """

    def test_yes_period_flushes_on_push(self) -> None:
        chunker = SentenceChunker()
        assert chunker.push("Yes.") == ["Yes."]

    def test_done_exclamation_flushes_on_push(self) -> None:
        chunker = SentenceChunker()
        assert chunker.push("Done!") == ["Done!"]

    def test_really_question_flushes_on_push(self) -> None:
        chunker = SentenceChunker()
        assert chunker.push("Really?") == ["Really?"]

    def test_si_with_diacritic_flushes(self) -> None:
        chunker = SentenceChunker()
        assert chunker.push("Sì.") == ["Sì."]

    def test_japanese_single_word_flushes(self) -> None:
        chunker = SentenceChunker()
        # "はい。" is two-char Japanese for "yes" + fullwidth period.
        assert chunker.push("はい。") == ["はい。"]

    def test_single_word_split_across_two_pushes(self) -> None:
        """Realistic streaming: word arrives in one token, period in next."""
        chunker = SentenceChunker()
        assert chunker.push("Yes") == []
        assert chunker.push(".") == ["Yes."]

    # --- still-blocked cases (regression guards) --- #

    def test_blocked_acronym_us(self) -> None:
        """``"U.S."`` is an acronym, not a sentence end — must not flush."""
        chunker = SentenceChunker()
        assert chunker.push("U.S.") == []

    def test_blocked_decimal_mid_stream(self) -> None:
        """``"f(x) = 2."`` is decimal-like — must not flush."""
        chunker = SentenceChunker()
        assert chunker.push("f(x) = 2.") == []

    def test_blocked_honorific_mr(self) -> None:
        """``"Mr."`` alone must not flush — name almost certainly follows."""
        chunker = SentenceChunker()
        assert chunker.push("Mr.") == []

    def test_blocked_honorific_sr_spanish(self) -> None:
        """Spanish ``"Sr."`` alone must not flush — name almost certainly follows."""
        chunker = SentenceChunker()
        assert chunker.push("Sr.") == []

    def test_blocked_honorific_hr_german(self) -> None:
        """German ``"Hr."`` alone must not flush."""
        chunker = SentenceChunker()
        assert chunker.push("Hr.") == []

    def test_blocked_honorific_mme_french(self) -> None:
        """French ``"Mme."`` alone must not flush."""
        chunker = SentenceChunker()
        assert chunker.push("Mme.") == []
