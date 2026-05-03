"""Unit tests for ElevenLabs ConvAI native μ-law telephony negotiation.

Verifies:

1. ``ElevenLabsConvAIAdapter.for_twilio`` / ``for_telnyx`` factories return
   instances whose ``output_audio_format`` and ``input_audio_format`` are
   both ``"ulaw_8000"``.
2. ``ElevenLabsConvAIStreamHandler`` enables the native μ-law fast-path
   (skips inbound resampling and flips the audio sender's
   ``_input_is_mulaw_8k`` flag) when the adapter negotiates ``ulaw_8000``.
3. The default code path (no μ-law negotiated) keeps the legacy resample +
   transcode behaviour intact — no regression for existing PCM16 setups.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.stream_handler import ElevenLabsConvAIStreamHandler
from getpatter.providers.elevenlabs_convai import ElevenLabsConvAIAdapter

from tests.conftest import fake_mulaw_frame, make_agent


# ---------------------------------------------------------------------------
# Adapter factories
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestElevenLabsConvAIAdapterTelephonyFactories:
    """``for_twilio`` / ``for_telnyx`` set both audio formats to ulaw_8000."""

    def test_for_twilio_negotiates_ulaw_8000_both_directions(self) -> None:
        adapter = ElevenLabsConvAIAdapter.for_twilio(
            api_key="el-test", agent_id="agent_123"
        )
        assert adapter.output_audio_format == "ulaw_8000"
        assert adapter.input_audio_format == "ulaw_8000"
        assert adapter.agent_id == "agent_123"
        # Other defaults should still be applied.
        assert adapter.voice_id == "EXAVITQu4vr4xnSDxMaL"
        assert adapter.model_id == "eleven_flash_v2_5"

    def test_for_twilio_respects_overrides(self) -> None:
        adapter = ElevenLabsConvAIAdapter.for_twilio(
            api_key="el-test",
            agent_id="agent_xyz",
            voice_id="custom_voice",
            language="en",
            first_message="Hi there!",
        )
        assert adapter.output_audio_format == "ulaw_8000"
        assert adapter.input_audio_format == "ulaw_8000"
        assert adapter.voice_id == "custom_voice"
        assert adapter.language == "en"
        assert adapter.first_message == "Hi there!"

    def test_for_telnyx_negotiates_ulaw_8000_both_directions(self) -> None:
        adapter = ElevenLabsConvAIAdapter.for_telnyx(
            api_key="el-test", agent_id="agent_456"
        )
        assert adapter.output_audio_format == "ulaw_8000"
        assert adapter.input_audio_format == "ulaw_8000"

    def test_bare_constructor_default_unchanged(self) -> None:
        """Non-telephony users keep getting None (server default = PCM16)."""
        adapter = ElevenLabsConvAIAdapter(api_key="el-test", agent_id="agent_x")
        assert adapter.output_audio_format is None
        assert adapter.input_audio_format is None


# ---------------------------------------------------------------------------
# Handler μ-law fast-path
# ---------------------------------------------------------------------------


def _make_audio_sender_mock() -> MagicMock:
    """A duck-typed audio sender exposing ``_input_is_mulaw_8k``."""
    sender = MagicMock()
    sender._input_is_mulaw_8k = False
    sender.send_audio = AsyncMock()
    sender.send_clear = AsyncMock()
    sender.send_mark = AsyncMock()
    return sender


@pytest.mark.unit
class TestConvAIStreamHandlerNativeMulaw:
    """Handler skips resampling + flips audio_sender flag for ulaw_8000."""

    @pytest.mark.asyncio
    async def test_native_mulaw_path_flips_audio_sender_flag(self) -> None:
        agent = make_agent(provider="elevenlabs_convai")
        # ``agent.elevenlabs_convai`` carries the ConvAI agent_id and the
        # negotiated codec choices.
        object.__setattr__(
            agent,
            "elevenlabs_convai",
            {
                "agent_id": "agent_x",
                "output_audio_format": "ulaw_8000",
                "input_audio_format": "ulaw_8000",
            },
        )
        sender = _make_audio_sender_mock()

        handler = ElevenLabsConvAIStreamHandler(
            agent=agent,
            audio_sender=sender,
            call_id="CA1",
            caller="+1",
            callee="+2",
            resolved_prompt="Test",
            metrics=None,
            elevenlabs_key="el-test",
            for_twilio=True,
        )

        # Mock the adapter so connect() doesn't reach the network.
        with patch(
            "getpatter.providers.elevenlabs_convai.ElevenLabsConvAIAdapter"
        ) as MockAdapter:
            adapter_instance = MagicMock()
            adapter_instance.connect = AsyncMock()
            adapter_instance.send_audio = AsyncMock()
            adapter_instance.close = AsyncMock()
            adapter_instance.receive_events = MagicMock(return_value=_empty_events())
            MockAdapter.return_value = adapter_instance

            await handler.start()

        assert handler._native_mulaw_8k is True
        # Audio sender's transcoding flag flipped → outbound bytes go raw.
        assert sender._input_is_mulaw_8k is True
        # Cleanup background task.
        await handler.cleanup()

    @pytest.mark.asyncio
    async def test_native_mulaw_inbound_skips_resampler(self) -> None:
        agent = make_agent(provider="elevenlabs_convai")
        object.__setattr__(
            agent,
            "elevenlabs_convai",
            {"agent_id": "agent_x"},
        )
        sender = _make_audio_sender_mock()

        handler = ElevenLabsConvAIStreamHandler(
            agent=agent,
            audio_sender=sender,
            call_id="CA1",
            caller="+1",
            callee="+2",
            resolved_prompt="Test",
            metrics=None,
            elevenlabs_key="el-test",
            for_twilio=True,
            output_audio_format="ulaw_8000",
            input_audio_format="ulaw_8000",
        )

        adapter_instance = MagicMock()
        adapter_instance.connect = AsyncMock()
        adapter_instance.send_audio = AsyncMock()
        adapter_instance.close = AsyncMock()
        adapter_instance.receive_events = MagicMock(return_value=_empty_events())
        with patch(
            "getpatter.providers.elevenlabs_convai.ElevenLabsConvAIAdapter",
            return_value=adapter_instance,
        ):
            await handler.start()

        # Send one inbound frame: should be forwarded raw, no resampler created.
        frame = fake_mulaw_frame()
        await handler.on_audio_received(frame)
        adapter_instance.send_audio.assert_awaited_with(frame)
        # No 8k→16k resampler should have been instantiated.
        assert handler._resampler_8k_to_16k is None

        await handler.cleanup()

    @pytest.mark.asyncio
    async def test_legacy_pcm16_path_unchanged(self) -> None:
        """No regression: default ConvAI (no ulaw negotiated) still resamples."""
        agent = make_agent(provider="elevenlabs_convai")
        object.__setattr__(
            agent,
            "elevenlabs_convai",
            {"agent_id": "agent_x"},  # no output/input format → PCM16
        )
        sender = _make_audio_sender_mock()

        handler = ElevenLabsConvAIStreamHandler(
            agent=agent,
            audio_sender=sender,
            call_id="CA1",
            caller="+1",
            callee="+2",
            resolved_prompt="Test",
            metrics=None,
            elevenlabs_key="el-test",
            for_twilio=True,
        )

        adapter_instance = MagicMock()
        adapter_instance.connect = AsyncMock()
        adapter_instance.send_audio = AsyncMock()
        adapter_instance.close = AsyncMock()
        adapter_instance.receive_events = MagicMock(return_value=_empty_events())
        with patch(
            "getpatter.providers.elevenlabs_convai.ElevenLabsConvAIAdapter",
            return_value=adapter_instance,
        ):
            await handler.start()

        assert handler._native_mulaw_8k is False
        assert sender._input_is_mulaw_8k is False

        # Sending a Twilio mulaw frame should trigger the resample chain.
        await handler.on_audio_received(fake_mulaw_frame())
        adapter_instance.send_audio.assert_awaited()
        # PCM16 16 kHz forwarded — frame size will differ from raw mulaw input.
        forwarded = adapter_instance.send_audio.await_args.args[0]
        assert forwarded != fake_mulaw_frame()
        assert handler._resampler_8k_to_16k is not None

        await handler.cleanup()


async def _empty_events():
    """Async generator that yields nothing — keeps ``_forward_events`` quiet."""
    if False:
        yield  # pragma: no cover
