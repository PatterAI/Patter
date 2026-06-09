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
