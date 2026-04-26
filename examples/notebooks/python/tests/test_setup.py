"""TDD pair for examples/notebooks/python/_setup.py."""

from __future__ import annotations

import dataclasses

import pytest


def test_notebook_env_is_frozen_dataclass():
    import _setup

    env = _setup.NotebookEnv(
        openai_key="", anthropic_key="", google_key="", groq_key="",
        cerebras_key="", deepgram_key="", assemblyai_key="", soniox_key="",
        speechmatics_key="", cartesia_key="", elevenlabs_key="",
        elevenlabs_voice_id="", elevenlabs_agent_id="",
        lmnt_key="", rime_key="", ultravox_key="",
        twilio_sid="", twilio_token="", twilio_number="",
        telnyx_key="", telnyx_connection_id="", telnyx_number="", telnyx_public_key="",
        target_number="", ngrok_token="", public_webhook_url="",
        patter_version="0.5.2",
        enable_live_calls=False,
        max_call_seconds=90,
        max_cost_usd=0.25,
    )
    assert dataclasses.is_dataclass(env)
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.openai_key = "x"  # type: ignore[misc]


def test_load_reads_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-proj-xxx\n"
        "ENABLE_LIVE_CALLS=1\n"
        "NOTEBOOK_MAX_COST_USD=1.5\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ENABLE_LIVE_CALLS", raising=False)
    monkeypatch.delenv("NOTEBOOK_MAX_COST_USD", raising=False)

    import _setup
    env = _setup.load(env_file=env_file)

    assert env.openai_key == "sk-proj-xxx"
    assert env.enable_live_calls is True
    assert env.max_cost_usd == 1.5


def test_load_returns_empty_strings_for_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import _setup
    env = _setup.load(env_file=tmp_path / "nonexistent.env")
    assert env.openai_key == ""
    assert env.enable_live_calls is False
