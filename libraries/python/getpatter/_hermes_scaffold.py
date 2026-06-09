"""Project scaffold for the Hermes phone agent.

Single source of truth for the ``hermes-phone-agent`` starter project. The
``patter hermes setup`` wizard (see :mod:`getpatter.cli_hermes`) writes these
files for a user, and the committed ``examples/hermes-phone-agent/`` tree is
generated from the same :data:`FILES` map (a test asserts they stay in sync).

Each entry maps a project-relative path to its file contents. Keep the contents
runnable against the real public API — they double as the example the docs
point at.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["FILES", "scaffold"]


_APP_PY = '''\
"""Hermes phone agent — Patter is the voice shell, Hermes is the brain.

A caller dials your number, Patter answers (carrier + STT + turn-taking + TTS),
and every conversation turn is routed to your local Hermes gateway as the LLM.
Hermes stays on loopback (127.0.0.1:8642); only Patter's carrier webhook is
exposed to the internet, via the tunnel.

Run:
    python app.py

Check your setup first with:
    patter hermes doctor
"""

from __future__ import annotations

import os

from getpatter import (
    DeepgramSTT,
    ElevenLabsRestTTS,
    ElevenLabsTTS,
    HermesLLM,
    Patter,
    Twilio,
)

# REST TTS is the safer default for a first PSTN demo: there is no long-lived
# WebSocket that can stall before the first audio frame. Set
# PATTER_ELEVENLABS_TRANSPORT=ws to opt into streaming once the basics work.
if os.environ.get("PATTER_ELEVENLABS_TRANSPORT", "rest").lower() == "ws":
    tts = ElevenLabsTTS.for_twilio()
else:
    tts = ElevenLabsRestTTS.for_twilio()

phone = Patter(
    carrier=Twilio(),                       # TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN
    phone_number=os.environ["PATTER_PHONE_NUMBER"],
    tunnel=True,                            # auto Cloudflare quick-tunnel (local dev)
)

agent = phone.agent(
    system_prompt=(
        "You are Hermes on a live phone call. Keep replies concise, warm, and "
        "spoken-friendly. Avoid markdown, code blocks, long lists, and URLs "
        "unless the caller asks. If you use a tool, say you are checking, then "
        "summarize the result naturally. If interrupted, stop and answer the "
        "latest request."
    ),
    language=os.environ.get("PATTER_LANGUAGE", "en"),
    first_message="Hello, this is Hermes. How can I help?",
    stt=DeepgramSTT(),                      # DEEPGRAM_API_KEY
    llm=HermesLLM(session_key_from="caller_hash"),   # http://127.0.0.1:8642/v1
    tts=tts,                                # ELEVENLABS_API_KEY
    long_turn_message="One moment, let me check that.",
    llm_error_message="Sorry, I'm having trouble reaching Hermes right now.",
)

if __name__ == "__main__":
    phone.serve(agent)                      # answers inbound calls
'''


_ENV_EXAMPLE = """\
# ── Hermes gateway (the brain — keep it on loopback) ──────────────────
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=choose-a-strong-key
API_SERVER_MODEL_NAME=hermes-agent

# ── Patter (the voice shell) ──────────────────────────────────────────
PATTER_PHONE_NUMBER=+15551234567
PATTER_LANGUAGE=en
# REST is the safer default for a first PSTN demo; set to ws for streaming.
PATTER_ELEVENLABS_TRANSPORT=rest
# Per-call logs — enables `patter hermes trace` / `patter hermes diagnose`.
PATTER_LOG_DIR=./patter-logs

# ── Twilio carrier ────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-twilio-auth-token

# ── STT / TTS providers ───────────────────────────────────────────────
DEEPGRAM_API_KEY=your-deepgram-key
ELEVENLABS_API_KEY=your-elevenlabs-key
# ELEVENLABS_VOICE_ID=EXAVITQu4vr4xnSDxMaL
"""


_README = """\
# Hermes phone agent

A self-hosted phone line for your [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Patter is the **voice shell** (carrier, speech-to-text, turn-taking, barge-in,
text-to-speech); Hermes is the **brain** on the line. Each conversation turn is
one `POST http://127.0.0.1:8642/v1/chat/completions` against your local Hermes
gateway — so Hermes keeps its tools, memory, and skills, and **never leaves
loopback**. The only thing exposed to the internet is Patter's carrier webhook.

## 1. Configure

```bash
cp .env.example .env
# then fill in API_SERVER_KEY, TWILIO_*, DEEPGRAM_API_KEY, ELEVENLABS_API_KEY,
# and PATTER_PHONE_NUMBER
```

## 2. Check everything is wired up

```bash
pip install getpatter
patter hermes doctor
```

Fix anything it flags (it prints a suggested command for each problem), then
smoke-test the brain without spending a phone call:

```bash
python scripts/test_text_turn.py "say hello in one sentence"
```

## 3. Answer the phone

```bash
python app.py
```

Patter opens a tunnel and prints the public webhook URL. Point your Twilio
number's voice webhook at it — or let Patter do it for you:

```bash
patter hermes attach-number "$PATTER_PHONE_NUMBER" --url https://<your-tunnel>/calls/inbound
```

Now call your number and talk to Hermes.

## 4. Place an outbound call (optional)

```bash
python scripts/test_outbound_call.py +15557654321
```

## Debug a call

With `PATTER_LOG_DIR` set (see `.env`), Patter writes a per-call log. After a
call, inspect what happened stage by stage, or get a one-line verdict:

```bash
patter hermes trace        # latest call: carrier → STT → Hermes → TTS + latency
patter hermes diagnose     # "Hermes replied but no audio — TTS stage" + fix
```

Before placing a call at all, confirm the brain answers and providers are ready:

```bash
patter hermes test         # /v1/models + a real chat turn + provider keys
```

## Why Patter instead of a hosted custom-LLM voice agent?

- **Hermes stays private.** A hosted platform has to reach your "brain" endpoint
  over the public internet; here Hermes is loopback-only and only Patter is
  exposed.
- **You own the voice layer** — STT, turn-taking, barge-in, TTS — and can script it.
- **Inbound *and* outbound**, plus the Patter MCP server so Hermes can place calls.
"""


_DOCKER_COMPOSE = """\
# Patter + Hermes on one box. Hermes stays on loopback; only Patter is exposed.
#
# This runs the Patter voice shell in a container that shares the host network
# so it can reach the Hermes gateway on 127.0.0.1:8642. Start your Hermes
# gateway on the host first (see the Hermes docs), then `docker compose up`.
services:
  patter:
    image: python:3.12-slim
    working_dir: /app
    env_file: .env
    network_mode: host          # so Patter reaches Hermes on 127.0.0.1:8642
    volumes:
      - .:/app
    command: sh -c "pip install --quiet getpatter && python app.py"
"""


_SCRIPT_DOCTOR = '''\
#!/usr/bin/env python
"""Run the Patter Hermes preflight checks (wraps `patter hermes doctor`)."""

import subprocess
import sys

raise SystemExit(subprocess.call(["patter", "hermes", "doctor", *sys.argv[1:]]))
'''


_SCRIPT_TEXT_TURN = '''\
#!/usr/bin/env python
"""Text-only smoke test: send one turn to the local Hermes gateway.

Verifies the brain answers before you spend a phone call debugging it. Reads
API_SERVER_PORT / API_SERVER_MODEL_NAME / API_SERVER_KEY from the environment.

    python scripts/test_text_turn.py "say hello in one short sentence"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

base = f"http://127.0.0.1:{os.environ.get('API_SERVER_PORT', '8642')}/v1"
model = os.environ.get("API_SERVER_MODEL_NAME", "hermes-agent")
key = os.environ.get("API_SERVER_KEY", "")
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
    print(f"Hermes did not answer: {exc}")
    raise SystemExit(1)

print(data["choices"][0]["message"]["content"])
'''


_SCRIPT_OUTBOUND = '''\
#!/usr/bin/env python
"""Place a test outbound call through the Hermes voice shell.

    python scripts/test_outbound_call.py +15557654321

The callee picks up and talks to Hermes. Requires the same env as app.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

from getpatter import (
    DeepgramSTT,
    ElevenLabsRestTTS,
    HermesLLM,
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
        phone_number=os.environ["PATTER_PHONE_NUMBER"],
        tunnel=True,
    )
    agent = phone.agent(
        system_prompt=(
            "You are Hermes on a short test call. Greet the person warmly and "
            "ask how they are. Keep it brief and spoken-friendly."
        ),
        first_message="Hi, this is a Patter and Hermes test call.",
        stt=DeepgramSTT(),
        llm=HermesLLM(session_key_from="caller_hash"),
        tts=ElevenLabsRestTTS.for_twilio(),
    )
    result = await phone.call(to, agent=agent, wait=True)
    print(f"Call outcome: {result.outcome if result else 'unknown'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
'''


# Project-relative path -> file contents. The committed
# ``examples/hermes-phone-agent/`` tree is generated from this map.
FILES: dict[str, str] = {
    "app.py": _APP_PY,
    ".env.example": _ENV_EXAMPLE,
    "README.md": _README,
    "docker-compose.yml": _DOCKER_COMPOSE,
    "scripts/doctor.py": _SCRIPT_DOCTOR,
    "scripts/test_text_turn.py": _SCRIPT_TEXT_TURN,
    "scripts/test_outbound_call.py": _SCRIPT_OUTBOUND,
}


def scaffold(target_dir: Path | str, *, force: bool = False) -> list[Path]:
    """Write the project files under ``target_dir``.

    Args:
        target_dir: Destination directory (created if missing).
        force: Overwrite existing files. When ``False`` (default), existing
            files are left untouched and skipped.

    Returns:
        The list of paths that were written (skipped files are excluded).
    """
    root = Path(target_dir)
    written: list[Path] = []
    for rel, content in FILES.items():
        dest = root / rel
        if dest.exists() and not force:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(dest)
    return written
