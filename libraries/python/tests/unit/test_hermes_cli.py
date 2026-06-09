"""Unit tests for the ``patter hermes ...`` CLI (doctor / setup / attach-number).

Live probes are exercised by monkeypatching ``httpx`` at the boundary, so no
real Hermes gateway or Twilio account is touched.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

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


def test_parser_wires_subcommands() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_hermes.build_hermes_parser(sub)
    ns = parser.parse_args(["hermes", "attach-number", "+15551234567", "--url", "https://x/y"])
    assert ns.command == "hermes"
    assert ns.hermes_command == "attach-number"
    assert ns.number == "+15551234567"
    assert ns.url == "https://x/y"
