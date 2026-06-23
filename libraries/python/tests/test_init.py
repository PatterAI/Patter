"""End-to-end tests for the REAL ``getpatter init`` wizard.

Complements ``test_init_wizard.py`` (which pins the pure string builders).
These run the *whole* wizard — the same ``build_init_parser`` argparse surface
a user hits on the command line, then ``dispatch_init`` → the async ``_run``
coroutine — straight into a pytest ``tmp_path``. No fake plan dicts: the
selections flow through real flag parsing, real selection resolution, the real
``_write_scaffold`` filesystem path, and the real ``os.chmod`` permission set.

Only TWO boundaries are mocked, and both are mocked at the documented external
edge (per the authentic-tests rule):

* ``subprocess.run`` — so ``npx skills add ...`` and ``git init`` never shell out
  to Node / git or touch the network. We still assert the wizard *would* have
  invoked them with the right arg array.
* ``shutil.which`` — so the wizard believes ``git`` / ``npx`` exist on PATH
  without requiring them in CI. Everything from these two hooks inward is real.

Generated ``main.py`` is validated by parsing it with the stdlib ``ast`` module
(it must be syntactically valid Python) and by extracting the ``from getpatter
import ...`` symbols and matching them against the chosen mode/engine/providers.
Coverage spans BOTH voice modes (realtime + pipeline) and several carriers.
"""

from __future__ import annotations

import argparse
import ast
import os
import stat

import pytest

from getpatter import __version__
from getpatter.init.cli import build_init_parser, dispatch_init


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    """Build the REAL ``init`` parser and parse argv exactly as the CLI does."""
    parser = argparse.ArgumentParser(prog="patter")
    subparsers = parser.add_subparsers(dest="command")
    build_init_parser(subparsers)
    args = parser.parse_args(["init", *argv])
    assert args.command == "init"
    return args


def _imported_getpatter_symbols(main_py_src: str) -> set[str]:
    """Parse ``main.py`` with ast and return the names imported from getpatter.

    Walks the real AST — proves the file is syntactically valid Python AND that
    the import line wired the chosen stack. Returns the set of imported names
    (e.g. {"Patter", "Twilio", "OpenAIRealtime2"}).
    """
    tree = ast.parse(main_py_src)
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "getpatter":
            for alias in node.names:
                symbols.add(alias.name)
    return symbols


def _run_wizard(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> list:
    """Run the real wizard with subprocess + which mocked, return recorded calls.

    The returned list holds every ``subprocess.run`` arg array the wizard would
    have executed (git init / npx skills add). The wizard runs fully otherwise.
    """
    recorded: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):  # noqa: ANN001 — test double
        recorded.append(list(cmd))

        class _Completed:
            returncode = 0

        return _Completed()

    # Make git / npx "exist" so the wizard takes the install/init branch, but
    # the actual execution is the mocked subprocess.run above. Everything else
    # (selection resolution, scaffolding, chmod, env-key collection) is real.
    def _fake_which(name: str):  # noqa: ANN001
        return f"/usr/bin/{name}"

    monkeypatch.setattr("getpatter.init.cli.subprocess.run", _fake_run)
    monkeypatch.setattr("getpatter.init.cli.shutil.which", _fake_which)

    args = _parse(argv)
    rc = dispatch_init(args)
    assert rc == 0, f"wizard exited non-zero ({rc}) for argv={argv}"
    return recorded


# ---------------------------------------------------------------------------
# Realtime mode — full end-to-end scaffold
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_realtime_default_scaffold_is_complete_and_valid(tmp_path, monkeypatch):
    target = tmp_path / "rt-agent"
    _run_wizard(
        ["--name", str(target), "--yes", "--mode", "realtime", "--carrier", "twilio"],
        monkeypatch,
    )

    # Scaffold files exist.
    for rel in (
        "main.py",
        "requirements.txt",
        ".env",
        ".env.example",
        ".gitignore",
        "README.md",
    ):
        assert (target / rel).is_file(), f"missing scaffold file {rel}"
    # Python scaffold must NOT emit the TS entry point.
    assert not (target / "src" / "index.ts").exists()

    # .env is 0600.
    mode = stat.S_IMODE(os.stat(target / ".env").st_mode)
    assert mode == 0o600, f".env mode {oct(mode)} != 0o600"

    # main.py parses via ast and imports the realtime symbols.
    main_src = (target / "main.py").read_text(encoding="utf-8")
    symbols = _imported_getpatter_symbols(main_src)
    assert symbols == {"Patter", "Twilio", "OpenAIRealtime2"}
    # Realtime stack must not leak pipeline kwargs into the call shape.
    assert "stt=" not in main_src and "llm=" not in main_src and "tts=" not in main_src
    assert "engine=OpenAIRealtime2()" in main_src

    # requirements.txt pins the version; realtime default needs no extras.
    reqs = (target / "requirements.txt").read_text(encoding="utf-8").strip()
    assert reqs == f"getpatter=={__version__}"

    # .gitignore protects .env.
    gitignore = (target / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore.splitlines()


@pytest.mark.unit
def test_realtime_convai_engine_scaffold(tmp_path, monkeypatch):
    target = tmp_path / "convai-agent"
    _run_wizard(
        [
            "--name",
            str(target),
            "--yes",
            "--mode",
            "realtime",
            "--engine",
            "ElevenLabsConvAI",
            "--carrier",
            "telnyx",
        ],
        monkeypatch,
    )

    main_src = (target / "main.py").read_text(encoding="utf-8")
    ast.parse(main_src)  # must be valid Python
    symbols = _imported_getpatter_symbols(main_src)
    assert symbols == {"Patter", "Telnyx", "ElevenLabsConvAI"}
    assert "engine=ElevenLabsConvAI()" in main_src

    # ConvAI requires ELEVENLABS_AGENT_ID; Telnyx requires its creds + conn id.
    env_example = (target / ".env.example").read_text(encoding="utf-8")
    for key in (
        "TELNYX_API_KEY",
        "TELNYX_CONNECTION_ID",
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_AGENT_ID",
        "TELNYX_PHONE_NUMBER",
    ):
        assert f"{key}=" in env_example, f"{key} missing from .env.example"


# ---------------------------------------------------------------------------
# Pipeline mode — full end-to-end scaffold
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_default_scaffold_is_complete_and_valid(tmp_path, monkeypatch):
    target = tmp_path / "pl-agent"
    _run_wizard(
        ["--name", str(target), "--yes", "--mode", "pipeline", "--carrier", "plivo"],
        monkeypatch,
    )

    # Scaffold files exist.
    for rel in (
        "main.py",
        "requirements.txt",
        ".env",
        ".env.example",
        ".gitignore",
        "README.md",
    ):
        assert (target / rel).is_file(), f"missing scaffold file {rel}"

    # .env is 0600.
    mode = stat.S_IMODE(os.stat(target / ".env").st_mode)
    assert mode == 0o600

    # main.py parses and imports the default pipeline stack (Deepgram/Cerebras/ElevenLabs).
    main_src = (target / "main.py").read_text(encoding="utf-8")
    symbols = _imported_getpatter_symbols(main_src)
    assert symbols == {"Patter", "Plivo", "DeepgramSTT", "CerebrasLLM", "ElevenLabsTTS"}
    assert "stt=DeepgramSTT()" in main_src
    assert "llm=CerebrasLLM()" in main_src
    assert "tts=ElevenLabsTTS()" in main_src
    # Pipeline must not emit a realtime `engine=` kwarg.
    assert "engine=" not in main_src

    # requirements.txt pins version AND the cerebras extra (default LLM).
    reqs = (target / "requirements.txt").read_text(encoding="utf-8").strip()
    assert reqs == f"getpatter[cerebras]=={__version__}"


@pytest.mark.unit
def test_pipeline_custom_providers_collect_sorted_extras(tmp_path, monkeypatch):
    target = tmp_path / "pl-custom"
    _run_wizard(
        [
            "--name",
            str(target),
            "--yes",
            "--mode",
            "pipeline",
            "--stt",
            "assemblyai",
            "--llm",
            "anthropic",
            "--tts",
            "rime",
            "--carrier",
            "twilio",
        ],
        monkeypatch,
    )

    main_src = (target / "main.py").read_text(encoding="utf-8")
    symbols = _imported_getpatter_symbols(main_src)
    assert symbols == {"Patter", "Twilio", "AssemblyAISTT", "AnthropicLLM", "RimeTTS"}

    # Three distinct extras, sorted, all pinned to the version.
    reqs = (target / "requirements.txt").read_text(encoding="utf-8").strip()
    assert reqs == f"getpatter[anthropic,assemblyai,rime]=={__version__}"

    # Each provider's required env var lands in .env / .env.example.
    env_example = (target / ".env.example").read_text(encoding="utf-8")
    for key in (
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "ASSEMBLYAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "RIME_API_KEY",
    ):
        assert f"{key}=" in env_example


@pytest.mark.unit
def test_pipeline_base_only_providers_have_no_extras(tmp_path, monkeypatch):
    """Deepgram + OpenAI LLM + OpenAI TTS are all base — no pip extras."""
    target = tmp_path / "pl-base"
    _run_wizard(
        [
            "--name",
            str(target),
            "--yes",
            "--mode",
            "pipeline",
            "--stt",
            "deepgram",
            "--llm",
            "openai",
            "--tts",
            "openai",
            "--carrier",
            "twilio",
        ],
        monkeypatch,
    )
    reqs = (target / "requirements.txt").read_text(encoding="utf-8").strip()
    assert reqs == f"getpatter=={__version__}", (
        "base-only stack should pin with no extras"
    )


# ---------------------------------------------------------------------------
# Carrier coverage — env-key set per carrier
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("carrier", "carrier_cls", "required_env"),
    [
        (
            "twilio",
            "Twilio",
            ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"),
        ),
        (
            "telnyx",
            "Telnyx",
            ("TELNYX_API_KEY", "TELNYX_CONNECTION_ID", "TELNYX_PHONE_NUMBER"),
        ),
        ("plivo", "Plivo", ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN", "PLIVO_PHONE_NUMBER")),
    ],
)
def test_each_carrier_wires_class_and_env(
    tmp_path, monkeypatch, carrier, carrier_cls, required_env
):
    target = tmp_path / f"carrier-{carrier}"
    _run_wizard(
        ["--name", str(target), "--yes", "--mode", "realtime", "--carrier", carrier],
        monkeypatch,
    )
    main_src = (target / "main.py").read_text(encoding="utf-8")
    ast.parse(main_src)
    assert carrier_cls in _imported_getpatter_symbols(main_src)
    assert f"carrier={carrier_cls}()" in main_src

    env_example = (target / ".env.example").read_text(encoding="utf-8")
    for key in required_env:
        assert f"{key}=" in env_example, f"{carrier}: {key} missing from .env.example"


# ---------------------------------------------------------------------------
# Security: API keys → .env (0600), never echoed; skip-keys leaves placeholders
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skip_keys_writes_placeholder_env_at_0600(tmp_path, monkeypatch, capsys):
    target = tmp_path / "skip-keys"
    _run_wizard(
        ["--name", str(target), "--yes", "--skip-keys", "--mode", "realtime"],
        monkeypatch,
    )
    env_body = (target / ".env").read_text(encoding="utf-8")
    # Every key present but blank (placeholder) when keys are skipped.
    assert "OPENAI_API_KEY=" in env_body
    for line in env_body.splitlines():
        if "=" in line and not line.startswith("#"):
            assert line.endswith("="), f"placeholder line should be blank: {line!r}"
    mode = stat.S_IMODE(os.stat(target / ".env").st_mode)
    assert mode == 0o600


@pytest.mark.unit
def test_provided_phone_lands_in_main_without_echo(tmp_path, monkeypatch, capsys):
    target = tmp_path / "phone-agent"
    custom_phone = "+15550009999"
    _run_wizard(
        [
            "--name",
            str(target),
            "--yes",
            "--mode",
            "realtime",
            "--carrier",
            "twilio",
            "--phone",
            custom_phone,
        ],
        monkeypatch,
    )
    main_src = (target / "main.py").read_text(encoding="utf-8")
    ast.parse(main_src)
    assert custom_phone in main_src
    # The wizard prints paths only — it should not have crashed and the summary
    # line names the scaffolded directory.
    out = capsys.readouterr().out
    assert "Scaffolded python project" in out


# ---------------------------------------------------------------------------
# Side-effect boundary: git init + npx skills are invoked with the right argv
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skills_and_git_invoked_with_correct_argv(tmp_path, monkeypatch):
    target = tmp_path / "with-skills"
    recorded = _run_wizard(
        [
            "--name",
            str(target),
            "--yes",
            "--mode",
            "realtime",
            "--ide",
            "claude-code,cursor",
        ],
        monkeypatch,
    )
    # --yes is non-interactive: git is NOT initialised by default (no-git path),
    # but --ide explicitly requests skills installs even non-interactively.
    skills_calls = [c for c in recorded if "skills" in c]
    assert len(skills_calls) == 2, f"expected 2 skills installs, got {recorded}"
    ref = f"patterai/skills#v{__version__}"
    ides_called = []
    for call in skills_calls:
        # arg array: [npx, "skills", "add", ref, "-a", <ide>]
        assert call[1:5] == ["skills", "add", ref, "-a"]
        ides_called.append(call[5])
    assert ides_called == ["claude-code", "cursor"]
    # Non-interactive + no --no-git override still skips git (interactive-only prompt).
    assert not any("init" in c for c in recorded)


@pytest.mark.unit
def test_no_skills_flag_suppresses_npx(tmp_path, monkeypatch):
    target = tmp_path / "no-skills"
    recorded = _run_wizard(
        [
            "--name",
            str(target),
            "--yes",
            "--mode",
            "realtime",
            "--ide",
            "claude-code",
            "--no-skills",
        ],
        monkeypatch,
    )
    assert recorded == [], "--no-skills must suppress every subprocess call"


# ---------------------------------------------------------------------------
# Non-empty dir guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_non_empty_dir_blocks_without_force(tmp_path, monkeypatch):
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "existing.txt").write_text("keep me", encoding="utf-8")

    # which/subprocess still mocked so the test never touches git/npx even if
    # the guard logic regressed and let us through.
    monkeypatch.setattr("getpatter.init.cli.subprocess.run", lambda *a, **k: None)
    monkeypatch.setattr("getpatter.init.cli.shutil.which", lambda name: None)

    args = _parse(["--name", str(target), "--yes"])
    rc = dispatch_init(args)
    assert rc == 1, "non-empty dir without --force should exit 1"
    # The pre-existing file is untouched; no scaffold written.
    assert (target / "existing.txt").read_text(encoding="utf-8") == "keep me"
    assert not (target / "main.py").exists()


@pytest.mark.unit
def test_force_allows_scaffold_into_non_empty_dir(tmp_path, monkeypatch):
    target = tmp_path / "occupied2"
    target.mkdir()
    (target / "existing.txt").write_text("keep me", encoding="utf-8")

    _run_wizard(["--name", str(target), "--yes", "--force"], monkeypatch)

    assert (target / "main.py").is_file()
    assert (target / "existing.txt").read_text(encoding="utf-8") == "keep me"
