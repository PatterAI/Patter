"""ShipCall — AI voice agent that calls developers about code events.

Runs a FastAPI server with a Patter embedded server for outbound calls.
Endpoints:
    GET  /health          — Health check
    POST /api/call-me     — Trigger a call to the developer
    POST /api/webhook     — Simulate a GitHub webhook event
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from patter import Patter

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shipcall")

# ---------------------------------------------------------------------------
# Configuration (fail fast if required env vars are missing)
# ---------------------------------------------------------------------------

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
DEV_PHONE_NUMBER = os.getenv("DEV_PHONE_NUMBER", "")
PORT = int(os.getenv("PORT", "8080"))

_MISSING = []
if not TWILIO_ACCOUNT_SID:
    _MISSING.append("TWILIO_ACCOUNT_SID")
if not TWILIO_AUTH_TOKEN:
    _MISSING.append("TWILIO_AUTH_TOKEN")
if not TWILIO_PHONE_NUMBER:
    _MISSING.append("TWILIO_PHONE_NUMBER")
if not OPENAI_API_KEY:
    _MISSING.append("OPENAI_API_KEY")
if not WEBHOOK_URL:
    _MISSING.append("WEBHOOK_URL")
if _MISSING:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(_MISSING)}. "
        "Copy .env.example to .env and fill in the values."
    )

# ---------------------------------------------------------------------------
# Patter client (local mode — embedded server, no cloud backend)
# ---------------------------------------------------------------------------

phone = Patter(
    mode="local",
    twilio_sid=TWILIO_ACCOUNT_SID,
    twilio_token=TWILIO_AUTH_TOKEN,
    openai_key=OPENAI_API_KEY,
    phone_number=TWILIO_PHONE_NUMBER,
    webhook_url=WEBHOOK_URL,
)

# ---------------------------------------------------------------------------
# Tool handlers (mock data — hardcoded for demo reliability)
# ---------------------------------------------------------------------------


async def read_logs(arguments: dict, context: dict) -> str:
    """Read recent log entries from the affected service."""
    return (
        "Last 5 log entries from auth-service:\n"
        "ERROR 08:42:01 - Redis connection refused at 10.0.1.5:6379\n"
        "ERROR 08:42:03 - Session validation failed: cache unavailable\n"
        "WARN  08:42:05 - Falling back to DB session store\n"
        "ERROR 08:42:07 - DB session query timeout after 5000ms\n"
        "FATAL 08:42:09 - Auth middleware returning 503 for all requests"
    )


async def get_test_results(arguments: dict, context: dict) -> str:
    """Get the latest test suite results."""
    return (
        "Test suite: 47 passed, 3 failed\n"
        "FAIL test_session_cache - Expected 200, got 503\n"
        "FAIL test_login_flow - Timeout after 10s\n"
        "FAIL test_token_refresh - Redis connection refused"
    )


async def suggest_fix(arguments: dict, context: dict) -> str:
    """Analyze the issue and suggest a fix."""
    return (
        "Root cause: Redis instance at 10.0.1.5:6379 is unreachable.\n"
        "Fix: The REDIS_URL env var in production is pointing to the old "
        "instance. Update it to redis://10.0.2.3:6379 in your deploy config.\n"
        "One-liner: heroku config:set REDIS_URL=redis://10.0.2.3:6379"
    )


# ---------------------------------------------------------------------------
# Agent factory — creates a Patter agent configured for a specific event
# ---------------------------------------------------------------------------


def create_shipcall_agent(
    event_type: str,
    event_summary: str,
    repo: str,
) -> "Patter.agent":
    """Create a Patter Agent for a specific code event."""
    return phone.agent(
        system_prompt=(
            f"You are ShipCall, a senior engineer watching the {repo} repository. "
            f"Event type: {event_type}. "
            f"You just detected: {event_summary}. "
            "Help the developer understand what happened and take action. "
            "Be concise, specific, and actionable. You're on a phone call, "
            "not writing an essay. If they ask you to dig into logs or suggest "
            "a fix, use your tools. Keep responses under 3 sentences unless "
            "reading back data from a tool."
        ),
        voice="alloy",
        first_message=f"Hey Francesco, {event_summary}",
        tools=[
            Patter.tool(
                name="read_logs",
                description="Read recent log entries from the affected service.",
                parameters={
                    "type": "object",
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "Service name to read logs from",
                        },
                        "lines": {
                            "type": "number",
                            "description": "Number of log lines to retrieve",
                        },
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
                        "suite": {
                            "type": "string",
                            "description": "Test suite name",
                        },
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
                        "issue": {
                            "type": "string",
                            "description": "Description of the issue to analyze",
                        },
                    },
                    "required": ["issue"],
                },
                handler=suggest_fix,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Outbound call trigger
# ---------------------------------------------------------------------------


async def trigger_shipcall(
    dev_phone: str,
    event_type: str = "error_spike",
    event_summary: str = (
        "your deploy landed on production. 47 tests passing. "
        "But there's a new error spike in the auth service — "
        "12 failures in the last 5 minutes"
    ),
    repo: str = "patter-app/api",
) -> None:
    """Place an outbound call to a developer about a code event."""
    agent = create_shipcall_agent(
        event_type=event_type,
        event_summary=event_summary,
        repo=repo,
    )
    await phone.call(
        to=dev_phone,
        agent=agent,
        machine_detection=True,
        voicemail_message=(
            "ShipCall alert: error spike detected in auth-service. "
            "12 failures in 5 minutes. Check your dashboard."
        ),
    )
    logger.info("ShipCall outbound call initiated to %s", dev_phone)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start the Patter embedded server in the background on startup."""
    default_agent = create_shipcall_agent(
        event_type="error_spike",
        event_summary="standing by for code events",
        repo="patter-app/api",
    )

    async def start_patter():
        await phone.serve(default_agent, port=PORT)

    patter_task = asyncio.create_task(start_patter())
    logger.info("Patter embedded server starting on port %d", PORT)

    yield

    patter_task.cancel()
    try:
        await patter_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="ShipCall",
    description="AI voice agent that calls developers about code events",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- Request models --


class CallMeRequest(BaseModel):
    phone_number: str | None = None


class WebhookEvent(BaseModel):
    event_type: str = "error_spike"
    summary: str = (
        "your deploy landed on production. 47 tests passing. "
        "But there's a new error spike in the auth service — "
        "12 failures in the last 5 minutes"
    )
    repo: str = "patter-app/api"
    phone_number: str | None = None


# -- Endpoints --


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "shipcall"}


@app.get("/api/events")
async def get_events():
    """Return mock code events for the FastShot event feed."""
    return {"events": [
        {"id": 1, "type": "error_spike", "message": "Auth service: 12 failures in 5 min", "timestamp": "2026-04-12T11:42:00Z", "severity": "critical"},
        {"id": 2, "type": "deploy", "message": "v2.3.1 deployed to production", "timestamp": "2026-04-12T11:38:00Z", "severity": "info"},
        {"id": 3, "type": "test_failure", "message": "3 tests failing: test_session_cache", "timestamp": "2026-04-12T11:35:00Z", "severity": "warning"},
    ]}


@app.post("/api/call-me")
async def call_me(request: CallMeRequest | None = None):
    """Trigger an outbound ShipCall to the developer.

    If phone_number is omitted, uses DEV_PHONE_NUMBER from env.
    """
    target = (request.phone_number if request and request.phone_number else DEV_PHONE_NUMBER)
    if not target:
        raise HTTPException(
            status_code=400,
            detail="No phone number provided and DEV_PHONE_NUMBER not set in env.",
        )

    asyncio.create_task(trigger_shipcall(dev_phone=target))
    return {
        "status": "calling",
        "message": f"ShipCall is calling {target}. Pick up your phone!",
    }


@app.post("/api/webhook")
async def webhook(event: WebhookEvent):
    """Simulate a GitHub webhook event that triggers a ShipCall.

    In production this would be a real GitHub webhook. For the hackathon
    demo, we accept a JSON body with event details and trigger the call.
    """
    target = event.phone_number or DEV_PHONE_NUMBER
    if not target:
        raise HTTPException(
            status_code=400,
            detail="No phone number provided and DEV_PHONE_NUMBER not set in env.",
        )

    asyncio.create_task(
        trigger_shipcall(
            dev_phone=target,
            event_type=event.event_type,
            event_summary=event.summary,
            repo=event.repo,
        )
    )
    return {
        "status": "calling",
        "event_type": event.event_type,
        "message": f"ShipCall triggered for {event.repo}. Calling {target}...",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
