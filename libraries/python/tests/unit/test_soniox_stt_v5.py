"""Unit tests for Soniox v5 real-time STT adoption.

Mocks ``aiohttp.ClientSession`` so the test can assert the model string and the
new opt-in v5 endpoint controls land in the initial Soniox config message sent
over the WebSocket — without any network I/O.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from getpatter.providers.soniox_stt import SonioxModel, SonioxSTT


class _FakeWebSocket:
    """Captures ``send_str`` payloads (the Soniox config message)."""

    def __init__(self) -> None:
        self.closed = False
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []

    async def send_str(self, payload: str) -> None:
        self.sent_text.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.sent_bytes.append(payload)

    async def close(self) -> None:
        self.closed = True


class _FakeSession:
    """Stand-in for ``aiohttp.ClientSession`` returning a fixed fake ws."""

    def __init__(self, ws: _FakeWebSocket) -> None:
        self._ws = ws
        self.closed = False

    async def ws_connect(self, *_a: Any, **_kw: Any) -> _FakeWebSocket:
        return self._ws

    async def close(self) -> None:
        self.closed = True


async def _connect_and_capture(stt: SonioxSTT) -> dict[str, Any]:
    """Connect with a mocked ClientSession; return the parsed config payload."""
    fake_ws = _FakeWebSocket()
    fake_session = _FakeSession(fake_ws)
    with patch(
        "getpatter.providers.soniox_stt.aiohttp.ClientSession",
        return_value=fake_session,
    ):
        await stt.connect()
        try:
            assert fake_ws.sent_text, "no config frame sent over the WebSocket"
            return json.loads(fake_ws.sent_text[0])
        finally:
            await stt.close()


@pytest.mark.unit
def test_v5_enum_value_and_older_models_reachable() -> None:
    assert SonioxModel.STT_RT_V5 == "stt-rt-v5"
    # Older models remain reachable for back-compat.
    assert SonioxModel.STT_RT_V4 == "stt-rt-v4"
    assert SonioxModel.STT_RT_V3 == "stt-rt-v3"
    assert SonioxModel.STT_RT_V2 == "stt-rt-v2"


@pytest.mark.unit
def test_default_model_is_v5() -> None:
    assert SonioxSTT(api_key="sx_test").model == SonioxModel.STT_RT_V5
    assert SonioxSTT.for_twilio(api_key="sx_test").model == SonioxModel.STT_RT_V5


@pytest.mark.unit
async def test_default_config_sends_v5_model() -> None:
    config = await _connect_and_capture(SonioxSTT(api_key="sx_test"))
    assert config["model"] == "stt-rt-v5"


@pytest.mark.unit
async def test_explicit_older_model_is_honored() -> None:
    config = await _connect_and_capture(
        SonioxSTT(api_key="sx_test", model=SonioxModel.STT_RT_V4)
    )
    assert config["model"] == "stt-rt-v4"


@pytest.mark.unit
async def test_v5_endpoint_controls_are_opt_in() -> None:
    # Unset by default -> omitted (config stays byte-compatible with pre-v5).
    base = await _connect_and_capture(SonioxSTT(api_key="sx_test"))
    assert "endpoint_sensitivity" not in base
    assert "endpoint_latency_adjustment_level" not in base

    tuned = await _connect_and_capture(
        SonioxSTT(
            api_key="sx_test",
            endpoint_sensitivity=0.5,
            endpoint_latency_adjustment_level=2,
        )
    )
    assert tuned["endpoint_sensitivity"] == 0.5
    assert tuned["endpoint_latency_adjustment_level"] == 2


@pytest.mark.unit
def test_endpoint_control_validation() -> None:
    with pytest.raises(ValueError, match="endpoint_sensitivity"):
        SonioxSTT(api_key="sx_test", endpoint_sensitivity=1.5)
    with pytest.raises(ValueError, match="endpoint_latency_adjustment_level"):
        SonioxSTT(api_key="sx_test", endpoint_latency_adjustment_level=4)
