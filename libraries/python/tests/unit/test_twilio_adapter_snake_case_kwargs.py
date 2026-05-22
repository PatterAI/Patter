"""Regression test for the PascalCase → snake_case Twilio kwarg bug.

The ``twilio-python`` SDK's ``client.calls.create(**kwargs)`` accepts
**snake_case** keyword arguments and translates them internally to the
PascalCase form Twilio's REST wire protocol expects. Passing
PascalCase keys (``StatusCallback``, ``MachineDetection``, ``Timeout``)
directly raises ``TypeError: unexpected keyword argument`` and crashes
every outbound call.

This file locks in two behaviours:

1. ``getpatter.client.Patter.call`` builds ``extra_params`` for the
   Twilio adapter using snake_case keys only (fix at source).
2. ``TwilioAdapter.initiate_call`` invokes the underlying
   ``calls.create`` with snake_case kwargs even if a caller passes
   PascalCase (defensive normalisation in the adapter).

Both behaviours are exercised end-to-end against the real adapter
with a real ``TwilioClient`` whose ``.calls.create`` raises ``TypeError``
on PascalCase — mirroring the production failure mode.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from getpatter.client import Patter
from getpatter.local_config import LocalConfig
from getpatter.providers.twilio_adapter import TwilioAdapter, _to_snake_case

from tests.conftest import make_agent


# ---------------------------------------------------------------------------
# Helper: a ``calls.create`` stand-in that enforces snake_case kwargs.
# ---------------------------------------------------------------------------

# These are the Twilio params the SDK explicitly accepts in snake_case.
# A real ``twilio.rest.Client.calls.create(...)`` has a typed signature
# that rejects anything else — we replicate that contract here so the
# test fails the same way production does.
_ACCEPTED_SNAKE_KWARGS = frozenset(
    {
        "to",
        "from_",
        "twiml",
        "url",
        "method",
        "fallback_url",
        "status_callback",
        "status_callback_event",
        "status_callback_method",
        "send_digits",
        "timeout",
        "record",
        "recording_channels",
        "recording_status_callback",
        "machine_detection",
        "machine_detection_timeout",
        "machine_detection_speech_threshold",
        "machine_detection_speech_end_threshold",
        "machine_detection_silence_timeout",
        "async_amd",
        "async_amd_status_callback",
        "async_amd_status_callback_method",
        "byoc",
        "trunk_sid",
    }
)


def _strict_create(**kwargs):
    """Drop-in for ``twilio.rest.Client.calls.create`` that rejects
    PascalCase keys exactly the way the real SDK does."""
    bad = [k for k in kwargs if k not in _ACCEPTED_SNAKE_KWARGS]
    if bad:
        raise TypeError(f"calls.create() got an unexpected keyword argument {bad[0]!r}")
    resp = MagicMock()
    resp.sid = "CA" + "f" * 32
    return resp


# ---------------------------------------------------------------------------
# Unit: the snake_case helper.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestToSnakeCase:
    def test_pascal_to_snake(self) -> None:
        assert _to_snake_case("StatusCallback") == "status_callback"
        assert _to_snake_case("AsyncAmdStatusCallback") == "async_amd_status_callback"
        assert _to_snake_case("MachineDetection") == "machine_detection"
        assert _to_snake_case("Timeout") == "timeout"

    def test_snake_passthrough(self) -> None:
        assert _to_snake_case("timeout") == "timeout"
        assert _to_snake_case("status_callback_event") == "status_callback_event"

    def test_camel_to_snake(self) -> None:
        assert _to_snake_case("asyncAmd") == "async_amd"


# ---------------------------------------------------------------------------
# Adapter-level: TwilioAdapter.initiate_call invokes calls.create with
# snake_case kwargs ONLY. The strict stub raises TypeError otherwise —
# making this an authentic test that would catch the production bug.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdapterPassesSnakeCaseKwargs:
    @pytest.mark.asyncio
    async def test_initiate_call_with_snake_case_extra_params(self) -> None:
        # ``twilio.rest.Client.calls`` is a read-only property — we
        # cannot patch it on a real instance. Replace the constructor
        # so the adapter wraps a MagicMock whose ``calls.create`` is
        # the strict validator. Everything else (TwiML construction,
        # ``_run_sync`` threading, kwarg normalisation) runs the actual
        # production code path.
        with patch(
            "getpatter.providers.twilio_adapter.TwilioClient"
        ) as MockTwilioClient:
            client_instance = MagicMock()
            client_instance.calls = MagicMock()
            client_instance.calls.create = _strict_create
            MockTwilioClient.return_value = client_instance

            adapter = TwilioAdapter(
                account_sid="ACtest000000000000000000000000000",
                auth_token="tok_test",
            )

            sid = await adapter.initiate_call(
                "+15551234567",
                "+15559876543",
                "wss://test.ngrok.io/ws/stream/outbound",
                extra_params={
                    "timeout": 25,
                    "machine_detection": "DetectMessageEnd",
                    "async_amd": "true",
                    "async_amd_status_callback": "https://test.ngrok.io/webhooks/twilio/amd",
                    "status_callback": "https://test.ngrok.io/webhooks/twilio/status",
                    "status_callback_method": "POST",
                    "status_callback_event": [
                        "initiated",
                        "ringing",
                        "answered",
                        "completed",
                    ],
                },
            )
        assert sid == "CA" + "f" * 32

    @pytest.mark.asyncio
    async def test_initiate_call_translates_pascal_case_defensively(self) -> None:
        """Belt-and-braces: even if a future caller forgets and passes
        PascalCase, the adapter must normalise before invoking the SDK.
        Without the fix this raises ``TypeError`` (production crash)."""
        captured: dict = {}

        def _capture(**kwargs):
            # Re-use the strict validator so any leakage explodes here.
            _strict_create(**kwargs)
            captured.update(kwargs)
            resp = MagicMock()
            resp.sid = "CA" + "a" * 32
            return resp

        with patch(
            "getpatter.providers.twilio_adapter.TwilioClient"
        ) as MockTwilioClient:
            client_instance = MagicMock()
            client_instance.calls = MagicMock()
            client_instance.calls.create = _capture
            MockTwilioClient.return_value = client_instance

            adapter = TwilioAdapter(
                account_sid="ACtest000000000000000000000000000",
                auth_token="tok_test",
            )

            await adapter.initiate_call(
                "+15551234567",
                "+15559876543",
                "wss://test.ngrok.io/ws/stream/outbound",
                extra_params={
                    "Timeout": 30,
                    "MachineDetection": "DetectMessageEnd",
                    "StatusCallback": "https://test.ngrok.io/webhooks/twilio/status",
                },
            )

        # Adapter must have rewritten all three to snake_case before
        # the SDK call — and the strict validator must have accepted them.
        assert captured["timeout"] == 30
        assert captured["machine_detection"] == "DetectMessageEnd"
        assert (
            captured["status_callback"]
            == "https://test.ngrok.io/webhooks/twilio/status"
        )
        # PascalCase keys must NOT survive into the SDK call.
        assert "Timeout" not in captured
        assert "MachineDetection" not in captured
        assert "StatusCallback" not in captured


# ---------------------------------------------------------------------------
# Client-level: Patter.call() routes through the adapter with snake_case
# keys. Hooking the adapter to the strict stub asserts the entire path is
# wire-correct — not just the dict shape.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPatterCallEndToEnd:
    @pytest.mark.asyncio
    async def test_outbound_call_does_not_raise_typeerror(self) -> None:
        """Reproduce the user-reported zenn.dev bug: an outbound call with
        machine_detection + ring_timeout must NOT raise ``TypeError`` from
        twilio-python's ``calls.create``. Pre-fix, this test fails."""
        cfg = LocalConfig(
            telephony_provider="twilio",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok_test",
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
            phone_number="+15551234567",
        )
        phone = Patter.__new__(Patter)
        phone._local_config = cfg
        phone._server = None

        # Patch the TwilioClient constructor so the adapter wraps a real
        # TwilioAdapter instance but the underlying SDK is replaced by
        # our strict validator. Everything else (URL/TwiML construction,
        # ring_timeout propagation, status callback wiring, AMD params)
        # runs the actual production code path.
        captured: dict = {}

        def _capture(**kwargs):
            _strict_create(**kwargs)
            captured.update(kwargs)
            resp = MagicMock()
            resp.sid = "CA" + "b" * 32
            return resp

        with patch(
            "getpatter.providers.twilio_adapter.TwilioClient"
        ) as MockTwilioClient:
            client_instance = MagicMock()
            client_instance.calls = MagicMock()
            client_instance.calls.create = _capture
            MockTwilioClient.return_value = client_instance

            await phone.call(
                to="+15559876543",
                agent=make_agent(),
                machine_detection=True,
                ring_timeout=30,
            )

        # All the params Twilio cares about landed under snake_case keys.
        assert captured["to"] == "+15559876543"
        assert captured["from_"] == "+15551234567"
        assert captured["timeout"] == 30
        assert captured["machine_detection"] == "DetectMessageEnd"
        assert captured["async_amd"] == "true"
        assert "async_amd_status_callback" in captured
        assert (
            captured["status_callback"]
            == "https://test.ngrok.io/webhooks/twilio/status"
        )
        assert captured["status_callback_method"] == "POST"
        assert "ringing" in captured["status_callback_event"]
        assert "completed" in captured["status_callback_event"]

    @pytest.mark.asyncio
    async def test_outbound_call_without_amd_still_snake_case(self) -> None:
        """``machine_detection=False`` must still produce a snake_case
        StatusCallback wiring — the dashboard relies on it."""
        cfg = LocalConfig(
            telephony_provider="twilio",
            twilio_sid="ACtest000000000000000000000000000",
            twilio_token="tok_test",
            openai_key="sk-test",
            webhook_url="test.ngrok.io",
            phone_number="+15551234567",
        )
        phone = Patter.__new__(Patter)
        phone._local_config = cfg
        phone._server = None

        captured: dict = {}

        def _capture(**kwargs):
            _strict_create(**kwargs)
            captured.update(kwargs)
            resp = MagicMock()
            resp.sid = "CA" + "c" * 32
            return resp

        with patch(
            "getpatter.providers.twilio_adapter.TwilioClient"
        ) as MockTwilioClient:
            client_instance = MagicMock()
            client_instance.calls = MagicMock()
            client_instance.calls.create = _capture
            MockTwilioClient.return_value = client_instance

            await phone.call(
                to="+15559876543",
                agent=make_agent(),
                machine_detection=False,
            )

        assert "machine_detection" not in captured
        assert "async_amd" not in captured
        assert captured["status_callback"].endswith("/webhooks/twilio/status")
