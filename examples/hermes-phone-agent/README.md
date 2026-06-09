# Hermes phone agent

A self-hosted phone line for your [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Patter is the **voice shell** (carrier, speech-to-text, turn-taking, barge-in,
text-to-speech); Hermes is the **brain** on the line. Each conversation turn is
one `POST http://127.0.0.1:8642/v1/chat/completions` against your local Hermes
gateway — so Hermes keeps its tools, memory, and skills, and **never leaves
loopback**. The only thing exposed to the internet is Patter's carrier webhook.

## 1. Configure

```bash
cp .env.example .env
# then fill in API_SERVER_KEY, TWILIO_*, DEEPGRAM_API_KEY, ELEVENLABS_API_KEY,
# and PATTER_PHONE_NUMBER
```

## 2. Check everything is wired up

```bash
pip install getpatter
patter hermes doctor
```

Fix anything it flags (it prints a suggested command for each problem), then
smoke-test the brain without spending a phone call:

```bash
python scripts/test_text_turn.py "say hello in one sentence"
```

## 3. Answer the phone

```bash
python app.py
```

Patter opens a tunnel and prints the public webhook URL. Point your Twilio
number's voice webhook at it — or let Patter do it for you:

```bash
patter hermes attach-number "$PATTER_PHONE_NUMBER" --url https://<your-tunnel>/calls/inbound
```

Now call your number and talk to Hermes.

## 4. Place an outbound call (optional)

```bash
python scripts/test_outbound_call.py +15557654321
```

## Debug a call

With `PATTER_LOG_DIR` set (see `.env`), Patter writes a per-call log. After a
call, inspect what happened stage by stage, or get a one-line verdict:

```bash
patter hermes trace        # latest call: carrier → STT → Hermes → TTS + latency
patter hermes diagnose     # "Hermes replied but no audio — TTS stage" + fix
```

Before placing a call at all, confirm the brain answers and providers are ready:

```bash
patter hermes test         # /v1/models + a real chat turn + provider keys
```

## Why Patter instead of a hosted custom-LLM voice agent?

- **Hermes stays private.** A hosted platform has to reach your "brain" endpoint
  over the public internet; here Hermes is loopback-only and only Patter is
  exposed.
- **You own the voice layer** — STT, turn-taking, barge-in, TTS — and can script it.
- **Inbound *and* outbound**, plus the Patter MCP server so Hermes can place calls.
