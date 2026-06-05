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
    # Hermes is stateless and keys continuity off HEADERS:
    #   X-Hermes-Session-Id (per call) + optional X-Hermes-Session-Key (memory).
    assert llm._session_user_prefix == "patter-call-"
    assert llm._session_id_header == "X-Hermes-Session-Id"
    assert llm._session_id_prefix == "patter-call-"
    assert llm._session_key_header == "X-Hermes-Session-Key"
    assert llm.provider_key == "hermes"


@pytest.mark.unit
def test_hermes_session_key_off_by_default_and_configurable() -> None:
    # Default: no session_key value held => X-Hermes-Session-Key not emitted.
    assert hermes.LLM()._session_key is None
    # Configurable long-term memory scope.
    llm = hermes.LLM(session_key="mem-123")
    assert llm._session_key == "mem-123"
    kwargs = llm._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="c1"
    )
    assert kwargs["extra_headers"]["X-Hermes-Session-Key"] == "mem-123"
    assert kwargs["extra_headers"]["X-Hermes-Session-Id"] == "patter-call-c1"


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
    # Wire-identical to the old session_header behaviour: per-call header with
    # an empty prefix => the raw call id.
    assert (
        llm._session_id_header == _OPENCLAW_SESSION_HEADER == "x-openclaw-session-key"
    )
    assert llm._session_id_prefix == ""
    assert llm._session_user_prefix == "patter-call-"
    # OpenClaw has no separate memory-scope header.
    assert llm._session_key_header is None
    assert llm._client.timeout == 120.0
    assert llm.provider_key == "openclaw"


@pytest.mark.unit
def test_openclaw_wire_output_is_byte_identical(monkeypatch) -> None:
    """The OpenClaw preset emits user='patter-call-<id>' and the raw call id in
    the x-openclaw-session-key header — unchanged by the param rename."""
    monkeypatch.delenv("OPENCLAW_API_KEY", raising=False)
    llm = openclaw.LLM(agent="receptionist")
    kwargs = llm._build_completion_kwargs(
        [{"role": "user", "content": "hi"}], None, call_id="c2"
    )
    assert kwargs["user"] == "patter-call-c2"
    assert kwargs["extra_headers"] == {"x-openclaw-session-key": "c2"}


@pytest.mark.unit
def test_openclaw_api_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_API_KEY", "operator-grade-token")
    llm = openclaw.LLM(agent="receptionist")
    assert llm._client.api_key == "operator-grade-token"


# ---------------------------------------------------------------------------
# Wire-level — mocks ONLY the paid boundary (chat.completions.create).
# ---------------------------------------------------------------------------


class _Choice:
    def __init__(self, content) -> None:
        self.delta = type("D", (), {"content": content, "tool_calls": None})()


class _Chunk:
    def __init__(self, content) -> None:
        self.choices = [_Choice(content)]
        self.usage = None


class _FakeStream:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:  # pragma: no cover - not exercised
        pass


async def _capture_create_kwargs(llm, call_id="hid-1") -> dict:
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeStream([_Chunk("ok")])

    llm._client.chat.completions.create = fake_create
    async for _ in llm.stream(
        [{"role": "user", "content": "hi"}], None, call_id=call_id
    ):
        pass
    return captured


@pytest.mark.mocked
async def test_hermes_sends_session_id_header_by_default() -> None:
    """Hermes emits X-Hermes-Session-Id=patter-call-<id> on the wire; the
    memory-scope header is absent unless session_key is configured."""
    captured = await _capture_create_kwargs(hermes.LLM(), call_id="hid-1")
    headers = captured["extra_headers"]
    assert headers["X-Hermes-Session-Id"] == "patter-call-hid-1"
    assert "X-Hermes-Session-Key" not in headers


@pytest.mark.mocked
async def test_hermes_sends_session_key_header_when_configured() -> None:
    captured = await _capture_create_kwargs(
        hermes.LLM(session_key="mem-xyz"), call_id="hid-2"
    )
    headers = captured["extra_headers"]
    assert headers["X-Hermes-Session-Id"] == "patter-call-hid-2"
    assert headers["X-Hermes-Session-Key"] == "mem-xyz"


@pytest.mark.mocked
async def test_openclaw_sends_raw_call_id_header_on_the_wire() -> None:
    captured = await _capture_create_kwargs(
        openclaw.LLM(agent="receptionist"), call_id="c3"
    )
    assert captured["user"] == "patter-call-c3"
    assert captured["extra_headers"] == {"x-openclaw-session-key": "c3"}
