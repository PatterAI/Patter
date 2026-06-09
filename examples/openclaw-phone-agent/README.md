# OpenClaw phone agent (inbound receptionist + outbound dialer)

Give one scoped [OpenClaw](https://openclaw.ai) agent a phone with Patter.
Patter is the **voice shell** (carrier, speech-to-text, turn-taking, barge-in,
text-to-speech); the OpenClaw agent is the **brain** on the line. Every turn is
one `POST http://127.0.0.1:18789/v1/chat/completions` with
`model="openclaw/<OPENCLAW_AGENT>"` — so OpenClaw keeps its tools, memory, and
skills, and **never leaves loopback**. Only Patter's carrier webhook is exposed.

The same agent powers both directions:

- **Inbound** — `app.py` runs `phone.serve(...)`; Patter answers any caller.
- **Outbound** — `dialer.py` runs a supervised `phone.call(...)` loop over
  `numbers.txt` (24/7 AI-sales), with answering-machine detection + voicemail drop.

> **Pick a scoped agent, never the default/master.** The gateway credential
> authorises the whole gateway, so `OPENCLAW_AGENT` picks the persona, not a
> security boundary. Give the receptionist its own least-privileged
> `agents.list[]` entry with a tight tool allow/deny.

## 1. Configure

```bash
cp .env.example .env
# fill in OPENCLAW_API_KEY, OPENCLAW_AGENT, TWILIO_*, DEEPGRAM_API_KEY,
# ELEVENLABS_API_KEY, and PATTER_PHONE_NUMBER
```

Enable OpenClaw's OpenAI-compatible endpoint (disabled by default) and check
everything is wired up:

```bash
pip install getpatter
patter openclaw setup --enable-openclaw --agent receptionist   # flips chatCompletions.enabled
patter openclaw doctor                                         # lists agents, flags the default/master
patter openclaw agents                                         # the agents your gateway serves
python scripts/test_text_turn.py "say hello in one sentence"   # smoke-test the brain
```

## 2a. Answer the phone (inbound)

```bash
python app.py
patter openclaw attach-number "$PATTER_PHONE_NUMBER" --url https://<your-tunnel>/calls/inbound
```

## 2b. Dial out (outbound)

```bash
# edit numbers.txt (one E.164 per line), then:
python scripts/test_outbound_call.py +15557654321   # one test call
python dialer.py                                     # the 24/7 dialer loop
```

## 3. Run it 24/7 (always-on)

Patter is kept alive by an **OS service supervisor**, exactly like the OpenClaw
gateway. On a per-client always-on box you run three co-located services:

1. **OpenClaw gateway** — its own daemon (systemd / launchd), loopback `:18789`,
   `chatCompletions` enabled. The persistent agent's memory lives here.
2. **Patter voice-shell** — `app.py` (inbound) and/or `dialer.py` (outbound), as
   an OS service that restarts on crash and starts on boot. See `deploy/`:
   ```bash
   # systemd (Linux): edit the User/paths, then
   loginctl enable-linger "$USER"     # if running as a --user service
   systemctl --user enable --now patter-receptionist
   # launchd (macOS): cp deploy/com.patter.receptionist.plist ~/Library/LaunchAgents/ && launchctl load -w ...
   ```
3. **Named Cloudflare tunnel** — a STABLE hostname (the dev quick-tunnel rotates
   on restart and breaks the carrier webhook). Run `cloudflared` as a service and
   set `PATTER_WEBHOOK_URL` to it. See
   [production deployment](https://docs.getpatter.com/dev-tools/production-deployment).

Keep the OpenClaw gateway on loopback, turn the dashboard off or token it, and
verify the carrier signature at the boundary.

## Debug a call

```bash
patter openclaw test     # /v1/models + a real chat turn + provider keys
```

## Why Patter instead of OpenClaw's native voice plugin?

- **Anyone can call.** Patter answers the carrier leg for any caller and verifies
  the carrier signature; OpenClaw's native voice plugin is allowlist-only.
- **OpenClaw stays private** on loopback — only Patter is exposed.
- **You own the voice layer** (STT, turn-taking, barge-in, TTS) and can script it,
  inbound *and* outbound.
