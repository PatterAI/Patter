"""Unit tests for the ``patter openclaw ...`` CLI.

Live probes are exercised by monkeypatching ``httpx`` at the boundary, so no
real OpenClaw gateway or Twilio account is touched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from getpatter import _openclaw_scaffold, cli_openclaw


# ── agent-id helpers ─────────────────────────────────────────────────────────
def test_agent_to_model() -> None:
    assert cli_openclaw._agent_to_model("receptionist") == "openclaw/receptionist"
    assert cli_openclaw._agent_to_model("openclaw/x") == "openclaw/x"
    assert cli_openclaw._agent_to_model("agent:y") == "agent:y"


def test_is_default_agent() -> None:
    assert cli_openclaw._is_default_agent("default")
    assert cli_openclaw._is_default_agent("openclaw/master")
    assert cli_openclaw._is_default_agent("MAIN")
    assert not cli_openclaw._is_default_agent("receptionist")
    assert not cli_openclaw._is_default_agent("openclaw/sales")


# ── scaffold (mode-aware) ────────────────────────────────────────────────────
def test_scaffold_inbound_mode(tmp_path: Path) -> None:
    written = _openclaw_scaffold.scaffold(tmp_path, mode="inbound")
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "app.py" in rel and "agent.py" in rel
    assert "deploy/patter-receptionist.service" in rel
    assert "dialer.py" not in rel and "numbers.example.txt" not in rel


def test_scaffold_outbound_mode(tmp_path: Path) -> None:
    written = _openclaw_scaffold.scaffold(tmp_path, mode="outbound")
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert "dialer.py" in rel and "numbers.example.txt" in rel
    assert "scripts/test_outbound_call.py" in rel
    assert "app.py" not in rel


def test_scaffold_both_mode_writes_all(tmp_path: Path) -> None:
    written = _openclaw_scaffold.scaffold(tmp_path, mode="both")
    rel = {p.relative_to(tmp_path).as_posix() for p in written}
    assert rel == set(_openclaw_scaffold.FILES)


def test_scaffold_skips_existing_without_force(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("# mine", encoding="utf-8")
    written = _openclaw_scaffold.scaffold(tmp_path, mode="both")
    assert tmp_path / "app.py" not in written
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "# mine"
    written2 = _openclaw_scaffold.scaffold(tmp_path, force=True, mode="both")
    assert tmp_path / "app.py" in written2
    assert (tmp_path / "app.py").read_text(encoding="utf-8") != "# mine"


def test_committed_example_matches_scaffold() -> None:
    """The committed examples/ tree must stay in sync with the scaffold map."""
    root = Path(__file__).resolve().parents[4] / "examples" / "openclaw-phone-agent"
    assert root.is_dir(), f"missing example dir: {root}"
    for rel, content in _openclaw_scaffold.FILES.items():
        on_disk = (root / rel).read_text(encoding="utf-8")
        assert on_disk == content, f"{rel} drifted from the scaffold"


# ── doctor ───────────────────────────────────────────────────────────────────
def _doctor_args(**over) -> argparse.Namespace:
    base = {
        "base_url": None,
        "no_network": True,
        "json": True,
        "agent": "receptionist",
        "mode": "inbound",
        "env_file": None,
        "no_env_file": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_doctor_no_network_skips_probes(capsys, monkeypatch) -> None:
    for var in ("OPENCLAW_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    rc = cli_openclaw.cmd_doctor(_doctor_args())
    out = capsys.readouterr().out
    assert "skipped (--no-network)" in out
    assert rc == 0  # warnings don't fail the run


def test_doctor_gateway_unreachable_is_failure(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise OSError("Connection refused")

    monkeypatch.setenv("OPENCLAW_HOME", "/nonexistent-openclaw-home")
    monkeypatch.setattr("httpx.get", boom)
    sec = cli_openclaw._check_openclaw(
        "http://127.0.0.1:18789/v1", network=True, agent="receptionist"
    )
    statuses = {c.label: c.status for c in sec.checks}
    assert statuses["Gateway unreachable"] == cli_openclaw.FAIL


def test_doctor_gateway_ok_lists_agents(monkeypatch) -> None:
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "openclaw/receptionist"}, {"id": "openclaw/sales"}]}

    monkeypatch.setenv("OPENCLAW_API_KEY", "k")
    monkeypatch.setattr("httpx.get", lambda *a, **k: Resp())
    sec = cli_openclaw._check_openclaw(
        "http://127.0.0.1:18789/v1", network=True, agent="receptionist"
    )
    labels = {c.label: c.status for c in sec.checks}
    assert labels["Gateway reachable"] == cli_openclaw.OK
    assert labels["Agents served"] == cli_openclaw.OK
    assert labels["Target agent available"] == cli_openclaw.OK


def test_doctor_target_agent_not_found_warns(monkeypatch) -> None:
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "openclaw/sales"}]}

    monkeypatch.setattr("httpx.get", lambda *a, **k: Resp())
    sec = cli_openclaw._check_openclaw(
        "http://127.0.0.1:18789/v1", network=True, agent="receptionist"
    )
    labels = {c.label: c.status for c in sec.checks}
    assert labels["Target agent not found"] == cli_openclaw.WARN


def test_doctor_exit_code_one_when_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_openclaw,
        "_run_doctor",
        lambda _a: [
            cli_openclaw.Section("X", [cli_openclaw.Check(cli_openclaw.FAIL, "bad")])
        ],
    )
    assert cli_openclaw.cmd_doctor(_doctor_args(json=False)) == 1


# ── security section ─────────────────────────────────────────────────────────
def test_security_flags_default_agent(monkeypatch) -> None:
    monkeypatch.delenv("OPENCLAW_API_KEY", raising=False)
    sec = cli_openclaw._check_security("http://127.0.0.1:18789/v1", "default")
    labels = {c.label: c.status for c in sec.checks}
    assert labels["Targeting the default/master agent"] == cli_openclaw.WARN
    assert labels["Gateway on loopback"] == cli_openclaw.OK


def test_security_scoped_agent_and_non_loopback(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_API_KEY", "strong")
    sec = cli_openclaw._check_security("http://10.0.0.5:18789/v1", "receptionist")
    labels = {c.label: c.status for c in sec.checks}
    assert labels["Scoped agent"] == cli_openclaw.OK
    assert labels["Gateway not on loopback"] == cli_openclaw.WARN
    assert labels["Gateway bearer set"] == cli_openclaw.OK


# ── JSON5 config + endpoint enable ───────────────────────────────────────────
def test_strip_jsonc_handles_comments_and_trailing_commas() -> None:
    src = '{\n  // line comment\n  "url": "http://x//y", /* block */\n  "a": 1,\n}'
    assert json.loads(cli_openclaw._strip_jsonc(src)) == {"url": "http://x//y", "a": 1}


def test_read_openclaw_config_jsonc(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path))
    (tmp_path / "openclaw.json").write_text(
        '{ "gateway": { "http": { "endpoints": { "chatCompletions": '
        '{ "enabled": true } } } } } // trailing comment\n',
        encoding="utf-8",
    )
    cfg = cli_openclaw._read_openclaw_config()
    assert cli_openclaw._endpoint_enabled(cfg) is True


def test_endpoint_enabled_variants() -> None:
    on = {"gateway": {"http": {"endpoints": {"chatCompletions": {"enabled": True}}}}}
    off = {"gateway": {"http": {"endpoints": {"chatCompletions": {"enabled": False}}}}}
    assert cli_openclaw._endpoint_enabled(on) is True
    assert cli_openclaw._endpoint_enabled(off) is False
    assert cli_openclaw._endpoint_enabled({}) is None


def test_enable_openclaw_gateway_writes_and_backs_up(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCLAW_API_KEY", raising=False)
    cfg_path = tmp_path / "openclaw.json"
    cfg_path.write_text(
        '{\n  // keep this comment in the backup\n  "gateway": { "bind": "loopback" },\n}\n',
        encoding="utf-8",
    )
    key = cli_openclaw._enable_openclaw_gateway()
    assert (tmp_path / "openclaw.json.bak").exists()
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["gateway"]["http"]["endpoints"]["chatCompletions"]["enabled"] is True
    assert data["gateway"]["bind"] == "loopback"  # preserved
    assert key  # generated a bearer to mirror into .env


def test_enable_openclaw_gateway_missing_file_creates_it(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "fresh"
    monkeypatch.setenv("OPENCLAW_HOME", str(home))
    monkeypatch.delenv("OPENCLAW_API_KEY", raising=False)
    cli_openclaw._enable_openclaw_gateway()
    out = capsys.readouterr().out
    assert "chatCompletions" in out
    data = json.loads((home / "openclaw.json").read_text(encoding="utf-8"))
    assert data["gateway"]["http"]["endpoints"]["chatCompletions"]["enabled"] is True


# ── agents ───────────────────────────────────────────────────────────────────
def _agents_args(**over) -> argparse.Namespace:
    base = {"base_url": None, "json": False, "env_file": None, "no_env_file": True}
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_agents_lists(monkeypatch, capsys) -> None:
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "openclaw/receptionist"}, {"id": "openclaw/master"}]}

    monkeypatch.setattr("httpx.get", lambda *a, **k: Resp())
    assert cli_openclaw.cmd_agents(_agents_args()) == 0
    out = capsys.readouterr().out
    assert "openclaw/receptionist" in out
    assert "default/master" in out  # the master agent is flagged


def test_cmd_agents_unreachable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))
    )
    assert cli_openclaw.cmd_agents(_agents_args()) == 1
    assert "Could not reach" in capsys.readouterr().err


# ── chat-turn acceptance ─────────────────────────────────────────────────────
def test_chat_turn_check_ok(monkeypatch) -> None:
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "Hi there"}}]}

    monkeypatch.setattr("httpx.post", lambda *a, **k: Resp())
    c = cli_openclaw._chat_turn_check("http://x/v1", "k", "openclaw/receptionist", "hi")
    assert c.status == cli_openclaw.OK
    assert "Hi there" in c.detail


# ── outbound call command ────────────────────────────────────────────────────
def test_cmd_call_invalid_number(capsys) -> None:
    args = argparse.Namespace(
        to="15551234567", agent=None, from_number=None, first_message=None,
        env_file=None, no_env_file=True,
    )
    assert cli_openclaw.cmd_call(args) == 2
    assert "E.164" in capsys.readouterr().err


def test_cmd_call_dials(monkeypatch, capsys) -> None:
    async def fake_dial(to, *, agent, from_number, first_message):
        assert to == "+15557654321"
        assert agent == "sales"
        return "answered"

    monkeypatch.setattr(cli_openclaw, "_dial", fake_dial)
    args = argparse.Namespace(
        to="+15557654321", agent="sales", from_number=None, first_message=None,
        env_file=None, no_env_file=True,
    )
    assert cli_openclaw.cmd_call(args) == 0
    assert "answered" in capsys.readouterr().out


# ── dispatch + parser wiring ─────────────────────────────────────────────────
def test_dispatch_unknown_subcommand_returns_usage() -> None:
    args = argparse.Namespace(openclaw_command=None)
    assert cli_openclaw.dispatch_openclaw(args) == 2


def test_parser_wires_subcommands() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_openclaw.build_openclaw_parser(sub)

    ns = parser.parse_args(
        ["openclaw", "setup", "--mode", "both", "--agent", "receptionist"]
    )
    assert ns.command == "openclaw"
    assert ns.openclaw_command == "setup"
    assert ns.mode == "both"
    assert ns.agent == "receptionist"

    ns2 = parser.parse_args(["openclaw", "call", "+15557654321", "--from", "+15551112222"])
    assert ns2.openclaw_command == "call"
    assert ns2.to == "+15557654321"
    assert ns2.from_number == "+15551112222"

    ns3 = parser.parse_args(
        ["openclaw", "attach-number", "+15551234567", "--url", "https://x/y"]
    )
    assert ns3.openclaw_command == "attach-number"
    assert ns3.number == "+15551234567"


def test_setup_invalid_agent_returns_two(capsys) -> None:
    args = argparse.Namespace(
        dir="oc", mode="inbound", agent="bad agent!", yes=True, force=False,
        number=None, url=None, status_callback=None, base_url=None, no_network=True,
        generate_key=False, enable_openclaw=False, start_gateway=False,
        env_file=None, no_env_file=True,
    )
    assert cli_openclaw.cmd_setup(args) == 2
    assert "Invalid --agent" in capsys.readouterr().err
