"""Inworld integration: TTS pricing/models, config path, and Router LLM preset.

Pricing rates verified against the On-Demand list price on
https://inworld.ai/pricing (TTS-2 $25/1M, TTS 1.5 Max $35/1M, TTS 1.5 Mini
$15/1M → $/1k chars) and the OpenAI-compatible Router endpoint
(https://docs.inworld.ai/router/openai-compatibility).
"""

from __future__ import annotations

import pytest

from getpatter import InworldLLM, providers
from getpatter.llm.inworld import INWORLD_ROUTER_BASE_URL
from getpatter.pricing import DEFAULT_PRICING, PricingUnit


class TestInworldTTSPricing:
    def test_unit_is_per_thousand_chars(self) -> None:
        assert DEFAULT_PRICING["inworld"]["unit"] == PricingUnit.THOUSAND_CHARS

    def test_all_three_models_priced_on_demand_rates(self) -> None:
        models = DEFAULT_PRICING["inworld"]["models"]
        assert models["inworld-tts-2"]["price"] == 0.025  # $25 / 1M
        assert models["inworld-tts-1.5-max"]["price"] == 0.035  # $35 / 1M
        assert models["inworld-tts-1.5-mini"]["price"] == 0.015  # $15 / 1M

    def test_default_is_tts_2_rate(self) -> None:
        assert DEFAULT_PRICING["inworld"]["price"] == 0.025


class TestInworldTTSConfigHelper:
    def test_providers_inworld_builds_tts_config(self) -> None:
        cfg = providers.inworld("tok", voice="Olivia", model="inworld-tts-1.5-mini")
        assert cfg.provider == "inworld"
        assert cfg.api_key == "tok"
        assert cfg.voice == "Olivia"
        assert cfg.options == {"model": "inworld-tts-1.5-mini"}

    def test_default_model_and_voice(self) -> None:
        cfg = providers.inworld("tok")
        assert cfg.voice == "Ashley"
        assert cfg.options == {"model": "inworld-tts-2"}

    def test_create_tts_from_config_builds_adapter(self) -> None:
        pytest.importorskip("aiohttp")
        from getpatter.telephony.common import _create_tts_from_config

        cfg = providers.inworld("tok", voice="Olivia", model="inworld-tts-1.5-mini")
        adapter = _create_tts_from_config(cfg)
        assert type(adapter).__name__ == "InworldTTS"
        assert adapter.voice == "Olivia"
        assert adapter.model == "inworld-tts-1.5-mini"
        assert adapter.auth_token == "tok"


class TestInworldRouterLLM:
    def test_constructs_with_explicit_key(self) -> None:
        llm = InworldLLM(api_key="basic-token", model="openai/gpt-4o-mini")
        assert llm.provider_key == "inworld_router"
        assert str(llm._client.base_url).rstrip("/") == INWORLD_ROUTER_BASE_URL

    def test_reads_env_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INWORLD_API_KEY", "from-env")
        llm = InworldLLM(model="anthropic/claude-haiku-4-5-20251001")
        assert str(llm._client.base_url).rstrip("/") == INWORLD_ROUTER_BASE_URL

    def test_requires_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("INWORLD_API_KEY", raising=False)
        with pytest.raises(ValueError, match="Inworld Router LLM requires an api_key"):
            InworldLLM(model="openai/gpt-4o-mini")

    def test_provider_key_distinct_from_tts(self) -> None:
        # The TTS provider key 'inworld' is char-priced; the Router LLM must NOT
        # collide with it (token-based, at-cost), so it uses 'inworld_router'.
        assert InworldLLM.provider_key == "inworld_router"
        assert "inworld_router" not in DEFAULT_PRICING  # at-cost, per-model
