"""Unit tests for built-in text transforms (filter_markdown, filter_emoji, filter_for_tts).

Covers:
  1. filter_markdown -- header stripping
  2. filter_markdown -- bold and italic
  3. filter_markdown -- inline code and fenced code blocks
  4. filter_markdown -- links and images
  5. filter_markdown -- list markers (ordered + unordered)
  6. filter_markdown -- block quotes
  7. filter_markdown -- strikethrough
  8. filter_markdown -- horizontal rules
  9. filter_markdown -- HTML tags
 10. filter_markdown -- plain text passes through unchanged
 11. filter_markdown -- empty string
 12. filter_markdown -- combined markdown
 13. filter_emoji -- removes common emoji
 14. filter_emoji -- preserves non-emoji Unicode (CJK, accented chars)
 15. filter_emoji -- empty string
 16. filter_emoji -- text without emoji passes through unchanged
 17. filter_for_tts -- combined markdown + emoji removal
"""

from __future__ import annotations

import pytest

from patter.services.text_transforms import filter_emoji, filter_for_tts, filter_markdown


# ---------------------------------------------------------------------------
# filter_markdown
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFilterMarkdownHeaders:
    def test_strips_h1(self):
        assert filter_markdown("# Hello World") == "Hello World"

    def test_strips_h2(self):
        assert filter_markdown("## Section Title") == "Section Title"

    def test_strips_h3_through_h6(self):
        assert filter_markdown("### H3") == "H3"
        assert filter_markdown("#### H4") == "H4"
        assert filter_markdown("###### H6") == "H6"


@pytest.mark.unit
class TestFilterMarkdownBoldItalic:
    def test_strips_bold_asterisks(self):
        assert filter_markdown("This is **bold** text") == "This is bold text"

    def test_strips_bold_underscores(self):
        assert filter_markdown("This is __bold__ text") == "This is bold text"

    def test_strips_italic_asterisks(self):
        assert filter_markdown("This is *italic* text") == "This is italic text"

    def test_strips_italic_underscores(self):
        assert filter_markdown("This is _italic_ text") == "This is italic text"


@pytest.mark.unit
class TestFilterMarkdownCode:
    def test_strips_inline_code(self):
        assert filter_markdown("Use the `console.log` function") == "Use the console.log function"

    def test_strips_fenced_code_block(self):
        text = "```javascript\nconst x = 1;\n```"
        assert filter_markdown(text) == "const x = 1;"

    def test_strips_fenced_code_block_no_language(self):
        text = "```\nhello\n```"
        assert filter_markdown(text) == "hello"


@pytest.mark.unit
class TestFilterMarkdownLinksImages:
    def test_strips_links(self):
        assert filter_markdown("Visit [Google](https://google.com) now") == "Visit Google now"

    def test_strips_images(self):
        assert filter_markdown("![Logo](https://example.com/logo.png)") == "Logo"


@pytest.mark.unit
class TestFilterMarkdownListMarkers:
    def test_strips_unordered_dash(self):
        text = "- Item one\n- Item two"
        assert filter_markdown(text) == "Item one\nItem two"

    def test_strips_unordered_asterisk(self):
        text = "* Item one\n* Item two"
        assert filter_markdown(text) == "Item one\nItem two"

    def test_strips_ordered(self):
        text = "1. First\n2. Second\n3. Third"
        assert filter_markdown(text) == "First\nSecond\nThird"


@pytest.mark.unit
class TestFilterMarkdownBlockQuotes:
    def test_strips_block_quotes(self):
        assert filter_markdown("> This is a quote") == "This is a quote"


@pytest.mark.unit
class TestFilterMarkdownStrikethrough:
    def test_strips_strikethrough(self):
        assert filter_markdown("This is ~~deleted~~ text") == "This is deleted text"


@pytest.mark.unit
class TestFilterMarkdownHorizontalRules:
    def test_strips_dashes(self):
        text = "Above\n---\nBelow"
        assert filter_markdown(text) == "Above\n\nBelow"

    def test_strips_asterisks(self):
        text = "Above\n***\nBelow"
        assert filter_markdown(text) == "Above\n\nBelow"


@pytest.mark.unit
class TestFilterMarkdownHTMLTags:
    def test_strips_html_tags(self):
        assert filter_markdown("Hello <b>world</b>") == "Hello world"

    def test_strips_self_closing_tags(self):
        assert filter_markdown("Line<br/>break") == "Linebreak"


@pytest.mark.unit
class TestFilterMarkdownPassthrough:
    def test_plain_text_unchanged(self):
        plain = "Hello, this is a normal sentence."
        assert filter_markdown(plain) == plain

    def test_empty_string(self):
        assert filter_markdown("") == ""

    def test_combined_markdown(self):
        text = (
            "# Welcome\n"
            "\n"
            "This is **bold** and *italic* text.\n"
            "\n"
            "- Item one\n"
            "- Item two\n"
            "\n"
            "> A quote\n"
            "\n"
            "Visit [here](https://example.com)."
        )
        expected = (
            "Welcome\n"
            "\n"
            "This is bold and italic text.\n"
            "\n"
            "Item one\n"
            "Item two\n"
            "\n"
            "A quote\n"
            "\n"
            "Visit here."
        )
        assert filter_markdown(text) == expected


# ---------------------------------------------------------------------------
# filter_emoji
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFilterEmoji:
    def test_removes_common_emoticons(self):
        assert filter_emoji("Hello! \U0001F600") == "Hello!"

    def test_removes_multiple_emoji(self):
        assert filter_emoji("\U0001F680 Launch \U0001F31F now \U0001F389") == "Launch now"

    def test_removes_misc_symbols(self):
        assert filter_emoji("Sunny \u2600 day") == "Sunny day"

    def test_removes_dingbats(self):
        assert filter_emoji("Check \u2714 done") == "Check done"

    def test_preserves_cjk(self):
        assert filter_emoji("\u4F60\u597D") == "\u4F60\u597D"

    def test_preserves_accented_characters(self):
        assert filter_emoji("caf\u00E9 r\u00E9sum\u00E9") == "caf\u00E9 r\u00E9sum\u00E9"

    def test_preserves_punctuation(self):
        assert filter_emoji("Hello, world! How are you?") == "Hello, world! How are you?"

    def test_text_without_emoji_unchanged(self):
        plain = "No emoji here."
        assert filter_emoji(plain) == plain

    def test_empty_string(self):
        assert filter_emoji("") == ""

    def test_only_emoji(self):
        assert filter_emoji("\U0001F600\U0001F601\U0001F602") == ""


# ---------------------------------------------------------------------------
# filter_for_tts
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFilterForTTS:
    def test_removes_markdown_and_emoji(self):
        text = "## Hello \U0001F44B\n\nThis is **great** \U0001F389!"
        result = filter_for_tts(text)
        assert result == "Hello\n\nThis is great !"

    def test_plain_text_unchanged(self):
        plain = "Just a normal sentence without formatting."
        assert filter_for_tts(plain) == plain

    def test_empty_string(self):
        assert filter_for_tts("") == ""
