# Patter Python SDK

Connect AI agents to phone numbers with ~10 lines of code.

```bash
pip install patter[local]
```

## Quick Example

```python
import asyncio
from patter import Patter

async def main():
    phone = Patter(
        mode="local",
        twilio_sid="AC...", twilio_token="...",
        openai_key="sk-...",
        phone_number="+1...",
        webhook_url="xxx.ngrok-free.dev",
    )

    agent = phone.agent(
        system_prompt="You are a friendly customer service agent.",
        voice="alloy",
        first_message="Hello! How can I help?",
    )

    await phone.serve(agent=agent, port=8000)

asyncio.run(main())
```

## Documentation

See the [main README](../README.md) for full documentation, features, and API reference.

## License

MIT
