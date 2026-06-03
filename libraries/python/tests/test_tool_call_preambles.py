"""Unit tests for the ``tool_call_preambles`` knob (feature #1).

The pure-helper tests exercise the real ``apply_tool_call_preambles`` with no
external boundary — no WebSocket, no mock. They verify the byte-identical
no-op default, the built-in block prepend, and the verbatim string override.
The ``Patter.agent`` plumbing tests confirm the frozen field is opt-in and the
old (no-kwarg) shape still constructs.
"""

from __future__ import annotations

import dataclasses

import pytest

from getpatter import OpenAIRealtime2, Patter, Twilio
from getpatter.stream_handler import (
    DEFAULT_TOOL_CALL_PREAMBLE_BLOCK,
    apply_tool_call_preambles,
)


@pytest.fixture(autouse=True)
def _openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def _phone() -> Patter:
    return Patter(
        carrier=Twilio(account_sid="ACtest", auth_token="tok"),
        phone_number="+15555550100",
    )


def _agent(phone: Patter, **kwargs):
    # The OpenAIRealtime2 engine marker reads OPENAI_API_KEY into the local
    # config so the Realtime-mode key guard in agent() is satisfied — no
    # network is touched.
    return phone.agent(engine=OpenAIRealtime2(), **kwargs)


# ---------------------------------------------------------------------------
# Pure helper — no mock
# ---------------------------------------------------------------------------


def test_false_returns_prompt_byte_identical() -> None:
    prompt = "You are a helpful receptionist."
    assert apply_tool_call_preambles(prompt, False) == prompt
    assert apply_tool_call_preambles(prompt, False) is prompt


def test_false_on_empty_prompt_is_byte_identical() -> None:
    assert apply_tool_call_preambles("", False) == ""


def test_true_prepends_default_block_then_prompt() -> None:
    prompt = "You are a helpful receptionist."
    out = apply_tool_call_preambles(prompt, True)
    assert out == f"{DEFAULT_TOOL_CALL_PREAMBLE_BLOCK}\n\n{prompt}"
    assert out.startswith("# Preambles")
    # An OpenAI-approved action-describing phrasing is present verbatim.
    assert "I'll check that order now." in out


def test_true_on_empty_prompt_returns_block_only() -> None:
    out = apply_tool_call_preambles("", True)
    assert out == DEFAULT_TOOL_CALL_PREAMBLE_BLOCK
    assert out.startswith("# Preambles")


def test_str_override_used_verbatim() -> None:
    prompt = "You are a helpful receptionist."
    out = apply_tool_call_preambles(prompt, "CUSTOM PREAMBLE BLOCK")
    assert out == f"CUSTOM PREAMBLE BLOCK\n\n{prompt}"
    # Full override — the built-in block text is absent.
    assert "I'll check that order now." not in out
    assert "## When to use a preamble" not in out


def test_str_override_on_empty_prompt() -> None:
    assert apply_tool_call_preambles("", "ONLY BLOCK") == "ONLY BLOCK"


# ---------------------------------------------------------------------------
# Patter.agent plumbing + backward compat
# ---------------------------------------------------------------------------


def test_agent_defaults_tool_call_preambles_false() -> None:
    agent = _agent(_phone(), system_prompt="x")
    assert agent.tool_call_preambles is False


def test_agent_old_shape_still_constructs_and_field_is_frozen() -> None:
    agent = _agent(_phone(), system_prompt="x")
    # Frozen dataclass: direct write raises.
    with pytest.raises(dataclasses.FrozenInstanceError):
        agent.tool_call_preambles = True  # type: ignore[misc]


def test_agent_true_sets_field() -> None:
    agent = _agent(_phone(), system_prompt="x", tool_call_preambles=True)
    assert agent.tool_call_preambles is True


def test_agent_str_override_sets_field() -> None:
    block = "# Preambles\n\nSay one short line before slow tools."
    agent = _agent(_phone(), system_prompt="x", tool_call_preambles=block)
    assert agent.tool_call_preambles == block
