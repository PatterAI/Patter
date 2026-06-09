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
# .env / Hermes config helpers
# ──────────────────────────────────────────────────────────────────────────
def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` dotenv file. Ignores blanks, comments, ``export``.

    Surrounding single/double quotes are stripped. Returns an empty dict if the
    file is missing or unreadable.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def _hermes_home() -> Path:
    """Hermes config dir — ``$HERMES_HOME`` or ``~/.hermes`` (override for tests)."""
    override = os.environ.get("HERMES_HOME")
    return Path(override) if override else Path.home() / ".hermes"


def _read_hermes_config() -> dict[str, str]:
    """Read Hermes ``api_server`` settings from ``~/.hermes/.env`` and
    ``~/.hermes/config.yaml``.

    Returns a flat dict that may include ``API_SERVER_ENABLED``,
    ``API_SERVER_KEY``, ``API_SERVER_HOST``, ``API_SERVER_PORT``, and
    ``API_SERVER_MODEL_NAME``. The ``.env`` file wins over ``config.yaml``.
    Returns an empty dict when ``~/.hermes`` is absent.
    """
    home = _hermes_home()
    if not home.exists():
        return {}
    cfg: dict[str, str] = {}
    yaml_path = home / "config.yaml"
    if yaml_path.exists():
        try:
            import yaml

            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            api = data.get("api_server") if isinstance(data, dict) else None
            if isinstance(api, dict):
                _map = {
                    "enabled": "API_SERVER_ENABLED",
                    "key": "API_SERVER_KEY",
                    "host": "API_SERVER_HOST",
                    "port": "API_SERVER_PORT",
                    "model_name": "API_SERVER_MODEL_NAME",
                }
                for src, dst in _map.items():
                    if src in api and api[src] is not None:
                        cfg[dst] = str(api[src])
        except Exception:  # noqa: BLE001 - malformed yaml shouldn't crash doctor
            pass
    cfg.update(_parse_env_file(home / ".env"))  # .env overrides config.yaml
    return cfg


def _env_files_to_load(
    explicit: list[str] | None, *, project_dir: Path | None
) -> list[Path]:
    """Resolve which dotenv files to autoload, in increasing priority order."""
    if explicit:
        return [Path(p) for p in explicit]
    chain: list[Path] = [_hermes_home() / ".env"]
    if project_dir is not None:
        chain.append(project_dir / ".env")
    chain.append(Path.cwd() / ".env")
    return chain


def _load_env_files(paths: list[Path], *, override: bool = False) -> list[Path]:
    """Load dotenv files into ``os.environ``. Returns the files actually applied.

    Later paths win over earlier ones. Existing ``os.environ`` values are kept
    unless ``override`` is set.
    """
    applied: list[Path] = []
    for path in paths:
        values = _parse_env_file(path)
        if not values:
            continue
        for key, val in values.items():
            if override or key not in os.environ:
                os.environ[key] = val
        applied.append(path)
    return applied


def _upsert_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set ``KEY=VALUE`` pairs in a dotenv file, preserving other lines.

    Existing keys are replaced in place; new keys are appended. Creates the file
    (and parent dir) if missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, val in remaining.items():
        lines.append(f"{key}={val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _generate_key() -> str:
    """A strong, URL-safe API key."""
    import secrets

    return secrets.token_urlsafe(32)


def _hermes_gateway_status() -> Check | None:
    """Best-effort ``hermes gateway status`` probe. ``None`` if CLI absent."""
    if not shutil.which("hermes"):
        return None
    import subprocess

    try:
        proc = subprocess.run(
            ["hermes", "gateway", "status"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:  # noqa: BLE001 - missing subcommand / timeout
        return None
    if proc.returncode == 0:
        first = (proc.stdout or proc.stderr or "").strip().splitlines()
        return Check(OK, "Gateway service", first[0] if first else "status ok")
    return Check(
        WARN,
        "Gateway service",
        "hermes gateway status reported a problem",
        "hermes gateway start",
    )


# ──────────────────────────────────────────────────────────────────────────
# Check groups
# ──────────────────────────────────────────────────────────────────────────
def _check_hermes(base_url: str, *, network: bool) -> Section:
    sec = Section("Hermes")
    have_cli = bool(shutil.which("hermes"))
    hermes_cfg = _read_hermes_config()
    home = _hermes_home()

    # CLI presence (severity finalised at the end once we know gateway state).
    if have_cli:
        sec.checks.append(Check(OK, "CLI found", "hermes on PATH"))
    else:
        sec.checks.append(
            Check(
                WARN,
                "CLI not found",
                "optional when the gateway is already running",
                "install Hermes: https://github.com/NousResearch/hermes-agent",
            )
        )

    # Hermes-side config (~/.hermes/.env + config.yaml), read directly.
    if hermes_cfg:
        enabled = hermes_cfg.get("API_SERVER_ENABLED", "").lower()
        if enabled in ("true", "1", "yes"):
            sec.checks.append(
                Check(OK, "API server enabled", f"API_SERVER_ENABLED=true in {home}")
            )
        elif enabled:
            sec.checks.append(
                Check(
                    WARN,
                    "API server disabled",
                    f"API_SERVER_ENABLED={enabled!r} in {home}",
                    "patter hermes setup --enable-hermes",
                )
            )
        else:
            sec.checks.append(
                Check(
                    WARN,
                    "API server flag absent",
                    f"API_SERVER_ENABLED not set in {home}",
                    "patter hermes setup --enable-hermes",
                )
            )
    else:
        sec.checks.append(
            Check(SKIP, "Hermes config", f"no {home} directory found")
        )

    # Gateway service status via the CLI (best-effort).
    gw_status = _hermes_gateway_status()
    if gw_status is not None:
        sec.checks.append(gw_status)

    # A key may live in the process env or in ~/.hermes — accept either.
    key = os.environ.get("API_SERVER_KEY", "") or hermes_cfg.get("API_SERVER_KEY", "")
    if key:
        sec.checks.append(Check(OK, "API_SERVER_KEY set"))
    else:
        sec.checks.append(
            Check(
                WARN,
                "API_SERVER_KEY not set",
                "keyless local gateways work, but a key is recommended",
                "patter hermes setup --generate-key",
            )
        )

    if not network:
        sec.checks.append(Check(SKIP, "Gateway reachable", "skipped (--no-network)"))
        return sec

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    status, body, err = _get_json(f"{base_url}/models", headers=headers)
    if status == 200:
        sec.checks.append(Check(OK, "Gateway reachable", base_url))
        want = os.environ.get("API_SERVER_MODEL_NAME") or hermes_cfg.get(
            "API_SERVER_MODEL_NAME", "hermes-agent"
        )
        ids = _model_ids(body)
        if want in ids:
            sec.checks.append(Check(OK, "Model available", want))
        elif ids:
            sec.checks.append(
                Check(
                    WARN,
                    "Model not found",
                    f"{want!r} missing; saw {', '.join(sorted(ids)[:5])}",
                    "set API_SERVER_MODEL_NAME to one of the served models",
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
        # Gateway down with no CLI to start it is a hard stop — promote the
        # CLI-not-found note to a failure so the verdict isn't a soft warning.
        fix = (
            "hermes gateway start"
            if have_cli
            else "install Hermes + start the gateway (API_SERVER_ENABLED=true)"
        )
        sec.checks.append(
            Check(
                FAIL,
                "Gateway unreachable",
                f"{base_url} — {detail}",
                fix + ", or pass --base-url",
            )
        )
        if not have_cli:
            for c in sec.checks:
                if c.label == "CLI not found":
                    c.status = FAIL
                    c.detail = "and the gateway is unreachable"
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
def _apply_env(args: argparse.Namespace, *, project_dir: Path | None = None) -> list[Path]:
    """Autoload dotenv files for a command unless ``--no-env-file`` was passed."""
    if getattr(args, "no_env_file", False):
        return []
    paths = _env_files_to_load(getattr(args, "env_file", None), project_dir=project_dir)
    return _load_env_files(paths)


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


def cmd_setup(args: argparse.Namespace) -> int:
    from getpatter import _hermes_scaffold

    target = Path(args.dir).resolve()
    interactive = sys.stdin.isatty() and not args.yes

    print(f"Patter + Hermes setup\n  project: {target}\n")

    # 0. Optionally enable the Hermes API server in ~/.hermes/.env. This is the
    #    one step that writes to your Hermes install — explicit opt-in, backed up.
    gateway_key = ""
    if getattr(args, "enable_hermes", False):
        gateway_key = _enable_hermes_gateway()
        print()

    # 1. Load any existing env (project .env, ~/.hermes/.env) for the preflight.
    loaded = _apply_env(args, project_dir=target)
    if loaded:
        print("Loaded env from: " + ", ".join(str(p) for p in loaded))

    # 2. Preflight.
    print("Checking your environment…")
    sections = _run_doctor(args)
    _print_sections(sections)
    failures = sum(1 for s in sections for c in s.checks if c.status == FAIL)
    print()

    # 3. Scaffold the project.
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
        # Put an API_SERVER_KEY into the project .env. When we just enabled the
        # gateway, reuse ITS key so Patter and Hermes agree (a mismatch is a 401
        # at call time); otherwise generate a fresh one on --generate-key.
        if gateway_key:
            _upsert_env_file(env_path, {"API_SERVER_KEY": gateway_key})
            print("  + API_SERVER_KEY in .env (matches the gateway key)")
        elif getattr(args, "generate_key", False):
            existing = _parse_env_file(env_path).get("API_SERVER_KEY", "")
            if existing and existing != "choose-a-strong-key" and not args.force:
                print("  · API_SERVER_KEY already set (use --force to regenerate)")
            else:
                _upsert_env_file(env_path, {"API_SERVER_KEY": _generate_key()})
                print("  + API_SERVER_KEY generated in .env")
    print()

    # 4. Optionally attach a Twilio number.
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

    # 5. Next steps.
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
# Hermes gateway enablement (the one step that writes to ~/.hermes)
# ──────────────────────────────────────────────────────────────────────────
def _enable_hermes_gateway() -> str:
    """Write ``API_SERVER_ENABLED=true`` (+ a key if absent) to ``~/.hermes/.env``.

    Backs the file up to ``.env.bak`` first. Prints what it changed and reminds
    the operator to (re)start the gateway — Patter does not manage the service.
    Returns the gateway's ``API_SERVER_KEY`` (existing or freshly generated) so
    the caller can keep the project ``.env`` in sync with it.
    """
    home = _hermes_home()
    env_path = home / ".env"
    existing = _parse_env_file(env_path)

    if env_path.exists():
        backup = env_path.parent / (env_path.name + ".bak")
        backup.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Backed up {env_path} → {backup}")

    updates: dict[str, str] = {"API_SERVER_ENABLED": "true"}
    key = existing.get("API_SERVER_KEY", "")
    if not key:
        key = _generate_key()
        updates["API_SERVER_KEY"] = key
    _upsert_env_file(env_path, updates)

    print(f"✓ API_SERVER_ENABLED=true written to {env_path}")
    if "API_SERVER_KEY" in updates:
        print("✓ API_SERVER_KEY generated for the gateway")
    if shutil.which("hermes"):
        print("Now (re)start the gateway:  hermes gateway start")
    else:
        print(
            "Hermes CLI not found — start the gateway your usual way so the new "
            "settings take effect."
        )
    return key


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
    doctor.add_argument(
        "--env-file",
        action="append",
        default=None,
        help="dotenv file(s) to load (repeatable; default: ~/.hermes/.env + ./.env)",
    )
    doctor.add_argument(
        "--no-env-file", action="store_true", help="Do not autoload any .env file"
    )

    setup = hsub.add_parser("setup", help="Scaffold a hermes-phone-agent project")
    setup.add_argument("--dir", default="hermes-phone-agent", help="Target directory")
    setup.add_argument("--force", action="store_true", help="Overwrite existing files")
    setup.add_argument("--yes", action="store_true", help="Non-interactive (assume yes)")
    setup.add_argument("--number", default=None, help="Twilio number to attach")
    setup.add_argument("--url", default=None, help="Public webhook URL to attach")
    setup.add_argument("--status-callback", default=None, help="Twilio status callback URL")
    setup.add_argument("--base-url", default=None, help="Hermes gateway base URL")
    setup.add_argument("--no-network", action="store_true", help="Skip live probes")
    setup.add_argument(
        "--generate-key",
        action="store_true",
        help="Generate a strong API_SERVER_KEY into the project .env",
    )
    setup.add_argument(
        "--enable-hermes",
        action="store_true",
        help="Write API_SERVER_ENABLED=true (+ key) to ~/.hermes/.env (backed up)",
    )
    setup.add_argument(
        "--env-file", action="append", default=None, help="dotenv file(s) to load"
    )
    setup.add_argument(
        "--no-env-file", action="store_true", help="Do not autoload any .env file"
    )

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
