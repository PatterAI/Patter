"""Mocked unit tests for GeminiLiveAdapter fixes.

Mocks the google.genai boundary only. Tests verify:
  1. send_audio sends audio= field (not deprecated media=)
  2. api_version auto-detection from model name
  3. GEMINI_LIVE_3_1_FLASH_PREVIEW constant value
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from getpatter.providers.gemini_live import (
    GeminiLiveAdapter,
    GeminiLiveModel,
    GEMINI_LIVE_3_1_FLASH_PREVIEW,
)


def make_fake_session() -> MagicMock:
    async def fake_receive():
        yield MagicMock(server_content=None, go_away=None, tool_call=None)
        # Block forever (simulates open session)
        await asyncio.sleep(9999)

    session = MagicMock()
    session.send_realtime_input = AsyncMock()
    session.send_client_content = AsyncMock()
    session.send_tool_response = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.receive = fake_receive
    return session


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_send_audio_uses_audio_field_not_media():
    """send_audio must use audio= kwarg (media= is deprecated in google-genai >=2.x)."""
    fake_session = make_fake_session()

    with patch.dict("sys.modules", {
        "google": MagicMock(),
        "google.genai": MagicMock(),
        "google.genai.types": MagicMock(),
    }):
        import google.genai as genai_mod
        genai_mod.Client.return_value.aio.live.connect.return_value = fake_session

        adapter = GeminiLiveAdapter("test-key", model=GEMINI_LIVE_3_1_FLASH_PREVIEW)
        await adapter.connect()

        pcm = bytes([0x00, 0x01, 0x02])
        await adapter.send_audio(pcm)

        fake_session.send_realtime_input.assert_called_once()
        call_kwargs = fake_session.send_realtime_input.call_args
        # audio= kwarg must be present; media= must NOT be present
        assert "audio" in call_kwargs.kwargs or (
            len(call_kwargs.args) > 0 and "audio" in str(call_kwargs.args[0])
        ), f"Expected 'audio' kwarg; got: {call_kwargs}"
        assert "media" not in str(call_kwargs), f"Deprecated 'media' field found: {call_kwargs}"

        await adapter.close()


@pytest.mark.mocked
def test_api_version_auto_detect_native_audio():
    """native-audio models → v1alpha; 3.1-flash-live → v1beta."""
    adapter_native = GeminiLiveAdapter(
        "test-key",
        model="gemini-2.5-flash-native-audio-preview-09-2025",
    )
    assert adapter_native._api_version == "v1alpha"

    adapter_new = GeminiLiveAdapter(
        "test-key",
        model=GEMINI_LIVE_3_1_FLASH_PREVIEW,
    )
    assert adapter_new._api_version == "v1beta"


@pytest.mark.mocked
def test_explicit_api_version_overrides_auto_detect():
    adapter = GeminiLiveAdapter(
        "test-key",
        model=GEMINI_LIVE_3_1_FLASH_PREVIEW,
        api_version="v1alpha",
    )
    assert adapter._api_version == "v1alpha"


@pytest.mark.mocked
def test_gemini_live_model_enum_has_3_1_flash():
    assert GeminiLiveModel.FLASH_3_1_LIVE_PREVIEW == "gemini-3.1-flash-live-preview"


@pytest.mark.mocked
def test_module_constant_value():
    assert GEMINI_LIVE_3_1_FLASH_PREVIEW == "gemini-3.1-flash-live-preview"
