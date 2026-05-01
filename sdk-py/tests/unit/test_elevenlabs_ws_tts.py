"""Unit tests for the WebSocket-based ElevenLabs TTS provider.

Construction, factories, URL build, and a minimal end-to-end synthesise
run with a fully-mocked websockets connection. Heavier WS lifecycle
coverage (reconnect, 5-context limit, ULAW binary frames) lives in the
parity / integration suite.
"""

from __future__ import annotations

import base64
import json
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.providers.elevenlabs_ws_tts import (
    DEFAULT_INACTIVITY_TIMEOUT,
    ElevenLabsWebSocketTTS,
)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_options(self) -> None:
        tts = ElevenLabsWebSocketTTS(api_key="el-key")
        assert tts.api_key == "el-key"
        assert tts.model_id == "eleven_flash_v2_5"
        assert tts.output_format == "pcm_16000"
        assert tts.auto_mode is True
        assert tts.inactivity_timeout == DEFAULT_INACTIVITY_TIMEOUT

    def test_custom_voice_model_format(self) -> None:
        tts = ElevenLabsWebSocketTTS(
            api_key="k",
            voice_id="custom-id",
            model_id="eleven_turbo_v2_5",
            output_format="ulaw_8000",
        )
        assert tts.model_id == "eleven_turbo_v2_5"
        assert tts.output_format == "ulaw_8000"

    def test_rejects_eleven_v3(self) -> None:
        with pytest.raises(ValueError, match="not supported by the WebSocket"):
            ElevenLabsWebSocketTTS(api_key="k", model_id="eleven_v3")

    def test_rejects_eleven_v3_variants(self) -> None:
        # Phase 5 fix: prefix-based check rejects eleven_v3_preview, eleven_v3_alpha, etc.
        for variant in ("eleven_v3_preview", "eleven_v3_alpha"):
            with pytest.raises(ValueError, match="not supported by the WebSocket"):
                ElevenLabsWebSocketTTS(api_key="k", model_id=variant)

    def test_for_twilio_default(self) -> None:
        tts = ElevenLabsWebSocketTTS.for_twilio(api_key="k")
        assert tts.output_format == "ulaw_8000"
        assert tts.voice_settings == {
            "stability": 0.6,
            "similarity_boost": 0.75,
            "use_speaker_boost": False,
        }

    def test_for_telnyx_default(self) -> None:
        tts = ElevenLabsWebSocketTTS.for_telnyx(api_key="k")
        assert tts.output_format == "pcm_16000"

    def test_for_twilio_custom_voice_settings(self) -> None:
        tts = ElevenLabsWebSocketTTS.for_twilio(
            api_key="k",
            voice_settings={"stability": 0.9, "similarity_boost": 0.5},
        )
        assert tts.voice_settings == {"stability": 0.9, "similarity_boost": 0.5}


# ---------------------------------------------------------------------------
# URL build
# ---------------------------------------------------------------------------


class TestUrlBuild:
    def test_includes_required_params(self) -> None:
        tts = ElevenLabsWebSocketTTS(
            api_key="k",
            voice_id="voice-123",
            model_id="eleven_flash_v2_5",
            output_format="pcm_16000",
            auto_mode=True,
        )
        url = tts._build_url()
        assert "voice-123" in url
        assert "model_id=eleven_flash_v2_5" in url
        assert "output_format=pcm_16000" in url
        assert "inactivity_timeout=60" in url
        assert "auto_mode=true" in url

    def test_omits_auto_mode_when_disabled(self) -> None:
        tts = ElevenLabsWebSocketTTS(api_key="k", auto_mode=False)
        url = tts._build_url()
        assert "auto_mode" not in url

    def test_includes_language_code(self) -> None:
        tts = ElevenLabsWebSocketTTS(api_key="k", language_code="it")
        url = tts._build_url()
        assert "language_code=it" in url


# ---------------------------------------------------------------------------
# synthesize() protocol — uses a mocked websockets.connect()
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal WS mock matching the subset of ``websockets`` API we use.

    The new resilient ``synthesize`` flow uses ``await
    asyncio.wait_for(websockets.connect(...), ...)`` followed by
    ``ws.recv()`` per frame and ``ws.close()`` in finally — no
    ``async with`` context manager and no ``async for`` iteration. The
    mock therefore exposes ``recv``/``send``/``close`` and is itself
    awaitable so ``await connect_coro`` resolves to it.
    """

    def __init__(self, frames: list[Any]) -> None:
        self.sent: list[str] = []
        self._frames: deque = deque(frames)
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> Any:
        if not self._frames:
            # Mimic websockets ConnectionClosedOK on EOS by raising — the
            # synthesize loop catches and re-raises as ElevenLabsTTSError
            # only when the consumer was expecting more; for a clean end
            # the test relies on isFinal having been emitted earlier.
            from websockets.exceptions import ConnectionClosedOK
            raise ConnectionClosedOK(None, None)
        return self._frames.popleft()

    async def close(self) -> None:
        self.closed = True

    def __await__(self):
        # Allow ``await fake_ws`` to resolve directly to ``self`` so a
        # patched ``websockets.connect`` that returns this instance is
        # awaitable as required by ``await asyncio.wait_for(connect)``.
        async def _self() -> "_FakeWebSocket":
            return self
        return _self().__await__()


class TestSynthesizeProtocol:
    @pytest.mark.asyncio
    async def test_send_sequence_and_decoded_audio(self) -> None:
        audio = b"\x00\x01\x02\x03"
        frames = [
            json.dumps({"audio": base64.b64encode(audio).decode("ascii")}),
            json.dumps({"isFinal": True}),
        ]
        fake_ws = _FakeWebSocket(frames)

        with patch(
            "getpatter.providers.elevenlabs_ws_tts.websockets.connect",
            return_value=fake_ws,
        ):
            tts = ElevenLabsWebSocketTTS(api_key="k")
            collected: list[bytes] = []
            async for chunk in tts.synthesize("Hello world"):
                collected.append(chunk)

        assert b"".join(collected) == audio
        # Init keep-alive, text+flush, EOS.
        assert len(fake_ws.sent) == 3
        init = json.loads(fake_ws.sent[0])
        payload = json.loads(fake_ws.sent[1])
        eos = json.loads(fake_ws.sent[2])
        assert init["text"] == " "
        assert payload == {"text": "Hello world ", "flush": True}
        assert eos == {"text": ""}

    @pytest.mark.asyncio
    async def test_terminates_on_socket_close(self) -> None:
        # Empty frame queue + StopAsyncIteration ⇒ no audio, clean exit.
        fake_ws = _FakeWebSocket([])
        with patch(
            "getpatter.providers.elevenlabs_ws_tts.websockets.connect",
            return_value=fake_ws,
        ):
            tts = ElevenLabsWebSocketTTS(api_key="k")
            collected = [chunk async for chunk in tts.synthesize("hi")]
        assert collected == []

    @pytest.mark.asyncio
    async def test_voice_settings_in_init_packet(self) -> None:
        fake_ws = _FakeWebSocket([json.dumps({"isFinal": True})])
        with patch(
            "getpatter.providers.elevenlabs_ws_tts.websockets.connect",
            return_value=fake_ws,
        ):
            tts = ElevenLabsWebSocketTTS(
                api_key="k",
                voice_settings={"stability": 0.5, "similarity_boost": 0.8},
            )
            async for _ in tts.synthesize("hi"):
                pass

        init = json.loads(fake_ws.sent[0])
        assert init["voice_settings"] == {"stability": 0.5, "similarity_boost": 0.8}

    @pytest.mark.asyncio
    async def test_chunk_length_schedule_only_with_auto_mode_disabled(self) -> None:
        fake_ws = _FakeWebSocket([json.dumps({"isFinal": True})])
        with patch(
            "getpatter.providers.elevenlabs_ws_tts.websockets.connect",
            return_value=fake_ws,
        ):
            tts = ElevenLabsWebSocketTTS(
                api_key="k",
                auto_mode=False,
                chunk_length_schedule=[80, 120, 200, 290],
            )
            async for _ in tts.synthesize("hi"):
                pass

        init = json.loads(fake_ws.sent[0])
        assert init.get("generation_config") == {
            "chunk_length_schedule": [80, 120, 200, 290]
        }

    @pytest.mark.asyncio
    async def test_chunk_length_schedule_skipped_when_auto_mode_on(self) -> None:
        fake_ws = _FakeWebSocket([json.dumps({"isFinal": True})])
        with patch(
            "getpatter.providers.elevenlabs_ws_tts.websockets.connect",
            return_value=fake_ws,
        ):
            tts = ElevenLabsWebSocketTTS(
                api_key="k",
                auto_mode=True,
                chunk_length_schedule=[80, 120, 200, 290],
            )
            async for _ in tts.synthesize("hi"):
                pass

        init = json.loads(fake_ws.sent[0])
        # Auto-mode lets the server pick — manual schedule must NOT be sent.
        assert "generation_config" not in init


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------


class TestPublicWrapper:
    def test_public_class_resolves_env_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELEVENLABS_API_KEY", "env-key-123")
        from getpatter.tts.elevenlabs_ws import TTS
        tts = TTS()
        assert tts.api_key == "env-key-123"

    def test_public_class_explicit_api_key(self) -> None:
        from getpatter.tts.elevenlabs_ws import TTS
        tts = TTS(api_key="explicit-key")
        assert tts.api_key == "explicit-key"

    def test_public_class_raises_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        from getpatter.tts.elevenlabs_ws import TTS
        with pytest.raises(ValueError, match="requires an api_key"):
            TTS()

    def test_public_for_twilio_factory(self) -> None:
        from getpatter.tts.elevenlabs_ws import TTS
        tts = TTS.for_twilio(api_key="k")
        assert tts.output_format == "ulaw_8000"

    def test_public_for_telnyx_factory(self) -> None:
        from getpatter.tts.elevenlabs_ws import TTS
        tts = TTS.for_telnyx(api_key="k")
        assert tts.output_format == "pcm_16000"

    def test_re_exported_from_top_level(self) -> None:
        from getpatter import ElevenLabsWebSocketTTS as TopLevel
        from getpatter.tts.elevenlabs_ws import TTS
        assert TopLevel is TTS
