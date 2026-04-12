# CLAUDE.md — VoiceScope

## What is VoiceScope

VoiceScope is a hackathon project for the **Stanford x DeepMind Hackathon** (April 12, 2026, Google AI Studio track). The concept: snap a photo of anything, and an AI expert calls you back to explain what it sees and what to do. Gemini 3 vision analyzes the image, then Patter places an outbound phone call to deliver the analysis conversationally.

**Hackathon context:** 3-hour build sprint (11:30 AM - 2:30 PM PT), solo team (Francesco Rosciano), judged on technical feasibility, innovation, real-world value, market potential, and social traction. VoiceScope is the AI Studio track submission; ShipCall is the separate FastShot track submission. Both share the Patter backend.

## Tech Stack

- **Python 3.11+** (use `python3`, not `python`)
- **FastAPI** — web server for the upload endpoint and static file serving
- **Patter SDK** (local mode) — outbound phone calls with voice AI agents
- **Google Generative AI** (`google-generativeai`) — Gemini 3 vision for image analysis
- **OpenAI Realtime** — voice provider for Patter (STT + LLM + TTS in one WebSocket)
- **Twilio** — telephony provider for placing actual phone calls
- **uvicorn** — ASGI server

## Architecture

```
Web UI (index.html)
  │  POST /analyze (image + phone number)
  ▼
FastAPI Backend (app.py)
  │  1. Send image to Gemini 3 vision API
  │  2. Get structured analysis back
  │  3. Create Patter agent with analysis injected into system prompt
  │  4. Place outbound call via Patter → Twilio → user's phone
  ▼
User's Phone rings — AI expert explains what it saw
```

## Build Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Also install the Patter SDK from the parent repo
pip install -e ../../sdk/

# Run the server
uvicorn app:app --host 0.0.0.0 --port 8080

# Run tests (if any)
python3 -m pytest tests/ -v
```

## Parent Patter SDK

The Patter SDK source is at `../../sdk/` (relative to this directory). Key files:
- `../../sdk/patter/client.py` — `Patter` class, `agent()`, `call()`, `serve()`, `tool()` methods
- `../../sdk/patter/models.py` — `Agent`, `STTConfig`, `TTSConfig` frozen dataclasses
- `../../CLAUDE.md` — full Patter SDK documentation

## Build Timeline (VoiceScope portion)

| Time | Task | Done When |
|------|------|-----------|
| 1:00-1:15 | Gemini 3 vision pipeline | Script sends image to Gemini 3, gets structured analysis back |
| 1:15-1:30 | Patter callback + web UI | Upload photo + phone number, click analyze, phone rings with analysis |

## Key API Patterns (from real Patter SDK)

```python
# Initialize Patter in local mode
phone = Patter(
    mode="local",
    openai_key=os.getenv("OPENAI_API_KEY"),
    twilio_sid=os.getenv("TWILIO_ACCOUNT_SID"),
    twilio_token=os.getenv("TWILIO_AUTH_TOKEN"),
    phone_number=os.getenv("TWILIO_PHONE_NUMBER"),
    webhook_url=os.getenv("WEBHOOK_URL"),
)

# Create an agent (returns a frozen Agent dataclass)
agent = phone.agent(
    system_prompt="...",
    voice="alloy",
    first_message="...",
)

# Place outbound call
await phone.call(to="+1234567890", agent=agent)

# Start embedded server (blocks — handles Twilio webhooks)
await phone.serve(agent, port=8000)
```

## Deployment

### Cloud Run (recommended for hackathon submission)

Deploy to Cloud Run for a stable, public URL:

```bash
# One-command deploy (builds, pushes, and deploys)
./deploy.sh <GCP_PROJECT_ID>
```

This takes ~10 minutes from running the script to a live URL. The script will print the service URL when done.

**After deploying:**
1. Copy the Cloud Run URL from the deploy output
2. Set `WEBHOOK_URL` in your `.env` to the Cloud Run URL (Patter needs this for Twilio webhook callbacks)
3. Redeploy with `./deploy.sh <PROJECT_ID>` to pick up the updated `WEBHOOK_URL`
4. The Cloud Run URL is your submitted prototype link

**Deployment files:**
- `Dockerfile` — Python 3.11-slim, installs `patter` from PyPI, exposes port 8080
- `.dockerignore` — excludes .env, __pycache__, venv
- `cloudbuild.yaml` — Google Cloud Build config (alternative to deploy.sh)
- `deploy.sh` — One-command build + push + deploy script

### Local development with ngrok

```bash
# Terminal 1: start the server
uvicorn app:app --host 0.0.0.0 --port 8080

# Terminal 2: expose via ngrok
ngrok http 8080
# Copy the ngrok URL to WEBHOOK_URL in .env, then restart the server
```

## Environment Variables

See `.env.example` for all required variables.
