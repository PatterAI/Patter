"""Unit tests for the five public symbols ported from the TypeScript SDK.

Each test exercises real behaviour — no mocks of the unit under test (per
``.claude/rules/authentic-tests.md``).

Symbols covered
---------------
- :class:`getpatter.DefaultToolExecutor`
- :class:`getpatter.LLMChunk`
- :func:`getpatter.builtin_clip_path`
- :func:`getpatter.select_sound_from_list`
- :func:`getpatter.resample_24k_to_16k`
"""

from __future__ import annotations

import json
import math
import struct
import warnings
from collections import Counter
from typing import Any

import pytest

import getpatter
from getpatter.audio.background_audio import (
    AudioConfig,
    BuiltinAudioClip,
    builtin_clip_path,
    select_sound_from_list,
)
from getpatter.services.llm_loop import DefaultToolExecutor, LLMChunk

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from getpatter.audio.transcoding import resample_24k_to_16k

try:  # ``audioop`` is optional on Python ≥ 3.13.
    import audioop  # type: ignore[import]
except ImportError:
    try:
        import audioop_lts as audioop  # type: ignore[import,no-redef]
    except ImportError:
        audioop = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Top-level re-exports
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPublicReexports:
    """Each ported symbol is reachable from the package root and listed in ``__all__``."""

    @pytest.mark.parametrize(
        "name",
        [
            "DefaultToolExecutor",
            "LLMChunk",
            "builtin_clip_path",
            "select_sound_from_list",
            "resample_24k_to_16k",
        ],
    )
    def test_symbol_in_all_and_attr(self, name: str) -> None:
        assert name in getpatter.__all__, f"{name} missing from getpatter.__all__"
        assert hasattr(getpatter, name), f"getpatter.{name} not importable"


# ---------------------------------------------------------------------------
# LLMChunk
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMChunk:
    """LLMChunk is a frozen dataclass that round-trips to dict shape."""

    def test_text_chunk_to_dict(self) -> None:
        chunk = LLMChunk(type="text", content="hi")
        assert chunk.to_dict() == {"type": "text", "content": "hi"}

    def test_tool_call_chunk_omits_none_fields(self) -> None:
        chunk = LLMChunk(
            type="tool_call",
            index=0,
            id="tc_42",
            name="lookup",
            arguments='{"q":"x"}',
        )
        d = chunk.to_dict()
        assert d == {
            "type": "tool_call",
            "index": 0,
            "id": "tc_42",
            "name": "lookup",
            "arguments": '{"q":"x"}',
        }
        # Optional usage fields stay omitted when not set.
        assert "input_tokens" not in d
        assert "output_tokens" not in d

    def test_usage_chunk_carries_token_counts(self) -> None:
        chunk = LLMChunk(
            type="usage",
            input_tokens=120,
            output_tokens=42,
            cache_read_tokens=64,
        )
        d = chunk.to_dict()
        assert d["type"] == "usage"
        assert d["input_tokens"] == 120
        assert d["output_tokens"] == 42
        assert d["cache_read_tokens"] == 64

    def test_is_frozen(self) -> None:
        chunk = LLMChunk(type="text", content="hi")
        with pytest.raises(Exception):  # FrozenInstanceError subclasses AttributeError
            chunk.content = "bye"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DefaultToolExecutor
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultToolExecutor:
    """DefaultToolExecutor.execute exercises real handler/webhook dispatch logic."""

    async def test_local_handler_returns_json_dict(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(args: dict, ctx: dict) -> dict:
            captured["args"] = args
            captured["ctx"] = ctx
            return {"sum": args["a"] + args["b"]}

        executor = DefaultToolExecutor()
        result = await executor.execute(
            tool_name="add",
            arguments={"a": 1, "b": 2},
            call_context={"call_id": "abc"},
            handler=handler,
        )
        assert json.loads(result) == {"sum": 3}
        assert captured["args"] == {"a": 1, "b": 2}
        assert captured["ctx"] == {"call_id": "abc"}

    async def test_sync_handler_returning_str_is_passed_through(self) -> None:
        def handler(args: dict, ctx: dict) -> str:
            return "raw-string"

        executor = DefaultToolExecutor()
        result = await executor.execute(
            tool_name="echo",
            arguments={},
            call_context={},
            handler=handler,
        )
        # String results are returned verbatim (no double-encoding).
        assert result == "raw-string"

    async def test_handler_exception_returns_fallback_error(self) -> None:
        def boom(args: dict, ctx: dict) -> dict:
            raise RuntimeError("kaboom")

        executor = DefaultToolExecutor()
        result = await executor.execute(
            tool_name="exploder",
            arguments={},
            call_context={},
            handler=boom,
        )
        payload = json.loads(result)
        assert payload["fallback"] is True
        assert "kaboom" in payload["error"]

    async def test_no_handler_no_webhook_returns_fallback_error(self) -> None:
        executor = DefaultToolExecutor()
        result = await executor.execute(
            tool_name="orphan",
            arguments={},
            call_context={},
        )
        payload = json.loads(result)
        assert payload["fallback"] is True
        assert "orphan" in payload["error"]

    async def test_blocked_webhook_returns_fallback_error(self) -> None:
        # Loopback addresses are blocked by validate_webhook_url so the
        # executor never makes an HTTP call. Authentic — exercises the real
        # SSRF guard wired into the executor.
        executor = DefaultToolExecutor()
        result = await executor.execute(
            tool_name="blocked",
            arguments={},
            call_context={},
            webhook_url="http://127.0.0.1:1/x",
        )
        payload = json.loads(result)
        assert payload["fallback"] is True
        assert "rejected" in payload["error"]


# ---------------------------------------------------------------------------
# builtin_clip_path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuiltinClipPath:
    """builtin_clip_path resolves bundled .ogg clips to filesystem paths."""

    def test_returns_path_for_enum(self) -> None:
        path = builtin_clip_path(BuiltinAudioClip.HOLD_MUSIC)
        with open(path, "rb") as f:
            assert f.read(4) == b"OggS"

    def test_accepts_raw_filename_string(self) -> None:
        # TS API accepts the string literal — Python should match.
        path = builtin_clip_path("hold_music.ogg")
        with open(path, "rb") as f:
            assert f.read(4) == b"OggS"

    def test_matches_enum_path_method(self) -> None:
        # The enum's ``.path()`` should now delegate to this function.
        for clip in BuiltinAudioClip:
            assert builtin_clip_path(clip) == clip.path()


# ---------------------------------------------------------------------------
# select_sound_from_list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSelectSoundFromList:
    """select_sound_from_list matches the TS implementation behaviourally."""

    def test_empty_list_returns_none(self) -> None:
        assert select_sound_from_list([]) is None

    def test_zero_probability_returns_none(self) -> None:
        cfg = AudioConfig(source="x.ogg", probability=0.0)
        assert select_sound_from_list([cfg]) is None

    def test_full_probability_always_selects(self) -> None:
        a = AudioConfig(source="a.ogg", probability=1.0)
        b = AudioConfig(source="b.ogg", probability=0.0)
        # b has zero probability so a is the only valid pick.
        for _ in range(50):
            assert select_sound_from_list([a, b]) == a

    def test_weighted_selection_respects_probabilities(self) -> None:
        # 70/30 split; with 5000 trials, observed frequencies should be
        # within 5% of the configured weights.
        a = AudioConfig(source="a.ogg", probability=0.7)
        b = AudioConfig(source="b.ogg", probability=0.3)
        counts: Counter[str] = Counter()
        for _ in range(5000):
            picked = select_sound_from_list([a, b])
            assert picked is not None
            counts[picked.source] += 1  # type: ignore[index]
        ratio_a = counts["a.ogg"] / 5000
        assert 0.65 <= ratio_a <= 0.75, f"observed ratio for a={ratio_a}"


# ---------------------------------------------------------------------------
# resample_24k_to_16k
# ---------------------------------------------------------------------------


def _sine_pcm16_24k(num_samples: int, freq: float = 440.0) -> bytes:
    return b"".join(
        struct.pack(
            "<h",
            int(16383 * math.sin(2 * math.pi * freq * i / 24000)),
        )
        for i in range(num_samples)
    )


@pytest.mark.unit
@pytest.mark.skipif(
    audioop is None,
    reason="audioop / audioop-lts not installed",
)
class TestResample24kTo16k:
    """resample_24k_to_16k mirrors the TS one-shot helper and warns once."""

    def test_empty_input_returns_empty(self) -> None:
        assert resample_24k_to_16k(b"") == b""

    def test_output_length_matches_3_to_2_ratio(self) -> None:
        # 600 samples @ 24 kHz → ~400 samples @ 16 kHz (1200 → 800 bytes).
        pcm24k = _sine_pcm16_24k(600)
        out = resample_24k_to_16k(pcm24k)
        # audioop.ratecv yields the canonical resampled length; the helper
        # is a thin wrapper. Verify the ratio is in the expected range.
        sample_count = len(out) // 2
        # ~2/3 of 600 = 400. Allow ±2 for ratecv boundary edges.
        assert 395 <= sample_count <= 405

    def test_emits_deprecation_warning_once(self) -> None:
        # Reset the module-level latch so the warning fires for this test.
        from getpatter.audio import transcoding as t

        t._warned_resample_24k_16k = False  # type: ignore[attr-defined]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            resample_24k_to_16k(_sine_pcm16_24k(100))
            resample_24k_to_16k(_sine_pcm16_24k(100))
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        # Latched: only the first call emits.
        assert len(dep_warnings) == 1
        assert "resample_24k_to_16k" in str(dep_warnings[0].message)

    def test_matches_stateful_one_shot(self) -> None:
        # Authentic: the helper should produce the same bytes as a
        # single-shot StatefulResampler (no chunking — same code path).
        from getpatter.audio.transcoding import StatefulResampler

        pcm24k = _sine_pcm16_24k(900)
        sr = StatefulResampler(24000, 16000)
        expected = sr.process(pcm24k) + sr.flush()

        # Reset the deprecation latch so we don't depend on test order.
        from getpatter.audio import transcoding as t

        t._warned_resample_24k_16k = False  # type: ignore[attr-defined]

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            actual = resample_24k_to_16k(pcm24k)
        assert actual == expected
