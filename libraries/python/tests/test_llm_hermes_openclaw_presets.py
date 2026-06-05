"""Tests for the Hermes and OpenClaw thin LLM presets.

Real construction throughout — no mocks. The presets defer to
``OpenAICompatibleLLMProvider`` so these assertions read the live constructed
client (base URL / timeout) and the session-continuity config.
"""

from __future__ import annotations

import pytest

from getpatter.llm import hermes, openclaw
from getpatter.models import (
    _OPENCLAW_API_KEY_ENV,
    _OPENCLAW_DEFAULT_BASE_URL,
    _OPENCLAW_SESSION_HEADER,
)


def _base_url_str(provider) -> str:
    return str(provider._client.base_url)


# ---------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hermes_defaults_base_url_model_timeout(monkeypatch) -> None:
    monkeypatch.delenv("API_SERVER_MODEL_NAME", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    llm = hermes.LLM()
    assert _base_url_str(llm).startswith("http://127.0.0.1:8642/v1")
    assert llm._model == "hermes-agent"
    assert llm._client.timeout == 120.0
    assert llm._session_user_prefix == "patter-call-"
    # Hermes keys sessions off the OpenAI `user` field only — no session header.
    assert llm._session_header is None
    assert llm.provider_key == "hermes"


@pytest.mark.unit
def test_hermes_model_env_override(monkeypatch) -> None:
    monkeypatch.setenv("API_SERVER_MODEL_NAME", "hermes-7b")
    assert hermes.LLM()._model == "hermes-7b"
    # Explicit model arg still wins over the env default.
    assert hermes.LLM(model="hermes-custom")._model == "hermes-custom"


@pytest.mark.unit
def test_hermes_api_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv("API_SERVER_KEY", "hermes-key")
    assert hermes.LLM()._client.api_key == "hermes-key"
    # Keyless local Hermes — absent env, no api_key — still constructs.
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    assert hermes.LLM()._client.api_key == "EMPTY"


# ---------------------------------------------------------------------------
# OpenClaw
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_openclaw_agent_maps_to_namespaced_model() -> None:
    assert openclaw.LLM(agent="receptionist")._model == "openclaw/receptionist"
    # Already-namespaced ids pass through unchanged.
    assert openclaw.LLM(agent="openclaw/custom")._model == "openclaw/custom"
    assert openclaw.LLM(agent="openclaw:custom")._model == "openclaw:custom"
    assert openclaw.LLM(agent="agent:x")._model == "agent:x"


@pytest.mark.unit
def test_openclaw_rejects_invalid_agent_id() -> None:
    with pytest.raises(ValueError, match="letters, digits"):
        openclaw.LLM(agent="a b")  # space is outside the charset
    with pytest.raises(ValueError):
        openclaw.LLM(agent="")


@pytest.mark.unit
def test_openclaw_defaults_match_consult_preset(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_API_KEY", raising=False)
    llm = openclaw.LLM(agent="receptionist")
    # Byte-identical to the shipped consult preset constants in models.py.
    assert _base_url_str(llm).startswith(_OPENCLAW_DEFAULT_BASE_URL)
    assert _OPENCLAW_DEFAULT_BASE_URL == "http://127.0.0.1:18789/v1"
    assert _OPENCLAW_API_KEY_ENV == "OPENCLAW_API_KEY"
    assert llm._session_header == _OPENCLAW_SESSION_HEADER == "x-openclaw-session-key"
    assert llm._session_user_prefix == "patter-call-"
    assert llm._client.timeout == 120.0
    assert llm.provider_key == "openclaw"


@pytest.mark.unit
def test_openclaw_api_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_API_KEY", "operator-grade-token")
    llm = openclaw.LLM(agent="receptionist")
    assert llm._client.api_key == "operator-grade-token"
