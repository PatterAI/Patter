# VoiceScope

Snap a photo of anything. Get a phone call from an AI expert explaining what it sees and what to do.

Built for the **Stanford x DeepMind Hackathon** (Google AI Studio track). Uses Gemini 3 vision + Patter voice AI.

## Quick Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt
pip install -e ../../sdk/    # Install Patter SDK from parent repo

# 2. Configure environment
cp .env.example .env
# Edit .env with your API keys (Google AI Studio, OpenAI, Twilio)

# 3. Start ngrok (separate terminal)
ngrok http 8000
# Copy the hostname (e.g., abc.ngrok-free.app) into .env as WEBHOOK_URL

# 4. Run the server
uvicorn app:app --host 0.0.0.0 --port 8080
```

Open http://localhost:8080, upload a photo, enter your phone number, and click "Analyze & Call Me".

## Demo Script

For a quick test without the web UI:

```bash
python3 demo.py photo.jpg +15551234567
```

## Architecture

```
Web UI → POST /analyze → Gemini 3 vision → Patter outbound call → Phone rings
```

1. User uploads a photo and enters their phone number
2. Gemini 3 analyzes the image (what it sees, what's wrong, what to do)
3. Patter creates a voice agent with the analysis as context
4. Patter calls the user via Twilio
5. The AI expert explains the analysis conversationally

## Required API Keys

| Service | Variable | Get From |
|---------|----------|----------|
| Google AI Studio | `GOOGLE_AI_STUDIO_KEY` | https://aistudio.google.com/apikey |
| OpenAI | `OPENAI_API_KEY` | https://platform.openai.com/api-keys |
| Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` | https://console.twilio.com/ |
