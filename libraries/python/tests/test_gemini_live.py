"""Authentic tests for the Gemini Live adapter's async-mode tool-call fixes.

The ``gemini-3.1-flash-live`` family wedges in the default BLOCKING
function-call mode: the model halts audio at the ``toolCall`` and never
resumes after the response (no error, just silence until the caller hangs
up). The fix opts flash-live into the Live API's async mode —
``behavior: NON_BLOCKING`` on the declaration and ``scheduling: INTERRUPT``
on the response — while every other model stays byte-identical on the wire.

Mock surface: only the google-genai Live session boundary (a paid external
WebSocket). Everything else — config assembly, the flash-live predicate, the
tool-response shaping — is the real adapter code, so swapping in a real
session would exercise the same paths.
"""

from __future__ import annotations

import logging

import pytest

from getpatter.providers.gemini_live import GeminiLiveAdapter, GeminiLiveModel


class _CapturingSession:
    """Stand-in for the real google-genai Live session (the WebSocket boundary).

    Records the tool responses it is sent. ``send_tool_response`` mirrors the
    real coroutine's keyword-only ``function_responses`` signature.
    """

    def __init__(self) -> None:
        self.tool_responses: list[list[dict]] = []

    async def send_tool_response(self, *, function_responses: list[dict]) -> None:
        self.tool_responses.append(function_responses)


@pytest.mark.mocked
async def test_flash_live_tool_response_carries_interrupt_scheduling() -> None:
    adapter = GeminiLiveAdapter(
        api_key="test-key",
        model=GeminiLiveModel.LIVE_3_1_FLASH_PREVIEW,
    )
    session = _CapturingSession()
    # Boundary injection: swap in the fake session so we exercise the real
    # send_function_result shaping without opening a live socket.
    adapter._session = session
    adapter._pending_tool_calls["call-1"] = "qualifica_lead"

    await adapter.send_function_result("call-1", '{"ok": true}')

    assert len(session.tool_responses) == 1
    fr = session.tool_responses[0][0]
    assert fr["id"] == "call-1"
    # Gemini needs the original function name, not the call id.
    assert fr["name"] == "qualifica_lead"
    assert fr["response"] == {"result": '{"ok": true}'}
    # The fix: async pickup so 3.1 resumes speaking after the round-trip.
    assert fr["scheduling"] == "INTERRUPT"


@pytest.mark.mocked
async def test_non_flash_live_tool_response_has_no_scheduling() -> None:
    adapter = GeminiLiveAdapter(
        api_key="test-key",
        model=GeminiLiveModel.NATIVE_AUDIO_PREVIEW_09_2025,
    )
    session = _CapturingSession()
    adapter._session = session
    adapter._pending_tool_calls["call-2"] = "lookup"

    await adapter.send_function_result("call-2", "result")

    fr = session.tool_responses[0][0]
    # 2.5 native-audio is byte-identical on the wire — no scheduling key.
    assert "scheduling" not in fr


@pytest.mark.mocked
async def test_tool_response_logged_at_info(caplog) -> None:
    adapter = GeminiLiveAdapter(
        api_key="test-key",
        model=GeminiLiveModel.LIVE_3_1_FLASH_PREVIEW,
    )
    adapter._session = _CapturingSession()
    adapter._pending_tool_calls["call-3"] = "book_slot"

    with caplog.at_level(logging.INFO, logger="getpatter.gemini_live"):
        await adapter.send_function_result("call-3", "ok")

    # The round-trip was invisible at default log level in prod — this stall
    # was undiagnosable without it. It must now surface at info.
    assert any(
        "toolResponse sent" in r.message and "call-3" in r.message
        for r in caplog.records
    )


@pytest.mark.mocked
async def test_tool_response_send_failure_is_logged_and_reraised(caplog) -> None:
    class _FailingSession:
        async def send_tool_response(self, *, function_responses):
            raise RuntimeError("socket closed")

    adapter = GeminiLiveAdapter(
        api_key="test-key",
        model=GeminiLiveModel.LIVE_3_1_FLASH_PREVIEW,
    )
    adapter._session = _FailingSession()
    adapter._pending_tool_calls["call-4"] = "book_slot"

    with caplog.at_level(logging.ERROR, logger="getpatter.gemini_live"):
        with pytest.raises(RuntimeError, match="socket closed"):
            await adapter.send_function_result("call-4", "ok")

    assert any("FAILED" in r.message and "call-4" in r.message for r in caplog.records)


class _FakeConnectCM:
    """Async context manager returned by the fake ``aio.live.connect``."""

    def __init__(self) -> None:
        self.session = _CapturingSession()

    async def __aenter__(self) -> _CapturingSession:
        return self.session

    async def __aexit__(self, *exc) -> bool:
        return False


def _install_config_capturing_client(monkeypatch, captured: dict) -> None:
    """Patch ``google.genai.Client`` to capture the config sent to connect.

    Only the genai client/session (the external WebSocket boundary) is faked;
    the real ``genai_types`` builders and the adapter's config assembly run.
    """
    genai = pytest.importorskip("google.genai")

    class _FakeAioLive:
        def connect(self, *, model, config):
            captured["model"] = model
            captured["config"] = config
            return _FakeConnectCM()

    class _FakeAio:
        live = _FakeAioLive()

    class _FakeClient:
        def __init__(self, **kwargs):
            self.aio = _FakeAio()

    monkeypatch.setattr(genai, "Client", _FakeClient)


@pytest.mark.mocked
async def test_flash_live_declaration_opts_into_non_blocking(monkeypatch) -> None:
    captured: dict = {}
    _install_config_capturing_client(monkeypatch, captured)

    tools = [
        {
            "name": "qualifica_lead",
            "description": "Qualify the inbound lead.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    adapter = GeminiLiveAdapter(
        api_key="test-key",
        model=GeminiLiveModel.LIVE_3_1_FLASH_PREVIEW,
        tools=tools,
    )
    await adapter.connect()

    decls = captured["config"]["tools"][0]["function_declarations"]
    assert decls[0]["behavior"] == "NON_BLOCKING"
    await adapter.close()


@pytest.mark.mocked
async def test_non_flash_live_declaration_has_no_behavior(monkeypatch) -> None:
    captured: dict = {}
    _install_config_capturing_client(monkeypatch, captured)

    tools = [
        {
            "name": "lookup",
            "description": "Look something up.",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    adapter = GeminiLiveAdapter(
        api_key="test-key",
        model=GeminiLiveModel.NATIVE_AUDIO_PREVIEW_09_2025,
        tools=tools,
    )
    await adapter.connect()

    decls = captured["config"]["tools"][0]["function_declarations"]
    # 2.5 native-audio is byte-identical on the wire — no behavior key.
    assert "behavior" not in decls[0]
    await adapter.close()


@pytest.mark.unit
def test_default_model_is_gemini_3_1_flash_live_preview() -> None:
    """3.x catalog refresh (2026-08-24): no explicit model resolves to the
    current audio-to-audio Live model, not the retired 2.5 dated preview."""
    adapter = GeminiLiveAdapter(api_key="test-key")
    assert adapter.model == GeminiLiveModel.LIVE_3_1_FLASH_PREVIEW
    assert adapter.model == "gemini-3.1-flash-live-preview"
