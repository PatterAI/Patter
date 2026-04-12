"""ShipCall Demo — Standalone script to trigger a demo call.

Usage:
    python3 demo.py                  # Call DEV_PHONE_NUMBER from .env
    python3 demo.py +15559876543     # Call a specific number

This script starts the Patter embedded server, waits for it to be ready,
triggers an outbound call, and keeps running so the call can complete.
Press Ctrl+C to stop.
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from patter import Patter

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("shipcall.demo")


async def main():
    target_phone = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DEV_PHONE_NUMBER", "")
    if not target_phone:
        print("Usage: python3 demo.py [+1XXXXXXXXXX]")
        print("Or set DEV_PHONE_NUMBER in .env")
        sys.exit(1)

    if not target_phone.startswith("+"):
        print(f"Phone number must be in E.164 format (e.g., +15559876543), got: {target_phone}")
        sys.exit(1)

    # Validate required env vars
    required = {
        "TWILIO_ACCOUNT_SID": os.getenv("TWILIO_ACCOUNT_SID", ""),
        "TWILIO_AUTH_TOKEN": os.getenv("TWILIO_AUTH_TOKEN", ""),
        "TWILIO_PHONE_NUMBER": os.getenv("TWILIO_PHONE_NUMBER", ""),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        "WEBHOOK_URL": os.getenv("WEBHOOK_URL", ""),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in the values.")
        sys.exit(1)

    port = int(os.getenv("PORT", "8080"))

    phone = Patter(
        mode="local",
        twilio_sid=required["TWILIO_ACCOUNT_SID"],
        twilio_token=required["TWILIO_AUTH_TOKEN"],
        openai_key=required["OPENAI_API_KEY"],
        phone_number=required["TWILIO_PHONE_NUMBER"],
        webhook_url=required["WEBHOOK_URL"],
    )

    # -- Tool handlers (same mock data as app.py) --

    async def read_logs(arguments: dict, context: dict) -> str:
        return (
            "Last 5 log entries from auth-service:\n"
            "ERROR 08:42:01 - Redis connection refused at 10.0.1.5:6379\n"
            "ERROR 08:42:03 - Session validation failed: cache unavailable\n"
            "WARN  08:42:05 - Falling back to DB session store\n"
            "ERROR 08:42:07 - DB session query timeout after 5000ms\n"
            "FATAL 08:42:09 - Auth middleware returning 503 for all requests"
        )

    async def get_test_results(arguments: dict, context: dict) -> str:
        return (
            "Test suite: 47 passed, 3 failed\n"
            "FAIL test_session_cache - Expected 200, got 503\n"
            "FAIL test_login_flow - Timeout after 10s\n"
            "FAIL test_token_refresh - Redis connection refused"
        )

    async def suggest_fix(arguments: dict, context: dict) -> str:
        return (
            "Root cause: Redis instance at 10.0.1.5:6379 is unreachable.\n"
            "Fix: The REDIS_URL env var in production is pointing to the old "
            "instance. Update it to redis://10.0.2.3:6379 in your deploy config.\n"
            "One-liner: heroku config:set REDIS_URL=redis://10.0.2.3:6379"
        )

    agent = phone.agent(
        system_prompt=(
            "You are ShipCall, a senior engineer watching the patter-app/api repository. "
            "Event type: error_spike. "
            "You just detected: your deploy landed on production. 47 tests passing. "
            "But there's a new error spike in the auth service — "
            "12 failures in the last 5 minutes. "
            "Help the developer understand what happened and take action. "
            "Be concise, specific, and actionable. You're on a phone call, "
            "not writing an essay. If they ask you to dig into logs or suggest "
            "a fix, use your tools. Keep responses under 3 sentences unless "
            "reading back data from a tool."
        ),
        voice="alloy",
        first_message=(
            "Hey Francesco, your deploy just landed on production. "
            "47 tests passing, but I'm seeing a new error spike in the auth service. "
            "12 failures in the last 5 minutes. Want me to dig into the logs?"
        ),
        tools=[
            Patter.tool(
                name="read_logs",
                description="Read recent log entries from the affected service.",
                parameters={
                    "type": "object",
                    "properties": {
                        "service": {"type": "string", "description": "Service name"},
                        "lines": {"type": "number", "description": "Number of lines"},
                    },
                    "required": ["service"],
                },
                handler=read_logs,
            ),
            Patter.tool(
                name="get_test_results",
                description="Get the latest test suite results.",
                parameters={
                    "type": "object",
                    "properties": {
                        "suite": {"type": "string", "description": "Test suite name"},
                    },
                    "required": [],
                },
                handler=get_test_results,
            ),
            Patter.tool(
                name="suggest_fix",
                description="Analyze the issue and suggest a fix.",
                parameters={
                    "type": "object",
                    "properties": {
                        "issue": {"type": "string", "description": "Issue to analyze"},
                    },
                    "required": ["issue"],
                },
                handler=suggest_fix,
            ),
        ],
    )

    # Start the embedded server in the background (handles Twilio webhooks)
    async def start_server():
        logger.info("Starting Patter embedded server on port %d...", port)
        await phone.serve(agent, port=port)

    server_task = asyncio.create_task(start_server())

    # Give the server a moment to bind
    await asyncio.sleep(2)

    # Trigger the outbound call
    logger.info("Calling %s...", target_phone)
    await phone.call(
        to=target_phone,
        agent=agent,
        machine_detection=True,
        voicemail_message=(
            "ShipCall alert: error spike detected in auth-service. "
            "12 failures in 5 minutes. Check your dashboard."
        ),
    )
    logger.info("Call initiated! Pick up your phone.")

    # Keep running until Ctrl+C
    try:
        await server_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShipCall demo stopped.")
