"""Unit tests for SonioxSTT and SpeechmaticsSTT adapters.

These tests use **synthetic mocks** of the Soniox / Speechmatics protocol
frames. They exercise the adapter state machines (token accumulation,
endpoint flushes, interim vs final transcripts) without any real network
traffic. See the module-level MOCK docstring below for details on the
protocol payloads each test replays.
"""

from __future__ import annotations

import json
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# MOCK HELPERS (synthetic Soniox / Speechmatics frames)
# ---------------------------------------------------------------------------
#
# The tests below synthesise the exact JSON payloads that Soniox emits over
# its real-time WebSocket API (``tokens`` with ``is_final`` flags, plus the
# ``<end>`` endpoint sentinel), and the object-style dict messages that the
# Speechmatics Voice SDK forwards from its server stream.  Both shapes are
# drawn from the upstream LiveKit plugin behaviour, which is our reference.


class _FakeWSMsg:
    """Duck-typed mock of ``aiohttp.WSMessage`` used by the aiohttp iterator."""

    def __init__(self, kind: str, data: str | bytes = ""):
        self.type = kind
        self.data = data


class _FakeAiohttpWS:
    """Async-iterable fake of ``aiohttp.ClientWebSocketResponse``."""

    def __init__(self, incoming: list[_FakeWSMsg]):
        self._incoming = incoming
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False

    async def send_str(self, payload: str) -> None:
        self.sent_text.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.sent_bytes.append(payload)

    async def close(self) -> None:
        self.closed = True

    def exception(self) -> Exception | None:  # pragma: no cover - defensive
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


class _FakeAiohttpSession:
    def __init__(self, ws: _FakeAiohttpWS):
        self._ws = ws
        self.closed = False

    async def ws_connect(self, url: str):
        self._last_url = url
        return self._ws

    async def close(self):
        self.closed = True


# Lazy import helpers — the aiohttp import happens at module import time
# in soniox_stt.py, but we treat it as an optional runtime dependency in
# the tests to avoid hard-requiring it in the base dev env.
aiohttp = pytest.importorskip("aiohttp")


# ---------------------------------------------------------------------------
# SonioxSTT
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSonioxSTTInit:
    def test_requires_api_key(self):
        from patter.providers.soniox_stt import SonioxSTT

        with pytest.raises(ValueError, match="api_key"):
            SonioxSTT(api_key="")

    def test_validates_endpoint_delay_range(self):
        from patter.providers.soniox_stt import SonioxSTT

        with pytest.raises(ValueError, match="max_endpoint_delay_ms"):
            SonioxSTT(api_key="k", max_endpoint_delay_ms=100)

    def test_for_twilio_uses_8khz(self):
        from patter.providers.soniox_stt import SonioxSTT

        stt = SonioxSTT.for_twilio(api_key="k")
        assert stt.sample_rate == 8000

    def test_build_config_defaults(self):
        from patter.providers.soniox_stt import SonioxSTT

        stt = SonioxSTT(api_key="k", language_hints=["en", "it"])
        config = stt._build_config()
        assert config["api_key"] == "k"
        assert config["audio_format"] == "pcm_s16le"
        assert config["sample_rate"] == 16000
        assert config["language_hints"] == ["en", "it"]
        assert config["enable_endpoint_detection"] is True

    def test_repr(self):
        from patter.providers.soniox_stt import SonioxSTT

        stt = SonioxSTT(api_key="k")
        assert "SonioxSTT" in repr(stt)


@pytest.mark.unit
class TestSonioxSTTIO:
    """Mocked-WebSocket tests for SonioxSTT behaviour."""

    @pytest.mark.asyncio
    async def test_connect_sends_initial_config(self):
        from patter.providers.soniox_stt import SonioxSTT

        ws = _FakeAiohttpWS([])
        session = _FakeAiohttpSession(ws)
        stt = SonioxSTT(api_key="test-key")
        stt._session = session  # inject fake session
        await stt.connect()
        try:
            assert stt._ws is ws
            assert len(ws.sent_text) == 1
            payload = json.loads(ws.sent_text[0])
            assert payload["api_key"] == "test-key"
            assert payload["model"] == "stt-rt-v4"
        finally:
            await stt.close()

    @pytest.mark.asyncio
    async def test_send_audio_requires_connect(self):
        from patter.providers.soniox_stt import SonioxSTT

        stt = SonioxSTT(api_key="k")
        with pytest.raises(RuntimeError):
            await stt.send_audio(b"abc")

    @pytest.mark.asyncio
    async def test_receive_accumulates_finals_until_endpoint(self):
        """MOCK: simulate two final tokens followed by an ``<end>`` token.

        Expected: one interim transcript for each partial message, then one
        final transcript carrying the concatenated text once the endpoint
        arrives.
        """
        from patter.providers.soniox_stt import SonioxSTT

        frames = [
            _FakeWSMsg(
                aiohttp.WSMsgType.TEXT,
                json.dumps(
                    {"tokens": [{"text": "Hello ", "is_final": True, "confidence": 0.9}]}
                ),
            ),
            _FakeWSMsg(
                aiohttp.WSMsgType.TEXT,
                json.dumps(
                    {"tokens": [{"text": "world", "is_final": True, "confidence": 0.8}]}
                ),
            ),
            _FakeWSMsg(
                aiohttp.WSMsgType.TEXT,
                json.dumps({"tokens": [{"text": "<end>", "is_final": True}]}),
            ),
        ]
        ws = _FakeAiohttpWS(frames)
        stt = SonioxSTT(api_key="k")
        stt._ws = ws  # bypass connect to drive only the receive path

        transcripts = []
        async for t in stt.receive_transcripts():
            transcripts.append(t)

        # Two interim emissions (one after each is_final-but-not-endpoint),
        # followed by one final emission.
        assert transcripts[-1].is_final is True
        assert transcripts[-1].text == "Hello world"
        assert any(not t.is_final for t in transcripts[:-1])

    @pytest.mark.asyncio
    async def test_receive_emits_interim_for_nonfinal_tokens(self):
        """MOCK: a single message containing only non-final tokens should
        produce an interim transcript with the best in-flight hypothesis."""
        from patter.providers.soniox_stt import SonioxSTT

        frames = [
            _FakeWSMsg(
                aiohttp.WSMsgType.TEXT,
                json.dumps(
                    {
                        "tokens": [
                            {"text": "Hi ", "is_final": False, "confidence": 0.5},
                            {"text": "there", "is_final": False, "confidence": 0.6},
                        ]
                    }
                ),
            ),
        ]
        ws = _FakeAiohttpWS(frames)
        stt = SonioxSTT(api_key="k")
        stt._ws = ws

        transcripts = [t async for t in stt.receive_transcripts()]
        assert len(transcripts) == 1
        assert transcripts[0].is_final is False
        assert transcripts[0].text == "Hi there"

    @pytest.mark.asyncio
    async def test_receive_skips_empty_tokens(self):
        """MOCK: a message with no tokens must not yield anything."""
        from patter.providers.soniox_stt import SonioxSTT

        frames = [_FakeWSMsg(aiohttp.WSMsgType.TEXT, json.dumps({"tokens": []}))]
        ws = _FakeAiohttpWS(frames)
        stt = SonioxSTT(api_key="k")
        stt._ws = ws

        transcripts = [t async for t in stt.receive_transcripts()]
        assert transcripts == []

    @pytest.mark.asyncio
    async def test_finished_flushes_pending_final(self):
        """MOCK: server finished flag flushes any buffered final tokens."""
        from patter.providers.soniox_stt import SonioxSTT

        frames = [
            _FakeWSMsg(
                aiohttp.WSMsgType.TEXT,
                json.dumps(
                    {"tokens": [{"text": "Goodbye", "is_final": True, "confidence": 0.95}]}
                ),
            ),
            _FakeWSMsg(
                aiohttp.WSMsgType.TEXT,
                json.dumps({"tokens": [], "finished": True}),
            ),
        ]
        ws = _FakeAiohttpWS(frames)
        stt = SonioxSTT(api_key="k")
        stt._ws = ws

        transcripts = [t async for t in stt.receive_transcripts()]
        # An interim after the first frame, then a final flush on finished=true.
        assert transcripts[-1].is_final is True
        assert transcripts[-1].text == "Goodbye"

    @pytest.mark.asyncio
    async def test_close_cancels_keepalive_and_owns_session(self):
        from patter.providers.soniox_stt import SonioxSTT

        ws = _FakeAiohttpWS([])
        session = _FakeAiohttpSession(ws)
        stt = SonioxSTT(api_key="k")
        stt._session = session
        stt._owns_session = True
        await stt.connect()
        assert stt._keepalive_task is not None
        await stt.close()
        assert stt._ws is None
        assert stt._keepalive_task is None
        assert session.closed is True


# ---------------------------------------------------------------------------
# SpeechmaticsSTT (SDK mocked via sys.modules injection)
# ---------------------------------------------------------------------------


def _install_fake_speechmatics(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install a fake ``speechmatics.voice`` module so the adapter can import.

    Returns a namespace with references to the fake classes so tests can
    assert on the VoiceAgentClient interactions.
    """

    class _FakeEnum(str):
        """String-compatible enum stand-in for message type sentinels."""

    ADD_PARTIAL = "AddPartialSegment"
    ADD_SEGMENT = "AddSegment"
    END_OF_TURN = "EndOfTurn"
    ERROR = "Error"
    WARNING = "Warning"

    AgentServerMessageType = SimpleNamespace(
        ADD_PARTIAL_SEGMENT=ADD_PARTIAL,
        ADD_SEGMENT=ADD_SEGMENT,
        END_OF_TURN=END_OF_TURN,
        ERROR=ERROR,
        WARNING=WARNING,
    )

    class _FakeAudioEncoding:
        PCM_S16LE = "pcm_s16le"

    class _FakeSpeakerFocusMode:
        RETAIN = "retain"

    class _FakeOperatingPoint:
        ENHANCED = "enhanced"

    class _FakeConfig:
        def __init__(self, mode: str):
            self.mode = mode
            self.sample_rate = 16000
            self.audio_encoding = None
            self.language = None
            self.domain = None
            self.output_locale = None
            self.additional_vocab = None
            self.operating_point = None
            self.max_delay = None
            self.end_of_utterance_silence_trigger = None
            self.end_of_utterance_max_delay = None
            self.enable_diarization = False
            self.include_partials = True

    class _FakeConfigPreset:
        @staticmethod
        def load(mode: str) -> _FakeConfig:
            return _FakeConfig(mode)

    class _FakeVoiceAgentClient:
        def __init__(self, *, api_key: str, config: _FakeConfig, app: str = "", url: str = ""):
            self.api_key = api_key
            self.config = config
            self.app = app
            self.url = url
            self.handlers: dict[str, list] = {}
            self.connected = False
            self.sent_audio: list[bytes] = []
            self.disconnected = False

        def on(self, event, handler):
            self.handlers.setdefault(event, []).append(handler)

        async def connect(self):
            self.connected = True

        async def send_audio(self, audio: bytes):
            self.sent_audio.append(audio)

        async def disconnect(self):
            self.disconnected = True

        # Helper used by tests to push a message to all registered handlers.
        def _emit(self, event: str, message: dict):
            for handler in self.handlers.get(event, []):
                handler(message)

    class _FakeAdditionalVocabEntry:
        def __init__(self, content: str):
            self.content = content

    class _FakeSpeakerIdentifier:
        def __init__(self, label: str):
            self.label = label

    fake_voice = ModuleType("speechmatics.voice")
    fake_voice.AgentServerMessageType = AgentServerMessageType
    fake_voice.AudioEncoding = _FakeAudioEncoding
    fake_voice.SpeakerFocusMode = _FakeSpeakerFocusMode
    fake_voice.OperatingPoint = _FakeOperatingPoint
    fake_voice.VoiceAgentConfig = _FakeConfig
    fake_voice.VoiceAgentConfigPreset = _FakeConfigPreset
    fake_voice.VoiceAgentClient = _FakeVoiceAgentClient
    fake_voice.AdditionalVocabEntry = _FakeAdditionalVocabEntry
    fake_voice.SpeakerIdentifier = _FakeSpeakerIdentifier
    fake_voice.SpeakerFocusConfig = MagicMock

    fake_parent = ModuleType("speechmatics")
    fake_parent.voice = fake_voice

    monkeypatch.setitem(sys.modules, "speechmatics", fake_parent)
    monkeypatch.setitem(sys.modules, "speechmatics.voice", fake_voice)

    return SimpleNamespace(
        voice=fake_voice,
        AgentServerMessageType=AgentServerMessageType,
        VoiceAgentClient=_FakeVoiceAgentClient,
        ADD_PARTIAL=ADD_PARTIAL,
        ADD_SEGMENT=ADD_SEGMENT,
        END_OF_TURN=END_OF_TURN,
        ERROR=ERROR,
        WARNING=WARNING,
    )


@pytest.mark.unit
class TestSpeechmaticsSTTInit:
    def test_raises_on_missing_sdk(self, monkeypatch):
        # Make sure the import fails by removing any previous stub.
        monkeypatch.setitem(sys.modules, "speechmatics", None)
        monkeypatch.setitem(sys.modules, "speechmatics.voice", None)
        # Reimport module to force re-evaluation of _require_voice_sdk().
        monkeypatch.delitem(sys.modules, "patter.providers.speechmatics_stt", raising=False)

        from patter.providers.speechmatics_stt import SpeechmaticsSTT

        with pytest.raises(RuntimeError, match="speechmatics-voice"):
            SpeechmaticsSTT(api_key="k")

    def test_rejects_invalid_api_key(self, monkeypatch):
        _install_fake_speechmatics(monkeypatch)
        monkeypatch.delitem(sys.modules, "patter.providers.speechmatics_stt", raising=False)
        from patter.providers.speechmatics_stt import SpeechmaticsSTT

        with pytest.raises(ValueError, match="api_key"):
            SpeechmaticsSTT(api_key="")

    def test_validates_max_delay_range(self, monkeypatch):
        _install_fake_speechmatics(monkeypatch)
        monkeypatch.delitem(sys.modules, "patter.providers.speechmatics_stt", raising=False)
        from patter.providers.speechmatics_stt import SpeechmaticsSTT

        with pytest.raises(ValueError, match="max_delay"):
            SpeechmaticsSTT(api_key="k", max_delay=0.1)

    def test_validates_eou_constraints(self, monkeypatch):
        _install_fake_speechmatics(monkeypatch)
        monkeypatch.delitem(sys.modules, "patter.providers.speechmatics_stt", raising=False)
        from patter.providers.speechmatics_stt import SpeechmaticsSTT

        with pytest.raises(ValueError, match="end_of_utterance_silence_trigger"):
            SpeechmaticsSTT(api_key="k", end_of_utterance_silence_trigger=3.0)
        with pytest.raises(ValueError, match="end_of_utterance_max_delay"):
            SpeechmaticsSTT(
                api_key="k",
                end_of_utterance_silence_trigger=1.0,
                end_of_utterance_max_delay=0.5,
            )


@pytest.mark.unit
class TestSpeechmaticsSTTIO:
    @pytest.mark.asyncio
    async def test_connect_configures_client(self, monkeypatch):
        fake = _install_fake_speechmatics(monkeypatch)
        monkeypatch.delitem(sys.modules, "patter.providers.speechmatics_stt", raising=False)
        from patter.providers.speechmatics_stt import SpeechmaticsSTT

        stt = SpeechmaticsSTT(api_key="k", language="fr", enable_diarization=True)
        await stt.connect()
        try:
            assert stt._client is not None
            assert stt._client.connected is True
            # Assert the fake client received ADD_PARTIAL_SEGMENT handler.
            assert fake.ADD_PARTIAL in stt._client.handlers
            assert fake.ADD_SEGMENT in stt._client.handlers
        finally:
            await stt.close()

    @pytest.mark.asyncio
    async def test_send_audio_forwards_to_client(self, monkeypatch):
        _install_fake_speechmatics(monkeypatch)
        monkeypatch.delitem(sys.modules, "patter.providers.speechmatics_stt", raising=False)
        from patter.providers.speechmatics_stt import SpeechmaticsSTT

        stt = SpeechmaticsSTT(api_key="k")
        await stt.connect()
        try:
            await stt.send_audio(b"\x01\x02")
            assert stt._client.sent_audio == [b"\x01\x02"]
            # Empty chunk is a no-op.
            await stt.send_audio(b"")
            assert stt._client.sent_audio == [b"\x01\x02"]
        finally:
            await stt.close()

    @pytest.mark.asyncio
    async def test_receive_transcripts_translates_partial_and_final(self, monkeypatch):
        """MOCK: push two messages (partial + final) through the fake client
        event handler and assert that the adapter yields matching interim /
        final Transcript objects.
        """
        fake = _install_fake_speechmatics(monkeypatch)
        monkeypatch.delitem(sys.modules, "patter.providers.speechmatics_stt", raising=False)
        from patter.providers.speechmatics_stt import SpeechmaticsSTT

        stt = SpeechmaticsSTT(api_key="k")
        await stt.connect()
        try:
            # Enqueue a partial and a final message as if coming from the server.
            stt._client._emit(
                fake.ADD_PARTIAL,
                {
                    "message": fake.ADD_PARTIAL,
                    "segments": [{"text": "hello"}, {"text": "there"}],
                },
            )
            stt._client._emit(
                fake.ADD_SEGMENT,
                {
                    "message": fake.ADD_SEGMENT,
                    "segments": [{"text": "hello there", "confidence": 0.9}],
                },
            )
            # Stop the loop cleanly.
            stt._queue.put_nowait(stt._STOP)

            results = []
            async for t in stt.receive_transcripts():
                results.append(t)

            assert len(results) == 2
            assert results[0].is_final is False
            assert results[0].text == "hello there"
            assert results[1].is_final is True
            assert results[1].text == "hello there"
            assert results[1].confidence == pytest.approx(0.9)
        finally:
            await stt.close()

    @pytest.mark.asyncio
    async def test_end_of_turn_is_silent(self, monkeypatch):
        """MOCK: EndOfTurn should not produce a transcript."""
        fake = _install_fake_speechmatics(monkeypatch)
        monkeypatch.delitem(sys.modules, "patter.providers.speechmatics_stt", raising=False)
        from patter.providers.speechmatics_stt import SpeechmaticsSTT

        stt = SpeechmaticsSTT(api_key="k")
        await stt.connect()
        try:
            stt._client._emit(
                fake.END_OF_TURN, {"message": fake.END_OF_TURN}
            )
            stt._queue.put_nowait(stt._STOP)
            results = [t async for t in stt.receive_transcripts()]
            assert results == []
        finally:
            await stt.close()

    @pytest.mark.asyncio
    async def test_close_disconnects_client(self, monkeypatch):
        _install_fake_speechmatics(monkeypatch)
        monkeypatch.delitem(sys.modules, "patter.providers.speechmatics_stt", raising=False)
        from patter.providers.speechmatics_stt import SpeechmaticsSTT

        stt = SpeechmaticsSTT(api_key="k")
        await stt.connect()
        client = stt._client
        await stt.close()
        assert client.disconnected is True
        assert stt._client is None


# ---------------------------------------------------------------------------
# Integration tests (skipped by default — require real API keys)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("SONIOX_API_KEY"),
    reason="SONIOX_API_KEY not set; skipping live integration test",
)
@pytest.mark.asyncio
async def test_soniox_live_smoke():  # pragma: no cover - gated on credentials
    from patter.providers.soniox_stt import SonioxSTT

    stt = SonioxSTT(api_key=os.environ["SONIOX_API_KEY"])
    await stt.connect()
    await stt.close()


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("SPEECHMATICS_API_KEY"),
    reason="SPEECHMATICS_API_KEY not set; skipping live integration test",
)
@pytest.mark.asyncio
async def test_speechmatics_live_smoke():  # pragma: no cover - gated on credentials
    try:
        import speechmatics.voice  # noqa: F401
    except ImportError:
        pytest.skip("speechmatics-voice not installed")
    from patter.providers.speechmatics_stt import SpeechmaticsSTT

    stt = SpeechmaticsSTT(api_key=os.environ["SPEECHMATICS_API_KEY"])
    await stt.connect()
    await stt.close()
