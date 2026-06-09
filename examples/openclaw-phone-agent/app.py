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
