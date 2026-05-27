# Twilio carrier setup

## Required env

```bash
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
PATTER_PHONE_NUMBER=+15550001234   # the number you bought from Twilio
```

`AccountSid` starts with `AC`. `AuthToken` is in **Console → Account → API
Keys & Tokens → Live Credentials**.

## Buy a number

1. Open <https://console.twilio.com/us1/develop/phone-numbers/manage/incoming>.
2. **Buy a number** → search by country / area code.
3. Confirm the number has **Voice** capability (not just SMS).

## Wire the voice webhook

1. Click the number you just bought.
2. **Voice Configuration** → **A call comes in** → **Webhook**.
3. URL: paste your Patter server's public URL + `/webhooks/twilio/incoming`.
   - For dev: copy the `tunnel ready:` URL Patter prints on startup, e.g.
     `https://abc.trycloudflare.com/webhooks/twilio/incoming`.
   - For prod: `https://patter.yourdomain.com/webhooks/twilio/incoming`.
4. HTTP method: `POST`.
5. Save.

## Code

### Python

```python
from getpatter import Patter, Twilio, OpenAIRealtime2

phone = Patter(
    carrier=Twilio(
        account_sid="AC...",        # optional, defaults to TWILIO_ACCOUNT_SID
        auth_token="...",            # optional, defaults to TWILIO_AUTH_TOKEN
    ),
    phone_number="+15550001234",
    webhook_url="https://patter.yourdomain.com",   # production
)
```

### TypeScript

```typescript
import { Patter, Twilio, OpenAIRealtime2 } from "getpatter";

const phone = new Patter({
  carrier: new Twilio({
    accountSid: process.env.TWILIO_ACCOUNT_SID,
    authToken: process.env.TWILIO_AUTH_TOKEN,
  }),
  phoneNumber: "+15550001234",
  webhookUrl: "https://patter.yourdomain.com",   // production
});
```

## Outbound features Twilio handles natively

| Feature | How |
|---|---|
| **AMD** | `phone.call(to=..., machine_detection=True)` — defaults to True in 0.6.2. |
| **Voicemail drop** | `phone.call(to=..., voicemail_message="...")` — implicitly enables AMD. |
| **Ring timeout** | `phone.call(to=..., ring_timeout=30)` — default 25 s. |
| **Recording** | `phone.serve(agent, recording=True)` — server-wide, all calls recorded. URL surfaced in logs. |
| **Caller ID** | `phone.call(to=..., from_number="+1...")` — must be a Twilio-owned number or verified caller ID. |
| **DTMF** | Patter forwards in/out DTMF events to the agent automatically. |

## Twilio-specific gotchas

- **Trial accounts** can only dial **verified** numbers. Verify the
  destination in **Console → Phone Numbers → Verified Caller IDs**.
- **Geographic permissions**: Twilio blocks high-cost destinations by
  default. Enable per-region in **Voice → Geo Permissions**.
- **AMD `Status` PascalCase**: Twilio's API uses PascalCase params, but
  Patter 0.6.2 normalizes Python `machine_detection` / TS `machineDetection`
  automatically. Don't pass `MachineDetection=`.
- **Public TwiML apps** can override the per-number webhook. If your
  number is bound to a TwiML App, change the App's voice URL, not the
  per-number setting.
