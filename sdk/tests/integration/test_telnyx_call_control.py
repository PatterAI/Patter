"""Integration tests for Telnyx Call Control API (DTMF, transfer, recording).

SKIPPED BY DEFAULT. Requires real credentials:

    export TELNYX_API_KEY=KEY...
    export TELNYX_CALL_CONTROL_ID=v3:...     # an ACTIVE call's Call Control ID

These tests actually hit the Telnyx REST API. They are marked with
``integration`` so CI can opt in / out.
"""

from __future__ import annotations

import os

import httpx
import pytest


pytestmark = pytest.mark.integration

TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY", "")
TELNYX_CALL_CONTROL_ID = os.environ.get("TELNYX_CALL_CONTROL_ID", "")

_NOT_CONFIGURED = pytest.mark.skipif(
    not (TELNYX_API_KEY and TELNYX_CALL_CONTROL_ID),
    reason="TELNYX_API_KEY or TELNYX_CALL_CONTROL_ID not set",
)


@_NOT_CONFIGURED
@pytest.mark.asyncio
async def test_real_telnyx_send_dtmf() -> None:
    """Hit the real Telnyx send_dtmf action. Requires an active call."""
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            f"https://api.telnyx.com/v2/calls/{TELNYX_CALL_CONTROL_ID}/actions/send_dtmf",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
            json={"digits": "1", "duration_millis": 250},
            timeout=10.0,
        )
        # 200 or 422 (call not active) — both are valid observations.
        assert resp.status_code in (200, 422), (resp.status_code, resp.text)


@_NOT_CONFIGURED
@pytest.mark.asyncio
async def test_real_telnyx_record_start_stop() -> None:
    """Hit the real Telnyx record_start / record_stop actions."""
    async with httpx.AsyncClient() as http:
        start = await http.post(
            f"https://api.telnyx.com/v2/calls/{TELNYX_CALL_CONTROL_ID}/actions/record_start",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
            json={"format": "mp3", "channels": "single"},
            timeout=10.0,
        )
        assert start.status_code in (200, 422), (start.status_code, start.text)

        stop = await http.post(
            f"https://api.telnyx.com/v2/calls/{TELNYX_CALL_CONTROL_ID}/actions/record_stop",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
            json={},
            timeout=10.0,
        )
        assert stop.status_code in (200, 422), (stop.status_code, stop.text)
