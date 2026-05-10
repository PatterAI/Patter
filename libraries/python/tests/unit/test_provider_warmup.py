"""Unit tests for the concrete provider WebSocket / HTTP warmup overrides.

Covers the per-provider ``warmup()`` overrides shipped on top of the no-op
default declared on :class:`STTProvider` / :class:`TTSProvider`. Each test
checks two invariants:

* ``warmup()`` completes without raising (best-effort contract).
* When a provider opens a connection, it does NOT request any synthesis or
  send any audio frames — billing-during-warmup must remain zero per the
  per-provider docstrings.

Tests use authentic real code paths — only the network boundary
(``websockets.connect`` / ``aiohttp.ws_connect`` / ``httpx`` / OpenAI
``response.create``) is mocked. See ``.claude/rules/authentic-tests.md``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Deepgram STT WS warmup
# ---------------------------------------------------------------------------


class _FakeAsyncWS:
    """Minimal stand-in for a websockets ClientConnection used in warmup tests.

    Tracks ``send`` / ``recv`` / ``close`` calls so assertions can verify
    no audio was sent and the socket was closed cleanly.
    """

    def __init__(self, *, recv_responses: list[str] | None = None) -> None:
        self.send_calls: list[bytes | str] = []
        self.close_calls = 0
        self._recv_responses = list(recv_responses or [])

    async def send(self, payload: bytes | str) -> None:
        self.send_calls.append(payload)

    async def recv(self) -> str:
        if not self._recv_responses:
            raise asyncio.TimeoutError
        return self._recv_responses.pop(0)

    async def close(self) -> None:
        self.close_calls += 1


import asyncio  # noqa: E402 — late import so _FakeAsyncWS can reference it


@pytest.mark.mocked
async def test_deepgram_stt_warmup_opens_and_closes_ws_without_audio() -> None:
    """Deepgram warmup opens the WS, sleeps briefly, and closes — no audio."""
    from getpatter.providers.deepgram_stt import DeepgramSTT

    fake_ws = _FakeAsyncWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    stt = DeepgramSTT(api_key="test-key")

    with patch(
        "getpatter.providers.deepgram_stt.websockets.connect",
        side_effect=fake_connect,
    ):
        await stt.warmup()

    # WS opened and closed — no audio frames sent during warmup.
    assert fake_ws.close_calls >= 1
    assert all(isinstance(call, str) for call in fake_ws.send_calls), (
        "warmup must never send binary audio frames (would consume billable seconds)"
    )
    # Specifically: no binary chunks, and no protocol-level synthesis.
    assert not any(isinstance(call, (bytes, bytearray)) for call in fake_ws.send_calls)


@pytest.mark.mocked
async def test_deepgram_stt_warmup_swallows_connect_errors() -> None:
    """A network failure during warmup must not raise — best-effort contract."""
    from getpatter.providers.deepgram_stt import DeepgramSTT

    async def fake_connect(*_args, **_kwargs):
        raise OSError("DNS down")

    stt = DeepgramSTT(api_key="test-key")
    with patch(
        "getpatter.providers.deepgram_stt.websockets.connect",
        side_effect=fake_connect,
    ):
        # Must not raise.
        await stt.warmup()


# ---------------------------------------------------------------------------
# Cartesia STT WS warmup
# ---------------------------------------------------------------------------


class _FakeAiohttpWS:
    """Minimal stand-in for ``aiohttp.ClientWebSocketResponse``."""

    def __init__(self) -> None:
        self.send_bytes_calls: list[bytes] = []
        self.send_str_calls: list[str] = []
        self.closed = False

    async def send_bytes(self, data: bytes) -> None:
        self.send_bytes_calls.append(data)

    async def send_str(self, payload: str) -> None:
        self.send_str_calls.append(payload)

    async def close(self) -> None:
        self.closed = True


class _FakeAiohttpSession:
    def __init__(self, ws: _FakeAiohttpWS) -> None:
        self._ws = ws
        self.closed = False

    async def ws_connect(self, *_args, **_kwargs) -> _FakeAiohttpWS:
        return self._ws

    async def close(self) -> None:
        self.closed = True


@pytest.mark.mocked
async def test_cartesia_stt_warmup_opens_and_closes_ws_without_audio() -> None:
    """Cartesia STT warmup opens WS, idles, closes — no audio bytes."""
    from getpatter.providers import cartesia_stt as mod
    from getpatter.providers.cartesia_stt import CartesiaSTT

    fake_ws = _FakeAiohttpWS()
    fake_session = _FakeAiohttpSession(fake_ws)

    stt = CartesiaSTT(api_key="test-key")
    with patch.object(mod.aiohttp, "ClientSession", return_value=fake_session):
        await stt.warmup()

    assert fake_ws.closed
    assert fake_session.closed
    # No audio frames sent during warmup — billing protection.
    assert fake_ws.send_bytes_calls == []


@pytest.mark.mocked
async def test_cartesia_stt_warmup_swallows_connect_errors() -> None:
    from getpatter.providers import cartesia_stt as mod
    from getpatter.providers.cartesia_stt import CartesiaSTT

    class _BoomSession:
        async def ws_connect(self, *_a, **_k):
            raise ConnectionError("network down")

        async def close(self) -> None:
            return None

    stt = CartesiaSTT(api_key="test-key")
    with patch.object(mod.aiohttp, "ClientSession", return_value=_BoomSession()):
        await stt.warmup()  # must not raise


@pytest.mark.mocked
async def test_cartesia_stt_warmup_handshake_error_does_not_leak_api_key(
    caplog,
) -> None:
    """Regression: a 401/403 from Cartesia must NOT log the request URL.

    Cartesia auth uses ``?api_key=...`` in the URL. The default
    ``aiohttp.WSServerHandshakeError.__str__`` includes the URL, which
    means a generic ``logger.debug("warmup failed: %s", exc)`` would
    write the API key straight into application logs.
    """
    import logging

    from getpatter.providers import cartesia_stt as mod
    from getpatter.providers.cartesia_stt import CartesiaSTT

    secret_key = "ck_secret_THIS_MUST_NEVER_LEAK"

    class _HandshakeFailSession:
        async def ws_connect(self, *_a, **_k):
            # Build a real WSServerHandshakeError so the regression test
            # exercises the same exception class as production.
            from aiohttp import WSServerHandshakeError
            from aiohttp.client_reqrep import RequestInfo
            from yarl import URL

            url = URL(f"wss://api.cartesia.ai/stt/websocket?api_key={secret_key}")
            req_info = RequestInfo(
                url=url,
                method="GET",
                headers={},  # type: ignore[arg-type]
                real_url=url,
            )
            raise WSServerHandshakeError(
                request_info=req_info,
                history=(),
                status=401,
                message="Unauthorized",
                headers=None,
            )

        async def close(self) -> None:
            return None

    stt = CartesiaSTT(api_key=secret_key)
    caplog.set_level(logging.DEBUG, logger="getpatter")
    with patch.object(
        mod.aiohttp, "ClientSession", return_value=_HandshakeFailSession()
    ):
        await stt.warmup()  # must not raise

    # The API key must not appear in any captured log message.
    for rec in caplog.records:
        assert secret_key not in rec.getMessage(), (
            f"API key leaked in log: {rec.getMessage()!r}"
        )
        assert "api_key=" not in rec.getMessage(), (
            f"URL with api_key= leaked in log: {rec.getMessage()!r}"
        )
    # And we should still log SOMETHING — namely the HTTP status — so
    # operators know the warmup failed and why.
    assert any(
        "HTTP 401" in rec.getMessage() or "401" in rec.getMessage()
        for rec in caplog.records
    ), (
        "expected status code in log; got: "
        f"{[rec.getMessage() for rec in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# AssemblyAI STT WS warmup
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_assemblyai_stt_warmup_opens_and_closes_ws_without_audio() -> None:
    """AssemblyAI warmup opens WS, sends Terminate (no audio), closes."""
    from getpatter.providers import assemblyai_stt as mod
    from getpatter.providers.assemblyai_stt import AssemblyAISTT

    fake_ws = _FakeAiohttpWS()
    fake_session = _FakeAiohttpSession(fake_ws)

    stt = AssemblyAISTT(api_key="test-key")
    with patch.object(mod.aiohttp, "ClientSession", return_value=fake_session):
        await stt.warmup()

    assert fake_ws.closed
    assert fake_session.closed
    # No audio frames sent during warmup.
    assert fake_ws.send_bytes_calls == []
    # Terminate frame is fine — it's a control message, not audio.
    if fake_ws.send_str_calls:
        for payload in fake_ws.send_str_calls:
            parsed = json.loads(payload)
            assert parsed.get("type") == "Terminate"


@pytest.mark.mocked
async def test_assemblyai_stt_warmup_swallows_connect_errors() -> None:
    from getpatter.providers import assemblyai_stt as mod
    from getpatter.providers.assemblyai_stt import AssemblyAISTT

    class _BoomSession:
        async def ws_connect(self, *_a, **_k):
            raise ConnectionError("network down")

        async def close(self) -> None:
            return None

    stt = AssemblyAISTT(api_key="test-key")
    with patch.object(mod.aiohttp, "ClientSession", return_value=_BoomSession()):
        await stt.warmup()  # must not raise


@pytest.mark.mocked
async def test_assemblyai_stt_warmup_handshake_error_does_not_leak_api_key(
    caplog,
) -> None:
    """Regression: a 401/403 from AssemblyAI must NOT log the request URL.

    AssemblyAI auth supports ``?token=...`` in the URL when
    ``use_query_token=True``. The default
    ``aiohttp.WSServerHandshakeError.__str__`` includes the URL, which
    means a generic ``logger.debug("warmup failed: %s", exc)`` would
    write the API key straight into application logs.
    """
    import logging

    from getpatter.providers import assemblyai_stt as mod
    from getpatter.providers.assemblyai_stt import AssemblyAISTT

    secret_key = "aai_secret_THIS_MUST_NEVER_LEAK"

    class _HandshakeFailSession:
        async def ws_connect(self, *_a, **_k):
            from aiohttp import WSServerHandshakeError
            from aiohttp.client_reqrep import RequestInfo
            from yarl import URL

            url = URL(f"wss://streaming.assemblyai.com/v3/ws?token={secret_key}")
            req_info = RequestInfo(
                url=url,
                method="GET",
                headers={},  # type: ignore[arg-type]
                real_url=url,
            )
            raise WSServerHandshakeError(
                request_info=req_info,
                history=(),
                status=401,
                message="Unauthorized",
                headers=None,
            )

        async def close(self) -> None:
            return None

    stt = AssemblyAISTT(api_key=secret_key, use_query_token=True)
    caplog.set_level(logging.DEBUG, logger="getpatter")
    with patch.object(
        mod.aiohttp, "ClientSession", return_value=_HandshakeFailSession()
    ):
        await stt.warmup()  # must not raise

    for rec in caplog.records:
        assert secret_key not in rec.getMessage(), (
            f"API key leaked in log: {rec.getMessage()!r}"
        )
        assert "token=" not in rec.getMessage(), (
            f"URL with token= leaked in log: {rec.getMessage()!r}"
        )
    assert any(
        "HTTP 401" in rec.getMessage() or "401" in rec.getMessage()
        for rec in caplog.records
    ), (
        "expected status code in log; got: "
        f"{[rec.getMessage() for rec in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# ElevenLabs WS TTS warmup
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_elevenlabs_ws_tts_warmup_opens_sends_keepalive_closes() -> None:
    """ElevenLabs WS TTS warmup: opens WS, sends single-space keepalive, closes.

    Specifically MUST NOT send any text + flush:true (which would commit a
    synthesis and consume billable characters).
    """
    from getpatter.providers import elevenlabs_ws_tts as mod
    from getpatter.providers.elevenlabs_ws_tts import ElevenLabsWebSocketTTS

    fake_ws = _FakeAsyncWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    tts = ElevenLabsWebSocketTTS(api_key="test-key")
    with patch.object(mod.websockets, "connect", side_effect=fake_connect):
        await tts.warmup()

    assert fake_ws.close_calls >= 1
    # Inspect every send during warmup: must be either the single-space
    # keepalive `{"text": " "}` OR nothing else. Specifically NO `flush:true`.
    for raw_payload in fake_ws.send_calls:
        # Should be string / json
        if isinstance(raw_payload, (bytes, bytearray)):
            raise AssertionError("warmup sent binary frame — must be JSON only")
        msg = json.loads(raw_payload)
        # No `flush: true` (would commit synthesis and bill characters).
        assert msg.get("flush") is not True, (
            f"warmup must not commit synthesis (flush:true). saw: {msg}"
        )
        # Text must be empty/space — no real transcript.
        text = msg.get("text", "")
        assert text.strip() == "", f"warmup sent non-empty text: {text!r}"


@pytest.mark.mocked
async def test_elevenlabs_ws_tts_warmup_swallows_connect_errors() -> None:
    from getpatter.providers import elevenlabs_ws_tts as mod
    from getpatter.providers.elevenlabs_ws_tts import ElevenLabsWebSocketTTS

    async def fake_connect(*_args, **_kwargs):
        raise OSError("DNS down")

    tts = ElevenLabsWebSocketTTS(api_key="test-key")
    with patch.object(mod.websockets, "connect", side_effect=fake_connect):
        await tts.warmup()  # must not raise


@pytest.mark.mocked
async def test_elevenlabs_ws_warmup_bos_frame_matches_live_synthesize() -> None:
    """Regression: warmup BOS bytes must equal synthesize() BOS bytes.

    If the warmup primer differs from the production BOS, ElevenLabs may
    instantiate a different per-session worker for the warm path vs the
    live path, defeating the warmup goal entirely. This test captures
    both BOS frames and asserts they're byte-identical.
    """
    from getpatter.providers import elevenlabs_ws_tts as mod
    from getpatter.providers.elevenlabs_ws_tts import ElevenLabsWebSocketTTS

    # Configure with non-default voice_settings + auto_mode=False +
    # chunk_length_schedule so the BOS frame carries every optional field.
    tts = ElevenLabsWebSocketTTS(
        api_key="test-key",
        voice_settings={"stability": 0.7, "similarity_boost": 0.8},
        auto_mode=False,
        chunk_length_schedule=[120, 160, 250, 290],
    )

    # --- Capture warmup BOS ---
    warmup_ws = _FakeAsyncWS()

    async def fake_warmup_connect(*_args, **_kwargs):
        return warmup_ws

    with patch.object(mod.websockets, "connect", side_effect=fake_warmup_connect):
        await tts.warmup()

    warmup_bos_bytes: bytes | None = None
    for payload in warmup_ws.send_calls:
        # First send is the BOS frame.
        if isinstance(payload, str):
            warmup_bos_bytes = payload.encode("utf-8")
            break
    assert warmup_bos_bytes is not None, "warmup did not send any frame"

    # --- Capture synthesize BOS ---
    class _SynthesizeFakeWS:
        """Fake WS for synthesize() that yields one final-marker frame so
        the generator ends quickly without any real audio."""

        def __init__(self) -> None:
            self.send_calls: list[str | bytes] = []
            self._recv_calls = 0

        async def send(self, payload: str | bytes) -> None:
            self.send_calls.append(payload)

        async def recv(self) -> str:
            self._recv_calls += 1
            # First recv: send isFinal=True so generator returns immediately.
            if self._recv_calls == 1:
                return json.dumps({"isFinal": True})
            await asyncio.sleep(0)
            raise asyncio.TimeoutError

        async def close(self) -> None:
            pass

    synth_ws = _SynthesizeFakeWS()

    async def fake_synth_connect(*_args, **_kwargs):
        return synth_ws

    with patch.object(mod.websockets, "connect", side_effect=fake_synth_connect):
        gen = tts.synthesize("hello")
        # Drain the generator — it should exit on the isFinal frame.
        async for _chunk in gen:
            pass

    synth_bos_bytes: bytes | None = None
    for payload in synth_ws.send_calls:
        if isinstance(payload, str):
            synth_bos_bytes = payload.encode("utf-8")
            break
    assert synth_bos_bytes is not None, "synthesize did not send any frame"

    # The BOS bytes must be byte-identical so ElevenLabs picks the same
    # per-session worker for warm and live.
    assert warmup_bos_bytes == synth_bos_bytes, (
        f"BOS drift: warmup={warmup_bos_bytes!r}, synthesize={synth_bos_bytes!r}"
    )
    # And specifically: must NOT include flush:true (would commit synthesis).
    parsed = json.loads(warmup_bos_bytes.decode("utf-8"))
    assert parsed.get("flush") is not True
    assert parsed.get("text", "").strip() == ""


# ---------------------------------------------------------------------------
# Cartesia TTS HTTP warmup
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_cartesia_tts_warmup_issues_get_to_voices_endpoint() -> None:
    """Cartesia TTS HTTP warmup issues GET /voices — no synthesis POST."""
    from getpatter.providers.cartesia_tts import CartesiaTTS

    captured: dict[str, object] = {}

    class _FakeResp:
        async def __aenter__(self) -> "_FakeResp":
            return self

        async def __aexit__(self, *_a) -> None:
            return None

        async def read(self) -> bytes:
            return b""

    class _FakeSession:
        def get(self, url: str, **kwargs: object) -> _FakeResp:
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _FakeResp()

        def post(self, *_a, **_k) -> _FakeResp:
            captured["post_called"] = True
            return _FakeResp()

        async def close(self) -> None:
            return None

    tts = CartesiaTTS(api_key="test-key", session=_FakeSession())  # type: ignore[arg-type]
    await tts.warmup()

    # GET landed on the /voices endpoint, not /tts/bytes (which would bill).
    assert "voices" in str(captured.get("url", ""))
    assert "post_called" not in captured, "warmup must not POST /tts/bytes"


@pytest.mark.mocked
async def test_cartesia_tts_warmup_swallows_errors() -> None:
    from getpatter.providers.cartesia_tts import CartesiaTTS

    class _BoomSession:
        def get(self, *_a, **_k):
            raise RuntimeError("DNS down")

        async def close(self) -> None:
            return None

    tts = CartesiaTTS(api_key="test-key", session=_BoomSession())  # type: ignore[arg-type]
    await tts.warmup()  # must not raise


# ---------------------------------------------------------------------------
# Inworld TTS HTTP warmup
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_inworld_tts_warmup_issues_get_voices_request() -> None:
    """Inworld TTS warmup issues GET /tts/v1/voices — 2xx, never synthesises.

    Earlier revisions used HEAD against the POST-only streaming endpoint,
    which returned 405. The new path uses the documented voices metadata
    GET so the response is 2xx and no 405s are spammed into audit logs.
    """
    from getpatter.providers.inworld_tts import InworldTTS

    captured: dict[str, object] = {}

    class _FakeResp:
        status = 200

        async def __aenter__(self) -> "_FakeResp":
            return self

        async def __aexit__(self, *_a) -> None:
            return None

        async def read(self) -> bytes:
            return b""

    class _FakeSession:
        def get(self, url: str, **kwargs: object) -> _FakeResp:
            captured["url"] = url
            captured["method"] = "GET"
            return _FakeResp()

        def post(self, *_a, **_k) -> _FakeResp:
            captured["post_called"] = True
            return _FakeResp()

        def head(self, *_a, **_k) -> _FakeResp:
            captured["head_called"] = True
            return _FakeResp()

        async def close(self) -> None:
            return None

    tts = InworldTTS(auth_token="test-token", session=_FakeSession())  # type: ignore[arg-type]
    await tts.warmup()

    assert captured.get("method") == "GET"
    # URL must point at the voices metadata endpoint, not the
    # POST-only streaming endpoint (which would have returned 405).
    assert "/tts/v1/voices" in str(captured.get("url", ""))
    assert "voice:stream" not in str(captured.get("url", "")), (
        "warmup must not target the POST-only streaming endpoint"
    )
    assert "post_called" not in captured, "warmup must not POST the synth endpoint"
    assert "head_called" not in captured, "warmup must not HEAD (returns 405)"
    # Response status must be 2xx (verified by the fake responding with 200).
    # The implementation does not surface the status, but the test
    # confirms the call lands on a 2xx-returning route by asserting the URL.


@pytest.mark.mocked
async def test_inworld_tts_warmup_swallows_errors() -> None:
    from getpatter.providers.inworld_tts import InworldTTS

    class _BoomSession:
        def get(self, *_a, **_k):
            raise RuntimeError("DNS down")

        async def close(self) -> None:
            return None

    tts = InworldTTS(auth_token="test-token", session=_BoomSession())  # type: ignore[arg-type]
    await tts.warmup()  # must not raise


# ---------------------------------------------------------------------------
# OpenAI Realtime warmup (session.update — billing-safe, no response.create)
# ---------------------------------------------------------------------------


@pytest.mark.mocked
async def test_openai_realtime_warmup_sends_session_update_only() -> None:
    """OpenAI Realtime warmup sends session.update + waits for session.updated.

    Critically: must NOT send ``response.create`` — that field is not in
    the OpenAI Realtime schema and either (a) bills tokens for a real
    response or (b) returns ``invalid_request_error``. Both are wrong.

    Must also NOT send ``input_audio_buffer.append`` (would consume
    billable audio).
    """
    from getpatter.providers import openai_realtime as mod
    from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

    sent: list[str] = []

    class _FakeWS:
        def __init__(self) -> None:
            # Pre-canned server frames: session.created → session.updated.
            self._recv_queue = [
                json.dumps({"type": "session.created"}),
                json.dumps({"type": "session.updated"}),
            ]
            self.closed = False

        async def send(self, payload: str) -> None:
            sent.append(payload)

        async def recv(self) -> str:
            if not self._recv_queue:
                # Simulate a server idle — give the warmup time to time out.
                await asyncio.sleep(0)
                raise asyncio.TimeoutError
            return self._recv_queue.pop(0)

        async def close(self) -> None:
            self.closed = True

    fake_ws = _FakeWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    adapter = OpenAIRealtimeAdapter(
        api_key="sk-test",
        voice="alloy",
        instructions="You are a test assistant.",
    )
    with patch.object(mod.websockets, "connect", side_effect=fake_connect):
        await adapter.warmup()

    # Must NOT send response.create — the field is not in the OpenAI
    # Realtime schema and is billing-unsafe.
    parsed = [json.loads(s) for s in sent]
    for p in parsed:
        assert p.get("type") != "response.create", (
            f"warmup must not invoke response.create — schema-invalid and "
            f"billing-unsafe. saw: {p}"
        )
        assert p.get("type") != "input_audio_buffer.append", (
            "warmup must not send audio — would consume billable seconds"
        )
    # Must send exactly one session.update with the production fields.
    updates = [p for p in parsed if p.get("type") == "session.update"]
    assert len(updates) == 1, f"expected one session.update, got: {parsed}"
    session = updates[0]["session"]
    # Production fields must be primed identically to ``connect()`` so the
    # upstream session state is warmed for the real call.
    for required in (
        "input_audio_format",
        "output_audio_format",
        "voice",
        "instructions",
        "turn_detection",
        "input_audio_transcription",
    ):
        assert required in session, f"session.update missing {required!r}: {session}"
    assert session["voice"] == "alloy"
    assert session["instructions"] == "You are a test assistant."
    assert fake_ws.closed


@pytest.mark.mocked
async def test_openai_realtime_warmup_does_not_send_response_create() -> None:
    """Regression: warmup never sends response.create — schema-invalid and unsafe."""
    from getpatter.providers import openai_realtime as mod
    from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

    sent: list[str] = []

    class _FakeWS:
        def __init__(self) -> None:
            self._recv_queue = [
                json.dumps({"type": "session.created"}),
                json.dumps({"type": "session.updated"}),
            ]
            self.closed = False

        async def send(self, payload: str) -> None:
            sent.append(payload)

        async def recv(self) -> str:
            if not self._recv_queue:
                await asyncio.sleep(0)
                raise asyncio.TimeoutError
            return self._recv_queue.pop(0)

        async def close(self) -> None:
            self.closed = True

    fake_ws = _FakeWS()

    async def fake_connect(*_args, **_kwargs):
        return fake_ws

    adapter = OpenAIRealtimeAdapter(api_key="sk-test")
    with patch.object(mod.websockets, "connect", side_effect=fake_connect):
        await adapter.warmup()

    for raw in sent:
        assert "response.create" not in raw, (
            f"warmup must not send response.create — saw: {raw}"
        )


@pytest.mark.mocked
async def test_openai_realtime_warmup_swallows_connect_errors() -> None:
    from getpatter.providers import openai_realtime as mod
    from getpatter.providers.openai_realtime import OpenAIRealtimeAdapter

    async def fake_connect(*_args, **_kwargs):
        raise OSError("DNS down")

    adapter = OpenAIRealtimeAdapter(api_key="sk-test")
    with patch.object(mod.websockets, "connect", side_effect=fake_connect):
        await adapter.warmup()  # must not raise
