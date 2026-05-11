"""Tests for the pricing registry and cost calculation functions."""

import pytest

from getpatter.pricing import (
    DEFAULT_PRICING,
    LLM_PRICING,
    calculate_llm_cost,
    calculate_realtime_cached_savings,
    calculate_realtime_cost,
    calculate_stt_cost,
    calculate_telephony_cost,
    calculate_tts_cost,
    merge_pricing,
)


class TestMergePricing:
    def test_returns_copy_without_overrides(self):
        result = merge_pricing(None)
        assert result == DEFAULT_PRICING
        # Should be a copy, not the original
        result["deepgram"]["price"] = 999
        assert DEFAULT_PRICING["deepgram"]["price"] != 999

    def test_overrides_existing_provider(self):
        result = merge_pricing({"deepgram": {"price": 0.005}})
        assert result["deepgram"]["price"] == 0.005
        # Unit should be preserved from default
        assert result["deepgram"]["unit"] == "minute"

    def test_adds_new_provider(self):
        result = merge_pricing({"custom_stt": {"unit": "minute", "price": 0.01}})
        assert result["custom_stt"]["price"] == 0.01
        # Defaults should still exist
        assert "deepgram" in result

    def test_empty_overrides(self):
        result = merge_pricing({})
        assert result == DEFAULT_PRICING


class TestCalculateSTTCost:
    def test_deepgram_cost(self):
        pricing = merge_pricing(None)
        # 60 seconds = 1 minute at $0.0048/min (Nova-3 streaming monolingual,
        # the Patter default). Current Pay-As-You-Go promotional rate per
        # deepgram.com/pricing (verified 2026-05-11). The legacy $0.0077/min
        # was the launch-era standard rate.
        cost = calculate_stt_cost("deepgram", 60.0, pricing)
        assert abs(cost - 0.0048) < 1e-6

    def test_whisper_cost(self):
        pricing = merge_pricing(None)
        cost = calculate_stt_cost("whisper", 120.0, pricing)
        # 2 minutes at $0.006/min = $0.012
        assert abs(cost - 0.012) < 1e-6

    def test_zero_duration(self):
        pricing = merge_pricing(None)
        cost = calculate_stt_cost("deepgram", 0.0, pricing)
        assert cost == 0.0

    def test_unknown_provider(self):
        pricing = merge_pricing(None)
        cost = calculate_stt_cost("unknown", 60.0, pricing)
        assert cost == 0.0


class TestCalculateTTSCost:
    def test_elevenlabs_cost(self):
        pricing = merge_pricing(None)
        # 1000 characters at $0.05/1k = $0.05 (eleven_flash_v2_5 default).
        # Source: https://elevenlabs.io/pricing/api (verified 2026-05-11).
        cost = calculate_tts_cost("elevenlabs", 1000, pricing)
        assert abs(cost - 0.05) < 1e-6

    def test_openai_tts_cost(self):
        pricing = merge_pricing(None)
        # 500 characters at $0.015/1k = $0.0075
        cost = calculate_tts_cost("openai_tts", 500, pricing)
        assert abs(cost - 0.0075) < 1e-6

    def test_zero_characters(self):
        pricing = merge_pricing(None)
        cost = calculate_tts_cost("elevenlabs", 0, pricing)
        assert cost == 0.0

    def test_unknown_provider(self):
        pricing = merge_pricing(None)
        cost = calculate_tts_cost("unknown", 1000, pricing)
        assert cost == 0.0


class TestCalculateRealtimeCost:
    def test_with_token_details(self):
        pricing = merge_pricing(None)
        usage = {
            "input_token_details": {"audio_tokens": 100, "text_tokens": 50},
            "output_token_details": {"audio_tokens": 200, "text_tokens": 30},
        }
        cost = calculate_realtime_cost(usage, pricing)
        config = pricing["openai_realtime"]
        expected = (
            100 * config["audio_input_per_token"]
            + 50 * config["text_input_per_token"]
            + 200 * config["audio_output_per_token"]
            + 30 * config["text_output_per_token"]
        )
        assert abs(cost - expected) < 1e-10

    def test_empty_usage(self):
        pricing = merge_pricing(None)
        cost = calculate_realtime_cost({}, pricing)
        assert cost == 0.0

    def test_missing_token_details(self):
        pricing = merge_pricing(None)
        usage = {"total_tokens": 100}
        cost = calculate_realtime_cost(usage, pricing)
        assert cost == 0.0

    def test_cached_tokens_discounted(self):
        pricing = merge_pricing(None)
        usage = {
            "input_token_details": {
                "audio_tokens": 1000,
                "text_tokens": 500,
                "cached_tokens_details": {"audio_tokens": 800, "text_tokens": 400},
            },
            "output_token_details": {"audio_tokens": 0, "text_tokens": 0},
        }
        cost = calculate_realtime_cost(usage, pricing)
        # (1000-800)*1e-5 + 800*3e-7 + (500-400)*6e-7 + 400*6e-8
        expected = 200 * 1e-5 + 800 * 3e-7 + 100 * 6e-7 + 400 * 6e-8
        assert abs(cost - expected) < 1e-10

    def test_cached_clamp_when_over_total(self):
        pricing = merge_pricing(None)
        usage = {
            "input_token_details": {
                "audio_tokens": 100,
                "cached_tokens_details": {"audio_tokens": 500},
            },
        }
        cost = calculate_realtime_cost(usage, pricing)
        # Clamped to 100: all 100 billed at cached rate
        expected = 100 * 3e-7
        assert abs(cost - expected) < 1e-10
        assert cost >= 0

    def test_null_input_token_details_does_not_crash(self):
        """OpenAI can emit ``null`` for input_token_details on early errors."""
        pricing = merge_pricing(None)
        usage = {
            "input_token_details": None,  # null in JSON
            "output_token_details": {"audio_tokens": 50},
        }
        # Must NOT raise AttributeError
        cost = calculate_realtime_cost(usage, pricing)
        assert abs(cost - 50 * 2e-5) < 1e-10


class TestCalculateRealtimeCachedSavings:
    def test_positive_savings(self):
        pricing = merge_pricing(None)
        usage = {
            "input_token_details": {
                "audio_tokens": 1000,
                "text_tokens": 500,
                "cached_tokens_details": {"audio_tokens": 800, "text_tokens": 400},
            },
        }
        savings = calculate_realtime_cached_savings(usage, pricing)
        # 800 * (1e-5 - 3e-7) + 400 * (6e-7 - 6e-8)
        expected = 800 * (1e-5 - 3e-7) + 400 * (6e-7 - 6e-8)
        assert abs(savings - expected) < 1e-10
        assert savings > 0

    def test_misconfigured_cached_rate_higher_than_full_clamps_to_zero(self):
        """If a user overrides cached rate HIGHER than full, savings would go
        negative. Must clamp to 0 — matching TS parity."""
        # cached_audio_input_per_token HIGHER than audio_input_per_token
        pricing = merge_pricing(
            {
                "openai_realtime": {
                    "cached_audio_input_per_token": 0.0001,  # 10x higher than full
                    "cached_text_input_per_token": 0.00001,
                }
            }
        )
        usage = {
            "input_token_details": {
                "audio_tokens": 1000,
                "text_tokens": 500,
                "cached_tokens_details": {"audio_tokens": 500, "text_tokens": 250},
            },
        }
        savings = calculate_realtime_cached_savings(usage, pricing)
        # Would otherwise be negative; must clamp to 0
        assert savings == 0.0

    def test_no_cached_tokens_zero_savings(self):
        pricing = merge_pricing(None)
        usage = {
            "input_token_details": {
                "audio_tokens": 1000,
                "text_tokens": 500,
            },
        }
        savings = calculate_realtime_cached_savings(usage, pricing)
        assert savings == 0.0


class TestCalculateTelephonyCost:
    def test_twilio_cost(self):
        pricing = merge_pricing(None)
        # 5 minutes at $0.0085/min (US inbound local default) = $0.0425
        cost = calculate_telephony_cost("twilio", 300.0, pricing)
        assert abs(cost - 0.0425) < 1e-6

    def test_telnyx_cost(self):
        pricing = merge_pricing(None)
        # 10 minutes at $0.007/min = $0.07
        cost = calculate_telephony_cost("telnyx", 600.0, pricing)
        assert abs(cost - 0.07) < 1e-6

    def test_telnyx_inbound_rate(self):
        """US inbound DID / local termination billed at $0.0035/min.

        Verified against https://telnyx.com/pricing/elastic-sip (2026-05-11).
        """
        pricing = merge_pricing(None)
        cost = calculate_telephony_cost("telnyx_inbound", 60.0, pricing)
        assert abs(cost - 0.0035) < 1e-6

    def test_telnyx_outbound_rate(self):
        """US outbound Pay-As-You-Go billed at $0.007/min (mid-range)."""
        pricing = merge_pricing(None)
        cost = calculate_telephony_cost("telnyx_outbound", 60.0, pricing)
        assert abs(cost - 0.007) < 1e-6

    def test_telnyx_legacy_falls_back_to_outbound(self):
        """Legacy flat ``telnyx`` key remains at $0.007/min (outbound-safe).

        Users who override ``pricing={"telnyx": {...}}`` without a
        direction-aware split keep the previous behaviour. Direction-aware
        billing is opt-in via the new ``telnyx_inbound`` / ``telnyx_outbound``
        keys.
        """
        pricing = merge_pricing(None)
        cost = calculate_telephony_cost("telnyx", 60.0, pricing)
        assert abs(cost - 0.007) < 1e-6

    def test_zero_duration(self):
        pricing = merge_pricing(None)
        cost = calculate_telephony_cost("twilio", 0.0, pricing)
        assert cost == 0.0


class TestRealtime2Pricing:
    """Per-model rates for ``gpt-realtime-2`` live under ``openai_realtime.models``."""

    def test_model_entry_present(self):
        models = DEFAULT_PRICING["openai_realtime"]["models"]
        assert "gpt-realtime-2" in models
        assert "gpt-realtime" in models
        assert "gpt-realtime-mini" in models
        assert "gpt-4o-realtime-preview" in models

    def test_rates_match_openai_published(self):
        """Spot-check the published per-1M-token rates match $/token math."""
        entry = DEFAULT_PRICING["openai_realtime"]["models"]["gpt-realtime-2"]
        # $4/M input tokens text -> 0.000004 per token
        assert entry["text_input_per_token"] == pytest.approx(4.0 / 1_000_000)
        assert entry["text_output_per_token"] == pytest.approx(24.0 / 1_000_000)
        # $32/M audio in, $64/M audio out
        assert entry["audio_input_per_token"] == pytest.approx(32.0 / 1_000_000)
        assert entry["audio_output_per_token"] == pytest.approx(64.0 / 1_000_000)
        # Cached input tier: $0.40/M for both audio and text
        assert entry["cached_audio_input_per_token"] == pytest.approx(0.4 / 1_000_000)
        assert entry["cached_text_input_per_token"] == pytest.approx(0.4 / 1_000_000)

    def test_realtime_cost_auto_resolves_per_model(self):
        """``calculate_realtime_cost(model='gpt-realtime-2')`` auto-picks the
        nested rate without any override.
        """
        pricing = merge_pricing(None)
        usage = {
            "input_token_details": {"audio_tokens": 1000, "text_tokens": 0},
            "output_token_details": {"audio_tokens": 0, "text_tokens": 0},
        }
        # 1000 audio input tokens at $32/M = $0.032 — gpt-realtime-2 rate
        cost = calculate_realtime_cost(usage, pricing, model="gpt-realtime-2")
        assert cost == pytest.approx(0.032)

    def test_realtime_cost_falls_back_to_default_when_model_absent(self):
        """Unknown model → provider defaults (gpt-realtime-mini rates)."""
        pricing = merge_pricing(None)
        usage = {
            "input_token_details": {"audio_tokens": 1000, "text_tokens": 0},
            "output_token_details": {"audio_tokens": 0, "text_tokens": 0},
        }
        # 1000 * $10/M = $0.01 (mini rate)
        cost_unknown = calculate_realtime_cost(
            usage, pricing, model="some-future-model"
        )
        cost_none = calculate_realtime_cost(usage, pricing)
        assert cost_unknown == pytest.approx(0.01)
        assert cost_none == pytest.approx(0.01)


class TestModelAwareSttPricing:
    """STT cost calc honours per-model overrides under ``models``."""

    def test_deepgram_default_is_nova3_streaming(self):
        pricing = merge_pricing(None)
        # 60 seconds at $0.0048/min = $0.0048 (nova-3 default, current PAYG
        # promotional rate per deepgram.com/pricing, verified 2026-05-11).
        assert calculate_stt_cost("deepgram", 60.0, pricing) == pytest.approx(0.0048)

    def test_deepgram_multilingual_uses_nested_rate(self):
        pricing = merge_pricing(None)
        # nova-3-multilingual is $0.0058/min (PAYG promo rate)
        cost = calculate_stt_cost(
            "deepgram", 60.0, pricing, model="nova-3-multilingual"
        )
        assert cost == pytest.approx(0.0058)

    def test_deepgram_unknown_model_falls_back_to_default(self):
        pricing = merge_pricing(None)
        cost = calculate_stt_cost("deepgram", 60.0, pricing, model="some-future-model")
        assert cost == pytest.approx(0.0048)

    def test_whisper_per_model_rates(self):
        pricing = merge_pricing(None)
        # gpt-4o-mini-transcribe at $0.003/min
        cost = calculate_stt_cost(
            "whisper", 60.0, pricing, model="gpt-4o-mini-transcribe"
        )
        assert cost == pytest.approx(0.003)
        # gpt-realtime-whisper at $0.017/min
        cost = calculate_stt_cost(
            "whisper", 60.0, pricing, model="gpt-realtime-whisper"
        )
        assert cost == pytest.approx(0.017)


class TestModelAwareTtsPricing:
    """TTS cost calc honours per-model overrides under ``models``."""

    def test_elevenlabs_default_is_flash_v2_5(self):
        pricing = merge_pricing(None)
        # 1000 chars at $0.05/1k = $0.05 (verified vs elevenlabs.io/pricing/api)
        assert calculate_tts_cost("elevenlabs", 1000, pricing) == pytest.approx(0.05)

    def test_elevenlabs_multilingual_v2_per_model_rate(self):
        pricing = merge_pricing(None)
        cost = calculate_tts_cost(
            "elevenlabs", 1000, pricing, model="eleven_multilingual_v2"
        )
        # Multilingual v2 / v3 share the $0.10/1k tier per the public API page.
        assert cost == pytest.approx(0.10)

    def test_openai_tts_hd_per_model_rate(self):
        pricing = merge_pricing(None)
        # tts-1-hd is $0.030/1k under the nested entry
        cost = calculate_tts_cost("openai_tts", 1000, pricing, model="tts-1-hd")
        assert cost == pytest.approx(0.030)

    def test_openai_tts_default_is_tts1(self):
        pricing = merge_pricing(None)
        cost = calculate_tts_cost("openai_tts", 1000, pricing)
        assert cost == pytest.approx(0.015)

    def test_inworld_tts_2_default(self):
        pricing = merge_pricing(None)
        cost = calculate_tts_cost("inworld", 1000, pricing, model="inworld-tts-2")
        assert cost == pytest.approx(0.020)


class TestPerModelOverrideMerge:
    """User overrides at the ``models`` level overlay rather than replace."""

    def test_models_dict_merges_shallowly(self):
        """Overriding one model leaves the others intact."""
        pricing = merge_pricing(
            {"elevenlabs": {"models": {"eleven_flash_v2_5": {"price": 0.04}}}}
        )
        # Overridden
        assert calculate_tts_cost(
            "elevenlabs", 1000, pricing, model="eleven_flash_v2_5"
        ) == pytest.approx(0.04)
        # Untouched — still original $0.10
        assert calculate_tts_cost(
            "elevenlabs", 1000, pricing, model="eleven_multilingual_v2"
        ) == pytest.approx(0.10)

    def test_user_can_register_brand_new_model(self):
        pricing = merge_pricing(
            {"deepgram": {"models": {"my-private-model": {"price": 0.012}}}}
        )
        cost = calculate_stt_cost("deepgram", 60.0, pricing, model="my-private-model")
        assert cost == pytest.approx(0.012)

    def test_longest_prefix_match_for_versioned_model_id(self):
        """Versioned IDs like ``gpt-realtime-2-2026-05-08`` resolve against
        the canonical ``gpt-realtime-2`` rate."""
        pricing = merge_pricing(None)
        usage = {
            "input_token_details": {"audio_tokens": 1000, "text_tokens": 0},
            "output_token_details": {"audio_tokens": 0, "text_tokens": 0},
        }
        cost = calculate_realtime_cost(
            usage, pricing, model="gpt-realtime-2-2026-05-08"
        )
        # Should resolve to gpt-realtime-2 rate ($32/M audio in)
        assert cost == pytest.approx(0.032)


class TestLLMCostBilling:
    """Regressions for the silent under-billing class — Cerebras + Groq.

    Before these entries were added, every Patter user running the Cerebras
    default model (``gpt-oss-120b``) or any Groq model outside the two
    versatile/instant ones billed exactly $0 for LLM tokens. ``calculate_llm_cost``
    falls through to ``return 0.0`` when the model is missing from the rate
    table, so the dashboard charged nothing without surfacing a warning.
    """

    def test_cerebras_default_model_is_billed(self):
        """Cerebras default ``gpt-oss-120b`` must produce a non-zero bill."""
        cost = calculate_llm_cost("cerebras", "gpt-oss-120b", 1000, 1000)
        # Real rate-card math (2026-05-11 docs): 1000 in @ $0.35/M + 1000 out @ $0.75/M.
        # Updated from $0.85/$1.20 — see CHANGELOG 0.6.1 pricing correction.
        assert cost == pytest.approx(
            (1000 / 1_000_000) * 0.35 + (1000 / 1_000_000) * 0.75
        )
        assert cost > 0.0

    def test_cerebras_llama_3_1_8b_is_billed(self):
        """``llama3.1-8b`` (deprecating 2026-05-27 but still supported) must bill."""
        cost = calculate_llm_cost("cerebras", "llama3.1-8b", 1000, 1000)
        # Real rate-card math (2026-05-11 docs): 1000 in @ $0.10/M + 1000 out @ $0.10/M.
        # Output was $0.20/M previously — corrected to match Cerebras docs.
        assert cost == pytest.approx(
            (1000 / 1_000_000) * 0.10 + (1000 / 1_000_000) * 0.10
        )
        assert cost > 0.0

    def test_cerebras_all_supported_models_billed(self):
        """Every model in the CerebrasModel enum has a pricing entry."""
        # Mirrors libraries/python/getpatter/providers/cerebras_llm.py CerebrasModel.
        cerebras_models = (
            "gpt-oss-120b",
            "llama3.1-8b",
            "llama-3.3-70b",
            "qwen-3-235b-a22b-instruct-2507",
            "zai-glm-4.7",
        )
        for model in cerebras_models:
            cost = calculate_llm_cost("cerebras", model, 10_000, 10_000)
            assert cost > 0.0, f"cerebras model {model!r} silently billed $0"
            assert model in LLM_PRICING["cerebras"], f"missing rate for {model!r}"

    def test_groq_all_models_billed(self):
        """Every model in the GroqModel enum has a pricing entry (no $0 holes)."""
        # Mirrors libraries/python/getpatter/providers/groq_llm.py GroqModel.
        groq_models = (
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "llama-3.3-70b-specdec",
            "llama3-70b-8192",
            "llama3-8b-8192",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        )
        for model in groq_models:
            cost = calculate_llm_cost("groq", model, 10_000, 10_000)
            assert cost > 0.0, f"groq model {model!r} silently billed $0"
            assert model in LLM_PRICING["groq"], f"missing rate for {model!r}"

    def test_groq_specdec_uses_higher_output_rate(self):
        """``llama-3.3-70b-specdec`` carries a different output rate than versatile."""
        # specdec has $0.99/M output (vs $0.79/M versatile) per Groq's published
        # pricing for speculative-decoding endpoints — verify the rate isn't
        # silently aliased to versatile's.
        spec_cost = calculate_llm_cost("groq", "llama-3.3-70b-specdec", 0, 1_000_000)
        ver_cost = calculate_llm_cost("groq", "llama-3.3-70b-versatile", 0, 1_000_000)
        assert spec_cost == pytest.approx(0.99)
        assert ver_cost == pytest.approx(0.79)
        assert spec_cost > ver_cost
