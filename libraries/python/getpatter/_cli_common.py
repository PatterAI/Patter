"""Shared, provider-agnostic helpers for the Patter setup wizards.

Both ``patter hermes ...`` (:mod:`getpatter.cli_hermes`) and
``patter openclaw ...`` (:mod:`getpatter.cli_openclaw`) are the same shape — a
preflight ``doctor``, a project ``setup`` scaffold, a chat-turn acceptance
``test``, and Twilio number wiring. The pieces that do not depend on which
agent runtime is the brain live here so the two wizards cannot drift:

* the ``Check`` / ``Section`` result model and its rendering,
* a best-effort sync HTTP probe (``_get_json``),
* dotenv parsing / upsert / autoload,
* the Twilio helpers (``_attach_number`` / ``cmd_numbers`` / ``_check_twilio``),
* the generic provider readiness checks (STT / TTS) and the OpenAI-compatible
  ``/chat/completions`` acceptance probe (``_chat_turn_check``).

Provider-specific logic (gateway config, the ``_check_<provider>`` section, the
gateway enable/start steps) stays in each wizard module.
"""

from __future__ import annotations

import importlib.util
import os
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
# .env helpers
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


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [Y/n] ").strip().lower() in ("", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


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


def _model_ids(body) -> set[str]:
    """Extract model ids from an OpenAI-style ``/models`` payload."""
    if not isinstance(body, dict):
        return set()
    data = body.get("data")
    if not isinstance(data, list):
        return set()
    return {m.get("id") for m in data if isinstance(m, dict) and m.get("id")}


def _env_key(var: str, label: str) -> Check:
    if os.environ.get(var):
        return Check(OK, f"{label} key found")
    return Check(
        WARN,
        f"{label} key missing",
        f"{var} not set",
        f"export {var}=...",
    )


def _provider_checks() -> list[Check]:
    """Generic STT/TTS readiness checks shared by every voice-shell wizard."""
    checks = [
        _env_key("DEEPGRAM_API_KEY", "Deepgram STT"),
        _env_key("ELEVENLABS_API_KEY", "ElevenLabs TTS"),
    ]

    transport = os.environ.get("PATTER_ELEVENLABS_TRANSPORT", "").lower()
    if transport == "rest":
        checks.append(Check(OK, "ElevenLabs transport", "rest"))
    elif transport == "ws":
        checks.append(
            Check(
                WARN,
                "ElevenLabs transport",
                "ws — WebSocket can stall before the first frame on PSTN",
                "PATTER_ELEVENLABS_TRANSPORT=rest for a more robust demo",
            )
        )
    else:
        checks.append(
            Check(OK, "ElevenLabs transport", "unset (example defaults to REST)")
        )

    if importlib.util.find_spec("onnxruntime") is not None:
        checks.append(Check(OK, "Silero VAD available"))
    else:
        checks.append(
            Check(
                WARN,
                "Silero VAD missing",
                "only needed for the pipeline VAD",
                'pip install "getpatter[silero]"',
            )
        )
    return checks


# ──────────────────────────────────────────────────────────────────────────
# OpenAI-compatible chat-turn acceptance probe
# ──────────────────────────────────────────────────────────────────────────
def _chat_turn_check(
    base_url: str,
    key: str,
    model: str,
    prompt: str,
    *,
    session_header: str = "",
    session_value: str = "",
    doctor_hint: str = "",
) -> Check:
    """Send one ``/chat/completions`` turn and grade the reply.

    ``session_header`` / ``session_value`` carry the runtime's per-call session
    continuity header (Hermes ``X-Hermes-Session-Id`` / OpenClaw
    ``x-openclaw-session-key``); ``doctor_hint`` is the fix command to suggest
    on a connection failure.
    """
    import time

    try:
        import httpx
    except ImportError:  # pragma: no cover
        return Check(SKIP, "Chat turn", "httpx not installed")
    headers = {"Content-Type": "application/json"}
    if session_header:
        headers[session_header] = session_value
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    start = time.monotonic()
    try:
        resp = httpx.post(
            f"{base_url}/chat/completions", json=payload, headers=headers, timeout=120.0
        )
    except Exception as exc:  # noqa: BLE001
        return Check(FAIL, "Chat turn", str(exc), doctor_hint)
    elapsed = int((time.monotonic() - start) * 1000)
    if resp.status_code != 200:
        return Check(
            FAIL,
            "Chat turn",
            f"HTTP {resp.status_code}: {resp.text[:160]}",
            "check the model name and the gateway API key",
        )
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        return Check(FAIL, "Chat turn", "200 but no choices[0].message.content")
    snippet = " ".join((content or "").split())[:60]
    if not snippet:
        return Check(WARN, "Chat turn", f"empty reply ({elapsed} ms)")
    return Check(OK, "Chat turn", f'{elapsed} ms — "{snippet}…"')


# ──────────────────────────────────────────────────────────────────────────
# Twilio helpers
# ──────────────────────────────────────────────────────────────────────────
def _twilio_creds() -> tuple[str, str]:
    return (
        os.environ.get("TWILIO_ACCOUNT_SID", ""),
        os.environ.get("TWILIO_AUTH_TOKEN", ""),
    )


def _check_twilio(*, network: bool, cli_name: str, check_webhook: bool = True) -> Section:
    """Carrier-credential + webhook readiness. ``cli_name`` parameterises the
    suggested fix command (``patter <cli_name> attach-number ...``).

    ``check_webhook=False`` stops after credential validity — for an outbound
    dialer the number is the caller ID, so no inbound voice webhook is needed.
    """
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

    if not check_webhook:
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
        rows = (
            resp.json().get("incoming_phone_numbers", [])
            if resp.status_code == 200
            else []
        )
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
                f"patter {cli_name} attach-number {number} "
                "--url https://<tunnel>/calls/inbound",
            )
        )
    return sec


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
            f"{number} is not on this account. Run `patter ... numbers` to "
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


def cmd_numbers(args) -> int:
    """List the Twilio numbers on the account (shared by every wizard)."""
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
