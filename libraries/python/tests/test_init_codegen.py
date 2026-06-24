"""Unit tests for the ``getpatter init`` setup wizard.

These exercise the REAL scaffold builders and the real write-to-disk flow —
no mocking. The wizard's codegen is parity-critical (the TypeScript port must
emit byte-identical files), so the assertions pin the exact import lines,
agent-call shapes, env-key ordering, pip-extra resolution, and the security
invariants (``.env`` mode 0600, secrets never appear in printed output).
"""

from __future__ import annotations

import argparse
import stat

import pytest

from getpatter import __version__
from getpatter.init.cli import (
    InitError,
    build_env,
    build_index_ts,
    build_main_py,
    build_package_json,
    build_readme,
    build_requirements_txt,
    _collect_env_keys,
    _collect_extras,
    _npm_package_name,
    _validate_name,
    dispatch_init,
)


def _plan(**overrides) -> dict:
    base = {
        "name": "my-patter-agent",
        "runtime": "python",
        "mode": "realtime",
        "engine": "OpenAIRealtime2",
        "stt": None,
        "llm": None,
        "tts": None,
        "carrier": "twilio",
        "phone": "+15550001234",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_realtime_main_py_imports_default_engine_and_carrier():
    code = build_main_py(_plan())
    assert "from getpatter import Patter, Twilio, OpenAIRealtime2" in code
    assert "engine=OpenAIRealtime2()," in code
    assert "carrier=Twilio()," in code
    assert "await phone.serve(agent, tunnel=True)" in code
    # Realtime mode must NOT emit pipeline kwargs.
    assert "stt=" not in code
    assert "llm=" not in code


@pytest.mark.unit
def test_pipeline_main_py_imports_three_providers_in_order():
    plan = _plan(
        mode="pipeline",
        engine=None,
        stt="deepgram",
        llm="cerebras",
        tts="elevenlabs",
    )
    code = build_main_py(plan)
    assert (
        "from getpatter import Patter, Twilio, DeepgramSTT, CerebrasLLM, ElevenLabsTTS"
        in code
    )
    assert "stt=DeepgramSTT()," in code
    assert "llm=CerebrasLLM()," in code
    assert "tts=ElevenLabsTTS()," in code
    assert "engine=" not in code


@pytest.mark.unit
def test_index_ts_uses_camelcase_and_new_keyword():
    plan = _plan(runtime="typescript")
    code = build_index_ts(plan)
    assert 'import { Patter, Twilio, OpenAIRealtime2 } from "getpatter";' in code
    assert "carrier: new Twilio()," in code
    assert "engine: new OpenAIRealtime2()," in code
    assert "phoneNumber: PHONE_NUMBER," in code
    assert "await phone.serve({ agent, tunnel: true });" in code


@pytest.mark.unit
def test_env_keys_carrier_first_then_providers_then_phone_env():
    plan = _plan(
        mode="pipeline",
        engine=None,
        stt="assemblyai",
        llm="anthropic",
        tts="cartesia",
        carrier="telnyx",
    )
    keys = _collect_env_keys(plan)
    assert keys == [
        "TELNYX_API_KEY",
        "TELNYX_CONNECTION_ID",
        "ASSEMBLYAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CARTESIA_API_KEY",
        "TELNYX_PHONE_NUMBER",
    ]


@pytest.mark.unit
def test_requirements_collects_sorted_pip_extras():
    plan = _plan(
        mode="pipeline",
        engine=None,
        stt="assemblyai",
        llm="anthropic",
        tts="cartesia",
    )
    assert _collect_extras(plan) == ["anthropic", "assemblyai", "cartesia"]
    req = build_requirements_txt(plan)
    assert req == f"getpatter[anthropic,assemblyai,cartesia]=={__version__}\n"


@pytest.mark.unit
def test_realtime_default_requirements_have_no_extras():
    assert build_requirements_txt(_plan()) == f"getpatter=={__version__}\n"


@pytest.mark.unit
def test_inworld_tts_pins_the_inworld_pip_extra():
    """InworldTTS imports aiohttp at runtime, declared in pyproject as the
    ``inworld`` extra. Selecting it must pin ``getpatter[inworld]`` so the
    install actually pulls aiohttp — otherwise the agent ImportErrors at
    call construction."""
    plan = _plan(
        mode="pipeline",
        engine=None,
        stt="deepgram",
        llm="openai",
        tts="inworld",
    )
    assert _collect_extras(plan) == ["inworld"]
    req = build_requirements_txt(plan)
    assert req == f"getpatter[inworld]=={__version__}\n"


@pytest.mark.unit
def test_env_body_keeps_provided_values_and_blanks_the_rest():
    plan = _plan()
    body = build_env(plan, with_values={"OPENAI_API_KEY": "sekret"})
    assert "OPENAI_API_KEY=sekret" in body
    assert "TWILIO_ACCOUNT_SID=" in body  # left blank
    assert "TWILIO_PHONE_NUMBER=" in body


@pytest.mark.unit
def test_readme_python_runtime_uses_python_tunnel_syntax():
    readme = build_readme(_plan(runtime="python"))
    assert "(`tunnel=True`)" in readme
    assert "tunnel: true" not in readme


@pytest.mark.unit
def test_readme_typescript_runtime_uses_ts_tunnel_syntax():
    """A TS scaffold's index.ts uses ``tunnel: true`` — the README prose must
    match that runtime, not leak Python keyword-argument syntax."""
    readme = build_readme(_plan(runtime="typescript"))
    assert "(`tunnel: true`)" in readme
    assert "tunnel=True" not in readme


@pytest.mark.unit
def test_non_interactive_scaffold_writes_files_with_0600_env(tmp_path, capsys):
    target = tmp_path / "agent"
    args = argparse.Namespace(
        name=str(target),
        runtime="python",
        mode="realtime",
        engine=None,
        stt=None,
        llm=None,
        tts=None,
        carrier=None,
        phone=None,
        skip_keys=True,
        ide=None,
        no_skills=True,
        no_git=True,
        force=False,
        yes=True,
    )
    rc = dispatch_init(args)
    assert rc == 0

    assert (target / "main.py").is_file()
    assert (target / "requirements.txt").is_file()
    assert (target / ".env").is_file()
    assert (target / ".env.example").is_file()
    assert (target / ".gitignore").is_file()
    assert (target / "README.md").is_file()

    # SECURITY: .env must be owner-only (0600).
    mode = stat.S_IMODE((target / ".env").stat().st_mode)
    assert mode == 0o600

    # .gitignore must keep .env out of version control.
    gi = (target / ".gitignore").read_text()
    assert ".env" in gi
    assert "!.env.example" in gi


@pytest.mark.unit
def test_non_empty_dir_guard_blocks_without_force(tmp_path):
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "existing.txt").write_text("keep me")
    args = argparse.Namespace(
        name=str(target),
        runtime="python",
        mode="realtime",
        engine=None,
        stt=None,
        llm=None,
        tts=None,
        carrier=None,
        phone=None,
        skip_keys=True,
        ide=None,
        no_skills=True,
        no_git=True,
        force=False,
        yes=True,
    )
    assert dispatch_init(args) == 1
    # Original file untouched, no scaffold written.
    assert (target / "existing.txt").read_text() == "keep me"
    assert not (target / "main.py").exists()


# ---------------------------------------------------------------------------
# Security regression tests — project-name sanitization (issue 3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_name_rejects_double_quote_injection():
    """A name with a double-quote would corrupt the generated package.json.
    Validation must reject it before any path resolution or codegen."""
    with pytest.raises(InitError):
        _validate_name('evil"proj')


@pytest.mark.unit
def test_validate_name_rejects_path_traversal():
    with pytest.raises(InitError):
        _validate_name("../../etc/passwd")


@pytest.mark.unit
def test_validate_name_rejects_shell_metacharacters():
    for bad in ("foo;rm -rf", "foo$(whoami)", "foo`id`", "foo bar"):
        with pytest.raises(InitError):
            _validate_name(bad)


@pytest.mark.unit
def test_validate_name_accepts_safe_label_and_path_prefix():
    # A path with a safe final segment is fine — only the label is validated.
    _validate_name("my-agent")
    _validate_name("/tmp/some/dir/my_agent.v2")


@pytest.mark.unit
def test_npm_package_name_strips_unsafe_chars_and_lowercases():
    assert _npm_package_name("My Cool Agent") == "my-cool-agent"
    # Defensive: even if an unsafe char slips through, the npm name is clean.
    assert '"' not in _npm_package_name('evil"proj')
    assert _npm_package_name("a" * 300) == "a" * 214


@pytest.mark.unit
def test_package_json_is_valid_json_for_a_safe_label():
    """package.json must always parse — the name is JSON-encoded, never raw."""
    import json

    plan = _plan(runtime="typescript")
    plan["name"] = "my-cool-agent"
    parsed = json.loads(build_package_json(plan))
    assert parsed["name"] == "my-cool-agent"
    assert parsed["dependencies"]["getpatter"] == __version__


@pytest.mark.unit
def test_dispatch_init_returns_2_on_invalid_name(tmp_path, capsys):
    """An invalid --name must fail fast with exit code 2 and a clear message,
    not a traceback, and must NOT write any scaffold."""
    args = argparse.Namespace(
        name=str(tmp_path / 'bad"name'),
        runtime="python",
        mode="realtime",
        engine=None,
        stt=None,
        llm=None,
        tts=None,
        carrier=None,
        phone=None,
        skip_keys=True,
        ide=None,
        no_skills=True,
        no_git=True,
        force=False,
        yes=True,
    )
    assert dispatch_init(args) == 2
    err = capsys.readouterr().err
    assert "error:" in err


# ---------------------------------------------------------------------------
# Security regression test — atomic 0600 .env (issue 2, no chmod race)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_created_with_0600_never_world_readable(tmp_path, monkeypatch):
    """The .env is created via os.open with the mode set from the start, so it
    is never group-/world-readable — even momentarily. We assert the chmod path
    is gone by checking os.chmod is never called on the .env path."""
    import getpatter.init.cli as init_cli

    real_chmod = init_cli.os.chmod
    chmodded_paths: list[str] = []

    def _spy_chmod(path, mode, *a, **k):  # pragma: no cover - simple spy
        chmodded_paths.append(str(path))
        return real_chmod(path, mode, *a, **k)

    monkeypatch.setattr(init_cli.os, "chmod", _spy_chmod)

    target = tmp_path / "agent"
    args = argparse.Namespace(
        name=str(target),
        runtime="python",
        mode="realtime",
        engine=None,
        stt=None,
        llm=None,
        tts=None,
        carrier=None,
        phone=None,
        skip_keys=True,
        ide=None,
        no_skills=True,
        no_git=True,
        force=False,
        yes=True,
    )
    assert dispatch_init(args) == 0
    mode = stat.S_IMODE((target / ".env").stat().st_mode)
    assert mode == 0o600
    # No path-based chmod on .env — the mode is set at creation (fchmod on fd).
    assert not any(p.endswith(".env") for p in chmodded_paths)
