# Telnyx carrier setup

## Required env

```bash
TELNYX_API_KEY=KEY...
TELNYX_CONNECTION_ID=2000...        # your Voice API SDK connection
PATTER_PHONE_NUMBER=+15550001234    # the number you bought
```

`API key` is in **Portal → Account → API V2 Keys**. `Connection ID` is in
**Portal → Connections → SIP / SDK Connections** — pick the one you want
Patter to use (or create a new **Voice API SDK** connection).

## Buy a number

1. Open <https://portal.telnyx.com/#/app/numbers/search-numbers>.
2. Search by country / area code → buy.
3. Open the number's settings, set **Connection** to your SDK connection.

## Wire the Call Control webhook

1. **Portal → Connections → your SDK connection → Webhook URL**.
2. URL: paste your Patter server's public URL + `/webhooks/telnyx/incoming`.
3. HTTP method: `POST`.
4. **Webhook API version**: v2 (not v1).
5. Save.

## Code

### Python

```python
from getpatter import Patter, Telnyx, OpenAIRealtime2

phone = Patter(
    carrier=Telnyx(
        api_key="KEY...",              # defaults to TELNYX_API_KEY
        connection_id="2000...",       # defaults to TELNYX_CONNECTION_ID
    ),
    phone_number="+15550001234",
    webhook_url="https://patter.yourdomain.com",
)
```

### TypeScript

```typescript
import { Patter, Telnyx, OpenAIRealtime2 } from "getpatter";

const phone = new Patter({
  carrier: new Telnyx({
    apiKey: process.env.TELNYX_API_KEY,
    connectionId: process.env.TELNYX_CONNECTION_ID,
  }),
  phoneNumber: "+15550001234",
  webhookUrl: "https://patter.yourdomain.com",
});
```

## Outbound on Telnyx

| Feature | How |
|---|---|
| **AMD** | `phone.call(to=..., machine_detection=True)` — defaults to True. Telnyx AMD is on the Call Control API. |
| **Voicemail drop** | `phone.call(to=..., voicemail_message="...")` — implicitly enables AMD. |
| **Ring timeout** | `phone.call(to=..., ring_timeout=30)` — default 25 s. |
| **Recording** | `phone.serve(agent, recording=True)` — server-wide. URL appears in logs / Telnyx Storage. |
| **Caller ID** | `phone.call(to=..., from_number="+1...")`. |
| **Outbound Voice Profile** required | Attach a profile to the number in **Portal → Outbound Voice Profiles**, otherwise dial-out fails. |

## Telnyx-specific gotchas

- **Ed25519 signature verification with timestamp**: Telnyx signs every
  webhook with Ed25519 and includes a timestamp. Patter rejects requests
  older than ±5 min — clock skew on your server matters.
- **Connection ID is required**, not optional. Telnyx error 90001 means
  the number isn't bound to a connection.
- **Outbound Voice Profile** must be attached to the number for outbound
  calls. Inbound works without it.
- **Native PCM 16 kHz**: Telnyx sends 16 kHz PCM up to Patter. Patter
  transcodes to whatever the engine expects. You don't need to touch this.
- **EU traffic**: Telnyx data residency can be locked to EU regions —
  useful for GDPR. Set the connection's region in the portal.
