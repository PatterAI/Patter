---
name: add-tools-and-handoffs
description: >
  Give a Patter voice agent custom tools (function calling), enable built-in
  handoffs (`transfer_call`, `end_call`), and wire output guardrails. Use
  when the user wants the agent to look up customer data, query a CRM,
  schedule a callback, transfer to a human, hang up on abuse, refuse to say
  certain phrases, validate caller intent, or do anything beyond
  conversation. Covers `@tool` (Python) and `defineTool` (TypeScript), as
  well as JSON-Schema webhook tools, in Patter 0.6.2.
license: MIT
compatibility: >
  Requires Patter >= 0.6.2 and an agent already built via `build-voice-agent`.
  Tools work in all three modes (Realtime, ConvAI, Pipeline), though
  Realtime supports the richest tool flow with `reassurance` messages.
metadata:
  author: patter
  version: "0.1.0"
  parity: both
---

# Add tools and handoffs to a Patter agent

Patter agents have **two built-in tools** (`transfer_call`, `end_call`) plus
unlimited **custom tools**. Custom tools can be **handler-based** (your
Python/TS function runs in-process) or **webhook-based** (the agent POSTs
JSON arguments to your URL). Both shapes use the same `Tool` model. Output
guardrails block or rephrase agent responses before they reach TTS.

## Built-in tools (always available)

Every agent automatically has:

- **`transfer_call(target: str)`** — bridge to another phone number. Caller
  hears the new party, your Patter session ends.
- **`end_call()`** — hang up. Use for "goodbye" flows or abusive callers.

You don't have to register these. The LLM picks them when the system prompt
authorizes it.

```python
agent = phone.agent(
    engine=OpenAIRealtime2(),
    system_prompt=(
        "You are Mia, the receptionist for Acme. "
        "If the caller asks for a human, call `transfer_call` with target='+14155559999'. "
        "If the caller wants to hang up, call `end_call`."
    ),
    first_message="Hi, how can I help?",
)
```

## Custom tools — handler form

The `@tool` decorator (Python) and `defineTool` (TypeScript) auto-extract the
JSON schema from your function signature and docstring. Best for simple
synchronous lookups.

### Python

```python
from getpatter import Patter, Twilio, OpenAIRealtime2, tool

@tool
async def lookup_customer(phone: str) -> dict:
    """Look up customer details by phone number.

    Args:
        phone: E.164 phone number, e.g. +14155551234.

    Returns:
        Customer name, tier, and last order date.
    """
    # your CRM call here
    return {"name": "Jane Doe", "tier": "gold", "last_order": "2026-04-12"}

phone = Patter(carrier=Twilio(), phone_number="+15550001234")
agent = phone.agent(
    engine=OpenAIRealtime2(),
    system_prompt="You are a customer-service agent. Use `lookup_customer` when you need account info.",
    first_message="Hi, can I help with your account?",
    tools=[lookup_customer],
)
```

### TypeScript

```typescript
import { Patter, Twilio, OpenAIRealtime2, defineTool } from "getpatter";

const lookupCustomer = defineTool({
  name: "lookup_customer",
  description: "Look up customer details by phone number.",
  parameters: {
    type: "object",
    properties: {
      phone: { type: "string", description: "E.164 phone number, e.g. +14155551234." },
    },
    required: ["phone"],
  },
  handler: async ({ phone }) => {
    // your CRM call here
    return { name: "Jane Doe", tier: "gold", lastOrder: "2026-04-12" };
  },
});

const phone = new Patter({ carrier: new Twilio(), phoneNumber: "+15550001234" });
const agent = phone.agent({
  engine: new OpenAIRealtime2(),
  systemPrompt: "You are a customer-service agent. Use lookup_customer when you need account info.",
  firstMessage: "Hi, can I help with your account?",
  tools: [lookupCustomer],
});
```

## Custom tools — webhook form

Better for cross-language deployments or when the tool lives in a separate
service. Patter POSTs the tool args as JSON to your URL and uses the JSON
response as the tool result.

```python
from getpatter import Tool

crm_lookup = Tool(
    name="lookup_customer",
    description="Look up customer details by phone number.",
    parameters={
        "type": "object",
        "properties": {"phone": {"type": "string"}},
        "required": ["phone"],
    },
    webhook_url="https://api.acme.com/crm/lookup",
)

agent = phone.agent(
    engine=OpenAIRealtime2(),
    system_prompt="…",
    tools=[crm_lookup],
)
```

The endpoint receives `POST` with body `{"phone": "+14155551234"}` and must
respond with JSON. Authentication: add your own header via reverse proxy,
or use Patter's `Tool(webhook_headers={...})` (omitted here for brevity).

## Reassurance messages (Realtime mode)

In Realtime mode, tool calls happen *during* the call — the LLM has to
decide whether to keep talking while the tool runs. Set `reassurance` to
play a "hold on" message while the handler executes:

```python
@tool(reassurance="Let me check that for you, one moment.")
async def lookup_customer(phone: str) -> dict:
    await asyncio.sleep(2)  # imagine a slow CRM
    return {"name": "Jane"}
```

Without it, there's silence while the tool runs — which sounds broken to
the caller.

## Output guardrails

Block specific phrases or run a custom check before the agent's response
reaches the TTS.

```python
from getpatter import guardrail, Patter, Twilio, OpenAIRealtime2

@guardrail
def no_competitors(text: str) -> bool:
    """Block any response that mentions a competitor."""
    return "patter" not in text.lower() or "alternative" not in text.lower()

phone = Patter(carrier=Twilio(), phone_number="+15550001234")
agent = phone.agent(
    engine=OpenAIRealtime2(),
    system_prompt="You are a sales agent for Acme.",
    first_message="Hi, how can I help?",
    guardrails=[no_competitors],
)
```

When a guardrail blocks, the agent speaks the guardrail's `replacement`
text instead (default: `"I'm sorry, I can't respond to that."`).

For a simple keyword list:

```python
from getpatter import Guardrail

agent = phone.agent(
    ...,
    guardrails=[Guardrail(
        name="no-pii",
        blocked_terms=["social security", "credit card"],
        replacement="I can't help with sensitive information. Let me transfer you.",
    )],
)
```

## Pipeline hooks (advanced)

For more control than guardrails, pipeline hooks intercept every leg of
the pipeline (only available in Pipeline mode):

| Hook | Fires when | Use case |
|---|---|---|
| `before_send_to_stt` | Audio in | Echo cancellation, noise gate. |
| `after_transcribe` | After STT | PII redaction, command parsing. |
| `before_llm` | Before LLM | RAG injection, context rewrites. |
| `after_llm` | After LLM | Output filter, persona enforcement. |
| `before_synthesize` | Before TTS | Sentence chunking, voice-tag insertion. |
| `after_synthesize` | After TTS | Audio post-processing. |

Hooks are async functions returning `None` (no change) or the modified
value. See <https://docs.getpatter.com/python-sdk/events>.

## Gotchas

- **`transfer_call` requires `target`** in E.164. Tell the agent in the
  system prompt: "Use `transfer_call` with the target number `+1...`."
- **Webhook tools time out at 30 s**. For longer ops, return a job ID and
  expose a second tool to poll.
- **JSON Schema strict mode**: pass `strict=True` (Python) /
  `strict: true` (TS) to force OpenAI's strict-mode schema validation.
  Catches bad LLM args at the boundary.
- **Guardrails fire post-LLM, pre-TTS** — the caller never hears the
  filtered text. The agent's memory still has the original though.
- **Reassurance only works in Realtime mode**. In Pipeline mode the LLM
  generates the reassurance text itself; ConvAI mode doesn't expose this.

## Common errors

| Symptom | Fix |
|---|---|
| LLM never calls the tool | System prompt isn't authorizing it. Add "Use the X tool when ..." explicitly. |
| `ValueError: parameters must be a JSON Schema object` | You passed `parameters={"phone": "string"}` instead of `{"type": "object", "properties": {...}}`. |
| Tool returns `None` and LLM gets stuck | Always return *something* — a status string, a dict, an empty `{}`. Never `None`. |
| Transfer rings out | The `target` number isn't a Twilio-owned or verified caller ID (Twilio trial). |

## Related skills

- [`build-voice-agent`](../build-voice-agent/) — define the agent the tools attach to.
- [`inspect-calls-and-metrics`](../inspect-calls-and-metrics/) — see tool-call latency in the dashboard.

## References

- Patter Tools doc: <https://docs.getpatter.com/python-sdk/tools> · <https://docs.getpatter.com/typescript-sdk/tools>
- Patter Guardrails doc: <https://docs.getpatter.com/python-sdk/guardrails> · <https://docs.getpatter.com/typescript-sdk/guardrails>
- Pipeline hooks: <https://docs.getpatter.com/python-sdk/events>
