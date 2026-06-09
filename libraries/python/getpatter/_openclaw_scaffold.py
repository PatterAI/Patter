"""Project scaffold for the OpenClaw phone agent (inbound + outbound).

Single source of truth for the ``openclaw-phone-agent`` starter project. The
``patter openclaw setup`` wizard (see :mod:`getpatter.cli_openclaw`) writes the
files for the chosen ``--mode``, and the committed
``examples/openclaw-phone-agent/`` tree is generated from the same maps with
``mode="both"`` (a test asserts they stay in sync).

The shape mirrors the Hermes scaffold but covers OpenClaw's two directions with
one wiring: :data:`_AGENT_PY` builds the agent once (``OpenClawLLM`` is the
brain), ``app.py`` serves it inbound and ``dialer.py`` dials it outbound. The
``deploy/`` units make both run 24/7 under the OS service supervisor.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["FILES", "scaffold"]


_AGENT_PY = '''\
"""The OpenClaw agent that is the brain on the line.

One scoped OpenClaw agent powers both directions: app.py serves it to inbound
callers, dialer.py dials it outbound. Patter owns the voice (carrier, STT,
turn-taking, barge-in, TTS); every turn is one POST to the local OpenClaw
gateway's /v1/chat/completions as model "openclaw/<OPENCLAW_AGENT>".
"""

from __future__ import annotations

import os

from getpatter import DeepgramSTT, ElevenLabsRestTTS, ElevenLabsTTS, OpenClawLLM

_DEFAULT_SYSTEM_PROMPT = (
    "You are a phone receptionist. Keep replies concise, warm, and "
    "spoken-friendly. Avoid markdown, code blocks, long lists, and URLs unless "
    "the caller asks. If you use a tool, say you are checking, then summarize the "
    "result naturally. If interrupted, stop and answer the latest request."
)
_DEFAULT_FIRST_MESSAGE = "Hello, thanks for calling. How can I help?"


def build_agent(phone, *, system_prompt=None, first_message=None, agent=None):
    """Build the Patter agent backed by one scoped OpenClaw agent.

    Reads OPENCLAW_AGENT for the target (never the default/master). REST TTS is
    the safer default on PSTN; set PATTER_ELEVENLABS_TRANSPORT=ws for streaming.
    """
    if os.environ.get("PATTER_ELEVENLABS_TRANSPORT", "rest").lower() == "ws":
        tts = ElevenLabsTTS.for_twilio()
    else:
        tts = ElevenLabsRestTTS.for_twilio()

    return phone.agent(
        system_prompt=system_prompt or _DEFAULT_SYSTEM_PROMPT,
        language=os.environ.get("PATTER_LANGUAGE", "en"),
        first_message=first_message or _DEFAULT_FIRST_MESSAGE,
        stt=DeepgramSTT(),                                  # DEEPGRAM_API_KEY
        llm=OpenClawLLM(agent=agent or os.environ.get("OPENCLAW_AGENT", "receptionist")),
        tts=tts,                                            # ELEVENLABS_API_KEY
        long_turn_message="One moment, let me check that.",
        llm_error_message="Sorry, I'm having trouble reaching the system right now.",
    )
'''


_APP_PY = '''\
"""OpenClaw receptionist — Patter answers, one scoped OpenClaw agent is the brain.

A caller dials your number, Patter answers (carrier + STT + turn-taking + TTS),
and every turn is routed to your local OpenClaw gateway. OpenClaw stays on
loopback (127.0.0.1:18789); only Patter's carrier webhook is exposed.

phone.serve() blocks forever — it IS the 24/7 inbound process. For production,
run it under systemd/launchd so it restarts on crash/boot (see deploy/), and set
PATTER_WEBHOOK_URL to a STABLE named-tunnel hostname so restarts don't break the
carrier webhook.

Run:
    python app.py

Check your setup first with:
    patter openclaw doctor
"""

from __future__ import annotations

import os

from getpatter import Patter, Twilio

from agent import build_agent

webhook_url = os.environ.get("PATTER_WEBHOOK_URL", "")

phone = Patter(
    carrier=Twilio(),                       # TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN
    phone_number=os.environ["PATTER_PHONE_NUMBER"],
    webhook_url=webhook_url,                # set in production (stable named tunnel)
    tunnel=not webhook_url,                 # dev: auto Cloudflare quick-tunnel
)

if __name__ == "__main__":
    phone.serve(build_agent(phone))         # blocks forever — answers inbound calls
'''


_DIALER_PY = '''\
"""OpenClaw outbound dialer — Patter places calls, one OpenClaw agent is the brain.

A 24/7 AI-sales dialer: it keeps the embedded server up and calls each number in
numbers.txt with answering-machine detection + voicemail drop. This is a
LONG-RUNNING SUPERVISED PROCESS, not a batch — the infinite loop keeps the
process alive and the OS service (systemd/launchd, see deploy/) restarts it on
crash/boot. Do NOT wrap a one-shot async-with batch and expect it to stay up.

Compliance: outbound marketing calls are regulated. Honour consent / do-not-call
lists, disclose that the caller is an AI at the start (EU AI Act Art. 50), and
keep audio/transcripts in-region. This example dials sequentially and is
rate-limited on purpose — do not turn it into an aggressive auto-dialer.

Run:
    python dialer.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from getpatter import Patter, Twilio

from agent import build_agent

NUMBERS_FILE = os.environ.get("OPENCLAW_NUMBERS_FILE", "numbers.txt")
CALL_GAP_SECONDS = float(os.environ.get("OPENCLAW_CALL_GAP_SECONDS", "5"))
IDLE_POLL_SECONDS = 30.0


def load_numbers(path):
    """Read E.164 numbers from a file, skipping blanks and # comments."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [s.strip() for s in lines if s.strip().startswith("+")]


async def main():
    from_number = os.environ.get("PATTER_FROM_NUMBER") or os.environ["PATTER_PHONE_NUMBER"]
    webhook_url = os.environ.get("PATTER_WEBHOOK_URL", "")
    system_prompt = os.environ.get("OPENCLAW_SYSTEM_PROMPT") or (
        "You are an outbound assistant on a brief call. Introduce yourself, say in "
        "one sentence why you are calling, and be warm and spoken-friendly. You are "
        "an AI — say so if asked."
    )
    first_message = os.environ.get("OPENCLAW_FIRST_MESSAGE", "Hi, do you have a quick moment?")
    agent_id = os.environ.get("OPENCLAW_AGENT", "sales")

    # `async with` keeps the embedded server + tunnel up across the whole run; the
    # infinite loop keeps the PROCESS alive 24/7 (the OS service restarts on crash).
    async with Patter(
        carrier=Twilio(),
        phone_number=from_number,
        webhook_url=webhook_url,
        tunnel=not webhook_url,
    ) as phone:
        called = set()
        while True:
            pending = [n for n in load_numbers(NUMBERS_FILE) if n not in called]
            if not pending:
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue
            for to in pending:
                called.add(to)
                agent = build_agent(
                    phone,
                    system_prompt=system_prompt,
                    first_message=first_message,
                    agent=agent_id,
                )
                try:
                    result = await phone.call(
                        to,
                        agent=agent,
                        wait=True,
                        machine_detection=True,
                        voicemail_message="Sorry we missed you — we'll try again later.",
                    )
                    print(f"{to}: {result.outcome if result else 'unknown'}")
                except Exception as exc:  # noqa: BLE001
                    print(f"{to}: failed ({exc})")
                await asyncio.sleep(CALL_GAP_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
'''


_NUMBERS_EXAMPLE = """\
# Outbound dialer call list — one E.164 number per line.
# Lines starting with # are ignored. Honour consent / do-not-call lists before
# adding anyone here, and disclose the caller is an AI at the start of the call.
+15557654321
+15557654322
"""


_ENV_EXAMPLE = """\
# ── OpenClaw gateway (the brain — keep it on loopback) ────────────────
OPENCLAW_BASE_URL=http://127.0.0.1:18789/v1
OPENCLAW_API_KEY=choose-a-strong-key
# Which scoped OpenClaw agent is on the line — never the default/master.
OPENCLAW_AGENT=receptionist

# ── Patter (the voice shell) ──────────────────────────────────────────
PATTER_PHONE_NUMBER=+15551234567
PATTER_LANGUAGE=en
# REST is the safer default for a first PSTN demo; set to ws for streaming.
PATTER_ELEVENLABS_TRANSPORT=rest
# PRODUCTION: a STABLE named-tunnel hostname so restarts don't break the carrier
# webhook. Leave empty in dev to use the auto quick-tunnel.
# PATTER_WEBHOOK_URL=voice.example.com
# Per-call logs.
PATTER_LOG_DIR=./patter-logs

# ── Outbound dialer (mode: outbound) ──────────────────────────────────
# Caller ID for outbound calls (defaults to PATTER_PHONE_NUMBER).
# PATTER_FROM_NUMBER=+15551234567
# OPENCLAW_NUMBERS_FILE=numbers.txt
# OPENCLAW_CALL_GAP_SECONDS=5

# ── Twilio carrier ────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-twilio-auth-token

# ── STT / TTS providers ───────────────────────────────────────────────
DEEPGRAM_API_KEY=your-deepgram-key
ELEVENLABS_API_KEY=your-elevenlabs-key
# ELEVENLABS_VOICE_ID=EXAVITQu4vr4xnSDxMaL
"""


_README = """\
# OpenClaw phone agent (inbound receptionist + outbound dialer)

Give one scoped [OpenClaw](https://openclaw.ai) agent a phone with Patter.
Patter is the **voice shell** (carrier, speech-to-text, turn-taking, barge-in,
text-to-speech); the OpenClaw agent is the **brain** on the line. Every turn is
one `POST http://127.0.0.1:18789/v1/chat/completions` with
`model="openclaw/<OPENCLAW_AGENT>"` — so OpenClaw keeps its tools, memory, and
skills, and **never leaves loopback**. Only Patter's carrier webhook is exposed.

The same agent powers both directions:

- **Inbound** — `app.py` runs `phone.serve(...)`; Patter answers any caller.
- **Outbound** — `dialer.py` runs a supervised `phone.call(...)` loop over
  `numbers.txt` (24/7 AI-sales), with answering-machine detection + voicemail drop.

> **Pick a scoped agent, never the default/master.** The gateway credential
> authorises the whole gateway, so `OPENCLAW_AGENT` picks the persona, not a
> security boundary. Give the receptionist its own least-privileged
> `agents.list[]` entry with a tight tool allow/deny.

## 1. Configure

```bash
cp .env.example .env
# fill in OPENCLAW_API_KEY, OPENCLAW_AGENT, TWILIO_*, DEEPGRAM_API_KEY,
# ELEVENLABS_API_KEY, and PATTER_PHONE_NUMBER
```

Enable OpenClaw's OpenAI-compatible endpoint (disabled by default) and check
everything is wired up:

```bash
pip install getpatter
patter openclaw setup --enable-openclaw --agent receptionist   # flips chatCompletions.enabled
patter openclaw doctor                                         # lists agents, flags the default/master
patter openclaw agents                                         # the agents your gateway serves
python scripts/test_text_turn.py "say hello in one sentence"   # smoke-test the brain
```

## 2a. Answer the phone (inbound)

```bash
python app.py
patter openclaw attach-number "$PATTER_PHONE_NUMBER" --url https://<your-tunnel>/calls/inbound
```

## 2b. Dial out (outbound)

```bash
# edit numbers.txt (one E.164 per line), then:
python scripts/test_outbound_call.py +15557654321   # one test call
python dialer.py                                     # the 24/7 dialer loop
```

## 3. Run it 24/7 (always-on)

Patter is kept alive by an **OS service supervisor**, exactly like the OpenClaw
gateway. On a per-client always-on box you run three co-located services:

1. **OpenClaw gateway** — its own daemon (systemd / launchd), loopback `:18789`,
   `chatCompletions` enabled. The persistent agent's memory lives here.
2. **Patter voice-shell** — `app.py` (inbound) and/or `dialer.py` (outbound), as
   an OS service that restarts on crash and starts on boot. See `deploy/`:
   ```bash
   # systemd (Linux): edit the User/paths, then
   loginctl enable-linger "$USER"     # if running as a --user service
   systemctl --user enable --now patter-receptionist
   # launchd (macOS): cp deploy/com.patter.receptionist.plist ~/Library/LaunchAgents/ && launchctl load -w ...
   ```
3. **Named Cloudflare tunnel** — a STABLE hostname (the dev quick-tunnel rotates
   on restart and breaks the carrier webhook). Run `cloudflared` as a service and
   set `PATTER_WEBHOOK_URL` to it. See
   [production deployment](https://docs.getpatter.com/dev-tools/production-deployment).

Keep the OpenClaw gateway on loopback, turn the dashboard off or token it, and
verify the carrier signature at the boundary.

## Debug a call

```bash
patter openclaw test     # /v1/models + a real chat turn + provider keys
```

## Why Patter instead of OpenClaw's native voice plugin?

- **Anyone can call.** Patter answers the carrier leg for any caller and verifies
  the carrier signature; OpenClaw's native voice plugin is allowlist-only.
- **OpenClaw stays private** on loopback — only Patter is exposed.
- **You own the voice layer** (STT, turn-taking, barge-in, TTS) and can script it,
  inbound *and* outbound.
"""


_DOCKER_COMPOSE = """\
# Patter + OpenClaw on one box. OpenClaw stays on loopback; only Patter is exposed.
#
# Start your OpenClaw gateway on the host first (loopback 127.0.0.1:18789, with
# chatCompletions enabled), then `docker compose up`. The container shares the
# host network so Patter can reach the gateway on 127.0.0.1:18789.
services:
  receptionist:
    image: python:3.12-slim
    working_dir: /app
    env_file: .env
    network_mode: host          # so Patter reaches OpenClaw on 127.0.0.1:18789
    restart: unless-stopped     # 24/7 — restart on crash/boot
    volumes:
      - .:/app
    command: sh -c "pip install --quiet getpatter && python app.py"
"""


_SCRIPT_DOCTOR = '''\
#!/usr/bin/env python
"""Run the Patter OpenClaw preflight checks (wraps `patter openclaw doctor`)."""

import subprocess
import sys

raise SystemExit(subprocess.call(["patter", "openclaw", "doctor", *sys.argv[1:]]))
'''


_SCRIPT_TEXT_TURN = '''\
#!/usr/bin/env python
"""Text-only smoke test: send one turn to the local OpenClaw gateway.

Verifies the agent answers before you spend a phone call debugging it. Reads
OPENCLAW_BASE_URL / OPENCLAW_AGENT / OPENCLAW_API_KEY from the environment.

    python scripts/test_text_turn.py "say hello in one short sentence"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

base = os.environ.get("OPENCLAW_BASE_URL", "http://127.0.0.1:18789/v1")
agent = os.environ.get("OPENCLAW_AGENT", "receptionist")
model = agent if (":" in agent or "/" in agent) else f"openclaw/{agent}"
key = os.environ.get("OPENCLAW_API_KEY", "")
prompt = " ".join(sys.argv[1:]) or "Say hello in one short sentence."

headers = {"Content-Type": "application/json"}
if key:
    headers["Authorization"] = f"Bearer {key}"

req = urllib.request.Request(
    f"{base}/chat/completions",
    data=json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}]}
    ).encode(),
    headers=headers,
)

try:
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        data = json.load(resp)
except Exception as exc:  # noqa: BLE001
    print(f"OpenClaw did not answer: {exc}")
    raise SystemExit(1)

print(data["choices"][0]["message"]["content"])
'''


_SCRIPT_OUTBOUND = '''\
#!/usr/bin/env python
"""Place one test outbound call through the OpenClaw voice shell.

    python scripts/test_outbound_call.py +15557654321

The callee picks up and talks to the OpenClaw agent. Requires the same env as
app.py / dialer.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

from getpatter import (
    DeepgramSTT,
    ElevenLabsRestTTS,
    OpenClawLLM,
    Patter,
    Twilio,
)


async def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_outbound_call.py <+E164>")
        return 2
    to = sys.argv[1]
    phone = Patter(
        carrier=Twilio(),
        phone_number=os.environ.get("PATTER_FROM_NUMBER")
        or os.environ["PATTER_PHONE_NUMBER"],
        tunnel=True,
    )
    agent = phone.agent(
        system_prompt=(
            "You are on a short test call. Greet the person warmly and ask how "
            "they are. Keep it brief and spoken-friendly."
        ),
        first_message="Hi, this is a Patter and OpenClaw test call.",
        stt=DeepgramSTT(),
        llm=OpenClawLLM(agent=os.environ.get("OPENCLAW_AGENT", "sales")),
        tts=ElevenLabsRestTTS.for_twilio(),
    )
    result = await phone.call(to, agent=agent, wait=True)
    print(f"Call outcome: {result.outcome if result else 'unknown'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
'''


_DEPLOY_RECEPTIONIST_SERVICE = """\
# Patter OpenClaw receptionist — 24/7 inbound voice line (systemd).
#
# As a SYSTEM service: edit User= and the paths, then
#   sudo cp deploy/patter-receptionist.service /etc/systemd/system/
#   sudo systemctl enable --now patter-receptionist
# As a USER service (no root): cp to ~/.config/systemd/user/, then
#   loginctl enable-linger "$USER"   # run without a login session
#   systemctl --user enable --now patter-receptionist
[Unit]
Description=Patter OpenClaw receptionist (inbound 24/7)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=patter
WorkingDirectory=/home/patter/openclaw-phone-agent
EnvironmentFile=/home/patter/openclaw-phone-agent/.env
ExecStart=/usr/bin/env python app.py
Restart=always
RestartSec=3
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""


_DEPLOY_DIALER_SERVICE = """\
# Patter OpenClaw outbound dialer — 24/7 AI-sales (systemd).
#
# Edit User= and the paths, then install as a system or --user service (see the
# receptionist unit for the two install paths). The dialer is a supervised
# long-running loop; Restart=always brings it back after a crash or reboot.
[Unit]
Description=Patter OpenClaw outbound dialer (outbound 24/7)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=patter
WorkingDirectory=/home/patter/openclaw-phone-agent
EnvironmentFile=/home/patter/openclaw-phone-agent/.env
ExecStart=/usr/bin/env python dialer.py
Restart=always
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""


_DEPLOY_RECEPTIONIST_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Patter OpenClaw receptionist — launchd agent (macOS always-on Mac Mini model).
     Edit the WorkingDirectory + interpreter for your box, then:
       cp deploy/com.patter.receptionist.plist ~/Library/LaunchAgents/
       launchctl load -w ~/Library/LaunchAgents/com.patter.receptionist.plist -->
<plist version="1.0">
<dict>
  <key>Label</key><string>com.patter.receptionist</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python</string>
    <string>app.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/patter/openclaw-phone-agent</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/patter/openclaw-phone-agent/patter.out.log</string>
  <key>StandardErrorPath</key><string>/Users/patter/openclaw-phone-agent/patter.err.log</string>
</dict>
</plist>
"""


_DEPLOY_DIALER_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- Patter OpenClaw outbound dialer — launchd agent (macOS).
     Edit the WorkingDirectory + interpreter, then:
       cp deploy/com.patter.dialer.plist ~/Library/LaunchAgents/
       launchctl load -w ~/Library/LaunchAgents/com.patter.dialer.plist -->
<plist version="1.0">
<dict>
  <key>Label</key><string>com.patter.dialer</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python</string>
    <string>dialer.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/patter/openclaw-phone-agent</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/patter/openclaw-phone-agent/dialer.out.log</string>
  <key>StandardErrorPath</key><string>/Users/patter/openclaw-phone-agent/dialer.err.log</string>
</dict>
</plist>
"""


# Files shared by every mode.
_COMMON_FILES: dict[str, str] = {
    "agent.py": _AGENT_PY,
    ".env.example": _ENV_EXAMPLE,
    "README.md": _README,
    "scripts/doctor.py": _SCRIPT_DOCTOR,
    "scripts/test_text_turn.py": _SCRIPT_TEXT_TURN,
}

# Inbound-only (the receptionist that answers).
_INBOUND_FILES: dict[str, str] = {
    "app.py": _APP_PY,
    "docker-compose.yml": _DOCKER_COMPOSE,
    "deploy/patter-receptionist.service": _DEPLOY_RECEPTIONIST_SERVICE,
    "deploy/com.patter.receptionist.plist": _DEPLOY_RECEPTIONIST_PLIST,
}

# Outbound-only (the dialer that calls).
_OUTBOUND_FILES: dict[str, str] = {
    "dialer.py": _DIALER_PY,
    "numbers.example.txt": _NUMBERS_EXAMPLE,
    "scripts/test_outbound_call.py": _SCRIPT_OUTBOUND,
    "deploy/patter-dialer.service": _DEPLOY_DIALER_SERVICE,
    "deploy/com.patter.dialer.plist": _DEPLOY_DIALER_PLIST,
}


def _files_for_mode(mode: str) -> dict[str, str]:
    files = dict(_COMMON_FILES)
    if mode in ("inbound", "both"):
        files.update(_INBOUND_FILES)
    if mode in ("outbound", "both"):
        files.update(_OUTBOUND_FILES)
    return files


# The committed ``examples/openclaw-phone-agent/`` tree is generated with
# ``mode="both"`` (it carries every file; a test asserts they stay in sync).
FILES: dict[str, str] = _files_for_mode("both")


def scaffold(target_dir: Path | str, *, force: bool = False, mode: str = "inbound") -> list[Path]:
    """Write the project files for ``mode`` under ``target_dir``.

    Args:
        target_dir: Destination directory (created if missing).
        force: Overwrite existing files. When ``False`` (default), existing
            files are left untouched and skipped.
        mode: ``"inbound"`` (receptionist), ``"outbound"`` (dialer), or
            ``"both"``.

    Returns:
        The list of paths that were written (skipped files are excluded).
    """
    root = Path(target_dir)
    written: list[Path] = []
    for rel, content in _files_for_mode(mode).items():
        dest = root / rel
        if dest.exists() and not force:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(dest)
    return written
