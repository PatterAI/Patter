---
name: configure-telephony
description: >
  Configure Twilio or Telnyx as the telephony carrier for a Patter voice
  agent — buy or use a phone number, set up the carrier console, point the
  voice/Call-Control webhook at your Patter server (via Cloudflare tunnel,
  ngrok, or a static URL), and verify webhook signatures. Use when the user
  is wiring up Twilio, Telnyx, a phone number, a webhook URL, a tunnel, AMD
  (Answering Machine Detection), call recording, voicemail drop, or DTMF
  routing — even if they don't say "telephony". Covers both Patter 0.6.2
  SDKs (Python and TypeScript).
license: MIT
compatibility: >
  Requires Patter >= 0.6.2, an active Twilio or Telnyx account with at least
  one purchased number, and the ability to expose a public webhook URL
  (tunnel for dev, static URL for prod).
metadata:
  author: patter
  version: "0.1.0"
  parity: both
---

# Configure telephony for Patter

Telephony is the leg between the user's phone and your Patter server. Patter
supports two carriers with full feature parity:

| Carrier | Audio | Webhook framework | Why pick it |
|---|---|---|---|
| **Twilio** | mulaw 8 kHz | TwiML | Largest US/EU coverage, mature ecosystem, AMD + recording. |
| **Telnyx** | PCM 16 kHz | Call Control + Ed25519 sigs | Lower per-minute cost, native PCM, EU-friendly. |

Both expose the **same surface** in Patter (`phone.serve(agent)`, `phone.call(to)`).
The differences are in how you configure the carrier console.

## Decision tree

1. **Already have a Twilio or Telnyx account?** Use that one.
2. **Starting fresh in the US?** → Twilio. Easier onboarding, larger phone-number inventory.
3. **Starting fresh in Europe / cost-sensitive?** → Telnyx.
4. **Need carrier features Patter doesn't bridge yet?** Check the per-carrier
   reference below.

Detailed setup per carrier:

| Carrier | Reference |
|---|---|
| Twilio | [references/twilio.md](references/twilio.md) |
| Telnyx | [references/telnyx.md](references/telnyx.md) |

Read one — not both — once the user has chosen.

## Webhook URL options

Whichever carrier you use, the carrier needs a public URL to reach your
Patter server. Three patterns, ranked by reliability:

| Option | Setup | When to use |
|---|---|---|
| **Static URL** (`webhook_url="https://yourdomain.com"`) | Own subdomain pointed at your server's IP (DNS A record + TLS via Caddy/Cloudflare/etc.) | **Production**. Most reliable. |
| **ngrok** (`webhook_url="abc.ngrok.io"`) | Paid ngrok account with reserved subdomain | Acceptance / dev with stable URL. |
| **Cloudflare quick tunnel** (`tunnel=True`) | Patter spawns a tunnel automatically — no setup. URL changes every restart. | Dev / local demos only. |

**`webhook_url` is set on the `Patter` constructor**, not on `serve()`. `tunnel`
can be set on either — passing it to `serve()` is the most common pattern.

### Python

```python
from getpatter import Patter, Twilio, Ngrok

# Static (production) — set webhook_url on the constructor
phone = Patter(
    carrier=Twilio(),
    phone_number="+15550001234",
    webhook_url="patter.acme.com",   # no scheme, no trailing slash
)
await phone.serve(agent)

# Cloudflare tunnel (dev) — no webhook_url; pass tunnel=True to serve()
phone = Patter(carrier=Twilio(), phone_number="+15550001234")
await phone.serve(agent, tunnel=True)

# Ngrok with reserved subdomain
phone = Patter(
    carrier=Twilio(),
    phone_number="+15550001234",
    tunnel=Ngrok(hostname="acme-patter.ngrok.io"),
)
await phone.serve(agent)
```

### TypeScript

```typescript
import { Patter, Twilio, Ngrok } from "getpatter";

// Static (production) — set webhookUrl on the constructor
const phone = new Patter({
  carrier: new Twilio(),
  phoneNumber: "+15550001234",
  webhookUrl: "patter.acme.com",
});
await phone.serve({ agent });

// Cloudflare tunnel (dev) — no webhookUrl; pass tunnel: true to serve()
const dev = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
await dev.serve({ agent, tunnel: true });

// Ngrok with reserved subdomain
const prod = new Patter({
  carrier: new Twilio(),
  phoneNumber: "+15550001234",
  tunnel: new Ngrok({ hostname: "acme-patter.ngrok.io" }),
});
await prod.serve({ agent });
```

## Outbound calls (AMD, voicemail drop)

After `phone.serve(...)`, place an outbound call. `machine_detection` is **on
by default** in 0.6.2 — pass `False` only to skip per-call AMD billing.

### Python

```python
await phone.call(
    to="+14155551234",
    agent=agent,
    first_message="Hi, this is Mia from Acme.",
    machine_detection=True,                            # default in 0.6.2
    voicemail_message="Sorry we missed you. Call back at +1...",
    ring_timeout=30,                                    # default 25 s
)
```

### TypeScript

```typescript
await phone.call({
  to: "+14155551234",
  agent,
  firstMessage: "Hi, this is Mia from Acme.",
  machineDetection: true,
  voicemailMessage: "Sorry we missed you. Call back at +1...",
  ringTimeout: 30,
});
```

**AMD on Twilio**: requires the number to have voice capability. Setting a
non-empty `voicemail_message` implicitly enables AMD, so you can omit
`machine_detection=True` if you've set the voicemail. Patter normalizes
the Twilio parameter shape automatically in 0.6.2 — older versions required
PascalCase like `MachineDetection`.

**Recording is server-wide**, not per-call: pass `recording=True` to
`phone.serve(...)` to enable carrier-side recording for every call routed
through that server (inbound and outbound).

## Verify webhook signatures

Patter validates carrier webhook signatures by default. You don't have to
write code — the validators live in `getpatter.handlers.common`:

- **Twilio**: `validate_twilio_signature(headers, body, auth_token, url)` — HMAC-SHA1
  against the request URL + sorted body params.
- **Telnyx**: `validate_telnyx_signature(headers, body, public_key)` — Ed25519
  with anti-replay check (timestamp within ±5 min).

Both return `False` on missing header (do **not** raise). Patter's default
handler returns 401 on `False`. Don't disable signature verification.

## Gotchas

- **Twilio mulaw 8 kHz vs Telnyx PCM 16 kHz**: Patter transcodes both — your
  agent code never touches raw audio. Don't try to set sample rate manually.
- **Twilio Cloudflare tunnel race**: occasionally first call drops because
  the WSS upgrade hasn't propagated through the CF edge yet. Fixed by bumping
  grace to 5 s in 0.5.5; still less reliable than a static URL.
- **Telnyx connection ID** is required (not just API key). Find it in
  `Portal → Connections → SIP/SDK`. Set `TELNYX_CONNECTION_ID`.
- **Number formats**: always E.164 (`+15550001234`). Patter validates before
  dialing; non-E.164 raises `ValueError`.
- **Outbound trunking** on Telnyx requires an Outbound Voice Profile attached
  to the number — Twilio handles this automatically.
- **Recording** stores on the carrier side (Twilio Media / Telnyx Storage),
  never re-uploaded by Patter. URL appears in `CallMetrics.recording_url`.

## Common errors

| Symptom | Fix |
|---|---|
| Carrier dials but no audio | Webhook URL is wrong — carrier can't reach your server. Test with `curl <webhook>/health`. |
| Twilio 12300 (TwiML response invalid) | Your local server crashed before responding. Check Patter logs for stack trace. |
| Telnyx "no answer" on outbound | Outbound Voice Profile not attached to the number. Set in portal. |
| `signature verification failed` warning | Tunnel URL mismatch — Twilio signed for URL A, Patter validates against URL B. Use static URL or set `webhook_url` to the same one Twilio has. |
| AMD always returns "human" | Patter uses Twilio async AMD by default for low latency. If you need synchronous AMD accuracy, contact Twilio support to switch your account default. |

## Related skills

- [`setup-patter`](../setup-patter/) — install + provider API keys (do this first).
- [`build-voice-agent`](../build-voice-agent/) — define what the agent says once a call connects.
- [`inspect-calls-and-metrics`](../inspect-calls-and-metrics/) — dashboard, recordings, cost.

## References

- Twilio console: <https://console.twilio.com>
- Telnyx portal: <https://portal.telnyx.com>
- Patter Tunneling guide: <https://docs.getpatter.com/dev-tools/tunneling>
- Patter Carrier reference: <https://docs.getpatter.com/python-sdk/carrier> · <https://docs.getpatter.com/typescript-sdk/carrier>
