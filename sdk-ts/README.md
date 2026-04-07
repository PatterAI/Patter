# Patter TypeScript SDK

Connect AI agents to phone numbers with ~10 lines of code.

```bash
npm install patter
```

## Quick Example

```typescript
import { Patter } from "patter";

const phone = new Patter({
  mode: "local",
  twilioSid: "AC...", twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+1...",
  webhookUrl: "xxx.ngrok-free.dev",
});

const agent = phone.agent({
  systemPrompt: "You are a friendly customer service agent.",
  voice: "alloy",
  firstMessage: "Hello! How can I help?",
});

await phone.serve({ agent, port: 8000 });
```

## Documentation

See the [main README](../README.md) for full documentation, features, and API reference.

## License

MIT
