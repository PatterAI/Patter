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
    for k in ("OPENAI_API_KEY", "ENABLE_LIVE_CALLS", "NOTEBOOK_MAX_COST_USD"):
        monkeypatch.delenv(k, raising=False)
    import _setup
    env = _setup.load(env_file=tmp_path / "nonexistent.env")
    assert env.openai_key == ""
    assert env.enable_live_calls is False


def test_has_key_returns_true_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    assert _setup.has_key(env, "OPENAI_API_KEY") is True


def test_has_key_returns_false_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    assert _setup.has_key(env, "DEEPGRAM_API_KEY") is False


def test_skip_raises_notebook_skip():
    import _setup
    with pytest.raises(_setup.NotebookSkip) as exc:
        _setup.skip("missing key")
    assert "missing key" in str(exc.value)


def test_print_key_matrix_outputs_check_and_circle(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    _setup.print_key_matrix(env, required=["OPENAI_API_KEY", "DEEPGRAM_API_KEY"])
    out = capsys.readouterr().out
    assert "OPENAI_API_KEY" in out and "✅" in out
    assert "DEEPGRAM_API_KEY" in out and "⚪" in out


def test_cell_passes_when_keys_present(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    with _setup.cell("test_cell", tier=3, required=["OPENAI_API_KEY"], env=env) as ok:
        if ok:
            print("body ran")
    out = capsys.readouterr().out
    assert "body ran" in out


def test_cell_skips_on_missing_key(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    with _setup.cell("test_cell", tier=3, required=["OPENAI_API_KEY"], env=env) as ok:
        if ok:
            print("body ran")
    out = capsys.readouterr().out
    assert "body ran" not in out
    assert "OPENAI_API_KEY" in out
    assert "skipped" in out.lower()


def test_cell_skips_on_tier_4_when_live_disabled(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ENABLE_LIVE_CALLS", "0")
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    with _setup.cell("live_cell", tier=4, required=[], env=env) as ok:
        if ok:
            print("body ran")
    out = capsys.readouterr().out
    assert "body ran" not in out
    assert "ENABLE_LIVE_CALLS" in out


def test_cell_renders_banner_on_exception(tmp_path, capsys):
    import _setup
    env = _setup.load(env_file=tmp_path / "missing.env")
    with _setup.cell("test_cell", tier=1, required=[], env=env) as ok:
        if ok:
            raise RuntimeError("kaboom")
    out = capsys.readouterr().out
    assert "kaboom" in out
    assert "test_cell" in out


def test_load_fixture_returns_bytes():
    import _setup
    data = _setup.load_fixture("audio/hello_world_16khz_pcm.wav")
    assert isinstance(data, bytes)
    assert len(data) > 100


def test_load_fixture_unknown_path_raises():
    import _setup
    with pytest.raises(FileNotFoundError):
        _setup.load_fixture("audio/nonexistent.wav")


def test_assert_redacted_blocks_real_phone(tmp_path):
    import _setup
    bad = tmp_path / "bad.json"
    bad.write_text('{"From": "+14155551234"}')
    with pytest.raises(ValueError, match="phone"):
        _setup._assert_redacted(bad.read_text(), str(bad))


def test_assert_redacted_passes_placeholder(tmp_path):
    import _setup
    ok = tmp_path / "ok.json"
    ok.write_text('{"From": "+15555550100"}')
    _setup._assert_redacted(ok.read_text(), str(ok))


@pytest.mark.asyncio
async def test_run_stt_aggregates_transcripts():
    import _setup

    class FakeSTT:
        async def connect(self): pass
        async def send_audio(self, chunk): pass
        async def close(self): pass
        async def receive_transcripts(self):
            yield "hello "
            yield "world"

    transcript = await _setup.run_stt(FakeSTT(), b"\x00" * 16000)
    assert transcript.strip() == "hello world"


@pytest.mark.asyncio
async def test_run_tts_concatenates_chunks():
    import _setup

    class FakeTTS:
        async def synthesize(self, text):
            yield b"\x01\x02"
            yield b"\x03\x04"

    audio = await _setup.run_tts(FakeTTS(), "hi")
    assert audio == b"\x01\x02\x03\x04"
