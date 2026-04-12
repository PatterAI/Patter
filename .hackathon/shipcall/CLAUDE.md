# CLAUDE.md — ShipCall

## What is ShipCall

ShipCall is an AI voice agent that proactively calls developers when code events happen — build failures, deploys, error spikes. Built on the Patter SDK for the Stanford x DeepMind Hackathon (April 12, 2026). FastShot.ai mobile app track. Solo team (Francesco Rosciano).

The demo moment: mid-pitch, the presenter's phone rings. An AI reports a deploy issue, reads logs, and suggests a fix — live, on speaker, in front of 40+ VCs.

## Hackathon Context

- **Event:** Stanford x DeepMind Hackathon
- **Build window:** 3 hours (11:30 AM - 2:30 PM PT)
- **Track:** FastShot.ai (mobile app)
- **Judging:** Technical feasibility, innovation, real-world value, market potential, social traction (YouTube engagement over 2 weeks)
- **Deliverables:** One-pager, hosted prototype, 2-min team intro video, 1-min demo video, code repo

## Tech Stack

- **Language:** Python 3.11+
- **Framework:** FastAPI
- **Voice AI:** Patter SDK v0.3.0 (local mode — embedded server, no cloud backend needed)
- **Telephony:** Twilio (mulaw 8kHz, outbound calls via REST API)
- **AI Provider:** OpenAI Realtime API (single WebSocket — lowest latency)
- **Tunnel:** ngrok (exposes local server for Twilio webhooks)

## Patter SDK Reference

The parent Patter SDK lives at `../../sdk/`. Key files:
- `../../sdk/patter/client.py` — `Patter` class: constructor, `agent()`, `call()`, `serve()`, `tool()`
- `../../sdk/patter/models.py` — `Agent`, `STTConfig`, `TTSConfig` (frozen dataclasses)
- `../../sdk/patter/__init__.py` — public exports

### Patter Local Mode Pattern

```python
from patter import Patter

phone = Patter(
    mode="local",
    twilio_sid="AC...",
    twilio_token="...",
    openai_key="sk-...",
    phone_number="+1...",
    webhook_url="abc.ngrok.io",  # no scheme
)

agent = phone.agent(
    system_prompt="You are a helpful assistant.",
    voice="alloy",
    first_message="Hello!",
    tools=[
        Patter.tool(
            name="my_tool",
            description="Does something",
            parameters={"type": "object", "properties": {...}, "required": [...]},
            handler=my_async_handler,  # async (arguments: dict, context: dict) -> str
        ),
    ],
)

# For inbound calls (blocks):
await phone.serve(agent, port=8080)

# For outbound calls:
await phone.call(to="+1234567890", agent=agent)
```

### Important API Details

- `Patter()` constructor: `mode="local"` auto-detected when `twilio_sid` provided without `api_key`
- `phone.agent()` returns a frozen `Agent` dataclass — tools must be list of dicts from `Patter.tool()`
- `phone.call()` in local mode requires `agent` parameter (not `on_message`)
- `phone.serve()` starts the embedded server (blocks) — needed for outbound calls to work (handles Twilio webhooks)
- `Patter.tool()` is a static method — handler signature: `async (arguments: dict, context: dict) -> str`
- `webhook_url` must NOT include scheme (e.g., `"abc.ngrok.io"` not `"https://abc.ngrok.io"`)
- `machine_detection=True` on `call()` enables AMD; pair with `voicemail_message` for VM fallback

## Architecture

```
FastShot Mobile App  ──POST──>  FastAPI Backend  ──Twilio REST──>  Developer Phone
  "Call Me Now"                  (app.py)                           (rings)
                                    │
GitHub Webhook ──POST──>            │
  (simulated)                       ▼
                              Patter Agent
                              + Tool Handlers
                                    │
                              read_logs()
                              get_test_results()
                              suggest_fix()
```

The FastAPI app runs alongside the Patter embedded server. When `/api/call-me` is hit, it triggers `phone.call()` which uses Twilio to place an outbound call. The Patter embedded server handles the WebSocket audio streaming for the call.

## Build Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Copy env and fill in values
cp .env.example .env

# Start ngrok tunnel (separate terminal)
ngrok http 8080

# Run the server (update WEBHOOK_URL in .env with ngrok host first)
python3 app.py

# Run standalone demo call
python3 demo.py
```

## 3-Hour Build Timeline

| Time | Task | Done When |
|------|------|-----------|
| 0:00-0:25 | Patter agent + outbound call | Phone rings, AI speaks, you can talk back |
| 0:25-0:50 | 3 tool handlers wired up | AI can read logs, get test results, suggest fix |
| 0:50-1:15 | FastAPI endpoint + FastShot app | POST `/api/call-me` triggers call. Mobile app works |
| 1:15-1:45 | Switch to VoiceScope | (separate project) |
| 1:45-2:05 | Polish + rehearse | Run each demo 3x |
| 2:05-2:25 | Record videos + one-pagers | Upload to YouTube |
| 2:25-2:30 | Submit | Both tracks submitted |

**Critical checkpoint at 0:25:** If the phone isn't ringing, STOP everything. Debug until it rings.

## File Structure

```
shipcall/
├── CLAUDE.md          # This file
├── app.py             # FastAPI + Patter agent (main application)
├── demo.py            # Standalone demo call trigger
├── requirements.txt   # Python dependencies
├── .env.example       # Environment variable template
└── README.md          # Quick setup instructions
```

## FastShot Integration

The ShipCall system has two halves: the FastAPI backend (app.py) is the brain, and the FastShot mobile app is the face. The backend handles Patter agents, tool execution, and Twilio calls. The FastShot app is a lightweight mobile UI.

### Backend Endpoints for FastShot

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/events` | Returns mock code events for the event feed |
| `POST` | `/api/call-me` | Triggers an outbound ShipCall to the developer |
| `GET` | `/health` | Health check |

### FastShot App Screens

**Screen 1 — Event Feed:** Fetch `GET /api/events` and display events as a list. Each event has `type`, `message`, `timestamp`, and `severity` (critical/warning/info). Big "CALL ME NOW" button at the bottom.

**Screen 2 — Call Me Now:** Tapping the button sends `POST /api/call-me`. Show a "Calling you now..." state. The phone rings within 5-10 seconds.

### Backend URL

Set the ShipCall backend URL as the base URL in the FastShot app:
- **ngrok (local):** Run `ngrok http 8080`, use the generated URL
- **Cloud Run (stable):** Deploy to Cloud Run using the same pattern as VoiceScope's `deploy.sh`

CORS is already enabled in app.py (`allow_origins=["*"]`), so FastShot can call the API from any origin.

See `fastshot-notes.md` for full API response shapes, example fetch calls, and implementation details.

## Key Conventions

- All data is mock/hardcoded for demo reliability. Real integrations are not worth the risk in a 3-hour sprint.
- `python3` required (no `python` alias on this machine).
- No AI attribution in commits (per Patter repo rules).
- Prefer working subset over broken superset. If something breaks, cut scope.
