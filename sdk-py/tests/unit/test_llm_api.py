"""Unit tests for the public LLM API — Phase 2 of v0.5.1.

Covers:

* Per-provider wrapper instantiation (explicit key, env fallback, missing key).
* ``phone.agent(llm=...)`` wiring — happy path, type validation, engine-mode
  warning.
* ``LLMLoop`` accepting a pre-built ``LLMProvider``.
* ``phone.serve()`` conflict check between ``agent.llm`` and ``on_message``.
* Flat re-export parity (``from getpatter import OpenAILLM, ...``).
"""

from __future__ import annotations

import importlib.util
import logging
from unittest.mock import AsyncMock

import pytest

from getpatter import (
    AnthropicLLM,
    CerebrasLLM,
    DeepgramSTT,
    ElevenLabsTTS,
    GoogleLLM,
    GroqLLM,
    OpenAIRealtime,
    OpenAILLM,
    Twilio,
)
from getpatter.client import Patter
from getpatter.services.llm_loop import LLMLoop, LLMProvider


# Optional-extras availability — these tests construct adapter instances whose
# underlying providers import the vendor SDK lazily at class construction time.
# On the base CI matrix (no optional extras) those imports fail; skip
# parametrized entries that need a missing package. The all-extras CI job
# installs everything and exercises every branch.
_ANTHROPIC_AVAILABLE = importlib.util.find_spec("anthropic") is not None
_GOOGLE_GENAI_AVAILABLE = importlib.util.find_spec("google.genai") is not None

_skip_if_no_anthropic = pytest.mark.skipif(
    not _ANTHROPIC_AVAILABLE,
    reason="anthropic package not installed — run with getpatter[anthropic]",
)
_skip_if_no_google = pytest.mark.skipif(
    not _GOOGLE_GENAI_AVAILABLE,
    reason="google-genai package not installed — run with getpatter[google]",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_phone() -> Patter:
    """Build a default local-mode Patter instance for tests."""
    return Patter(
        carrier=Twilio(
            account_sid="ACtest000000000000000000000000000", auth_token="tok"
        ),
        phone_number="+15550001234",
        webhook_url="abc.ngrok.io",
    )


# ---------------------------------------------------------------------------
# Per-provider instantiation
# ---------------------------------------------------------------------------


PROVIDER_CASES = [
    pytest.param(OpenAILLM, "OPENAI_API_KEY", {}, id="openai"),
    pytest.param(
        AnthropicLLM, "ANTHROPIC_API_KEY", {}, id="anthropic",
        marks=_skip_if_no_anthropic,
    ),
    pytest.param(GroqLLM, "GROQ_API_KEY", {}, id="groq"),
    pytest.param(
        CerebrasLLM,
        "CEREBRAS_API_KEY",
        {"gzip_compression": False, "msgpack_encoding": False},
        id="cerebras",
    ),
]


@pytest.mark.unit
class TestProviderWrappers:
    """Each LLM wrapper resolves api_key, env fallback, and raises clearly."""

    @pytest.mark.parametrize("cls,env_var,extra", PROVIDER_CASES)
    def test_explicit_api_key(self, cls, env_var, extra, monkeypatch) -> None:
        # Ensure the env var is *not* what drives the test.
        monkeypatch.delenv(env_var, raising=False)
        llm = cls(api_key="explicit-key", **extra)
        assert isinstance(llm, LLMProvider)

    @pytest.mark.parametrize("cls,env_var,extra", PROVIDER_CASES)
    def test_env_fallback(self, cls, env_var, extra, monkeypatch) -> None:
        monkeypatch.setenv(env_var, "env-key")
        llm = cls(**extra)
        assert isinstance(llm, LLMProvider)

    @pytest.mark.parametrize("cls,env_var,extra", PROVIDER_CASES)
    def test_missing_key_raises(self, cls, env_var, extra, monkeypatch) -> None:
        monkeypatch.delenv(env_var, raising=False)
        with pytest.raises(ValueError, match=env_var):
            cls(**extra)

    # Google reads either GEMINI_API_KEY or GOOGLE_API_KEY; cover both plus
    # the missing-both case. Requires the optional ``google`` extra.

    @_skip_if_no_google
    def test_google_explicit_api_key(self, monkeypatch) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        llm = GoogleLLM(api_key="AIza-explicit")
        assert isinstance(llm, LLMProvider)

    @_skip_if_no_google
    def test_google_env_fallback_gemini(self, monkeypatch) -> None:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-gemini")
        llm = GoogleLLM()
        assert isinstance(llm, LLMProvider)

    @_skip_if_no_google
    def test_google_env_fallback_google(self, monkeypatch) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-google")
        llm = GoogleLLM()
        assert isinstance(llm, LLMProvider)

    def test_google_missing_both_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            GoogleLLM()


# ---------------------------------------------------------------------------
# phone.agent(llm=...)
# ---------------------------------------------------------------------------


class _DummyLLM:
    """Minimal LLMProvider implementation for wiring tests (no network)."""

    async def stream(self, messages, tools=None):
        # Satisfy the Protocol; never actually invoked in these tests.
        if False:  # pragma: no cover
            yield {"type": "done"}


@pytest.mark.unit
class TestAgentLLM:
    """phone.agent(llm=...) stores the provider and validates its type."""

    def test_stores_llm_on_agent(self) -> None:
        phone = _local_phone()
        llm = _DummyLLM()
        agent = phone.agent(
            system_prompt="hi",
            stt=DeepgramSTT(api_key="dg"),
            tts=ElevenLabsTTS(api_key="el"),
            llm=llm,
        )
        assert agent.llm is llm
        # `llm=` implies pipeline mode.
        assert agent.provider == "pipeline"

    def test_rejects_non_llm_provider(self) -> None:
        phone = _local_phone()
        with pytest.raises(TypeError, match="LLMProvider"):
            phone.agent(system_prompt="hi", llm="not-an-llm")

    def test_engine_plus_llm_warns(self, caplog) -> None:
        phone = _local_phone()
        engine = OpenAIRealtime(api_key="sk-test")
        llm = _DummyLLM()
        with caplog.at_level(logging.WARNING, logger="patter"):
            agent = phone.agent(
                system_prompt="hi",
                engine=engine,
                llm=llm,
            )
        # Engine mode wins — provider stays realtime.
        assert agent.provider == "openai_realtime"
        assert agent.llm is llm  # stored, but ignored by the engine-mode path.
        assert any(
            "llm= ignored" in rec.message for rec in caplog.records
        ), f"expected warning, got {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# LLMLoop accepts injected provider
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLLMLoopInjection:
    """LLMLoop accepts a pre-built ``LLMProvider`` via ``llm_provider=``."""

    def test_injected_provider_used(self) -> None:
        dummy = _DummyLLM()
        loop = LLMLoop(
            openai_key="",  # not used when llm_provider is supplied
            model="ignored",
            system_prompt="sys",
            llm_provider=dummy,
        )
        assert loop._provider is dummy

    def test_default_openai_path_still_works(self) -> None:
        # No llm_provider → constructs the default OpenAILLMProvider using the
        # provided openai_key. This must not raise at construction time.
        loop = LLMLoop(
            openai_key="sk-test",
            model="gpt-4o-mini",
            system_prompt="sys",
        )
        # We don't care about the exact type, only that a provider exists.
        assert loop._provider is not None


# ---------------------------------------------------------------------------
# serve() conflict — agent.llm + on_message
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServeConflict:
    """phone.serve() raises when both agent.llm and on_message are set."""

    async def test_llm_plus_on_message_raises(self) -> None:
        phone = _local_phone()
        llm = _DummyLLM()
        agent = phone.agent(
            system_prompt="hi",
            stt=DeepgramSTT(api_key="dg"),
            tts=ElevenLabsTTS(api_key="el"),
            llm=llm,
        )

        async def handler(msg):  # pragma: no cover — should never run
            return "ok"

        with pytest.raises(ValueError, match="both"):
            await phone.serve(agent, on_message=handler)


# ---------------------------------------------------------------------------
# Flat re-export parity
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFlatReExports:
    """The flat ``from getpatter import *LLM`` aliases resolve correctly."""

    def test_flat_aliases_match_namespaced(self) -> None:
        from getpatter.llm.openai import LLM as ns_openai
        from getpatter.llm.anthropic import LLM as ns_anthropic
        from getpatter.llm.groq import LLM as ns_groq
        from getpatter.llm.cerebras import LLM as ns_cerebras
        from getpatter.llm.google import LLM as ns_google

        assert OpenAILLM is ns_openai
        assert AnthropicLLM is ns_anthropic
        assert GroqLLM is ns_groq
        assert CerebrasLLM is ns_cerebras
        assert GoogleLLM is ns_google
