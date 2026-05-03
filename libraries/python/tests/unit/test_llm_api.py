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
#
# ``find_spec`` for a dotted child path raises ``ModuleNotFoundError`` when the
# parent doesn't exist (instead of returning None), so we catch it.
def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


_ANTHROPIC_AVAILABLE = _has_module("anthropic")
_GOOGLE_GENAI_AVAILABLE = _has_module("google.genai")

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

    async def stream(self, messages, tools=None, **_kwargs):
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


# ---------------------------------------------------------------------------
# OpenAILLMProvider — sampling kwargs forwarding (Wave 10A refactor)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenAIProviderSamplingKwargs:
    """Sampling kwargs configured on the parent class are forwarded to
    ``chat.completions.create`` (and ``max_tokens`` is mapped to
    ``max_completion_tokens`` on the wire, per current OpenAI spec)."""

    @pytest.mark.asyncio
    async def test_kwargs_forwarded_to_chat_completions(self, monkeypatch) -> None:
        from getpatter.services.llm_loop import OpenAILLMProvider

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        provider = OpenAILLMProvider(
            api_key="sk-test",
            model="gpt-4o-mini",
            temperature=0.6,
            max_tokens=200,
            response_format={"type": "json_object"},
            seed=42,
            top_p=0.9,
            frequency_penalty=0.1,
            presence_penalty=0.2,
            stop=["END"],
            tool_choice="auto",
            parallel_tool_calls=True,
        )

        captured: dict = {}

        async def _fake_create(**kwargs):
            captured.update(kwargs)

            async def _empty():
                if False:  # pragma: no cover
                    yield None

            return _empty()

        # Replace the bound coroutine on the underlying client.
        provider._client.chat.completions.create = _fake_create  # type: ignore[assignment]

        # Drain the generator so ``stream()`` actually invokes ``create``.
        async for _ in provider.stream([{"role": "user", "content": "hi"}]):
            pass

        assert captured["model"] == "gpt-4o-mini"
        assert captured["temperature"] == 0.6
        # max_tokens is mapped to ``max_completion_tokens`` on the wire.
        assert captured["max_completion_tokens"] == 200
        assert "max_tokens" not in captured
        assert captured["response_format"] == {"type": "json_object"}
        assert captured["seed"] == 42
        assert captured["top_p"] == 0.9
        assert captured["frequency_penalty"] == 0.1
        assert captured["presence_penalty"] == 0.2
        assert captured["stop"] == ["END"]
        assert captured["tool_choice"] == "auto"
        assert captured["parallel_tool_calls"] is True
        assert captured["stream"] is True
        assert captured["stream_options"] == {"include_usage": True}

    def test_user_agent_default_header(self) -> None:
        from getpatter import __version__
        from getpatter.services.llm_loop import OpenAILLMProvider

        provider = OpenAILLMProvider(api_key="sk-test", model="gpt-4o-mini")
        assert provider._user_agent == f"getpatter/{__version__}"

    def test_user_agent_override(self) -> None:
        from getpatter.services.llm_loop import OpenAILLMProvider

        provider = OpenAILLMProvider(
            api_key="sk-test",
            model="gpt-4o-mini",
            user_agent="myapp/1.0",
        )
        assert provider._user_agent == "myapp/1.0"
