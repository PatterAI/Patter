"""Additional unit tests to boost coverage across multiple modules.

Targets: dashboard/routes.py, services/transcoding.py, handlers/stream_handler.py
helper functions, services/call_orchestrator.py, and api_routes.py helpers.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# services/transcoding.py — mulaw/PCM conversions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTranscoding:
    """Audio transcoding functions."""

    def test_mulaw_to_pcm16(self) -> None:
        from patter.services.transcoding import mulaw_to_pcm16

        # mulaw silence = 0xFF bytes
        mulaw = b"\xff" * 160
        pcm = mulaw_to_pcm16(mulaw)
        assert isinstance(pcm, bytes)
        assert len(pcm) > 0

    def test_pcm16_to_mulaw(self) -> None:
        from patter.services.transcoding import pcm16_to_mulaw

        pcm = b"\x00\x00" * 160
        mulaw = pcm16_to_mulaw(pcm)
        assert isinstance(mulaw, bytes)
        assert len(mulaw) > 0

    def test_resample_8k_to_16k(self) -> None:
        from patter.services.transcoding import resample_8k_to_16k

        # 160 samples at 8kHz (20ms) = 320 bytes PCM16
        audio_8k = b"\x00\x00" * 160
        audio_16k = resample_8k_to_16k(audio_8k)
        assert isinstance(audio_16k, bytes)
        # Should be roughly doubled
        assert len(audio_16k) >= len(audio_8k)

    def test_resample_8k_to_16k_empty(self) -> None:
        from patter.services.transcoding import resample_8k_to_16k

        result = resample_8k_to_16k(b"")
        assert result == b""

    def test_resample_16k_to_8k(self) -> None:
        from patter.services.transcoding import resample_16k_to_8k

        audio_16k = b"\x00\x00" * 320
        audio_8k = resample_16k_to_8k(audio_16k)
        assert isinstance(audio_8k, bytes)

    def test_resample_16k_to_8k_empty(self) -> None:
        from patter.services.transcoding import resample_16k_to_8k

        result = resample_16k_to_8k(b"")
        assert result == b""

    def test_mulaw_pcm_roundtrip(self) -> None:
        from patter.services.transcoding import mulaw_to_pcm16, pcm16_to_mulaw

        # Start with mulaw, go to PCM, back to mulaw
        original_mulaw = b"\x80\x7f\xff\x00" * 40
        pcm = mulaw_to_pcm16(original_mulaw)
        back_to_mulaw = pcm16_to_mulaw(pcm)
        # mulaw is lossy, but sizes should match
        assert len(back_to_mulaw) == len(original_mulaw)


# ---------------------------------------------------------------------------
# api_routes.py — _parse_int helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseInt:
    """_parse_int helper in api_routes.py."""

    def test_valid_int(self) -> None:
        from patter.api_routes import _parse_int

        assert _parse_int("42", "limit", 10) == 42

    def test_invalid_returns_default(self) -> None:
        from patter.api_routes import _parse_int

        assert _parse_int("abc", "limit", 10) == 10

    def test_negative_returns_zero(self) -> None:
        from patter.api_routes import _parse_int

        assert _parse_int("-5", "offset", 0) == 0

    def test_max_val_capped(self) -> None:
        from patter.api_routes import _parse_int

        assert _parse_int("5000", "limit", 50, max_val=1000) == 1000

    def test_within_max_val(self) -> None:
        from patter.api_routes import _parse_int

        assert _parse_int("500", "limit", 50, max_val=1000) == 500


# ---------------------------------------------------------------------------
# dashboard/auth.py — make_auth_dependency
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMakeAuthDependency:
    """make_auth_dependency creates a FastAPI dependency for token auth."""

    @pytest.mark.asyncio
    async def test_empty_token_allows_all(self) -> None:
        from patter.dashboard.auth import make_auth_dependency

        auth = make_auth_dependency(token="")
        request = MagicMock()
        request.headers = {}
        request.query_params = {}
        # Should not raise
        await auth(request)

    @pytest.mark.asyncio
    async def test_valid_bearer_token(self) -> None:
        from patter.dashboard.auth import make_auth_dependency

        auth = make_auth_dependency(token="secret")
        request = MagicMock()
        request.headers = {"Authorization": "Bearer secret"}
        request.query_params = {}
        await auth(request)

    @pytest.mark.asyncio
    async def test_valid_query_param_token(self) -> None:
        from patter.dashboard.auth import make_auth_dependency

        auth = make_auth_dependency(token="secret")
        request = MagicMock()
        request.headers = {}
        request.query_params = {"token": "secret"}
        await auth(request)

    @pytest.mark.asyncio
    async def test_invalid_token_raises(self) -> None:
        from fastapi import HTTPException
        from patter.dashboard.auth import make_auth_dependency

        auth = make_auth_dependency(token="secret")
        request = MagicMock()
        request.headers = {"Authorization": "Bearer wrong"}
        request.query_params = {}
        with pytest.raises(HTTPException) as exc_info:
            await auth(request)
        assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# dashboard/export.py — CSV and JSON export
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDashboardExport:
    """calls_to_csv and calls_to_json."""

    def test_calls_to_csv_empty(self) -> None:
        from patter.dashboard.export import calls_to_csv

        result = calls_to_csv([])
        assert "call_id" in result  # header row
        lines = result.strip().split("\n")
        assert len(lines) == 1  # header only

    def test_calls_to_csv_with_data(self) -> None:
        from patter.dashboard.export import calls_to_csv

        calls = [{
            "call_id": "c1",
            "caller": "+1555",
            "callee": "+1666",
            "direction": "inbound",
            "started_at": "2025-01-01T00:00:00",
            "ended_at": "2025-01-01T00:01:00",
            "metrics": {
                "duration_seconds": 60,
                "cost": {"total": 0.05, "stt": 0.01, "tts": 0.02, "llm": 0.01, "telephony": 0.01},
                "latency_avg": {"total_ms": 200},
                "turns": [{}],
                "provider_mode": "pipeline",
            },
        }]
        result = calls_to_csv(calls)
        lines = result.strip().split("\n")
        assert len(lines) == 2  # header + data

    def test_calls_to_json(self) -> None:
        from patter.dashboard.export import calls_to_json

        calls = [{"call_id": "c1"}]
        result = calls_to_json(calls)
        parsed = json.loads(result)
        assert parsed[0]["call_id"] == "c1"


# ---------------------------------------------------------------------------
# handlers/stream_handler.py — apply_call_overrides edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyCallOverridesExtended:
    """Extended tests for apply_call_overrides."""

    def test_stt_config_override(self) -> None:
        from patter.handlers.stream_handler import apply_call_overrides

        agent = make_agent()
        result = apply_call_overrides(agent, {
            "stt_config": {"provider": "deepgram", "api_key": "dg-key", "language": "en"},
        })
        assert result.stt is not None
        assert result.stt.provider == "deepgram"

    def test_tts_config_override(self) -> None:
        from patter.handlers.stream_handler import apply_call_overrides

        agent = make_agent()
        result = apply_call_overrides(agent, {
            "tts_config": {"provider": "elevenlabs", "api_key": "el-key", "voice": "voice1"},
        })
        assert result.tts is not None
        assert result.tts.provider == "elevenlabs"

    def test_tools_override(self) -> None:
        from patter.handlers.stream_handler import apply_call_overrides

        agent = make_agent()
        new_tools = [{"name": "custom_tool", "description": "A tool"}]
        result = apply_call_overrides(agent, {"tools": new_tools})
        assert result.tools == new_tools

    def test_variables_override(self) -> None:
        from patter.handlers.stream_handler import apply_call_overrides

        agent = make_agent()
        result = apply_call_overrides(agent, {"variables": {"key": "value"}})
        assert result.variables == {"key": "value"}

    def test_no_overrides_returns_same_agent(self) -> None:
        from patter.handlers.stream_handler import apply_call_overrides

        agent = make_agent()
        result = apply_call_overrides(agent, {})
        assert result is agent

    def test_multiple_overrides(self) -> None:
        from patter.handlers.stream_handler import apply_call_overrides

        agent = make_agent()
        result = apply_call_overrides(agent, {
            "voice": "nova",
            "model": "gpt-4o",
            "language": "fr",
        })
        assert result.voice == "nova"
        assert result.model == "gpt-4o"
        assert result.language == "fr"


# ---------------------------------------------------------------------------
# handlers/stream_handler.py — create_metrics_accumulator
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateMetricsAccumulatorExtended:
    """Extended tests for create_metrics_accumulator."""

    def test_elevenlabs_convai_provider_names(self) -> None:
        from patter.handlers.stream_handler import create_metrics_accumulator

        agent = make_agent(provider="elevenlabs_convai")
        metrics = create_metrics_accumulator(
            call_id="c1",
            provider="elevenlabs_convai",
            telephony_provider="twilio",
            agent=agent,
            deepgram_key="",
            elevenlabs_key="el-key",
            pricing=None,
        )
        assert metrics.stt_provider == "elevenlabs"
        assert metrics.tts_provider == "elevenlabs"
        assert metrics.llm_provider == "elevenlabs"

    def test_pipeline_provider_with_custom_stt(self) -> None:
        from patter.handlers.stream_handler import create_metrics_accumulator
        from patter.models import STTConfig

        agent = make_agent(
            provider="pipeline",
            stt=STTConfig(provider="whisper", api_key="key", language="en"),
        )
        metrics = create_metrics_accumulator(
            call_id="c1",
            provider="pipeline",
            telephony_provider="telnyx",
            agent=agent,
            deepgram_key="dg-key",
            elevenlabs_key="",
            pricing=None,
        )
        assert metrics.stt_provider == "whisper"

    def test_pipeline_provider_defaults_to_deepgram(self) -> None:
        from patter.handlers.stream_handler import create_metrics_accumulator

        agent = make_agent(provider="pipeline", stt=None, tts=None)
        metrics = create_metrics_accumulator(
            call_id="c1",
            provider="pipeline",
            telephony_provider="twilio",
            agent=agent,
            deepgram_key="dg-key",
            elevenlabs_key="el-key",
            pricing=None,
        )
        assert metrics.stt_provider == "deepgram"
        assert metrics.tts_provider == "elevenlabs"


# ---------------------------------------------------------------------------
# services/call_orchestrator.py
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCallOrchestrator:
    """CallOrchestrator audio routing."""

    def _make_session(self, **kwargs):
        from patter.services.session_manager import CallSession

        session = MagicMock(spec=CallSession)
        session.call_id = "c1"
        session.caller = "+1555"
        session.callee = "+1666"
        session.direction = "inbound"
        session.tts = None
        session.telephony_websocket = AsyncMock()
        session.metadata = {"stream_sid": "MZ_test"}
        for k, v in kwargs.items():
            setattr(session, k, v)
        return session

    @pytest.mark.asyncio
    async def test_handle_transcript_final(self) -> None:
        from patter.providers.base import Transcript
        from patter.services.call_orchestrator import CallOrchestrator

        on_transcript = AsyncMock()
        session = self._make_session()
        orch = CallOrchestrator(session, on_transcript=on_transcript)
        t = Transcript(text="hello world", is_final=True)
        await orch.handle_transcript(t)
        on_transcript.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_transcript_non_final_no_barge_in(self) -> None:
        from patter.providers.base import Transcript
        from patter.services.call_orchestrator import CallOrchestrator

        on_transcript = AsyncMock()
        session = self._make_session()
        orch = CallOrchestrator(session, on_transcript=on_transcript)
        t = Transcript(text="partial", is_final=False)
        await orch.handle_transcript(t)
        on_transcript.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_barge_in_with_transcoding(self) -> None:
        from patter.services.call_orchestrator import CallOrchestrator

        session = self._make_session()
        orch = CallOrchestrator(session, needs_transcoding=True)
        await orch.handle_barge_in()
        session.telephony_websocket.send_text.assert_awaited_once()
        msg = json.loads(session.telephony_websocket.send_text.call_args[0][0])
        assert msg["event"] == "clear"
        assert "streamSid" in msg

    @pytest.mark.asyncio
    async def test_handle_barge_in_without_transcoding(self) -> None:
        from patter.services.call_orchestrator import CallOrchestrator

        session = self._make_session()
        orch = CallOrchestrator(session, needs_transcoding=False)
        await orch.handle_barge_in()
        msg = json.loads(session.telephony_websocket.send_text.call_args[0][0])
        assert msg["event"] == "clear"

    @pytest.mark.asyncio
    async def test_send_call_start(self) -> None:
        from patter.services.call_orchestrator import CallOrchestrator

        on_call_start = AsyncMock()
        session = self._make_session()
        orch = CallOrchestrator(session, on_call_start=on_call_start)
        await orch.send_call_start()
        on_call_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_call_end(self) -> None:
        from patter.services.call_orchestrator import CallOrchestrator

        on_call_end = AsyncMock()
        session = self._make_session()
        orch = CallOrchestrator(session, on_call_end=on_call_end)
        await orch.send_call_end()
        on_call_end.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_audio_with_transcoding(self) -> None:
        from patter.services.call_orchestrator import CallOrchestrator

        session = self._make_session()
        orch = CallOrchestrator(session, needs_transcoding=True)
        with patch("patter.services.call_orchestrator.pcm16_to_mulaw", return_value=b"\x00"):
            with patch("patter.services.call_orchestrator.resample_16k_to_8k", return_value=b"\x00"):
                await orch._send_audio_to_telephony(b"\x00\x00")
        session.telephony_websocket.send_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_audio_without_transcoding(self) -> None:
        from patter.services.call_orchestrator import CallOrchestrator

        session = self._make_session()
        orch = CallOrchestrator(session, needs_transcoding=False)
        await orch._send_audio_to_telephony(b"\x00\x00")
        session.telephony_websocket.send_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_audio_no_websocket(self) -> None:
        from patter.services.call_orchestrator import CallOrchestrator

        session = self._make_session(telephony_websocket=None)
        orch = CallOrchestrator(session, needs_transcoding=False)
        await orch._send_audio_to_telephony(b"\x00\x00")
        # No error raised


# ---------------------------------------------------------------------------
# dashboard/routes.py — test query param edge cases via httpx
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDashboardRoutesEdgeCases:
    """Dashboard route edge cases: pagination, date filtering, export."""

    @pytest.mark.asyncio
    async def test_calls_with_custom_limit_and_offset(self) -> None:
        import httpx
        from patter.local_config import LocalConfig
        from patter.server import EmbeddedServer

        srv = EmbeddedServer(
            config=LocalConfig(),
            agent=make_agent(),
            dashboard=True,
        )
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/calls?limit=10&offset=5")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_calls_with_bad_limit(self) -> None:
        import httpx
        from patter.local_config import LocalConfig
        from patter.server import EmbeddedServer

        srv = EmbeddedServer(
            config=LocalConfig(),
            agent=make_agent(),
            dashboard=True,
        )
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/dashboard/calls?limit=abc&offset=xyz")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_export_with_date_range(self) -> None:
        import httpx
        from patter.local_config import LocalConfig
        from patter.server import EmbeddedServer

        srv = EmbeddedServer(
            config=LocalConfig(),
            agent=make_agent(),
            dashboard=True,
        )
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/dashboard/export/calls?format=json&from=2025-01-01&to=2025-12-31"
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_export_with_bad_date(self) -> None:
        import httpx
        from patter.local_config import LocalConfig
        from patter.server import EmbeddedServer

        srv = EmbeddedServer(
            config=LocalConfig(),
            agent=make_agent(),
            dashboard=True,
        )
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Bad dates fall back gracefully
            resp = await client.get(
                "/api/dashboard/export/calls?format=json&from=not-a-date"
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_list_calls_with_pagination(self) -> None:
        import httpx
        from patter.local_config import LocalConfig
        from patter.server import EmbeddedServer

        srv = EmbeddedServer(
            config=LocalConfig(),
            agent=make_agent(),
            dashboard=True,
        )
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/calls?limit=5&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pagination"]["limit"] == 5

    @pytest.mark.asyncio
    async def test_api_costs_with_valid_dates(self) -> None:
        import httpx
        from patter.local_config import LocalConfig
        from patter.server import EmbeddedServer

        srv = EmbeddedServer(
            config=LocalConfig(),
            agent=make_agent(),
            dashboard=True,
        )
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/analytics/costs?from=2025-01-01&to=2025-12-31"
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_costs_invalid_to_date(self) -> None:
        import httpx
        from patter.local_config import LocalConfig
        from patter.server import EmbeddedServer

        srv = EmbeddedServer(
            config=LocalConfig(),
            agent=make_agent(),
            dashboard=True,
        )
        app = srv._create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/analytics/costs?to=not-valid"
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# StreamHandler base class
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStreamHandlerBase:
    """StreamHandler base class default methods."""

    @pytest.mark.asyncio
    async def test_on_dtmf_is_noop(self) -> None:
        from patter.handlers.stream_handler import StreamHandler

        # Create a minimal concrete subclass
        class _TestHandler(StreamHandler):
            async def start(self): pass
            async def on_audio_received(self, audio_bytes): pass
            async def cleanup(self): pass

        handler = _TestHandler(
            agent=make_agent(),
            audio_sender=AsyncMock(),
            call_id="c1",
            caller="+1555",
            callee="+1666",
            resolved_prompt="test",
            metrics=None,
        )
        # Should not raise
        await handler.on_dtmf("5")

    @pytest.mark.asyncio
    async def test_on_mark_is_noop(self) -> None:
        from patter.handlers.stream_handler import StreamHandler

        class _TestHandler(StreamHandler):
            async def start(self): pass
            async def on_audio_received(self, audio_bytes): pass
            async def cleanup(self): pass

        handler = _TestHandler(
            agent=make_agent(),
            audio_sender=AsyncMock(),
            call_id="c1",
            caller="+1555",
            callee="+1666",
            resolved_prompt="test",
            metrics=None,
        )
        await handler.on_mark("audio_1")

    def test_init_creates_history_deques(self) -> None:
        from patter.handlers.stream_handler import StreamHandler

        class _TestHandler(StreamHandler):
            async def start(self): pass
            async def on_audio_received(self, audio_bytes): pass
            async def cleanup(self): pass

        handler = _TestHandler(
            agent=make_agent(),
            audio_sender=AsyncMock(),
            call_id="c1",
            caller="+1555",
            callee="+1666",
            resolved_prompt="test",
            metrics=None,
        )
        assert isinstance(handler.conversation_history, deque)
        assert isinstance(handler.transcript_entries, deque)
        assert handler._background_task is None


# ---------------------------------------------------------------------------
# Exceptions module
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExceptions:
    """Patter exception hierarchy."""

    def test_patter_error(self) -> None:
        from patter.exceptions import PatterError

        exc = PatterError("test error")
        assert str(exc) == "test error"

    def test_connection_error(self) -> None:
        from patter.exceptions import PatterConnectionError

        exc = PatterConnectionError("conn failed")
        assert isinstance(exc, Exception)

    def test_authentication_error(self) -> None:
        from patter.exceptions import AuthenticationError

        exc = AuthenticationError("auth failed")
        assert isinstance(exc, Exception)

    def test_provision_error(self) -> None:
        from patter.exceptions import ProvisionError

        exc = ProvisionError("provision failed")
        assert isinstance(exc, Exception)


# ---------------------------------------------------------------------------
# LocalConfig defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLocalConfig:
    """LocalConfig frozen dataclass."""

    def test_defaults(self) -> None:
        from patter.local_config import LocalConfig

        cfg = LocalConfig()
        assert cfg.telephony_provider == "twilio"
        assert cfg.twilio_sid == ""
        assert cfg.telnyx_key == ""
        assert cfg.openai_key == ""

    def test_frozen(self) -> None:
        from patter.local_config import LocalConfig

        cfg = LocalConfig()
        with pytest.raises(AttributeError):
            cfg.twilio_sid = "new"  # type: ignore[misc]
