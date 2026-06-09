"""``patter openclaw ...`` — zero-config setup, diagnostics, and Twilio wiring
for the OpenClaw voice shell (Direction C: Patter is the voice, one scoped
OpenClaw agent is the brain).

OpenClaw is a multi-agent gateway, so unlike ``patter hermes`` (single agent)
this wizard helps you *pick* which agent is on the line and guards against
pointing the phone at the default / master agent. The same scoped agent powers
both directions — Patter answers inbound calls (``app.py`` → ``phone.serve``)
*and* places outbound calls (``dialer.py`` → ``phone.call``).

Subcommands:

* ``patter openclaw doctor`` — preflight checks across the OpenClaw gateway, the
  agent roster, the Patter providers, the carrier, and the security posture.
* ``patter openclaw setup`` — scaffold a ready-to-run ``openclaw-phone-agent``
  project for ``--mode {inbound|outbound|both}``, optionally enabling the
  gateway's OpenAI-compatible endpoint and attaching a Twilio number.
* ``patter openclaw test`` — acceptance: ``/v1/models`` + a real chat turn
  against ``openclaw/<agent>`` + provider readiness.
* ``patter openclaw call`` — place one outbound test call through the agent.
* ``patter openclaw agents`` — list the agents the gateway serves.
* ``patter openclaw attach-number`` / ``numbers`` — Twilio voice-webhook wiring.

Live probes are best-effort and time-bounded; pass ``--no-network`` to skip
them. Nothing is mutated unless you ask for it (``setup`` prompts before
writing and only touches ``~/.openclaw/openclaw.json`` with ``--enable-openclaw``,
backing it up first).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from getpatter import _cli_common
from getpatter._cli_common import (
    FAIL,
    OK,
    SKIP,
    WARN,
    Check,
    Section,
    _attach_number,
    _confirm,
    _env_key,
    _generate_key,
    _get_json,
    _load_env_files,
    _model_ids,
    _parse_env_file,
    _print_sections,
    _provider_checks,
    _sections_to_dict,
    _upsert_env_file,
    cmd_numbers,
)
from getpatter.models import (
    _OPENCLAW_AGENT_RE,
    _OPENCLAW_API_KEY_ENV,
    _OPENCLAW_DEFAULT_BASE_URL,
    _OPENCLAW_SESSION_HEADER,
)

# Agent ids that almost always mean the privileged default — the phone should
# never point at these (the gateway credential authorises the whole gateway, so
# choosing the agent picks the persona, not a security boundary).
_DEFAULT_AGENT_NAMES = {"default", "master", "main", "primary", "root", "admin"}


# ──────────────────────────────────────────────────────────────────────────
# OpenClaw gateway base URL + agent-target resolution (mirror OpenClawLLM)
# ──────────────────────────────────────────────────────────────────────────
def _openclaw_base_url(override: str | None) -> str:
    if override:
        return override.rstrip("/")
    env = os.environ.get("OPENCLAW_BASE_URL")
    if env:
        return env.rstrip("/")
    host = os.environ.get("OPENCLAW_HOST")
    port = os.environ.get("OPENCLAW_PORT")
    if host or port:
        return f"http://{host or '127.0.0.1'}:{port or '18789'}/v1"
    return _OPENCLAW_DEFAULT_BASE_URL


def _agent_to_model(agent: str) -> str:
    """Map a bare agent id to ``openclaw/<agent>`` (already-namespaced passes
    through), identical to :class:`getpatter.llm.openclaw.LLM`."""
    return agent if (":" in agent or "/" in agent) else f"openclaw/{agent}"


def _bare_agent(agent: str) -> str:
    """Strip an ``openclaw/`` / ``openclaw:`` / ``agent:`` namespace prefix."""
    for sep in ("/", ":"):
        if sep in agent:
            return agent.rsplit(sep, 1)[1]
    return agent


def _is_default_agent(agent: str) -> bool:
    return _bare_agent(agent).strip().lower() in _DEFAULT_AGENT_NAMES


# ──────────────────────────────────────────────────────────────────────────
# OpenClaw config (~/.openclaw/openclaw.json — JSON5/JSONC tolerant)
# ──────────────────────────────────────────────────────────────────────────
def _openclaw_home() -> Path:
    """OpenClaw config dir — ``$OPENCLAW_HOME`` or ``~/.openclaw``."""
    override = os.environ.get("OPENCLAW_HOME")
    return Path(override) if override else Path.home() / ".openclaw"


def _openclaw_config_path() -> Path:
    return _openclaw_home() / "openclaw.json"


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments and trailing commas, string-aware.

    OpenClaw's config is JSON5/JSONC. We have no json5 dependency, so this is a
    conservative pass good enough to read the ``enabled`` flag and round-trip the
    document as plain JSON (the wizard backs the original up before rewriting).
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:  # keep escaped char verbatim
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                in_str = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in ("\n", "\r"):
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    # Drop trailing commas before a closing brace/bracket.
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _read_openclaw_config() -> dict:
    """Parse ``~/.openclaw/openclaw.json`` (JSONC-tolerant). ``{}`` on miss."""
    path = _openclaw_config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(_strip_jsonc(raw))
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _endpoint_enabled(cfg: dict) -> bool | None:
    """Read ``gateway.http.endpoints.chatCompletions.enabled``. ``None`` if the
    path is absent (so doctor can distinguish 'off' from 'never configured')."""
    node: object = cfg
    for key in ("gateway", "http", "endpoints", "chatCompletions", "enabled"):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return bool(node) if isinstance(node, bool) else None


def _gateway_token_from_config(cfg: dict) -> str:
    """Best-effort read of a configured gateway auth token, if any."""
    node: object = cfg
    for key in ("gateway", "auth", "token"):
        if not isinstance(node, dict) or key not in node:
            return ""
        node = node[key]
    return node if isinstance(node, str) else ""


# ──────────────────────────────────────────────────────────────────────────
# .env autoload
# ──────────────────────────────────────────────────────────────────────────
def _env_files_to_load(
    explicit: list[str] | None, *, project_dir: Path | None
) -> list[Path]:
    if explicit:
        return [Path(p) for p in explicit]
    chain: list[Path] = [_openclaw_home() / ".env"]
    if project_dir is not None:
        chain.append(project_dir / ".env")
    chain.append(Path.cwd() / ".env")
    return chain


def _apply_env(args: argparse.Namespace, *, project_dir: Path | None = None) -> list[Path]:
    if getattr(args, "no_env_file", False):
        return []
    paths = _env_files_to_load(getattr(args, "env_file", None), project_dir=project_dir)
    return _load_env_files(paths)


# ──────────────────────────────────────────────────────────────────────────
# Gateway probe / agent roster
# ──────────────────────────────────────────────────────────────────────────
def _api_key() -> str:
    return os.environ.get(_OPENCLAW_API_KEY_ENV, "")


def _list_agents(base_url: str, key: str) -> tuple[list[str], int | None, str]:
    """``GET {base_url}/models`` → (sorted model ids, status, error)."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    status, body, err = _get_json(f"{base_url}/models", headers=headers)
    return sorted(_model_ids(body)), status, err


# ──────────────────────────────────────────────────────────────────────────
# Check groups
# ──────────────────────────────────────────────────────────────────────────
def _check_openclaw(base_url: str, *, network: bool, agent: str) -> Section:
    sec = Section("OpenClaw")
    have_cli = bool(shutil.which("openclaw"))
    cfg = _read_openclaw_config()
    cfg_path = _openclaw_config_path()

    if have_cli:
        sec.checks.append(Check(OK, "CLI found", "openclaw on PATH"))
    else:
        sec.checks.append(
            Check(
                WARN,
                "CLI not found",
                "optional when the gateway is already running",
                "install OpenClaw and run its gateway daemon",
            )
        )

    # OpenAI-compatible endpoint — disabled by default in OpenClaw.
    enabled = _endpoint_enabled(cfg) if cfg else None
    if enabled is True:
        sec.checks.append(
            Check(OK, "chatCompletions endpoint enabled", str(cfg_path))
        )
    elif enabled is False:
        sec.checks.append(
            Check(
                WARN,
                "chatCompletions endpoint disabled",
                f"gateway.http.endpoints.chatCompletions.enabled=false in {cfg_path}",
                "patter openclaw setup --enable-openclaw",
            )
        )
    elif cfg:
        sec.checks.append(
            Check(
                WARN,
                "chatCompletions endpoint not configured",
                f"not set in {cfg_path} (disabled by default)",
                "patter openclaw setup --enable-openclaw",
            )
        )
    else:
        sec.checks.append(
            Check(SKIP, "OpenClaw config", f"no {cfg_path} found")
        )

    key = _api_key()
    if key:
        sec.checks.append(Check(OK, f"{_OPENCLAW_API_KEY_ENV} set"))
    else:
        sec.checks.append(
            Check(
                WARN,
                f"{_OPENCLAW_API_KEY_ENV} not set",
                "the operator-grade gateway bearer (recommended; never logged)",
                f"export {_OPENCLAW_API_KEY_ENV}=...",
            )
        )

    if not network:
        sec.checks.append(Check(SKIP, "Gateway reachable", "skipped (--no-network)"))
        return sec

    agents, status, err = _list_agents(base_url, key)
    if status == 200:
        sec.checks.append(Check(OK, "Gateway reachable", base_url))
        if agents:
            shown = ", ".join(agents[:6]) + ("…" if len(agents) > 6 else "")
            sec.checks.append(Check(OK, "Agents served", f"{len(agents)}: {shown}"))
        else:
            sec.checks.append(
                Check(WARN, "Agent list empty", "gateway returned no models")
            )
        target = _agent_to_model(agent)
        if agents and target in agents:
            sec.checks.append(Check(OK, "Target agent available", target))
        elif agents:
            sec.checks.append(
                Check(
                    WARN,
                    "Target agent not found",
                    f"{target!r} missing; saw {', '.join(agents[:5])}",
                    "pass --agent <id> matching one of the served agents",
                )
            )
    elif status in (401, 403):
        sec.checks.append(
            Check(
                FAIL,
                "Gateway rejected key",
                f"HTTP {status} from {base_url}/models",
                f"check {_OPENCLAW_API_KEY_ENV} matches the gateway auth token",
            )
        )
    else:
        detail = f"HTTP {status}" if status else (err or "no response")
        fix = (
            "start the OpenClaw gateway daemon"
            if have_cli
            else "install OpenClaw + run its gateway daemon"
        )
        sec.checks.append(
            Check(
                FAIL,
                "Gateway unreachable",
                f"{base_url} — {detail}",
                fix
                + ", enable chatCompletions, then retry (or pass --base-url)",
            )
        )
        if not have_cli:
            for c in sec.checks:
                if c.label == "CLI not found":
                    c.status = FAIL
                    c.detail = "and the gateway is unreachable"
    return sec


def _check_patter(agent: str) -> Section:
    sec = Section("Patter")

    try:
        from getpatter import __version__

        sec.checks.append(Check(OK, "getpatter installed", __version__))
    except Exception as exc:  # noqa: BLE001
        sec.checks.append(Check(FAIL, "getpatter import failed", str(exc)))
        return sec

    try:
        from getpatter import OpenClawLLM

        OpenClawLLM(agent=_bare_agent(agent) or "receptionist")
        sec.checks.append(Check(OK, "OpenClawLLM constructible"))
    except Exception as exc:  # noqa: BLE001
        sec.checks.append(Check(FAIL, "OpenClawLLM construction failed", str(exc)))

    sec.checks.extend(_provider_checks())
    return sec


def _check_security(base_url: str, agent: str) -> Section:
    """Day-1 hardening posture for a public-facing 24/7 voice line.

    Verifiable items are OK/WARN; the un-probeable reminders (tool policy, EU
    consent/disclosure) are printed as informational notes so they do not
    inflate the warning count but still nudge the operator.
    """
    sec = Section("Security")

    host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host in ("127.0.0.1", "localhost", "::1"):
        sec.checks.append(Check(OK, "Gateway on loopback", base_url))
    else:
        sec.checks.append(
            Check(
                WARN,
                "Gateway not on loopback",
                f"{host} — only Patter's carrier webhook should be public",
                "bind the OpenClaw gateway to 127.0.0.1 / a private tailnet",
            )
        )

    if _api_key():
        sec.checks.append(Check(OK, "Gateway bearer set"))
    else:
        sec.checks.append(
            Check(
                WARN,
                "Gateway bearer missing",
                f"set a strong {_OPENCLAW_API_KEY_ENV}",
                f"export {_OPENCLAW_API_KEY_ENV}=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')",
            )
        )

    if _is_default_agent(agent):
        sec.checks.append(
            Check(
                WARN,
                "Targeting the default/master agent",
                f"{agent!r} looks like the privileged default",
                "point --agent at a scoped, least-privileged agent",
            )
        )
    else:
        sec.checks.append(Check(OK, "Scoped agent", _agent_to_model(agent)))

    # Informational reminders (cannot be probed from here).
    sec.checks.append(
        Check(
            SKIP,
            "Tool policy",
            "deny exec/bash/process/write/edit/browser/gateway on this agent",
        )
    )
    sec.checks.append(
        Check(
            SKIP,
            "Disclosure & data residency",
            "AI + recording notice at call start; keep audio/transcripts in-region (GDPR/EU AI Act)",
        )
    )
    return sec


def _run_doctor(args: argparse.Namespace) -> list[Section]:
    base_url = _openclaw_base_url(getattr(args, "base_url", None))
    network = not getattr(args, "no_network", False)
    agent = getattr(args, "agent", None) or "receptionist"
    mode = getattr(args, "mode", None) or "inbound"
    return [
        _check_openclaw(base_url, network=network, agent=agent),
        _check_patter(agent),
        _cli_common._check_twilio(
            network=network, cli_name="openclaw", check_webhook=(mode != "outbound")
        ),
        _check_security(base_url, agent),
    ]


# ──────────────────────────────────────────────────────────────────────────
# Subcommands
# ──────────────────────────────────────────────────────────────────────────
def cmd_doctor(args: argparse.Namespace) -> int:
    loaded = _apply_env(args)
    sections = _run_doctor(args)
    report = _sections_to_dict(sections)
    if getattr(args, "json", False):
        report["loaded_env_files"] = [str(p) for p in loaded]
        print(json.dumps(report, indent=2))
    else:
        if loaded:
            print("Loaded env from: " + ", ".join(str(p) for p in loaded))
        _print_sections(sections)
        print()
        if report["failures"]:
            print(
                f"{report['failures']} problem(s) to fix, "
                f"{report['warnings']} warning(s)."
            )
        elif report["warnings"]:
            print(f"Ready, with {report['warnings']} warning(s).")
        else:
            print("All checks passed. You're ready to take calls.")
    return 1 if report["failures"] else 0


def cmd_agents(args: argparse.Namespace) -> int:
    """List the agents the gateway serves (the ``openclaw/<agent>`` model ids)."""
    _apply_env(args)
    base_url = _openclaw_base_url(getattr(args, "base_url", None))
    agents, status, err = _list_agents(base_url, _api_key())
    if status != 200:
        detail = f"HTTP {status}" if status else (err or "no response")
        print(
            f"Could not reach the OpenClaw gateway at {base_url} — {detail}\n"
            "Start the gateway, enable chatCompletions, and set "
            f"{_OPENCLAW_API_KEY_ENV}.",
            file=sys.stderr,
        )
        return 1
    if getattr(args, "json", False):
        print(json.dumps({"agents": agents}, indent=2))
        return 0
    if not agents:
        print("The gateway returned no agents.")
        return 0
    print(f"{len(agents)} agent(s) on {base_url}:")
    for a in agents:
        note = "  (default/master — do not put on the phone)" if _is_default_agent(a) else ""
        print(f"  {a}{note}")
    return 0


def _chat_turn_check(base_url: str, key: str, model: str, prompt: str) -> Check:
    """Send one ``/chat/completions`` turn with the OpenClaw session header."""
    return _cli_common._chat_turn_check(
        base_url,
        key,
        model,
        prompt,
        session_header=_OPENCLAW_SESSION_HEADER,
        session_value="patter-cli-test",
        doctor_hint="patter openclaw doctor",
    )


def cmd_test(args: argparse.Namespace) -> int:
    """Acceptance: ``/v1/models`` + a real chat turn against ``openclaw/<agent>``."""
    loaded = _apply_env(args)
    base_url = _openclaw_base_url(getattr(args, "base_url", None))
    key = _api_key()
    agent = getattr(args, "agent", None) or "receptionist"
    model = _agent_to_model(agent)

    sec = Section("OpenClaw acceptance")
    agents, status, err = _list_agents(base_url, key)
    if status == 200:
        sec.checks.append(Check(OK, "Gateway reachable", base_url))
        sec.checks.append(
            Check(OK, "Agent available", model)
            if model in agents
            else Check(WARN, "Agent not found", f"{model!r} not in {agents[:5]}")
        )
        sec.checks.append(
            _chat_turn_check(
                base_url,
                key,
                model,
                getattr(args, "prompt", None)
                or "Reply with one short spoken sentence to confirm you are online.",
            )
        )
    else:
        detail = f"HTTP {status}" if status else (err or "no response")
        sec.checks.append(
            Check(FAIL, "Gateway reachable", f"{base_url} — {detail}", "patter openclaw doctor")
        )

    try:
        from getpatter import OpenClawLLM

        OpenClawLLM(agent=_bare_agent(agent) or "receptionist")
        sec.checks.append(Check(OK, "OpenClawLLM constructible"))
    except Exception as exc:  # noqa: BLE001
        sec.checks.append(Check(FAIL, "OpenClawLLM construction failed", str(exc)))
    sec.checks.append(_env_key("DEEPGRAM_API_KEY", "Deepgram STT"))
    sec.checks.append(_env_key("ELEVENLABS_API_KEY", "ElevenLabs TTS"))

    report = _sections_to_dict([sec])
    if getattr(args, "json", False):
        report["loaded_env_files"] = [str(p) for p in loaded]
        print(json.dumps(report, indent=2))
    else:
        if loaded:
            print("Loaded env from: " + ", ".join(str(p) for p in loaded))
        _print_sections([sec])
        print()
        if report["failures"]:
            print(f"{report['failures']} blocker(s) — fix before calling.")
        else:
            print("Acceptance passed — the agent is answering and providers are ready.")
    return 1 if report["failures"] else 0


async def _dial(
    to: str, *, agent: str, from_number: str, first_message: str
) -> str:
    """Place one outbound call through the OpenClaw voice shell. Returns the
    terminal outcome. Factored out so ``cmd_call`` is unit-testable."""
    from getpatter import (
        DeepgramSTT,
        ElevenLabsRestTTS,
        OpenClawLLM,
        Patter,
        Twilio,
    )

    phone = Patter(
        carrier=Twilio(),
        phone_number=from_number or os.environ.get("PATTER_PHONE_NUMBER", ""),
        tunnel=True,
    )
    built = phone.agent(
        system_prompt=(
            "You are on a short outbound call. Greet the person warmly, be brief "
            "and spoken-friendly, and explain why you called."
        ),
        first_message=first_message or "Hi, this is a quick call from Patter.",
        stt=DeepgramSTT(),
        llm=OpenClawLLM(agent=_bare_agent(agent) or "sales"),
        tts=ElevenLabsRestTTS.for_twilio(),
    )
    result = await phone.call(to, agent=built, wait=True)
    return result.outcome if result else "unknown"


def cmd_call(args: argparse.Namespace) -> int:
    """Place one outbound test call through ``openclaw/<agent>``."""
    import asyncio

    _apply_env(args)
    to = args.to
    if not isinstance(to, str) or not to.startswith("+"):
        print(f"`to` must be an E.164 number (e.g. +15551234567), got {to!r}", file=sys.stderr)
        return 2
    agent = getattr(args, "agent", None) or "sales"
    try:
        outcome = asyncio.run(
            _dial(
                to,
                agent=agent,
                from_number=getattr(args, "from_number", "") or "",
                first_message=getattr(args, "first_message", "") or "",
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Outbound call failed: {exc}", file=sys.stderr)
        return 1
    print(f"Call outcome: {outcome}")
    return 0


def cmd_attach_number(args: argparse.Namespace) -> int:
    return _attach_number(args.number, args.url, args.status_callback)


# ──────────────────────────────────────────────────────────────────────────
# Gateway enablement (the one step that writes to ~/.openclaw/openclaw.json)
# ──────────────────────────────────────────────────────────────────────────
_ENABLE_SNIPPET = """\
{
  "gateway": {
    "http": {
      "endpoints": {
        "chatCompletions": { "enabled": true }
      }
    }
  }
}"""


def _set_nested(cfg: dict, path: tuple[str, ...], value: object) -> None:
    node = cfg
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _enable_openclaw_gateway() -> str:
    """Set ``gateway.http.endpoints.chatCompletions.enabled=true`` in
    ``~/.openclaw/openclaw.json``.

    The config is JSON5/JSONC; we back it up to ``.bak`` first, then rewrite it
    as plain JSON (comments are dropped — the backup keeps them). If the file is
    missing or cannot be parsed, we print the snippet to paste by hand instead of
    risking the document. Returns the gateway bearer to mirror into the project
    ``.env`` (the existing ``OPENCLAW_API_KEY`` or a freshly generated one).
    """
    path = _openclaw_config_path()
    cfg_path = path

    if path.exists():
        raw = path.read_text(encoding="utf-8")
        backup = path.parent / (path.name + ".bak")
        backup.write_text(raw, encoding="utf-8")
        print(f"Backed up {path} → {backup}")
        try:
            cfg = json.loads(_strip_jsonc(raw))
            if not isinstance(cfg, dict):
                raise ValueError("top-level value is not an object")
        except (json.JSONDecodeError, ValueError) as exc:
            print(
                f"Could not safely parse {cfg_path} ({exc}). Add this by hand:\n\n"
                f"{_ENABLE_SNIPPET}\n"
            )
            return os.environ.get(_OPENCLAW_API_KEY_ENV, "")
    else:
        print(
            f"No {cfg_path} found. Create it with:\n\n{_ENABLE_SNIPPET}\n"
        )
        cfg = {}

    _set_nested(
        cfg, ("gateway", "http", "endpoints", "chatCompletions", "enabled"), True
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"✓ chatCompletions.enabled=true written to {cfg_path}")
    print("  (JSON5 comments were not preserved — the original is in .bak)")

    # Keep the gateway bound to loopback and require a strong bearer. We do not
    # edit OpenClaw's auth schema for you — just supply a key to share via .env.
    key = os.environ.get(_OPENCLAW_API_KEY_ENV, "") or _gateway_token_from_config(cfg)
    if not key:
        key = _generate_key()
        print(
            f"✓ generated an {_OPENCLAW_API_KEY_ENV} — configure the gateway to "
            "require this bearer, and keep it on loopback."
        )
    if shutil.which("openclaw"):
        print("Now (re)start the OpenClaw gateway daemon so the change takes effect.")
    else:
        print("Restart the OpenClaw gateway your usual way so the change takes effect.")
    return key


def _start_gateway() -> bool:
    """Best-effort ``openclaw gateway start`` (the daemon is OpenClaw's to own)."""
    if not shutil.which("openclaw"):
        print(
            "Cannot start the gateway: openclaw CLI not found. Start the gateway "
            "daemon your usual way, then re-run without --no-network to verify."
        )
        return False
    import subprocess

    print("Starting the OpenClaw gateway (openclaw gateway start)…")
    try:
        proc = subprocess.run(
            ["openclaw", "gateway", "start"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Could not start the gateway: {exc}")
        return False
    if proc.returncode == 0:
        return True
    print((proc.stderr or proc.stdout or "").strip()[:300])
    return False


def _wait_for_gateway(
    base_url: str, key: str, *, timeout: float = 60.0, interval: float = 2.0
) -> bool:
    """Poll ``{base_url}/models`` until it answers 200 or the timeout elapses."""
    import time

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    deadline = time.monotonic() + timeout
    print(f"Waiting for the gateway at {base_url} (up to {int(timeout)}s)…")
    while time.monotonic() < deadline:
        status, _body, _err = _get_json(f"{base_url}/models", headers=headers, timeout=3.0)
        if status == 200:
            print("✓ Gateway is ready.")
            return True
        time.sleep(interval)
    print("✗ Gateway did not become ready in time.")
    return False


def cmd_setup(args: argparse.Namespace) -> int:
    from getpatter import _openclaw_scaffold

    target = Path(args.dir).resolve()
    mode = getattr(args, "mode", None) or "inbound"
    agent = getattr(args, "agent", None) or "receptionist"
    interactive = sys.stdin.isatty() and not args.yes

    if not _OPENCLAW_AGENT_RE.fullmatch(agent):
        print(f"Invalid --agent {agent!r} (letters, digits, ._:/- only).", file=sys.stderr)
        return 2

    print(f"Patter + OpenClaw setup\n  project: {target}\n  mode: {mode}  agent: {agent}\n")
    if _is_default_agent(agent):
        print(
            f"  ! '{agent}' looks like the default/master agent. Point --agent at a "
            "scoped, least-privileged agent instead.\n"
        )

    # 0. Optionally enable the OpenAI-compatible endpoint in openclaw.json.
    gateway_key = ""
    if getattr(args, "enable_openclaw", False):
        gateway_key = _enable_openclaw_gateway()
        print()

    # 1. Load env for the preflight.
    loaded = _apply_env(args, project_dir=target)
    if loaded:
        print("Loaded env from: " + ", ".join(str(p) for p in loaded))

    # 1b. Optionally start the gateway and wait for readiness.
    if getattr(args, "start_gateway", False) and not getattr(args, "no_network", False):
        base_url = _openclaw_base_url(getattr(args, "base_url", None))
        key = gateway_key or _api_key()
        if _start_gateway():
            _wait_for_gateway(base_url, key)
        print()

    # 2. Preflight.
    print("Checking your environment…")
    sections = _run_doctor(args)
    _print_sections(sections)
    failures = sum(1 for s in sections for c in s.checks if c.status == FAIL)
    print()

    # 3. Scaffold the project for the chosen mode.
    if interactive and not _confirm(f"Scaffold the project into {target}?"):
        print("Skipped scaffolding.")
    else:
        written = _openclaw_scaffold.scaffold(target, force=args.force, mode=mode)
        if written:
            print(f"Wrote {len(written)} file(s):")
            for p in written:
                print(f"  + {p.relative_to(target)}")
        else:
            print("Project already exists (use --force to overwrite).")
        env_path = target / ".env"
        if not env_path.exists() and (target / ".env.example").exists():
            env_path.write_text(
                (target / ".env.example").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            print("  + .env (from .env.example — fill in your keys)")
        # Mirror the OPENCLAW_AGENT + the gateway bearer into the project .env.
        if env_path.exists():
            updates = {"OPENCLAW_AGENT": _bare_agent(agent)}
            if gateway_key:
                updates[_OPENCLAW_API_KEY_ENV] = gateway_key
            elif getattr(args, "generate_key", False) and not _parse_env_file(
                env_path
            ).get(_OPENCLAW_API_KEY_ENV):
                updates[_OPENCLAW_API_KEY_ENV] = _generate_key()
            _upsert_env_file(env_path, updates)
            print(f"  + OPENCLAW_AGENT={_bare_agent(agent)} in .env")
            if _OPENCLAW_API_KEY_ENV in updates:
                print(f"  + {_OPENCLAW_API_KEY_ENV} in .env")
    print()

    # 4. Optionally attach a Twilio number (inbound).
    if mode != "outbound" and args.number and args.url:
        print(f"Attaching {args.number} → {args.url}")
        rc = _attach_number(args.number, args.url, args.status_callback)
        if rc != 0:
            return rc
    elif mode != "outbound" and (args.number or args.url):
        print(
            "Note: pass both --number and --url to auto-configure the Twilio "
            "webhook, or run `patter openclaw attach-number` later."
        )

    # 5. Next steps.
    print("\nNext steps:")
    print(f"  cd {target}")
    print("  # edit .env with your keys")
    print("  python scripts/test_text_turn.py   # smoke-test the agent")
    if mode in ("inbound", "both"):
        print("  python app.py                      # answer inbound calls")
        if not (args.number and args.url):
            print("  patter openclaw attach-number <number> --url <tunnel-url>/calls/inbound")
    if mode in ("outbound", "both"):
        print("  python dialer.py                   # 24/7 outbound dialer (edit numbers.txt)")
    print("  # production 24/7: run app.py/dialer.py + a named tunnel as OS services")
    print("  #   see deploy/ and docs.getpatter.com/dev-tools/production-deployment")
    return 1 if failures else 0


# ──────────────────────────────────────────────────────────────────────────
# Parser wiring
# ──────────────────────────────────────────────────────────────────────────
def _add_env_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--env-file", action="append", default=None, help="dotenv file(s) to load")
    p.add_argument("--no-env-file", action="store_true", help="Do not autoload any .env file")


def build_openclaw_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``openclaw`` subcommand tree to the parent CLI."""
    openclaw = subparsers.add_parser(
        "openclaw",
        help="Set up, diagnose, and wire the OpenClaw voice shell (inbound + outbound)",
    )
    osub = openclaw.add_subparsers(dest="openclaw_command")

    doctor = osub.add_parser("doctor", help="Preflight checks for the OpenClaw voice shell")
    doctor.add_argument("--base-url", default=None, help="OpenClaw gateway base URL")
    doctor.add_argument("--agent", default=None, help="Agent to target (default: receptionist)")
    doctor.add_argument(
        "--mode", choices=["inbound", "outbound"], default="inbound",
        help="Tailor checks for an inbound line or an outbound dialer",
    )
    doctor.add_argument("--no-network", action="store_true", help="Skip live probes")
    doctor.add_argument("--json", action="store_true", help="Machine-readable output")
    _add_env_args(doctor)

    setup = osub.add_parser("setup", help="Scaffold an openclaw-phone-agent project")
    setup.add_argument("--dir", default="openclaw-phone-agent", help="Target directory")
    setup.add_argument(
        "--mode", choices=["inbound", "outbound", "both"], default="inbound",
        help="Scaffold an inbound receptionist, an outbound dialer, or both",
    )
    setup.add_argument("--agent", default="receptionist", help="OpenClaw agent to put on the line")
    setup.add_argument("--force", action="store_true", help="Overwrite existing files")
    setup.add_argument("--yes", action="store_true", help="Non-interactive (assume yes)")
    setup.add_argument("--number", default=None, help="Twilio number to attach (inbound)")
    setup.add_argument("--url", default=None, help="Public webhook URL to attach (inbound)")
    setup.add_argument("--status-callback", default=None, help="Twilio status callback URL")
    setup.add_argument("--base-url", default=None, help="OpenClaw gateway base URL")
    setup.add_argument("--no-network", action="store_true", help="Skip live probes")
    setup.add_argument(
        "--generate-key", action="store_true",
        help=f"Generate a strong {_OPENCLAW_API_KEY_ENV} into the project .env",
    )
    setup.add_argument(
        "--enable-openclaw", action="store_true",
        help="Set chatCompletions.enabled=true in ~/.openclaw/openclaw.json (backed up)",
    )
    setup.add_argument(
        "--start-gateway", action="store_true",
        help="Run `openclaw gateway start` and wait for /v1/models readiness",
    )
    _add_env_args(setup)

    test = osub.add_parser("test", help="Acceptance: /v1/models + a real chat turn + providers")
    test.add_argument("--base-url", default=None, help="OpenClaw gateway base URL")
    test.add_argument("--agent", default=None, help="Agent to target (default: receptionist)")
    test.add_argument("--prompt", default=None, help="Prompt to send for the chat turn")
    test.add_argument("--json", action="store_true", help="Machine-readable output")
    _add_env_args(test)

    call = osub.add_parser("call", help="Place one outbound test call through the agent")
    call.add_argument("to", help="Number to call in E.164 (e.g. +15557654321)")
    call.add_argument("--agent", default=None, help="Agent to drive the call (default: sales)")
    call.add_argument("--from", dest="from_number", default=None, help="Caller-ID number")
    call.add_argument("--first-message", default=None, help="What the agent says on pickup")
    _add_env_args(call)

    agents = osub.add_parser("agents", help="List the agents the gateway serves")
    agents.add_argument("--base-url", default=None, help="OpenClaw gateway base URL")
    agents.add_argument("--json", action="store_true", help="Machine-readable output")
    _add_env_args(agents)

    attach = osub.add_parser("attach-number", help="Point a Twilio number at your Patter URL")
    attach.add_argument("number", help="Phone number in E.164 (e.g. +15551234567)")
    attach.add_argument("--url", required=True, help="Public voice webhook URL (https)")
    attach.add_argument("--status-callback", default=None, help="Twilio status callback URL")

    osub.add_parser("numbers", help="List the Twilio numbers on your account")
    return openclaw


def dispatch_openclaw(args: argparse.Namespace) -> int:
    """Entry for ``patter openclaw ...``. Returns a process exit code."""
    command = getattr(args, "openclaw_command", None)
    if command == "doctor":
        return cmd_doctor(args)
    if command == "setup":
        return cmd_setup(args)
    if command == "test":
        return cmd_test(args)
    if command == "call":
        return cmd_call(args)
    if command == "agents":
        return cmd_agents(args)
    if command == "attach-number":
        return cmd_attach_number(args)
    if command == "numbers":
        return cmd_numbers(args)
    print(
        "Usage: patter openclaw "
        "{doctor|setup|test|call|agents|attach-number|numbers}\n"
        "Try:   patter openclaw doctor",
        file=sys.stderr,
    )
    return 2
