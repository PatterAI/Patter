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
