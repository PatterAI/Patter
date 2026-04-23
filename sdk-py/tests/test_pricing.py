"""Tests for the pricing registry and cost calculation functions."""

import pytest

from getpatter.pricing import (
    DEFAULT_PRICING,
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
        # 60 seconds = 1 minute at $0.0077/min (Nova-3 streaming monolingual,
        # the Patter default). Previous $0.0043/min was the batch rate.
        cost = calculate_stt_cost("deepgram", 60.0, pricing)
        assert abs(cost - 0.0077) < 1e-6

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
        # 1000 characters at $0.06/1k = $0.06 (eleven_flash_v2_5 default;
        # previous $0.18 was the Creator plan overage rate).
        cost = calculate_tts_cost("elevenlabs", 1000, pricing)
        assert abs(cost - 0.06) < 1e-6

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

    def test_zero_duration(self):
        pricing = merge_pricing(None)
        cost = calculate_telephony_cost("twilio", 0.0, pricing)
        assert cost == 0.0
