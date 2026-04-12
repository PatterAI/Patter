# ShipCall

AI voice agent that proactively calls you when your code needs attention. Built on [Patter](https://getpatter.com) for the Stanford x DeepMind Hackathon.

## Quick Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in your credentials in `.env`:
- `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_PHONE_NUMBER` — from [Twilio Console](https://console.twilio.com)
- `OPENAI_API_KEY` — from [OpenAI Platform](https://platform.openai.com)
- `DEV_PHONE_NUMBER` — your phone number in E.164 format (e.g., `+15559876543`)

### 3. Start ngrok

In a separate terminal:

```bash
ngrok http 8080
```

Copy the hostname (e.g., `abc123.ngrok.io`) and set `WEBHOOK_URL` in `.env` (no `https://` prefix).

### 4. Run the server

```bash
python3 app.py
```

### 5. Trigger a call

**Option A — curl:**

```bash
curl -X POST http://localhost:8080/api/call-me
```

**Option B — with a specific number:**

```bash
curl -X POST http://localhost:8080/api/call-me \
  -H "Content-Type: application/json" \
  -d '{"phone_number": "+15559876543"}'
```

**Option C — simulate a GitHub webhook:**

```bash
curl -X POST http://localhost:8080/api/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "deploy_success",
    "summary": "your deploy just shipped to production with 47 tests passing, but there are 12 auth failures in the last 5 minutes",
    "repo": "patter-app/api"
  }'
```

**Option D — standalone demo script:**

```bash
python3 demo.py +15559876543
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/api/call-me` | Trigger a call to the developer |
| POST | `/api/webhook` | Simulate a GitHub webhook event |

## How It Works

1. A code event happens (deploy, build failure, error spike)
2. ShipCall creates a Patter voice agent with context about the event
3. The agent calls the developer via Twilio
4. During the call, the AI can read logs, show test results, and suggest fixes
5. The developer gets actionable help without opening a laptop

## Tech Stack

- Python 3.11+ / FastAPI
- Patter SDK (local mode) — voice AI + telephony
- Twilio — outbound phone calls
- OpenAI Realtime API — conversational AI
- ngrok — webhook tunneling
