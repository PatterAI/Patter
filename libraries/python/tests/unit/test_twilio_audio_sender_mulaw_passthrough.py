"""Unit tests for the TwilioAudioSender μ-law pass-through fast-path.

Guards the 0.6.1 firstMessage audio glitch fix:

When ``ElevenLabsWebSocketTTS()`` is constructed without an explicit
``output_format`` and paired with a Twilio carrier,
``set_telephony_carrier('twilio')`` auto-flips the wire format to
``ulaw_8000``. The TTS adapter then streams μ-law bytes, but
``TwilioAudioSender.send_audio`` always assumed PCM16 16 kHz — resampling
+ re-encoding the μ-law bytes produced loud garbled hiss on the wire
(the "gracchia molto" firstMessage report).

The fix in ``StreamHandler._init_pipeline`` flips
``audio_sender._input_is_mulaw_8k = True`` after carrier negotiation when
the adapter reports ``output_format == 'ulaw_8000'``. These tests cover
the mutation contract that path relies on: construction-time pass-through
and runtime flip both correctly bypass the PCM16 transcoding pipeline.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.telephony.twilio import TwilioAudioSender


@pytest.mark.unit
class TestTwilioAudioSenderMulawPassthrough:
    """``TwilioAudioSender._input_is_mulaw_8k`` controls the transcode path."""

    async def test_input_is_mulaw_flag_pass_through(self) -> None:
        """When constructed with ``input_is_mulaw_8k=True`` the sender
        forwards bytes as-is — no resample, no transcode. Used by the
        ElevenLabs ConvAI path and by the 0.6.1 firstMessage fix when
        the ElevenLabs WS TTS adapter auto-flips to ``ulaw_8000`` for
        Twilio.
        """
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        sender = TwilioAudioSender(ws, stream_sid="MZ_test", input_is_mulaw_8k=True)

        mulaw_bytes = b"\x7f\x80\xff\x00\x7e\x81"
        await sender.send_audio(mulaw_bytes)

        ws.send_text.assert_awaited_once()
        payload = json.loads(ws.send_text.call_args[0][0])
        assert payload["event"] == "media"
        decoded = base64.b64decode(payload["media"]["payload"])
        assert decoded == mulaw_bytes  # byte-for-byte forwarded

    async def test_input_is_mulaw_flip_at_runtime(self) -> None:
        """``StreamHandler._init_pipeline`` flips the flag in place after
        the TTS adapter's ``set_telephony_carrier`` returns. This test
        guards that mutation contract: a sender constructed in default
        (PCM16) mode keeps producing transcoded output until the flag
        flips, then becomes a pure pass-through. Mirrors the in-place
        mutation already used for ElevenLabs ConvAI native μ-law.
        """
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        with (
            patch(
                "getpatter.audio.transcoding.pcm16_to_mulaw",
                side_effect=lambda x: b"X" * len(x),
                create=True,
            ),
            patch(
                "getpatter.audio.transcoding.create_resampler_16k_to_8k",
                return_value=MagicMock(
                    process=MagicMock(side_effect=lambda x: x),
                    flush=MagicMock(return_value=b""),
                ),
                create=True,
            ),
        ):
            sender = TwilioAudioSender(ws, stream_sid="MZ_test")

        # Pre-flip: PCM16 bytes get transcoded through the mock pipeline.
        # 2-byte aligned chunk → resampler returns it unchanged → mulaw
        # mock returns "XX". The decoded payload must be the mock output.
        await sender.send_audio(b"\x00\x00")
        payload_before = json.loads(ws.send_text.call_args[0][0])
        assert base64.b64decode(payload_before["media"]["payload"]) == b"XX"

        # Flip the flag in place — same shape as the production fix in
        # ``StreamHandler._init_pipeline`` after ``set_telephony_carrier``.
        sender._input_is_mulaw_8k = True

        # Post-flip: bytes flow through untouched. The mock transcode
        # helpers must not be invoked. We assert this via the payload
        # bytes matching the input exactly.
        await sender.send_audio(b"\xff\x7f\x80")
        payload_after = json.loads(ws.send_text.call_args[0][0])
        assert base64.b64decode(payload_after["media"]["payload"]) == b"\xff\x7f\x80"
