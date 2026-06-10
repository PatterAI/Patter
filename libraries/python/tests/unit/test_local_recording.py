"""Unit tests for carrier-neutral local call recording wiring.

Covers:
- handler-level taps (pipeline / realtime / ConvAI) feeding the recorder,
- cleanup() finalizing the WAV on every teardown path (incl. truncated),
- EmbeddedServer.create_local_recorder path resolution,
- bridge-level on_call_end ``recording_path`` surfacing.
"""

from __future__ import annotations

import base64
import json
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.audio.call_recorder import LocalCallRecorder
from getpatter.stream_handler import (
    ElevenLabsConvAIStreamHandler,
    OpenAIRealtimeStreamHandler,
    PipelineStreamHandler,
)
from tests.conftest import fake_mulaw_frame, fake_pcm_frame, make_agent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_audio_sender() -> MagicMock:
    sender = MagicMock()
    sender.send_audio = AsyncMock()
    sender.send_mark = AsyncMock()
    sender.send_clear = AsyncMock()
    sender.reset_pcm_carry = MagicMock()
    return sender


def make_pipeline_handler(tmp_path: Path, **overrides) -> PipelineStreamHandler:
    handler = PipelineStreamHandler(
        agent=make_agent(),
        audio_sender=make_audio_sender(),
        call_id="CA" + "a" * 32,
        caller="+15550000001",
        callee="+15550000002",
        resolved_prompt="hi",
        metrics=None,
        for_twilio=overrides.pop("for_twilio", True),
        **overrides,
    )
    handler.local_recorder = LocalCallRecorder(tmp_path / "recording.wav")
    return handler


def wav_params(path: Path):
    with wave.open(str(path), "rb") as wf:
        return wf.getparams()


# ---------------------------------------------------------------------------
# Pipeline handler taps
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPipelineHandlerTaps:
    async def test_inbound_tap_records_caller_even_without_stt(
        self, tmp_path: Path
    ) -> None:
        """The caller tap sits above the STT guard, so frames are captured
        even when no STT is configured (no gaps in the recording)."""
        handler = make_pipeline_handler(tmp_path)
        assert handler._stt is None
        for _ in range(5):
            await handler.on_audio_received(fake_mulaw_frame())
        # 5 × 160 mulaw bytes @8k → ≈ 5 × 320 samples @16k (ratecv lag ≤ 2)
        assert abs(handler.local_recorder.caller_samples - 5 * 320) <= 2

    async def test_outbound_tap_pcm_path(self, tmp_path: Path) -> None:
        handler = make_pipeline_handler(tmp_path)
        handler._tts_output_format_native_for_carrier = False
        handler._tap_pipeline_agent_audio(fake_pcm_frame())
        assert handler.local_recorder.agent_samples == 320

    async def test_outbound_tap_decodes_carrier_native_mulaw(
        self, tmp_path: Path
    ) -> None:
        """On the carrier-native fast path the chunk is μ-law 8 kHz — the
        tap DECODES it (unlike the AEC tap, which would have to skip)."""
        handler = make_pipeline_handler(tmp_path)
        handler._tts_output_format_native_for_carrier = True
        handler._tap_pipeline_agent_audio(fake_mulaw_frame())
        # 160 mulaw bytes @8k ≈ 320 samples @16k after decode + resample
        assert abs(handler.local_recorder.agent_samples - 320) <= 2

    async def test_outbound_tap_telnyx_native_pcm16(self, tmp_path: Path) -> None:
        """Telnyx-native TTS output (pcm_16000) is already PCM16 16 kHz."""
        handler = make_pipeline_handler(tmp_path, for_twilio=False)
        handler._tts_output_format_native_for_carrier = True
        handler._tap_pipeline_agent_audio(fake_pcm_frame())
        assert handler.local_recorder.agent_samples == 320

    async def test_cleanup_finalizes_wav(self, tmp_path: Path) -> None:
        handler = make_pipeline_handler(tmp_path)
        await handler.on_audio_received(fake_mulaw_frame())
        await handler.cleanup()
        assert handler.local_recorder.closed
        params = wav_params(tmp_path / "recording.wav")
        assert params.nchannels == 2
        assert params.framerate == 16000

    async def test_truncated_teardown_file_still_parseable(
        self, tmp_path: Path
    ) -> None:
        """Abnormal teardown (carrier WS drop mid-call) runs cleanup() via
        the bridge ``finally`` — the WAV header must still get patched."""
        handler = make_pipeline_handler(tmp_path)
        await handler.on_audio_received(fake_mulaw_frame())
        handler._tap_pipeline_agent_audio(fake_pcm_frame())  # agent mid-stream
        # No stop event, no graceful drain — straight to cleanup.
        await handler.cleanup()
        params = wav_params(tmp_path / "recording.wav")
        assert params.nframes > 0
        # Double cleanup (stop + ws close both firing) stays safe.
        await handler.cleanup()

    async def test_no_recorder_is_noop(self, tmp_path: Path) -> None:
        handler = make_pipeline_handler(tmp_path)
        handler.local_recorder = None
        await handler.on_audio_received(fake_mulaw_frame())
        handler._tap_pipeline_agent_audio(fake_pcm_frame())
        await handler.cleanup()  # nothing to close — must not raise


# ---------------------------------------------------------------------------
# Realtime handler taps
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRealtimeHandlerTaps:
    def _make(self, tmp_path: Path, **kwargs) -> OpenAIRealtimeStreamHandler:
        handler = OpenAIRealtimeStreamHandler(
            agent=make_agent(provider="openai_realtime"),
            audio_sender=make_audio_sender(),
            call_id="CA" + "b" * 32,
            caller="+15550000001",
            callee="+15550000002",
            resolved_prompt="hi",
            metrics=None,
            openai_key="sk-test",
            **kwargs,
        )
        handler.local_recorder = LocalCallRecorder(tmp_path / "recording.wav")
        return handler

    async def test_inbound_mulaw_tap_fires_before_adapter_ready(
        self, tmp_path: Path
    ) -> None:
        handler = self._make(tmp_path, audio_format="g711_ulaw")
        assert handler._adapter is None
        await handler.on_audio_received(fake_mulaw_frame())
        assert abs(handler.local_recorder.caller_samples - 320) <= 2

    async def test_inbound_pcm_tap_when_input_transcode(self, tmp_path: Path) -> None:
        """Telnyx-style realtime: carrier delivers PCM16 16 kHz inbound."""
        handler = self._make(
            tmp_path,
            audio_format="g711_ulaw",
            input_transcode="pcm16_16k_to_g711_ulaw",
        )
        await handler.on_audio_received(fake_pcm_frame())
        assert handler.local_recorder.caller_samples == 320

    async def test_cleanup_closes_recorder(self, tmp_path: Path) -> None:
        handler = self._make(tmp_path, audio_format="g711_ulaw")
        await handler.on_audio_received(fake_mulaw_frame())
        await handler.cleanup()
        assert handler.local_recorder.closed
        assert wav_params(tmp_path / "recording.wav").nchannels == 2


# ---------------------------------------------------------------------------
# ConvAI handler taps
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConvAIHandlerTaps:
    def _make(self, tmp_path: Path) -> ElevenLabsConvAIStreamHandler:
        handler = ElevenLabsConvAIStreamHandler(
            agent=make_agent(provider="elevenlabs_convai"),
            audio_sender=make_audio_sender(),
            call_id="CA" + "c" * 32,
            caller="+15550000001",
            callee="+15550000002",
            resolved_prompt="hi",
            metrics=None,
            elevenlabs_key="el-test",
            for_twilio=True,
        )
        handler.local_recorder = LocalCallRecorder(tmp_path / "recording.wav")
        return handler

    async def test_inbound_tap_fires_before_adapter_ready(
        self, tmp_path: Path
    ) -> None:
        handler = self._make(tmp_path)
        assert handler._adapter is None
        await handler.on_audio_received(fake_mulaw_frame())
        assert abs(handler.local_recorder.caller_samples - 320) <= 2

    async def test_cleanup_closes_recorder(self, tmp_path: Path) -> None:
        handler = self._make(tmp_path)
        await handler.on_audio_received(fake_mulaw_frame())
        await handler.cleanup()
        assert handler.local_recorder.closed
        assert wav_params(tmp_path / "recording.wav").nchannels == 2


# ---------------------------------------------------------------------------
# EmbeddedServer.create_local_recorder — path resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateLocalRecorder:
    def _server(self, tmp_path: Path, monkeypatch, **kwargs):
        from getpatter.local_config import LocalConfig
        from getpatter.server import EmbeddedServer

        monkeypatch.delenv("PATTER_LOG_DIR", raising=False)
        config = LocalConfig(
            telephony_provider="twilio",
            twilio_sid="AC_test",
            twilio_token="tok_test",
            openai_key="sk-test",
            phone_number="+15550000000",
            webhook_url="abc.ngrok.io",
            persist_root=kwargs.pop("persist_root", None),
        )
        return EmbeddedServer(config=config, agent=make_agent(), **kwargs)

    def test_disabled_by_default(self, tmp_path: Path, monkeypatch) -> None:
        server = self._server(tmp_path, monkeypatch)
        assert server.local_recording is False
        assert server.create_local_recorder("CA123") is None

    def test_disabled_means_zero_filesystem_writes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Config off → no recorder, no ``recordings/`` dir, no WAV bytes —
        the default must leave the filesystem completely untouched."""
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        server = self._server(tmp_path, monkeypatch)
        for call_id in ("CA1", "CA2"):
            assert server.create_local_recorder(call_id) is None
        assert list(workdir.iterdir()) == []
        assert list(tmp_path.rglob("*.wav")) == []

    def test_explicit_directory_string(self, tmp_path: Path, monkeypatch) -> None:
        server = self._server(
            tmp_path, monkeypatch, local_recording=str(tmp_path / "recs")
        )
        recorder = server.create_local_recorder("CA123")
        assert recorder is not None
        assert recorder.path == tmp_path / "recs" / "CA123.wav"
        recorder.close()

    def test_true_uses_call_log_dir_when_logging_enabled(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        server = self._server(
            tmp_path,
            monkeypatch,
            local_recording=True,
            persist_root=str(tmp_path / "logs"),
        )
        # Seed the per-call start time so the recorder lands in the same
        # YYYY/MM/DD directory as metadata.json.
        server._call_logger.log_call_start("CA123")
        recorder = server.create_local_recorder("CA123")
        assert recorder is not None
        assert recorder.path.name == "recording.wav"
        assert recorder.path.parent == server._call_logger.call_dir("CA123")
        recorder.close()

    def test_true_without_call_logging_falls_back_to_cwd_recordings(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        server = self._server(tmp_path, monkeypatch, local_recording=True)
        recorder = server.create_local_recorder("CA123")
        assert recorder is not None
        assert recorder.path == Path("recordings") / "CA123.wav"
        recorder.close()

    def test_call_id_sanitized_in_filename(self, tmp_path: Path, monkeypatch) -> None:
        server = self._server(
            tmp_path, monkeypatch, local_recording=str(tmp_path / "recs")
        )
        recorder = server.create_local_recorder("../evil/../../id")
        assert recorder is not None
        assert recorder.path.parent == tmp_path / "recs"
        assert "/" not in recorder.path.name[:-4]
        recorder.close()

    def test_factory_failure_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        # A file where the directory should be → recorder open fails → None.
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        server = self._server(tmp_path, monkeypatch, local_recording=str(blocker))
        assert server.create_local_recorder("CA123") is None


# ---------------------------------------------------------------------------
# Bridge-level: on_call_end carries recording_path
# ---------------------------------------------------------------------------


def _ws_message(event: str, **kwargs) -> str:
    return json.dumps({"event": event, **kwargs})


def _make_mock_ws(messages: list[str]) -> AsyncMock:
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.query_params = {"caller": "+15551234567", "callee": "+15559876543"}
    ws.receive_text = AsyncMock(side_effect=messages)
    ws.send_text = AsyncMock()
    return ws


@pytest.mark.unit
class TestBridgeRecordingPath:
    @pytest.mark.asyncio
    @patch("getpatter.telephony.twilio.OpenAIRealtimeStreamHandler")
    @patch("getpatter.telephony.twilio.create_metrics_accumulator")
    @patch("getpatter.telephony.twilio.resolve_agent_prompt", return_value="prompt")
    @patch("getpatter.telephony.twilio.fetch_deepgram_cost", new_callable=AsyncMock)
    async def test_twilio_bridge_surfaces_recording_path(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
        tmp_path: Path,
    ) -> None:
        from getpatter.telephony.twilio import twilio_stream_bridge

        call_sid = "CA" + "a" * 32
        media = base64.b64encode(fake_mulaw_frame()).decode("ascii")
        messages = [
            _ws_message(
                "start", streamSid="MZ_test", start={"callSid": call_sid}
            ),
            _ws_message("media", media={"payload": media}),
            _ws_message("stop"),
        ]
        ws = _make_mock_ws(messages)

        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_metrics = MagicMock()
        mock_metrics.end_call.return_value = None
        mock_create_metrics.return_value = mock_metrics

        created: list[LocalCallRecorder] = []

        def factory(call_id: str) -> LocalCallRecorder:
            recorder = LocalCallRecorder(tmp_path / f"{call_id}.wav")
            created.append(recorder)
            return recorder

        on_call_end = AsyncMock()
        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            on_call_end=on_call_end,
            local_recorder_factory=factory,
        )

        # The factory ran once and the handler received the recorder.
        assert len(created) == 1
        assert mock_handler.local_recorder is created[0]
        # on_call_end payload surfaces the finalized recording path.
        payload = on_call_end.await_args.args[0]
        assert payload["recording_path"] == str(tmp_path / f"{call_sid}.wav")
        assert created[0].closed
        assert wav_params(tmp_path / f"{call_sid}.wav").nchannels == 2

    @pytest.mark.asyncio
    @patch("getpatter.telephony.twilio.OpenAIRealtimeStreamHandler")
    @patch("getpatter.telephony.twilio.create_metrics_accumulator")
    @patch("getpatter.telephony.twilio.resolve_agent_prompt", return_value="prompt")
    @patch("getpatter.telephony.twilio.fetch_deepgram_cost", new_callable=AsyncMock)
    async def test_no_factory_means_no_recording_path_key(
        self,
        mock_fetch_dg,
        mock_resolve,
        mock_create_metrics,
        mock_handler_cls,
    ) -> None:
        from getpatter.telephony.twilio import twilio_stream_bridge

        messages = [
            _ws_message(
                "start", streamSid="MZ_test", start={"callSid": "CA" + "a" * 32}
            ),
            _ws_message("stop"),
        ]
        ws = _make_mock_ws(messages)
        mock_handler = AsyncMock()
        mock_handler.audio_sender = None
        mock_handler_cls.return_value = mock_handler
        mock_metrics = MagicMock()
        mock_metrics.end_call.return_value = None
        mock_create_metrics.return_value = mock_metrics

        on_call_end = AsyncMock()
        await twilio_stream_bridge(
            websocket=ws,
            agent=make_agent(provider="openai_realtime"),
            openai_key="sk-test",
            on_call_end=on_call_end,
        )
        payload = on_call_end.await_args.args[0]
        assert "recording_path" not in payload
