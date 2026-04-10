"""
Full Production Setup
=====================
Complete production configuration with custom LLM, guardrails, tools,
dynamic variables, recording, dashboard, and all lifecycle callbacks.
Use this as a starting point for production deployments.

Requirements:
    pip install patter python-dotenv httpx

Environment variables (.env):
    ANTHROPIC_API_KEY    - Anthropic API key for Claude
    DEEPGRAM_API_KEY     - Deepgram key for speech-to-text
    ELEVENLABS_API_KEY   - ElevenLabs key for text-to-speech
    TWILIO_ACCOUNT_SID   - Twilio account SID
    TWILIO_AUTH_TOKEN    - Twilio auth token
    TWILIO_PHONE_NUMBER  - Your Twilio phone number (E.164 format)
    WEBHOOK_URL          - Public URL where Twilio can reach this server
    DASHBOARD_TOKEN      - Secret token for dashboard authentication
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

from patter import Patter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("production")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "change-me-in-production")

SYSTEM_PROMPT = (
    "You are a professional customer service agent for Acme Corp. "
    "The caller's name is {customer_name} and their account ID is {account_id}. "
    "Help with orders, billing, and account questions. "
    "Never discuss competitor products or give medical/legal advice. "
    "Keep responses concise — this is a phone call."
)


# ── Lifecycle callbacks ──────────────────────────────────────────────

async def on_call_start(call: dict) -> dict | None:
    """Log call start and optionally return variable overrides."""
    logger.info("Call started: %s from %s", call.get("call_id"), call.get("caller"))
    # Look up caller in your CRM and inject variables into the system prompt
    # customer = await crm.lookup(call["caller"])
    return {
        "customer_name": "Jane Doe",   # Replace with CRM lookup
        "account_id": "ACME-12345",    # Replace with CRM lookup
    }


async def on_call_end(call: dict) -> None:
    """Save transcript and metrics to database."""
    logger.info(
        "Call ended: %s duration=%ss turns=%s",
        call.get("call_id"),
        call.get("duration", 0),
        call.get("turns", 0),
    )
    # db.calls.insert({
    #     "call_id": call["call_id"],
    #     "caller": call.get("caller"),
    #     "duration": call["duration"],
    #     "transcript": call.get("transcript"),
    #     "cost": call.get("cost"),
    #     "ended_at": datetime.now(timezone.utc),
    # })


async def on_metrics(metrics: dict) -> None:
    """Forward metrics to your monitoring system."""
    cost = metrics.get("cost", {})
    latency = metrics.get("latency_p95", {})
    logger.info(
        "Metrics: call=%s cost=$%.4f p95_latency=%dms",
        metrics.get("call_id"),
        cost.get("total", 0),
        latency.get("total_ms", 0),
    )
    # datadog.gauge("patter.cost.total", cost["total"])
    # datadog.gauge("patter.latency.p95", latency["total_ms"])


async def on_message(data: dict) -> str:
    """Custom LLM handler with fallback on error."""
    history = data["history"]
    messages = [{"role": m["role"], "content": m["text"]} for m in history]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 256,
                    "system": SYSTEM_PROMPT,
                    "messages": messages,
                },
            )
            response.raise_for_status()
            return response.json()["content"][0]["text"]
    except Exception:
        logger.exception("LLM request failed, returning fallback")
        return (
            "I'm sorry, I'm having a brief technical issue. "
            "Could you repeat that, or I can transfer you to a human agent?"
        )


# ── Initialise Patter ────────────────────────────────────────────────

phone = Patter(
    mode="local",
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
    pricing={
        "stt_per_minute": 0.0059,       # Deepgram Nova-2
        "tts_per_1k_chars": 0.30,       # ElevenLabs
        "llm_input_per_1k_tokens": 0.003,
        "llm_output_per_1k_tokens": 0.015,
        "telephony_per_minute": 0.014,   # Twilio
    },
)

agent = phone.agent(
    provider="pipeline",
    system_prompt=SYSTEM_PROMPT,
    stt=Patter.deepgram(api_key=os.environ["DEEPGRAM_API_KEY"]),
    tts=Patter.elevenlabs(api_key=os.environ["ELEVENLABS_API_KEY"], voice="aria"),
    language="en",
    first_message="Hello {customer_name}, thanks for calling Acme Corp. How can I help?",
    variables={
        "customer_name": "Valued Customer",   # Default; overridden by on_call_start
        "account_id": "unknown",
    },
    guardrails=[
        Patter.guardrail(
            name="no_competitor_mentions",
            blocked_terms=["competitor", "rival product", "switch providers"],
            replacement="I'd love to focus on how Acme can help you. What do you need?",
        ),
        Patter.guardrail(
            name="no_medical_legal",
            check=lambda text: any(
                term in text.lower()
                for term in ["diagnosis", "prescription", "legal advice", "lawsuit"]
            ),
            replacement="I'm not qualified to advise on that. Let me transfer you.",
        ),
    ],
    tools=[
        Patter.tool(
            name="check_order_status",
            description="Check the status of a customer order by order ID",
            parameters={
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "The order ID"},
                },
                "required": ["order_id"],
            },
            webhook_url="https://api.yourcompany.com/orders/status",
        ),
    ],
)

# ── Start the production server ──────────────────────────────────────
if __name__ == "__main__":
    logger.info("Starting production server on port 8000...")
    logger.info("Dashboard: http://localhost:8000/dashboard")
    asyncio.run(
        phone.serve(
            agent,
            port=8000,
            recording=True,
            dashboard=True,
            dashboard_token=DASHBOARD_TOKEN,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
            on_metrics=on_metrics,
            on_message=on_message,
        )
    )
