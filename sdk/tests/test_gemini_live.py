"""Tests for GeminiLiveAdapter.

These are unit tests that stub out the google-genai client — the adapter
itself is pure orchestration logic, so end-to-end tests against the real
Gemini Live endpoint live in ``tests/integration`` and skip when the extra
is not installed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from patter.providers.gemini_live import (
    DEFAULT_INPUT_SAMPLE_RATE_HZ,
    DEFAULT_OUTPUT_SAMPLE_RATE_HZ,
    GeminiLiveAdapter,
)


def _install_fake_genai(monkeypatch: pytest.MonkeyPatch, session_mock: MagicMock) -> None:
    """Install a minimal fake ``google.genai`` module into sys.modules."""
    genai_pkg = types.ModuleType("google")
    genai_mod = types.ModuleType("google.genai")
    genai_types_mod = types.ModuleType("google.genai.types")

    # Types used at connect time — fake constructors that just record args.
    class _Box:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    genai_types_mod.SpeechConfig = _Box
    genai_types_mod.VoiceConfig = _Box
    genai_types_mod.PrebuiltVoiceConfig = _Box
    genai_types_mod.Content = _Box
    genai_types_mod.Part = _Box

    # Client → aio.live.connect returns an async CM that yields the session.
    class _AioLive:
        def connect(self, **kwargs: object) -> object:  # noqa: D401
            return session_mock.connect(**kwargs)

    class _Aio:
        live = _AioLive()

    class _Client:
        def __init__(self, **_: object) -> None:
            self.aio = _Aio()

    genai_mod.Client = _Client
    genai_mod.types = genai_types_mod

    monkeypatch.setitem(sys.modules, "google", genai_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", genai_types_mod)


class _FakeSessionCM:
    """Async context manager that yields a session mock."""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return False


def _make_session_mock():
    session = MagicMock()
    session.send_realtime_input = AsyncMock()
    session.send_client_content = AsyncMock()
    session.send_tool_response = AsyncMock()

    factory = MagicMock()
    factory.connect = MagicMock(return_value=_FakeSessionCM(session))
    factory._session = session
    return factory


@pytest.mark.asyncio
async def test_connect_creates_client_and_enters_session(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _make_session_mock()
    _install_fake_genai(monkeypatch, factory)

    adapter = GeminiLiveAdapter(
        api_key="fake",
        instructions="be polite",
        tools=[{"name": "weather", "description": "get weather", "parameters": {"type": "object"}}],
    )
    await adapter.connect()

    assert adapter._session is not None
    # The fake connect() should have been called once
    assert factory.connect.called
    kwargs = factory.connect.call_args.kwargs
    assert kwargs["model"] == adapter.model
    assert "tools" in kwargs["config"]
    assert "system_instruction" in kwargs["config"]


@pytest.mark.asyncio
async def test_send_audio_uses_correct_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _make_session_mock()
    _install_fake_genai(monkeypatch, factory)

    adapter = GeminiLiveAdapter(api_key="fake", input_sample_rate=16000)
    await adapter.connect()
    await adapter.send_audio(b"\x01\x02\x03")

    call = adapter._session.send_realtime_input.await_args
    assert call is not None
    assert call.kwargs["media"]["mime_type"] == "audio/pcm;rate=16000"
    assert call.kwargs["media"]["data"] == b"\x01\x02\x03"


@pytest.mark.asyncio
async def test_missing_google_genai_raises_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Remove any genai import that may have leaked in.
    for k in list(sys.modules):
        if k == "google" or k.startswith("google.genai"):
            monkeypatch.delitem(sys.modules, k, raising=False)

    # Force import to fail by injecting a blocker.
    class _Blocker:
        def find_module(self, name, path=None):
            if name == "google.genai" or name == "google":
                return self
            return None

        def load_module(self, name):  # pragma: no cover - exercised via import
            raise ImportError(f"blocked: {name}")

    sys.meta_path.insert(0, _Blocker())
    try:
        adapter = GeminiLiveAdapter(api_key="fake")
        with pytest.raises(RuntimeError, match="google-genai"):
            await adapter.connect()
    finally:
        sys.meta_path.pop(0)


@pytest.mark.asyncio
async def test_receive_events_translates_audio_and_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _make_session_mock()
    _install_fake_genai(monkeypatch, factory)

    adapter = GeminiLiveAdapter(api_key="fake")
    await adapter.connect()

    # Craft fake server messages using simple namespace objects so attribute
    # access returns real values rather than auto-generated MagicMocks.
    from types import SimpleNamespace as NS

    audio_msg = NS(
        server_content=NS(
            model_turn=NS(parts=[NS(inline_data=NS(data=b"\xAA\xBB"), text=None)]),
            turn_complete=False,
            interrupted=False,
        ),
        tool_call=None,
    )
    tool_msg = NS(
        server_content=None,
        tool_call=NS(
            function_calls=[NS(id="abc", name="lookup", args={"q": "pizza"})]
        ),
    )
    done_msg = NS(
        server_content=NS(model_turn=None, turn_complete=True, interrupted=False),
        tool_call=None,
    )

    async def fake_receive():
        for m in [audio_msg, tool_msg, done_msg]:
            yield m

    adapter._session.receive = fake_receive

    events = []
    async for ev in adapter.receive_events():
        events.append(ev)

    types_ = [e[0] for e in events]
    assert "audio" in types_
    assert "function_call" in types_
    assert "response_done" in types_
    fn_call = next(e for e in events if e[0] == "function_call")
    assert fn_call[1]["call_id"] == "abc"
    assert fn_call[1]["name"] == "lookup"
    # arguments is JSON-serialised when args is a dict
    assert json.loads(fn_call[1]["arguments"]) == {"q": "pizza"}


@pytest.mark.asyncio
async def test_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = _make_session_mock()
    _install_fake_genai(monkeypatch, factory)

    adapter = GeminiLiveAdapter(api_key="fake")
    await adapter.connect()
    await adapter.close()
    await adapter.close()  # must not raise
    assert adapter._session is None


@pytest.mark.asyncio
async def test_defaults_are_sane() -> None:
    adapter = GeminiLiveAdapter(api_key="fake")
    assert adapter.model.startswith("gemini")
    assert adapter.voice == "Puck"
    assert adapter.language == "en-US"
    assert adapter.input_sample_rate == DEFAULT_INPUT_SAMPLE_RATE_HZ
    assert adapter.output_sample_rate == DEFAULT_OUTPUT_SAMPLE_RATE_HZ
