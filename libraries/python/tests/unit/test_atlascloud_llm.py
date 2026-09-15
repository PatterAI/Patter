"""Tests for the Atlas Cloud LLM provider (OpenAI-compatible gateway).

Atlas Cloud fronts many upstream model vendors behind one OpenAI-compatible
Chat Completions endpoint, so the provider is a thin
:class:`OpenAILLMProvider` subclass that only swaps the client's ``base_url``
and credential source. These tests pin the three things that swap can get
wrong: the endpoint actually points at Atlas, the parent's User-Agent survives
the client replacement (the gateway rejects the default ``python-httpx`` UA
family at the edge), and cost attribution is tagged ``atlascloud`` rather than
inheriting ``openai`` from the parent.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from getpatter.llm import atlascloud as atlascloud_ns
from getpatter.providers.atlascloud_llm import AtlasCloudLLMProvider, AtlasCloudModel


def _provider(**kwargs) -> AtlasCloudLLMProvider:
    kwargs.setdefault("api_key", "apikey-test")
    return AtlasCloudLLMProvider(**kwargs)


def test_default_model_is_gpt_4_1_mini() -> None:
    """Default is the cheapest broadly-available chat model on the gateway.

    Voice turns are short and latency-sensitive; the larger reasoning models in
    :class:`AtlasCloudModel` stay reachable via ``model=``.
    """
    assert _provider()._model == "openai/gpt-4.1-mini"


def test_explicit_model_override_is_honoured() -> None:
    assert _provider(model="deepseek-ai/deepseek-v3.2")._model == "deepseek-ai/deepseek-v3.2"


def test_model_enum_values_are_accepted() -> None:
    """``AtlasCloudModel`` is a ``StrEnum``, so members pass straight through."""
    provider = _provider(model=AtlasCloudModel.GLM_4_6)
    assert provider._model == "zai-org/GLM-4.6"


def test_client_points_at_the_atlas_cloud_gateway() -> None:
    provider = _provider()
    assert str(provider._client.base_url).rstrip("/") == "https://api.atlascloud.ai/v1"


def test_base_url_override_is_honoured() -> None:
    provider = _provider(base_url="https://gateway.internal/v1")
    assert str(provider._client.base_url).rstrip("/") == "https://gateway.internal/v1"


def test_client_carries_the_patter_user_agent() -> None:
    """The swapped-in client must keep the User-Agent the parent computed.

    Replacing ``self._client`` drops the parent's default headers, so the UA
    has to be re-applied explicitly — otherwise Patter traffic becomes
    indistinguishable from a bare SDK call at the gateway.
    """
    provider = _provider()
    assert provider._client.default_headers["User-Agent"] == provider._user_agent
    assert "getpatter" in provider._user_agent.lower()


def test_api_key_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLASCLOUD_API_KEY", "apikey-from-env")
    provider = AtlasCloudLLMProvider()
    assert provider._client.api_key == "apikey-from-env"


def test_explicit_api_key_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLASCLOUD_API_KEY", "apikey-from-env")
    assert _provider(api_key="apikey-explicit")._client.api_key == "apikey-explicit"


def test_missing_api_key_raises_with_a_named_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLASCLOUD_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ATLASCLOUD_API_KEY"):
        AtlasCloudLLMProvider()


def test_provider_key_is_atlascloud() -> None:
    """``provider_key`` drives pricing lookup and the dashboard breakdown."""
    assert AtlasCloudLLMProvider.provider_key == "atlascloud"


def test_cost_attribution_is_tagged_atlascloud() -> None:
    """Without the override, spend would be attributed to ``openai`` (parent)."""
    recorded: dict = {}

    def _capture(attrs):
        recorded.update(attrs)

    with patch(
        "getpatter.observability.attributes.record_patter_attrs", new=_capture
    ):
        _provider()._record_completion_cost(prompt_tokens=11, completion_tokens=22)

    assert recorded["patter.llm.provider"] == "atlascloud"
    assert recorded["patter.cost.llm_input_tokens"] == 11
    assert recorded["patter.cost.llm_output_tokens"] == 22


def test_namespace_llm_is_the_same_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """``from getpatter.llm import atlascloud`` mirrors the other adapters."""
    monkeypatch.setenv("ATLASCLOUD_API_KEY", "apikey-from-env")
    llm = atlascloud_ns.LLM()
    assert isinstance(llm, AtlasCloudLLMProvider)
    assert llm.provider_key == "atlascloud"
    assert llm._model == "openai/gpt-4.1-mini"


def test_namespace_llm_raises_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATLASCLOUD_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ATLASCLOUD_API_KEY"):
        atlascloud_ns.LLM()
