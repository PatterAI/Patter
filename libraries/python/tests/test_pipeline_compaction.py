"""Tests for pipeline-mode token counting + history compaction.

Covers:
  (a) compaction OFF → prompt assembly is byte-identical to today;
  (b) over the token budget the oldest turns are summarized while the last N
      stay verbatim, and a fact stated early stays recoverable via the summary;
  (c) the ``context_tokens`` metric grows and the warning fires at the budget;
  (d) ``Agent.max_history`` is honoured by the handler's working-memory ring.

The compaction partition / prune / trigger logic and the real
``CallMetricsAccumulator`` are exercised directly. The only mocked surface is
the LLM provider / summarizer (a paid external boundary) — those tests carry the
``mocked`` marker per ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import logging
from collections import deque

import pytest

from getpatter.models import Agent, CompactionConfig
from getpatter.services.compaction import (
    ContextCompactor,
    estimate_messages_tokens,
    estimate_tokens,
)
from getpatter.services.llm_loop import LLMLoop
from getpatter.services.metrics import CallMetricsAccumulator


# ---------------------------------------------------------------------------
# Local LLM provider / summarizer doubles (the only mocked boundary)
# ---------------------------------------------------------------------------


class _LocalProvider:
    """A real in-process LLM provider yielding deterministic chunks.

    Stands in for the paid OpenAI streaming boundary. Everything downstream
    (message assembly, token counting, metrics) runs the real code paths.
    """

    provider_key = "openai"

    def __init__(self, reply: str = "Understood.", emit_usage: bool = True) -> None:
        self.reply = reply
        self.emit_usage = emit_usage
        self.seen_messages: list[list[dict]] = []

    async def stream(self, messages, tools=None, *, cancel_event=None, **_kw):
        # Record the exact prompt the loop assembled so tests can assert the
        # summary / history made it into the request.
        self.seen_messages.append(list(messages))
        yield {"type": "text", "content": self.reply}
        if self.emit_usage:
            yield {"type": "usage", "input_tokens": 5, "output_tokens": 3}


async def _drain(gen) -> str:
    parts = []
    async for tok in gen:
        parts.append(tok)
    return "".join(parts)


def _make_turns(n: int, prefix: str = "msg") -> list[dict]:
    """Build n alternating user/assistant history entries."""
    out: list[dict] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(
            {"role": role, "text": f"{prefix} {i} " + "x" * 60, "timestamp": 0.0}
        )
    return out


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_estimate_tokens_uses_char_over_four_baseline():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("a" * 40) == 10
    # Short non-empty text still counts as at least one token.
    assert estimate_tokens("hi") == 1


@pytest.mark.unit
def test_estimate_messages_tokens_handles_content_and_text_keys():
    msgs = [
        {"role": "system", "content": "a" * 40},  # 10 + overhead
        {"role": "user", "text": "b" * 80},  # 20 + overhead
    ]
    total = estimate_messages_tokens(msgs)
    # 10 + 20 = 30 content tokens, plus 4 overhead per message (2 messages).
    assert total == 30 + 4 * 2


# ---------------------------------------------------------------------------
# CompactionConfig
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compaction_config_defaults_and_frozen():
    cfg = CompactionConfig()
    assert cfg.trigger_tokens == 8000
    assert cfg.target_tokens == 3000
    assert cfg.keep_last_turns == 4
    with pytest.raises(Exception):
        cfg.trigger_tokens = 1  # type: ignore[misc] — frozen dataclass


# ---------------------------------------------------------------------------
# (a) Compaction OFF → behaviour unchanged
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_messages_unchanged_without_summary():
    """With no compaction summary set, the prompt is system + history + user."""
    loop = LLMLoop(
        openai_key="",
        model="gpt-4o-mini",
        system_prompt="You are a helpful agent.",
        llm_provider=_LocalProvider(),
        disable_phone_preamble=True,
    )
    history = [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "hi there"},
    ]
    messages = loop._build_messages(history, "what time is it")
    assert messages[0] == {
        "role": "system",
        "content": "You are a helpful agent.",
    }
    # No injected summary system message — index 1 is the first history turn.
    assert messages[1] == {"role": "user", "content": "hello"}
    assert messages[-1] == {"role": "user", "content": "what time is it"}
    assert all("Summary of earlier conversation" not in m["content"] for m in messages)


@pytest.mark.unit
def test_should_compact_false_when_short():
    cfg = CompactionConfig(trigger_tokens=8000, keep_last_turns=4)
    compactor = ContextCompactor(cfg, summarizer=_unused_summarizer)
    history = _make_turns(4)  # below keep threshold
    assert compactor.should_compact(history) is False


async def _unused_summarizer(prior, old):  # pragma: no cover - never called here
    raise AssertionError("summarizer should not run")


# ---------------------------------------------------------------------------
# (b) Over budget → old turns summarized, last N verbatim, fact recoverable
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_compaction_summarizes_old_keeps_last_verbatim_and_preserves_fact():
    # A deterministic local summarizer standing in for the agent LLM: it folds
    # the prior summary plus the old turns' text into a new summary, so every
    # fact in the summarized turns survives — exactly what a good LLM summary
    # would do, but reproducibly.
    async def summarizer(prior: str, old: list[dict]) -> str:
        body = " | ".join(m["text"] for m in old)
        return (prior + " || " if prior else "") + body

    cfg = CompactionConfig(trigger_tokens=50, target_tokens=100, keep_last_turns=2)
    compactor = ContextCompactor(cfg, summarizer=summarizer)

    history: deque = deque(maxlen=200)
    # Early fact that must survive into the summary.
    history.append(
        {"role": "user", "text": "My account number is 12345.", "timestamp": 0.0}
    )
    history.append({"role": "assistant", "text": "Thanks, noted.", "timestamp": 0.0})
    for entry in _make_turns(8, prefix="later"):
        history.append(entry)

    keep_last = ["VERBATIM user line", "VERBATIM assistant line"]
    history.append({"role": "user", "text": keep_last[0], "timestamp": 0.0})
    history.append({"role": "assistant", "text": keep_last[1], "timestamp": 0.0})

    pre_len = len(history)
    assert compactor.should_compact(history) is True

    new_summary = await compactor.maybe_compact(history)

    # A summary was produced and the early fact is recoverable from it.
    assert new_summary is not None
    assert "12345" in new_summary
    assert compactor.summary == new_summary

    # The last keep_last_turns*2 = 4 entries remain verbatim... but we only
    # appended 2 explicit "VERBATIM" lines as the very last; assert they survive
    # and that the deque shrank (old turns pruned).
    remaining_texts = [e["text"] for e in history]
    assert keep_last[0] in remaining_texts
    assert keep_last[1] in remaining_texts
    assert "My account number is 12345." not in remaining_texts  # pruned
    assert len(history) < pre_len
    assert len(history) == cfg.keep_last_turns * 2


@pytest.mark.mocked
async def test_summary_is_prepended_into_prompt_so_fact_is_recoverable():
    """End-to-end: a compaction summary flows into the assembled LLM prompt."""
    provider = _LocalProvider(reply="ok")
    loop = LLMLoop(
        openai_key="",
        model="gpt-4o-mini",
        system_prompt="agent",
        llm_provider=provider,
        disable_phone_preamble=True,
    )
    loop.set_context_summary("Caller's account number is 12345; wants a refund.")

    messages = loop._build_messages([{"role": "user", "text": "and my balance?"}], "hi")
    # System prompt at 0, summary system message at 1, then history, then user.
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "system"
    assert "12345" in messages[1]["content"]
    assert "Summary of earlier conversation" in messages[1]["content"]
    assert messages[-1] == {"role": "user", "content": "hi"}


@pytest.mark.mocked
async def test_empty_summary_does_not_prune():
    """A summarizer that returns nothing must NOT drop the turns."""

    async def empty_summarizer(prior, old):
        return "   "

    cfg = CompactionConfig(trigger_tokens=20, keep_last_turns=2)
    compactor = ContextCompactor(cfg, summarizer=empty_summarizer)
    history: deque = deque(_make_turns(10), maxlen=200)
    pre_len = len(history)

    result = await compactor.maybe_compact(history)
    assert result is None
    assert len(history) == pre_len  # nothing pruned
    assert compactor.summary == ""


# ---------------------------------------------------------------------------
# (c) context_tokens metric grows + warning at threshold
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_context_tokens_metric_recorded_and_grows():
    acc = CallMetricsAccumulator("call-1", "pipeline", "twilio", llm_provider="openai")
    loop = LLMLoop(
        openai_key="",
        model="gpt-4o-mini",
        system_prompt="agent",
        llm_provider=_LocalProvider(),
        metrics=acc,
        disable_phone_preamble=True,
    )

    await _drain(loop.run("hi", [], {"call_id": "call-1"}))
    small = acc.max_context_tokens
    assert small > 0

    # A larger history must push the recorded peak up.
    big_history = _make_turns(30)
    await _drain(loop.run("hi again", big_history, {"call_id": "call-1"}))
    assert acc.max_context_tokens > small


@pytest.mark.mocked
async def test_context_token_budget_warning_fires_once(caplog):
    acc = CallMetricsAccumulator("call-2", "pipeline", "twilio", llm_provider="openai")
    loop = LLMLoop(
        openai_key="",
        model="gpt-4o-mini",
        system_prompt="agent",
        llm_provider=_LocalProvider(),
        metrics=acc,
        disable_phone_preamble=True,
        context_token_budget=10,  # tiny → any real prompt crosses 75%
    )

    with caplog.at_level(logging.WARNING, logger="getpatter"):
        await _drain(
            loop.run("a normal sentence that is long enough", [], {"call_id": "call-2"})
        )
        await _drain(loop.run("another turn", _make_turns(4), {"call_id": "call-2"}))

    warnings = [r for r in caplog.records if "context_tokens_high" in r.getMessage()]
    assert len(warnings) == 1  # fires once per call, not per turn


@pytest.mark.mocked
async def test_no_warning_without_budget(caplog):
    acc = CallMetricsAccumulator("call-3", "pipeline", "twilio", llm_provider="openai")
    loop = LLMLoop(
        openai_key="",
        model="gpt-4o-mini",
        system_prompt="agent",
        llm_provider=_LocalProvider(),
        metrics=acc,
        disable_phone_preamble=True,
        # No context_token_budget → no warning, but metric still recorded.
    )
    with caplog.at_level(logging.WARNING, logger="getpatter"):
        await _drain(loop.run("hello there", _make_turns(20), {"call_id": "call-3"}))
    assert not any("context_tokens_high" in r.getMessage() for r in caplog.records)
    assert acc.max_context_tokens > 0


@pytest.mark.unit
def test_call_metrics_exposes_context_tokens_peak():
    acc = CallMetricsAccumulator("call-4", "pipeline", "twilio", llm_provider="openai")
    acc.record_context_tokens(120)
    acc.record_context_tokens(80)  # lower — peak stays 120
    acc.record_context_tokens(300)
    assert acc.max_context_tokens == 300
    result = acc.end_call()
    assert result.context_tokens == 300


# ---------------------------------------------------------------------------
# (d) max_history configurable + honoured by the handler ring
# ---------------------------------------------------------------------------


class _StubSender:
    async def send_audio(self, pcm_audio: bytes) -> None: ...
    async def send_clear(self) -> None: ...
    async def send_mark(self, mark_name: str) -> None: ...


@pytest.mark.unit
def test_handler_history_ring_honours_max_history():
    """A handler built without an external deque sizes its ring from max_history."""
    from getpatter.stream_handler import StreamHandler

    class _Handler(StreamHandler):
        async def start(self) -> None: ...
        async def on_audio_received(self, audio_bytes: bytes) -> None: ...
        async def cleanup(self) -> None: ...

    agent = Agent(system_prompt="x", provider="pipeline", max_history=7)
    handler = _Handler(
        agent,
        _StubSender(),
        "call-d",
        "+15555550100",
        "+15555550111",
        "x",
        None,
    )
    assert handler.conversation_history.maxlen == 7

    # The ring really evicts past the cap.
    for i in range(20):
        handler.conversation_history.append({"role": "user", "text": str(i)})
    assert len(handler.conversation_history) == 7
    assert handler.conversation_history[0]["text"] == "13"


@pytest.mark.unit
def test_agent_max_history_default_is_200():
    assert Agent(system_prompt="x").max_history == 200
