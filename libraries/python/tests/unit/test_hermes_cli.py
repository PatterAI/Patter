"""Unit tests for the ``patter hermes ...`` CLI (doctor / setup / attach-number).

Live probes are exercised by monkeypatching ``httpx`` at the boundary, so no
real Hermes gateway or Twilio account is touched.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from getpatter import _hermes_scaffold, cli_hermes


# ── scaffold ───────────────────────────────────────────────────────────────
def test_scaffold_writes_all_files(tmp_path: Path) -> None:
    written = _hermes_scaffold.scaffold(tmp_path)
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert rel == set(_hermes_scaffold.FILES)
    for name in _hermes_scaffold.FILES:
        assert (tmp_path / name).exists()


def test_scaffold_skips_existing_without_force(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("# mine", encoding="utf-8")
    written = _hermes_scaffold.scaffold(tmp_path)
    assert tmp_path / "app.py" not in written
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "# mine"
    # force overwrites.
    written2 = _hermes_scaffold.scaffold(tmp_path, force=True)
    assert tmp_path / "app.py" in written2
    assert (tmp_path / "app.py").read_text(encoding="utf-8") != "# mine"


def test_committed_example_matches_scaffold() -> None:
    """The committed examples/ tree must stay in sync with the scaffold map."""
    root = Path(__file__).resolve().parents[4] / "examples" / "hermes-phone-agent"
    assert root.is_dir(), f"missing example dir: {root}"
    for rel, content in _hermes_scaffold.FILES.items():
        on_disk = (root / rel).read_text(encoding="utf-8")
        assert on_disk == content, f"{rel} drifted from the scaffold"


# ── doctor ─────────────────────────────────────────────────────────────────
def _doctor_args(**over) -> argparse.Namespace:
    base = {"base_url": None, "no_network": True, "json": True}
    base.update(over)
    return argparse.Namespace(**base)


def test_doctor_no_network_skips_probes(capsys, monkeypatch) -> None:
    for var in ("API_SERVER_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    rc = cli_hermes.cmd_doctor(_doctor_args())
    out = capsys.readouterr().out
    assert '"skipped (--no-network)"' in out
    # warnings don't fail the run.
    assert rc == 0


def test_doctor_gateway_unreachable_is_failure(monkeypatch) -> None:
    # Force the gateway probe to look like a refused connection.
    def boom(*_a, **_k):
        raise OSError("Connection refused")

    monkeypatch.setattr("httpx.get", boom)
    sections = cli_hermes._check_hermes("http://127.0.0.1:8642/v1", network=True)
    statuses = {c.label: c.status for c in sections.checks}
    assert statuses["Gateway unreachable"] == cli_hermes.FAIL


def test_doctor_gateway_ok_and_model_present(monkeypatch) -> None:
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "hermes-agent"}]}

    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setattr("httpx.get", lambda *a, **k: Resp())
    sec = cli_hermes._check_hermes("http://127.0.0.1:8642/v1", network=True)
    labels = {c.label: c.status for c in sec.checks}
    assert labels["Gateway reachable"] == cli_hermes.OK
    assert labels["Model available"] == cli_hermes.OK


def test_doctor_exit_code_one_when_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_hermes,
        "_run_doctor",
        lambda _a: [cli_hermes.Section("X", [cli_hermes.Check(cli_hermes.FAIL, "bad")])],
    )
    assert cli_hermes.cmd_doctor(_doctor_args(json=False)) == 1


# ── attach-number ──────────────────────────────────────────────────────────
def test_attach_number_requires_https(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    rc = cli_hermes._attach_number("+15551234567", "http://x/y", None)
    assert rc == 2
    assert "https" in capsys.readouterr().err


def test_attach_number_missing_creds(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    rc = cli_hermes._attach_number("+15551234567", "https://x/y", None)
    assert rc == 2
    assert "credentials not found" in capsys.readouterr().err.lower()


def test_attach_number_posts_voice_url(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    posted: dict = {}

    class Lookup:
        status_code = 200

        @staticmethod
        def json():
            return {"incoming_phone_numbers": [{"sid": "PN1"}]}

    class Update:
        status_code = 200
        text = ""

    def fake_get(url, **kw):
        assert "IncomingPhoneNumbers.json" in url
        return Lookup()

    def fake_post(url, **kw):
        posted["url"] = url
        posted["data"] = kw.get("data")
        return Update()

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr("httpx.post", fake_post)
    rc = cli_hermes._attach_number(
        "+15551234567", "https://abc.example.com/calls/inbound", None
    )
    assert rc == 0
    assert "PN1.json" in posted["url"]
    assert posted["data"]["VoiceUrl"] == "https://abc.example.com/calls/inbound"
    assert posted["data"]["VoiceMethod"] == "POST"
    assert "voice webhook" in capsys.readouterr().out


def test_attach_number_unknown_number(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")

    class Lookup:
        status_code = 200

        @staticmethod
        def json():
            return {"incoming_phone_numbers": []}

    monkeypatch.setattr("httpx.get", lambda *a, **k: Lookup())
    rc = cli_hermes._attach_number("+15550000000", "https://x/y", None)
    assert rc == 1
    assert "not on this account" in capsys.readouterr().err


# ── dispatch ───────────────────────────────────────────────────────────────
def test_dispatch_unknown_subcommand_returns_usage() -> None:
    args = argparse.Namespace(hermes_command=None)
    assert cli_hermes.dispatch_hermes(args) == 2


# ── env / config helpers ────────────────────────────────────────────────────
def test_parse_env_file_handles_quotes_export_comments(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n"
        "\n"
        "export API_SERVER_KEY='secret'\n"
        'PATTER_LANGUAGE="it"\n'
        "BARE=value\n"
        "NOEQUALS\n",
        encoding="utf-8",
    )
    parsed = cli_hermes._parse_env_file(p)
    assert parsed == {
        "API_SERVER_KEY": "secret",
        "PATTER_LANGUAGE": "it",
        "BARE": "value",
    }


def test_parse_env_file_missing_returns_empty(tmp_path: Path) -> None:
    assert cli_hermes._parse_env_file(tmp_path / "nope.env") == {}


def test_upsert_env_file_replaces_and_appends(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("# header\nAPI_SERVER_PORT=8642\nKEEP=me\n", encoding="utf-8")
    cli_hermes._upsert_env_file(p, {"API_SERVER_PORT": "9000", "NEW": "x"})
    text = p.read_text(encoding="utf-8")
    assert "# header" in text  # comments preserved
    assert "KEEP=me" in text
    assert "API_SERVER_PORT=9000" in text
    assert "API_SERVER_PORT=8642" not in text
    assert "NEW=x" in text


def test_upsert_env_file_creates_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "sub" / ".env"
    cli_hermes._upsert_env_file(p, {"A": "1"})
    assert p.read_text(encoding="utf-8").strip() == "A=1"


def test_load_env_files_does_not_override_existing(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / ".env"
    p.write_text("FOO=fromfile\nBAR=baz\n", encoding="utf-8")
    monkeypatch.setenv("FOO", "fromenv")
    monkeypatch.delenv("BAR", raising=False)
    applied = cli_hermes._load_env_files([p])
    assert applied == [p]
    assert os.environ["FOO"] == "fromenv"  # not overridden
    assert os.environ["BAR"] == "baz"  # newly loaded


def test_read_hermes_config_env_overrides_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "api_server:\n  enabled: true\n  port: 8642\n  key: fromyaml\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("API_SERVER_KEY=fromenv\n", encoding="utf-8")
    cfg = cli_hermes._read_hermes_config()
    assert cfg["API_SERVER_ENABLED"] == "True"
    assert cfg["API_SERVER_PORT"] == "8642"
    assert cfg["API_SERVER_KEY"] == "fromenv"  # .env wins


def test_read_hermes_config_absent_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "missing"))
    assert cli_hermes._read_hermes_config() == {}


def test_enable_hermes_gateway_writes_and_backs_up(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    env_path = tmp_path / ".env"
    env_path.write_text("API_SERVER_PORT=8642\n", encoding="utf-8")
    key = cli_hermes._enable_hermes_gateway()
    assert (tmp_path / ".env.bak").exists()
    parsed = cli_hermes._parse_env_file(env_path)
    assert parsed["API_SERVER_ENABLED"] == "true"
    assert parsed["API_SERVER_KEY"] == key  # returned key matches what was written
    assert parsed["API_SERVER_PORT"] == "8642"  # preserved


def test_enable_hermes_gateway_keeps_existing_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("API_SERVER_KEY=keepme\n", encoding="utf-8")
    key = cli_hermes._enable_hermes_gateway()
    assert key == "keepme"


# ── severity + autoload integration ──────────────────────────────────────────
def test_cli_missing_and_gateway_down_is_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "missing"))
    monkeypatch.setattr(cli_hermes.shutil, "which", lambda _name: None)
    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    sec = cli_hermes._check_hermes("http://127.0.0.1:8642/v1", network=True)
    by_label = {c.label: c.status for c in sec.checks}
    assert by_label["CLI not found"] == cli_hermes.FAIL
    assert by_label["Gateway unreachable"] == cli_hermes.FAIL


def test_doctor_autoloads_env_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("DEEPGRAM_API_KEY=dg-from-file\n", encoding="utf-8")
    args = argparse.Namespace(
        base_url=None,
        no_network=True,
        json=True,
        env_file=[str(env)],
        no_env_file=False,
    )
    cli_hermes.cmd_doctor(args)
    assert os.environ.get("DEEPGRAM_API_KEY") == "dg-from-file"
    out = capsys.readouterr().out
    assert "dg-from-file" not in out  # secrets aren't echoed
    assert str(env) in out  # but the loaded path is reported


def test_parser_wires_subcommands() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_hermes.build_hermes_parser(sub)
    ns = parser.parse_args(["hermes", "attach-number", "+15551234567", "--url", "https://x/y"])
    assert ns.command == "hermes"
    assert ns.hermes_command == "attach-number"
    assert ns.number == "+15551234567"
    assert ns.url == "https://x/y"
