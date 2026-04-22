"""Integration tests for cost tracking and latency profiling in stream handlers."""

import pytest

from patter import CallMetrics, CostBreakdown, LatencyBreakdown, TurnMetrics, Patter


class TestModelsExported:
    """Verify that new metric types are importable from the patter package."""

    def test_cost_breakdown_importable(self):
        from patter import CostBreakdown

        cb = CostBreakdown(stt=0.01, tts=0.02, llm=0.03, telephony=0.04, total=0.10)
        assert cb.total == 0.10

    def test_latency_breakdown_importable(self):
        from patter import LatencyBreakdown

        lb = LatencyBreakdown(stt_ms=50, llm_ms=100, tts_ms=30, total_ms=180)
        assert lb.total_ms == 180

    def test_turn_metrics_importable(self):
        from patter import TurnMetrics

        tm = TurnMetrics(
            turn_index=0,
            user_text="hi",
            agent_text="hello",
            latency=LatencyBreakdown(),
        )
        assert tm.turn_index == 0

    def test_call_metrics_importable(self):
        from patter import CallMetrics

        cm = CallMetrics(
            call_id="test",
            duration_seconds=30.0,
            turns=(),
            cost=CostBreakdown(),
            latency_avg=LatencyBreakdown(),
            latency_p95=LatencyBreakdown(),
            provider_mode="pipeline",
        )
        assert cm.provider_mode == "pipeline"


class TestPatterPricingParam:
    """Test that the Patter constructor accepts pricing parameter."""

    def test_pricing_param_accepted(self):
        from patter import Twilio

        phone = Patter(
            carrier=Twilio(account_sid="AC" + "a" * 32, auth_token="test_token"),
            phone_number="+15550001234",
            webhook_url="test.ngrok.io",
            pricing={"deepgram": {"price": 0.005}},
        )
        assert phone._pricing == {"deepgram": {"price": 0.005}}

    def test_pricing_param_none_by_default(self):
        from patter import Twilio

        phone = Patter(
            carrier=Twilio(account_sid="AC" + "a" * 32, auth_token="test_token"),
            phone_number="+15550001234",
            webhook_url="test.ngrok.io",
        )
        assert phone._pricing is None


class TestServeAcceptsOnMetrics:
    """Test that serve() accepts on_metrics callback."""

    def test_serve_signature_has_on_metrics(self):
        import inspect

        sig = inspect.signature(Patter.serve)
        assert "on_metrics" in sig.parameters

    def test_serve_signature_has_on_metrics_default_none(self):
        import inspect

        sig = inspect.signature(Patter.serve)
        param = sig.parameters["on_metrics"]
        assert param.default is None


class TestCostBreakdownArithmetic:
    """Test that CostBreakdown total is consistent."""

    def test_total_is_sum(self):
        cb = CostBreakdown(stt=0.001, tts=0.002, llm=0.003, telephony=0.004, total=0.010)
        assert abs(cb.total - (cb.stt + cb.tts + cb.llm + cb.telephony)) < 1e-6

    def test_default_values(self):
        cb = CostBreakdown()
        assert cb.stt == 0.0
        assert cb.tts == 0.0
        assert cb.llm == 0.0
        assert cb.telephony == 0.0
        assert cb.total == 0.0

    def test_frozen(self):
        cb = CostBreakdown(total=1.0)
        with pytest.raises(AttributeError):
            cb.total = 2.0


class TestDeepgramRequestIdCapture:
    """Test that DeepgramSTT captures request_id from Metadata messages."""

    def test_request_id_captured_from_metadata(self):
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="test-key")
        assert stt.request_id is None

        # Simulate a Metadata message
        import json

        metadata_msg = json.dumps(
            {"type": "Metadata", "request_id": "abc-123-def", "created": "2025-01-01"}
        )
        result = stt._parse_message(metadata_msg)
        assert result is None  # Metadata should not yield a transcript
        assert stt.request_id == "abc-123-def"

    def test_request_id_not_overwritten_by_results(self):
        from patter.providers.deepgram_stt import DeepgramSTT

        stt = DeepgramSTT(api_key="test-key")
        import json

        # First, receive Metadata
        stt._parse_message(
            json.dumps({"type": "Metadata", "request_id": "req-001"})
        )
        assert stt.request_id == "req-001"

        # Then receive a Results message — request_id should be unchanged
        stt._parse_message(
            json.dumps(
                {
                    "type": "Results",
                    "is_final": True,
                    "speech_final": True,
                    "channel": {
                        "alternatives": [{"transcript": "hello", "confidence": 0.99}]
                    },
                }
            )
        )
        assert stt.request_id == "req-001"
