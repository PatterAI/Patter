"""
Dashboard and Analytics Monitoring
===================================
Enable the built-in Patter dashboard for real-time call monitoring and
use lifecycle callbacks to persist metrics to your own database.

The dashboard is served at ``http://localhost:8000/dashboard`` and
exposes a REST API for programmatic access.

Requirements:
    pip install patter python-dotenv

Environment variables (.env):
    OPENAI_API_KEY       - OpenAI API key with Realtime access
    TWILIO_ACCOUNT_SID   - Twilio account SID
    TWILIO_AUTH_TOKEN    - Twilio auth token
    TWILIO_PHONE_NUMBER  - Your Twilio phone number (E.164 format)
    WEBHOOK_URL          - Public URL where Twilio can reach this server
    DASHBOARD_TOKEN      - Secret token for dashboard authentication
"""

import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from patter import Patter

DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "change-me-in-production")


async def on_call_start(call: dict) -> None:
    """Fires when a call connects. Log to your database."""
    print(
        f"[call:start] {call.get('call_id')} "
        f"from {call.get('caller', 'unknown')} "
        f"at {datetime.now(timezone.utc).isoformat()}"
    )
    # db.calls.insert({"call_id": call["call_id"], "started_at": utcnow(), ...})


async def on_call_end(call: dict) -> None:
    """Fires when a call ends. Save duration, transcript, and cost."""
    print(
        f"[call:end]   {call.get('call_id')} "
        f"duration={call.get('duration', 0)}s "
        f"turns={call.get('turns', 0)}"
    )
    # db.calls.update(
    #     {"call_id": call["call_id"]},
    #     {"ended_at": utcnow(), "duration": call["duration"],
    #      "transcript": call.get("transcript"), "cost": call.get("cost")},
    # )


async def on_metrics(metrics: dict) -> None:
    """Fires after each call with detailed cost and latency breakdown."""
    print(
        f"[metrics]    call={metrics.get('call_id')} "
        f"cost=${metrics.get('cost', {}).get('total', 0):.4f} "
        f"latency_avg={metrics.get('latency_avg', {}).get('total_ms', 0):.0f}ms"
    )
    # datadog.gauge("patter.call.cost", metrics["cost"]["total"])
    # datadog.gauge("patter.call.latency_p95", metrics["latency_p95"]["total_ms"])


# ── Dashboard REST API reference ─────────────────────────────────────
#
# All dashboard endpoints require the Authorization header when
# dashboard_token is set:
#   Authorization: Bearer <DASHBOARD_TOKEN>
#
# GET  /api/v1/calls                  — List recent calls (paginated)
# GET  /api/v1/calls/:id              — Single call with transcript
# GET  /api/v1/analytics/overview     — Aggregate stats (total calls,
#                                        avg duration, avg cost, etc.)
#
# Example — fetch analytics with httpx:
#
#   import httpx
#   resp = httpx.get(
#       "http://localhost:8000/api/v1/analytics/overview",
#       headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"},
#   )
#   print(resp.json())

# ── Initialise Patter ────────────────────────────────────────────────
phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
)

agent = phone.agent(
    system_prompt=(
        "You are a helpful customer support agent for Acme Corp. "
        "Answer questions about orders, billing, and account settings."
    ),
    voice="nova",
    first_message="Hello, thank you for calling Acme Corp. How can I help?",
)

# ── Start with dashboard enabled ─────────────────────────────────────
if __name__ == "__main__":
    print(f"Dashboard: http://localhost:8000/dashboard (token: {DASHBOARD_TOKEN})")
    asyncio.run(
        phone.serve(
            agent,
            port=8000,
            dashboard=True,
            dashboard_token=DASHBOARD_TOKEN,
            on_call_start=on_call_start,
            on_call_end=on_call_end,
            on_metrics=on_metrics,
        )
    )
