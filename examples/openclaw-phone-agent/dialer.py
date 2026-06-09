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
