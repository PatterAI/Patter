---
name: setup-patter
description: >
  Install the Patter voice/telephony SDK (Python or TypeScript) and configure
  the provider and carrier API keys required for real phone calls. Use when
  the user is starting a new Patter project, hitting "missing API key" errors,
  setting up Twilio or Telnyx, integrating OpenAI Realtime / ElevenLabs /
  Deepgram, or asking how to get Patter running — even if they don't
  explicitly say "setup" or "credentials". Covers Patter 0.6.2 in both
  Python (>=3.11) and TypeScript (Node >=20) runtimes.
license: MIT
compatibility: >
  Requires Python 3.11+ or Node.js 20+. Needs at least one provider key
  (e.g. OPENAI_API_KEY) and one carrier credential set (Twilio
  TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN, or Telnyx TELNYX_API_KEY) in env.
  Patter >= 0.6.2.
metadata:
  author: patter
  version: "0.1.0"
  parity: both
---

# Set up Patter

Install the Patter SDK and provision the credentials required to place or
receive real phone calls. Patter is open-source — there is no Patter API key.
You bring your own provider (OpenAI / ElevenLabs / Deepgram / …) and your own
carrier (Twilio or Telnyx) keys, and Patter wires them together.

## Workflow

### Step 1 — Pick a runtime

Ask the user which SDK they want. Both have full feature parity.

- **Python** (3.11+): `pip install "getpatter>=0.6.2"`
- **TypeScript** (Node 20+): `npm install "getpatter@>=0.6.2"`

If they're unsure, default to whichever language the rest of their project
is in. Do not install both.

### Step 2 — Pick an engine (decides which provider keys are needed)

| Engine | What it does | Required keys |
|---|---|---|
| `OpenAIRealtime2` (recommended, GA in 0.6.2) | Speech-to-speech via OpenAI Realtime API — lowest latency | `OPENAI_API_KEY` |
| `OpenAIRealtime` (legacy) | Older `gpt-realtime-mini` model. Same key. | `OPENAI_API_KEY` |
| `ElevenLabsConvAI` | ElevenLabs ConversationAI — turn-taking model | `ELEVENLABS_API_KEY` |
| `Pipeline` (no engine arg) | STT → LLM → TTS — mix providers freely | At least one each of STT/LLM/TTS key |

If the user wants the simplest setup, default to **`OpenAIRealtime2`** — single
key, lowest latency, GA quality. Mention Pipeline only if they ask for custom
STT/LLM/TTS or for cost tuning.

### Step 3 — Pick a carrier

| Carrier | Audio format | Required env |
|---|---|---|
| **Twilio** | mulaw 8 kHz | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` |
| **Telnyx** | PCM 16 kHz | `TELNYX_API_KEY` |

Twilio is the default suggestion — broader US/EU coverage, mature TwiML
ecosystem. Telnyx is preferable in regions where Twilio coverage is weak
and for users who want lower carrier cost.

Buying a phone number is **the user's job**, not the AI agent's — link the
console:

- Twilio: <https://console.twilio.com/us1/develop/phone-numbers/manage/incoming>
- Telnyx: <https://portal.telnyx.com/#/app/numbers/my-numbers>

### Step 4 — Write the keys to `.env`

Create or append to `.env` in the project root:

```bash
# Engine
OPENAI_API_KEY=sk-...

# Carrier (pick one)
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...

# Phone number you bought from the carrier console
PATTER_PHONE_NUMBER=+15550001234
```

Verify `.env` is in `.gitignore`. If not, add it:

```
.env
```

**Never commit credentials.** Patter will pick these up automatically — every
carrier and provider reads its key from env by default.

### Step 5 — Smoke-test the install

Run the canonical 4-line example and verify it boots without ImportError or
KeyError. The agent does not have to place a real call — `serve(... tunnel=True)`
exiting cleanly with a tunnel URL is enough.

**Python** (`test_setup.py`):

```python
import asyncio
from getpatter import Patter, Twilio, OpenAIRealtime2

async def main():
    phone = Patter(carrier=Twilio(), phone_number="+15550001234")
    agent = phone.agent(
        engine=OpenAIRealtime2(),
        system_prompt="You are a friendly receptionist.",
        first_message="Hello!",
    )
    await phone.serve(agent, tunnel=True)  # Ctrl+C to stop once tunnel URL prints

asyncio.run(main())
```

**TypeScript** (`test-setup.ts`):

```typescript
import { Patter, Twilio, OpenAIRealtime2 } from "getpatter";

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({
  engine: new OpenAIRealtime2(),
  systemPrompt: "You are a friendly receptionist.",
  firstMessage: "Hello!",
});
await phone.serve({ agent, tunnel: true });
```

Run with `python test_setup.py` or `tsx test-setup.ts`. Expected output: a
log line like `tunnel ready: https://<random>.trycloudflare.com` within ~5 seconds.

### Step 6 — Confirm and hand off

If smoke-test passes, tell the user:

> Patter 0.6.2 is set up. You can now use the `build-voice-agent` skill to
> design the agent, `configure-telephony` to wire the carrier webhook to the
> tunnel URL, or `add-tools-and-handoffs` to give the agent tools.

## Gotchas

- **`Patter(api_key=...)` raises `NotImplementedError`** in 0.6.2 — Patter
  Cloud was removed in 0.5.3 and will return as a future release. Always
  instantiate with `carrier=` + `phone_number=`, never with `api_key=`.
- **`pip install patter`** (no `get` prefix) installs an unrelated package.
  Always install `getpatter`.
- **Twilio kwargs in 0.6.2 normalize automatically** — `status_callback`,
  `machine_detection`, `timeout`, `async_amd` work as snake_case (Python) and
  camelCase (TS); no need to PascalCase them.
- **OpenAIRealtime vs OpenAIRealtime2** — both work in 0.6.2. Default to
  `OpenAIRealtime2` for new projects. `OpenAIRealtime` (model `gpt-realtime-mini`)
  is kept for back-compat.
- **Cloudflare `tunnel=True` is dev-only**. Production should use a static
  webhook URL (ngrok paid, or your own subdomain). The tunnel race on first
  call was fixed in 0.5.5 but a static URL is still more reliable for
  outbound campaigns.

## Common errors

| Symptom | Fix |
|---|---|
| `KeyError: OPENAI_API_KEY` | The env var isn't loaded. Source `.env` (`source .env` in bash, or use `python-dotenv` / `dotenv` package). |
| `twilio.base.exceptions.TwilioRestException: HTTP 401` | Wrong `TWILIO_AUTH_TOKEN`. Re-copy from console. |
| `RuntimeError: NotImplementedError: Patter Cloud …` | You passed `api_key=` to `Patter()`. Switch to `carrier=` + `phone_number=`. |
| TypeScript `Cannot find module 'getpatter'` | Wrong Node version (need 20+) or missed `npm install`. Check `node --version`. |
| `ModuleNotFoundError: No module named 'getpatter'` | Wrong Python venv active, or installed in the wrong interpreter. `pip list | grep getpatter`. |

## Related skills

- [`build-voice-agent`](../build-voice-agent/) — once setup is done, build the actual agent.
- [`configure-telephony`](../configure-telephony/) — full Twilio / Telnyx config beyond keys.

## References

- Patter Python quickstart: <https://docs.getpatter.com/python-sdk/quickstart>
- Patter TypeScript quickstart: <https://docs.getpatter.com/typescript-sdk/quickstart>
- Twilio console: <https://console.twilio.com>
- Telnyx portal: <https://portal.telnyx.com>
