"""Authentic tests for the ``config_incomplete`` activation-blocker signal (F1).

The real ``Patter`` client runs its real credential/engine validation. The only
substituted surface is the telemetry client's outbound boundary: a capturing
double records every ``record(name, **dims)`` call into a list (mirroring the
allowed mock surface in ``.claude/rules/authentic-tests.md`` — the telemetry
client is the last hop to the network). Everything from ``Patter.__init__`` /
``Patter.agent`` inward — the ``_record_config_incomplete`` dedupe, the
validation, and the ``raise`` — is exercised for real.

Each test asserts that the blocking validation BOTH emits exactly one
``config_incomplete`` event with the correct coarse ``missing`` enum AND still
raises the original error unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from getpatter import DeepgramSTT, Patter, Twilio


class _CaptureTelemetry:
    """Stands in for ``TelemetryClient`` at the outbound boundary only.

    Reports ``enabled`` truthfully (so the enabled-vs-opt-out behaviour is real)
    and appends each recorded event to ``calls`` instead of POSTing it. The
    ``record`` signature matches the real client: ``record(name, **dimensions)``.
    """

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def record(self, name: str, **dimensions: Any) -> None:
        # Mirror the real client: a disabled client drops every event.
        if not self.enabled:
            return
        self.calls.append((name, dict(dimensions)))

    # No-op lifecycle hooks the client exercises elsewhere — never relevant here.
    def flush_pending(self) -> None:  # pragma: no cover - not hit by these tests
        pass

    async def drain(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass

    async def aclose(self) -> None:  # pragma: no cover
        pass


def _config_incomplete(cap: _CaptureTelemetry) -> list[dict[str, Any]]:
    return [dims for name, dims in cap.calls if name == "config_incomplete"]


@pytest.fixture
def install_capture(monkeypatch):
    """Make ``Patter`` build an enabled capturing telemetry client.

    Returns the most-recently-built capture instance via a one-element holder so a
    test can inspect what the real validation path recorded.
    """
    holder: dict[str, _CaptureTelemetry] = {}

    def _build(flag: bool | None) -> _CaptureTelemetry:
        # Honour the opt-out flag exactly like the real client: telemetry=False
        # yields a disabled client. (Env-based opt-out is covered separately.)
        cap = _CaptureTelemetry(enabled=flag is not False)
        holder["client"] = cap
        return cap

    monkeypatch.setattr("getpatter.client._build_telemetry_client", _build)
    return holder


# --- (1) missing carrier credentials (constructor) --------------------------


def test_missing_phone_number_emits_carrier_credentials_and_still_raises(
    install_capture,
):
    with pytest.raises(ValueError, match="phone_number"):
        Patter(carrier=Twilio(account_sid="AC", auth_token="tk"), webhook_url="x")

    cap = install_capture["client"]
    events = _config_incomplete(cap)
    assert len(events) == 1
    assert events[0] == {"missing": "carrier_credentials"}


# --- (2) missing LLM / engine key (agent) -----------------------------------


def test_missing_openai_key_emits_llm_key_and_still_raises(
    install_capture, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    phone = Patter(
        carrier=Twilio(account_sid="AC", auth_token="tk"),
        phone_number="+15550000000",
    )
    cap = install_capture["client"]
    # Construction itself must not have emitted the activation-blocker signal.
    assert _config_incomplete(cap) == []

    # Default provider is openai_realtime → requires a key we have not supplied.
    with pytest.raises(
        ValueError, match="OpenAI Realtime mode requires an OpenAI API key"
    ):
        phone.agent("You are a helpful assistant.")

    events = _config_incomplete(cap)
    assert len(events) == 1
    assert events[0] == {"missing": "llm_key"}


# --- (3) incomplete pipeline / engine config (agent) ------------------------


def test_pipeline_without_stt_emits_engine_config_and_still_raises(install_capture):
    phone = Patter(
        carrier=Twilio(account_sid="AC", auth_token="tk"),
        phone_number="+15550000000",
    )
    cap = install_capture["client"]

    with pytest.raises(ValueError, match="Pipeline mode requires an STT provider"):
        phone.agent("You are a helpful assistant.", provider="pipeline")

    events = _config_incomplete(cap)
    assert len(events) == 1
    assert events[0] == {"missing": "engine_config"}


# --- opt-out emits nothing ---------------------------------------------------


def test_opt_out_flag_emits_no_config_incomplete(install_capture):
    with pytest.raises(ValueError, match="phone_number"):
        Patter(
            carrier=Twilio(account_sid="AC", auth_token="tk"),
            webhook_url="x",
            telemetry=False,
        )

    cap = install_capture["client"]
    assert cap.enabled is False
    # A disabled real client drops record() — the capture double mirrors that by
    # being constructed disabled; the activation path still ran but emitted nothing
    # observable on the wire. The dedupe flag flips, but no event leaves.
    assert _config_incomplete(cap) == []


def test_opt_out_env_var_emits_no_config_incomplete(monkeypatch):
    """With the real ``TelemetryClient`` and PATTER_TELEMETRY_DISABLED=1, the
    activation-blocker validation must not enqueue any event."""
    monkeypatch.setenv("PATTER_TELEMETRY_DISABLED", "1")
    monkeypatch.delenv("PATTER_TELEMETRY_DEBUG", raising=False)

    with pytest.raises(ValueError, match="phone_number"):
        phone = Patter(
            carrier=Twilio(account_sid="AC", auth_token="tk"), webhook_url="x"
        )
        del phone  # pragma: no cover - never reached, the constructor raises

    # The real client is disabled by the env var; nothing buffered. We assert via
    # the public ``enabled`` flag on a separately-built client under the same env.
    from getpatter.client import _build_telemetry_client

    real = _build_telemetry_client(None)
    assert real.enabled is False


# --- emitted at most once per instance --------------------------------------


def test_config_incomplete_emitted_at_most_once_per_instance(
    install_capture, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    phone = Patter(
        carrier=Twilio(account_sid="AC", auth_token="tk"),
        phone_number="+15550000000",
    )
    cap = install_capture["client"]

    # First failing agent() call emits once.
    with pytest.raises(ValueError):
        phone.agent("prompt one")
    # A retry / a second agent() call on the SAME instance must NOT double-emit.
    with pytest.raises(ValueError):
        phone.agent("prompt two")
    with pytest.raises(ValueError):
        phone.agent("prompt three", provider="pipeline")

    events = _config_incomplete(cap)
    assert len(events) == 1
    assert events[0] == {"missing": "llm_key"}


# --- a fully-valid agent never emits the activation-blocker signal -----------


def test_valid_realtime_agent_emits_no_config_incomplete(install_capture, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    phone = Patter(
        carrier=Twilio(account_sid="AC", auth_token="tk"),
        phone_number="+15550000000",
    )
    cap = install_capture["client"]
    phone.agent("You are a helpful assistant.")
    assert _config_incomplete(cap) == []


def test_valid_pipeline_agent_with_stt_emits_no_config_incomplete(install_capture):
    phone = Patter(
        carrier=Twilio(account_sid="AC", auth_token="tk"),
        phone_number="+15550000000",
    )
    cap = install_capture["client"]
    phone.agent(
        "You are a helpful assistant.",
        provider="pipeline",
        stt=DeepgramSTT(api_key="dg-test"),
    )
    assert _config_incomplete(cap) == []
