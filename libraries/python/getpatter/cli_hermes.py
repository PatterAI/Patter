"""``patter hermes ...`` — zero-config setup, diagnostics, and Twilio wiring
for the Hermes voice shell (Direction A: Patter is the voice, Hermes is the
brain).

Subcommands:

* ``patter hermes doctor`` — preflight checks across the Hermes gateway, the
  Patter providers, and the carrier, each with a suggested fix.
* ``patter hermes setup`` — scaffold a ready-to-run ``hermes-phone-agent``
  project, run the checks, and optionally attach a Twilio number.
* ``patter hermes attach-number`` — point a Twilio number's voice webhook at
  your Patter URL.
* ``patter hermes numbers`` — list the Twilio numbers on your account.

Live probes (gateway / Twilio API) are best-effort and time-bounded; pass
``--no-network`` to skip them. Nothing is mutated unless you ask for it
(``setup`` prompts before writing; ``attach-number`` is an explicit command).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Check statuses.
OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_SYMBOL = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "·"}


def _color(text: str, status: str) -> str:
    """Colorize a status symbol unless NO_COLOR is set or output isn't a tty."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    code = {OK: "32", WARN: "33", FAIL: "31", SKIP: "90"}.get(status, "0")
    return f"\033[{code}m{text}\033[0m"


@dataclass
class Check:
    """One diagnostic result."""

    status: str
    label: str
    detail: str = ""
    fix: str = ""


@dataclass
class Section:
    """A named group of checks."""

    title: str
    checks: list[Check] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────
# Hermes gateway base URL resolution (mirrors HermesLLM defaults)
# ──────────────────────────────────────────────────────────────────────────
def _hermes_base_url(override: str | None) -> str:
    if override:
        return override.rstrip("/")
    host = os.environ.get("API_SERVER_HOST", "127.0.0.1")
    port = os.environ.get("API_SERVER_PORT", "8642")
    return f"http://{host}:{port}/v1"


def _get_json(url: str, *, headers: dict | None = None, timeout: float = 4.0):
    """Best-effort sync GET returning ``(status_code, json_or_none, error)``."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a core dep
        return None, None, "httpx not installed"
    try:
        resp = httpx.get(url, headers=headers or {}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - surface any connection failure
        return None, None, str(exc)
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - non-JSON body
        body = None
    return resp.status_code, body, ""


# ──────────────────────────────────────────────────────────────────────────
# Check groups
# ──────────────────────────────────────────────────────────────────────────
def _check_hermes(base_url: str, *, network: bool) -> Section:
    sec = Section("Hermes")

    if shutil.which("hermes"):
        sec.checks.append(Check(OK, "CLI found", "hermes on PATH"))
    else:
        sec.checks.append(
            Check(
                WARN,
                "CLI not found",
                "optional — only the gateway is required",
                "install Hermes: https://github.com/NousResearch/hermes-agent",
            )
        )

    key = os.environ.get("API_SERVER_KEY", "")
    if key:
        sec.checks.append(Check(OK, "API_SERVER_KEY set"))
    else:
        sec.checks.append(
            Check(
                WARN,
                "API_SERVER_KEY not set",
                "keyless local gateways work, but a key is recommended",
                'export API_SERVER_KEY="choose-a-strong-key"',
            )
        )

    if not network:
        sec.checks.append(Check(SKIP, "Gateway reachable", "skipped (--no-network)"))
        return sec

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    status, body, err = _get_json(f"{base_url}/models", headers=headers)
    if status == 200:
        sec.checks.append(Check(OK, "Gateway reachable", base_url))
        want = os.environ.get("API_SERVER_MODEL_NAME", "hermes-agent")
        ids = _model_ids(body)
        if want in ids:
            sec.checks.append(Check(OK, "Model available", want))
        elif ids:
            sec.checks.append(
                Check(
                    WARN,
                    "Model not found",
                    f"{want!r} missing; saw {', '.join(sorted(ids)[:5])}",
                    f'set API_SERVER_MODEL_NAME to one of the served models',
                )
            )
        else:
            sec.checks.append(
                Check(WARN, "Model list empty", "gateway returned no models")
            )
    elif status in (401, 403):
        sec.checks.append(
            Check(
                FAIL,
                "Gateway rejected key",
                f"HTTP {status} from {base_url}/models",
                "check API_SERVER_KEY matches the gateway",
            )
        )
    else:
        detail = f"HTTP {status}" if status else (err or "no response")
        sec.checks.append(
            Check(
                FAIL,
                "Gateway unreachable",
                f"{base_url} — {detail}",
                "enable + start the gateway (API_SERVER_ENABLED=true), "
                "or pass --base-url",
            )
        )
    return sec


def _model_ids(body) -> set[str]:
    """Extract model ids from an OpenAI-style ``/models`` payload."""
    if not isinstance(body, dict):
        return set()
    data = body.get("data")
    if not isinstance(data, list):
        return set()
    return {m.get("id") for m in data if isinstance(m, dict) and m.get("id")}


def _check_patter() -> Section:
    sec = Section("Patter")

    try:
        from getpatter import __version__

        sec.checks.append(Check(OK, "getpatter installed", __version__))
    except Exception as exc:  # noqa: BLE001
        sec.checks.append(Check(FAIL, "getpatter import failed", str(exc)))
        return sec

    try:
        from getpatter import HermesLLM

        HermesLLM()
        sec.checks.append(Check(OK, "HermesLLM constructible"))
    except Exception as exc:  # noqa: BLE001
        sec.checks.append(Check(FAIL, "HermesLLM construction failed", str(exc)))

    sec.checks.append(_env_key("DEEPGRAM_API_KEY", "Deepgram STT"))
    sec.checks.append(_env_key("ELEVENLABS_API_KEY", "ElevenLabs TTS"))

    transport = os.environ.get("PATTER_ELEVENLABS_TRANSPORT", "").lower()
    if transport == "rest":
        sec.checks.append(Check(OK, "ElevenLabs transport", "rest"))
    elif transport == "ws":
        sec.checks.append(
            Check(
                WARN,
                "ElevenLabs transport",
                "ws — WebSocket can stall before the first frame on PSTN",
                "PATTER_ELEVENLABS_TRANSPORT=rest for a more robust demo",
            )
        )
    else:
        sec.checks.append(
            Check(OK, "ElevenLabs transport", "unset (example defaults to REST)")
        )

    if importlib.util.find_spec("onnxruntime") is not None:
        sec.checks.append(Check(OK, "Silero VAD available"))
    else:
        sec.checks.append(
            Check(
                WARN,
                "Silero VAD missing",
                "only needed for the pipeline VAD",
                'pip install "getpatter[silero]"',
            )
        )
    return sec


def _env_key(var: str, label: str) -> Check:
    if os.environ.get(var):
        return Check(OK, f"{label} key found")
    return Check(
        WARN,
        f"{label} key missing",
        f"{var} not set",
        f"export {var}=...",
    )


def _check_twilio(*, network: bool) -> Section:
    sec = Section("Twilio")
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not sid or not token:
        sec.checks.append(
            Check(
                WARN,
                "Carrier credentials missing",
                "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set",
                "set them, or use Telnyx/Plivo instead",
            )
        )
        return sec
    sec.checks.append(Check(OK, "Credentials present"))

    if not network:
        sec.checks.append(Check(SKIP, "Credentials valid", "skipped (--no-network)"))
        return sec

    try:
        import httpx
    except ImportError:  # pragma: no cover
        sec.checks.append(Check(SKIP, "Credentials valid", "httpx not installed"))
        return sec

    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
    try:
        resp = httpx.get(f"{base}.json", auth=(sid, token), timeout=6.0)
    except Exception as exc:  # noqa: BLE001
        sec.checks.append(Check(FAIL, "Twilio API unreachable", str(exc)))
        return sec
    if resp.status_code == 200:
        sec.checks.append(Check(OK, "Credentials valid"))
    else:
        sec.checks.append(
            Check(
                FAIL,
                "Credentials rejected",
                f"HTTP {resp.status_code}",
                "check TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN",
            )
        )
        return sec

    number = os.environ.get("PATTER_PHONE_NUMBER") or os.environ.get(
        "TWILIO_PHONE_NUMBER", ""
    )
    if not number:
        sec.checks.append(
            Check(SKIP, "Webhook configured", "set PATTER_PHONE_NUMBER to check")
        )
        return sec
    try:
        resp = httpx.get(
            f"{base}/IncomingPhoneNumbers.json",
            params={"PhoneNumber": number},
            auth=(sid, token),
            timeout=6.0,
        )
        rows = resp.json().get("incoming_phone_numbers", []) if resp.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        sec.checks.append(Check(WARN, "Webhook check failed", str(exc)))
        return sec
    if not rows:
        sec.checks.append(
            Check(
                WARN,
                "Number not on account",
                f"{number} not found",
                "buy/port the number in Twilio, or fix PATTER_PHONE_NUMBER",
            )
        )
        return sec
    voice_url = rows[0].get("voice_url", "")
    if voice_url:
        sec.checks.append(Check(OK, "Webhook configured", voice_url))
    else:
        sec.checks.append(
            Check(
                WARN,
                "Webhook not configured",
                f"{number} has no voice webhook",
                f'patter hermes attach-number {number} --url https://<tunnel>/calls/inbound',
            )
        )
    return sec


# ──────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────
def _print_sections(sections: list[Section]) -> None:
    for sec in sections:
        print(f"\n{sec.title}")
        for c in sec.checks:
            sym = _color(_SYMBOL.get(c.status, "?"), c.status)
            line = f"  {sym} {c.label}"
            if c.detail:
                line += f": {c.detail}"
            print(line)
            if c.fix and c.status in (WARN, FAIL):
                print(f"      fix: {c.fix}")


def _sections_to_dict(sections: list[Section]) -> dict:
    return {
        "sections": [
            {
                "title": s.title,
                "checks": [
                    {
                        "status": c.status,
                        "label": c.label,
                        "detail": c.detail,
                        "fix": c.fix,
                    }
                    for c in s.checks
                ],
            }
            for s in sections
        ],
        "failures": sum(
            1 for s in sections for c in s.checks if c.status == FAIL
        ),
        "warnings": sum(
            1 for s in sections for c in s.checks if c.status == WARN
        ),
    }


def _run_doctor(args: argparse.Namespace) -> list[Section]:
    base_url = _hermes_base_url(getattr(args, "base_url", None))
    network = not getattr(args, "no_network", False)
    return [
        _check_hermes(base_url, network=network),
        _check_patter(),
        _check_twilio(network=network),
    ]


# ──────────────────────────────────────────────────────────────────────────
# Subcommands
# ──────────────────────────────────────────────────────────────────────────
def cmd_doctor(args: argparse.Namespace) -> int:
    sections = _run_doctor(args)
    report = _sections_to_dict(sections)
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
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


def cmd_setup(args: argparse.Namespace) -> int:
    from getpatter import _hermes_scaffold

    target = Path(args.dir).resolve()
    interactive = sys.stdin.isatty() and not args.yes

    print(f"Patter + Hermes setup\n  project: {target}\n")

    # 1. Preflight.
    print("Checking your environment…")
    sections = _run_doctor(args)
    _print_sections(sections)
    failures = sum(1 for s in sections for c in s.checks if c.status == FAIL)
    print()

    # 2. Scaffold the project.
    if interactive and not _confirm(f"Scaffold the project into {target}?"):
        print("Skipped scaffolding.")
    else:
        written = _hermes_scaffold.scaffold(target, force=args.force)
        if written:
            print(f"Wrote {len(written)} file(s):")
            for p in written:
                print(f"  + {p.relative_to(target)}")
        else:
            print("Project already exists (use --force to overwrite).")
        env_path = target / ".env"
        if not env_path.exists():
            (env_path).write_text(
                (target / ".env.example").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            print("  + .env (from .env.example — fill in your keys)")
    print()

    # 3. Optionally attach a Twilio number.
    if args.number and args.url:
        print(f"Attaching {args.number} → {args.url}")
        rc = _attach_number(args.number, args.url, args.status_callback)
        if rc != 0:
            return rc
    elif args.number or args.url:
        print(
            "Note: pass both --number and --url to auto-configure the Twilio "
            "webhook, or run `patter hermes attach-number` later."
        )

    # 4. Next steps.
    print("\nNext steps:")
    print(f"  cd {target}")
    print("  # edit .env with your keys")
    print("  python scripts/test_text_turn.py   # smoke-test the Hermes brain")
    print("  python app.py                      # answer the phone")
    if not (args.number and args.url):
        print(
            "  patter hermes attach-number <number> --url <tunnel-url>/calls/inbound"
        )
    return 1 if failures else 0


def cmd_attach_number(args: argparse.Namespace) -> int:
    return _attach_number(args.number, args.url, args.status_callback)


def cmd_numbers(args: argparse.Namespace) -> int:
    sid, token = _twilio_creds()
    if not sid or not token:
        print(
            "Twilio credentials not found. Set TWILIO_ACCOUNT_SID and "
            "TWILIO_AUTH_TOKEN.",
            file=sys.stderr,
        )
        return 2
    try:
        import httpx

        resp = httpx.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json",
            auth=(sid, token),
            params={"PageSize": 50},
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Twilio API error: {exc}", file=sys.stderr)
        return 1
    if resp.status_code != 200:
        print(f"Twilio returned HTTP {resp.status_code}", file=sys.stderr)
        return 1
    rows = resp.json().get("incoming_phone_numbers", [])
    if not rows:
        print("No phone numbers on this account.")
        return 0
    print(f"{len(rows)} number(s):")
    for r in rows:
        num = r.get("phone_number", "?")
        url = r.get("voice_url", "") or "(no voice webhook)"
        print(f"  {num}  →  {url}")
    return 0


# ──────────────────────────────────────────────────────────────────────────
# Twilio helpers
# ──────────────────────────────────────────────────────────────────────────
def _twilio_creds() -> tuple[str, str]:
    return (
        os.environ.get("TWILIO_ACCOUNT_SID", ""),
        os.environ.get("TWILIO_AUTH_TOKEN", ""),
    )


def _attach_number(number: str, url: str, status_callback: str | None) -> int:
    """Set a Twilio number's voice webhook. Returns a process exit code."""
    sid, token = _twilio_creds()
    if not sid or not token:
        print(
            "Twilio credentials not found. Set TWILIO_ACCOUNT_SID and "
            "TWILIO_AUTH_TOKEN.",
            file=sys.stderr,
        )
        return 2
    if not url.lower().startswith("https://"):
        print(f"Webhook URL must be https:// (got {url!r})", file=sys.stderr)
        return 2
    try:
        import httpx
    except ImportError:  # pragma: no cover
        print("httpx is required for attach-number.", file=sys.stderr)
        return 1

    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
    # Resolve the number's SID.
    try:
        lookup = httpx.get(
            f"{base}/IncomingPhoneNumbers.json",
            params={"PhoneNumber": number},
            auth=(sid, token),
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Twilio API error: {exc}", file=sys.stderr)
        return 1
    if lookup.status_code != 200:
        print(f"Twilio returned HTTP {lookup.status_code}", file=sys.stderr)
        return 1
    rows = lookup.json().get("incoming_phone_numbers", [])
    if not rows:
        print(
            f"{number} is not on this account. Run `patter hermes numbers` to "
            "list available numbers.",
            file=sys.stderr,
        )
        return 1
    number_sid = rows[0].get("sid")

    data = {"VoiceUrl": url, "VoiceMethod": "POST"}
    if status_callback:
        data["StatusCallback"] = status_callback
        data["StatusCallbackMethod"] = "POST"
    try:
        upd = httpx.post(
            f"{base}/IncomingPhoneNumbers/{number_sid}.json",
            data=data,
            auth=(sid, token),
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Twilio API error: {exc}", file=sys.stderr)
        return 1
    if upd.status_code in (200, 201):
        print(f"✓ {number} voice webhook → {url}")
        if status_callback:
            print(f"✓ status callback → {status_callback}")
        return 0
    print(
        f"Failed to update webhook: HTTP {upd.status_code} {upd.text[:200]}",
        file=sys.stderr,
    )
    return 1


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [Y/n] ").strip().lower() in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ──────────────────────────────────────────────────────────────────────────
# Parser wiring
# ──────────────────────────────────────────────────────────────────────────
def build_hermes_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach the ``hermes`` subcommand tree to the parent CLI."""
    hermes = subparsers.add_parser(
        "hermes",
        help="Set up, diagnose, and wire the Hermes voice shell",
    )
    hsub = hermes.add_subparsers(dest="hermes_command")

    doctor = hsub.add_parser("doctor", help="Preflight checks for the Hermes voice shell")
    doctor.add_argument("--base-url", default=None, help="Hermes gateway base URL")
    doctor.add_argument("--no-network", action="store_true", help="Skip live probes")
    doctor.add_argument("--json", action="store_true", help="Machine-readable output")

    setup = hsub.add_parser("setup", help="Scaffold a hermes-phone-agent project")
    setup.add_argument("--dir", default="hermes-phone-agent", help="Target directory")
    setup.add_argument("--force", action="store_true", help="Overwrite existing files")
    setup.add_argument("--yes", action="store_true", help="Non-interactive (assume yes)")
    setup.add_argument("--number", default=None, help="Twilio number to attach")
    setup.add_argument("--url", default=None, help="Public webhook URL to attach")
    setup.add_argument("--status-callback", default=None, help="Twilio status callback URL")
    setup.add_argument("--base-url", default=None, help="Hermes gateway base URL")
    setup.add_argument("--no-network", action="store_true", help="Skip live probes")

    attach = hsub.add_parser("attach-number", help="Point a Twilio number at your Patter URL")
    attach.add_argument("number", help="Phone number in E.164 (e.g. +15551234567)")
    attach.add_argument("--url", required=True, help="Public voice webhook URL (https)")
    attach.add_argument("--status-callback", default=None, help="Twilio status callback URL")

    hsub.add_parser("numbers", help="List the Twilio numbers on your account")
    return hermes


def dispatch_hermes(args: argparse.Namespace) -> int:
    """Entry for ``patter hermes ...``. Returns a process exit code."""
    command = getattr(args, "hermes_command", None)
    if command == "doctor":
        return cmd_doctor(args)
    if command == "setup":
        return cmd_setup(args)
    if command == "attach-number":
        return cmd_attach_number(args)
    if command == "numbers":
        return cmd_numbers(args)
    print(
        "Usage: patter hermes {doctor|setup|attach-number|numbers}\n"
        "Try:   patter hermes doctor",
        file=sys.stderr,
    )
    return 2
