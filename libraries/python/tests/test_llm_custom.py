"""Unit tests for the generic ``CustomLLM`` provider.

``CustomLLM`` (= ``getpatter.llm.custom.LLM``) is the canonical "Custom LLM"
name for the OpenAI-compatible engine: one provider that works with Hermes,
OpenClaw, Ollama/vLLM/LM Studio, or any other endpoint speaking the OpenAI
Chat Completions protocol. The Hermes / OpenClaw presets subclass the same
engine.

Also covers ``session_key_from`` on the GENERIC provider (hoisted from the
Hermes preset so any header-scoped runtime gets per-caller memory): the
``"caller_hash"`` selector, factory precedence, and validation.
"""

from __future__ import annotations

import pytest

from getpatter import CustomLLM
from getpatter.llm import custom
from getpatter.llm.hermes import LLM as HermesLLM
from getpatter.llm.openai_compatible import OpenAICompatibleLLMProvider
from getpatter.models import hash_caller

BASE = {"base_url": "http://127.0.0.1:9000/v1", "model": "my-agent"}


@pytest.mark.unit
class TestCustomLLMSurface:
    def test_is_the_generic_engine(self) -> None:
        assert issubclass(CustomLLM, OpenAICompatibleLLMProvider)

    def test_provider_key(self) -> None:
        assert CustomLLM.provider_key == "custom"

    def test_namespace_and_top_level_exports_are_the_same_class(self) -> None:
        assert custom.LLM is CustomLLM

    def test_old_constructor_shape_still_works(self) -> None:
        # Opt-in rule: the new ``session_key_from`` field is optional — the
        # pre-existing shape constructs unchanged.
        llm = CustomLLM(**BASE)
        assert llm._model == "my-agent"

    def test_keyless_gateway_accepted(self) -> None:
        llm = CustomLLM(base_url="http://127.0.0.1:11434/v1", model="llama3.1")
        assert llm._model == "llama3.1"


@pytest.mark.unit
class TestGenericSessionKeyFrom:
    def test_caller_hash_selector_derives_per_caller_key(self) -> None:
        llm = CustomLLM(
            **BASE,
            session_key_header="X-My-Memory-Key",
            session_key_from="caller_hash",
        )
        caller = "+15551110000"
        key = llm._resolve_session_key(
            call_id="call-1", caller=caller, callee="+15552220000"
        )
        assert key == f"patter-caller-{hash_caller(caller)}"

    def test_selector_returns_none_without_caller(self) -> None:
        llm = CustomLLM(
            **BASE,
            session_key_header="X-My-Memory-Key",
            session_key_from="caller_hash",
        )
        assert (
            llm._resolve_session_key(call_id="call-1", caller=None, callee=None) is None
        )

    def test_explicit_factory_wins_over_selector(self) -> None:
        llm = CustomLLM(
            **BASE,
            session_key_header="X-My-Memory-Key",
            session_key_from="caller_hash",
            session_key_factory=lambda ctx: "explicit-key",
        )
        key = llm._resolve_session_key(
            call_id="call-1", caller="+15551110000", callee=None
        )
        assert key == "explicit-key"

    def test_invalid_selector_raises(self) -> None:
        with pytest.raises(ValueError, match="session_key_from"):
            CustomLLM(**BASE, session_key_from="raw_number")

    def test_hermes_preset_delegates_to_generic_selector(self) -> None:
        # Regression for the delegation refactor: the preset's behaviour is
        # byte-identical to before (same derived key shape).
        llm = HermesLLM(session_key_from="caller_hash")
        caller = "+15551110000"
        key = llm._resolve_session_key(call_id="c", caller=caller, callee=None)
        assert key == f"patter-caller-{hash_caller(caller)}"

    def test_hermes_preset_invalid_selector_still_raises(self) -> None:
        with pytest.raises(ValueError, match="session_key_from"):
            HermesLLM(session_key_from="raw_number")
