# Troubleshooting

Common issues and solutions when building Line SDK voice agents.

## Tool Signature Errors

### Missing ctx Parameter

**Error:**

```
TypeError: Tool 'my_tool' must have 'ctx' or 'context' as first parameter
```

**Fix:** First parameter must be `ctx: ToolEnv`:

```python
# Wrong
@loopback_tool
async def my_tool(order_id: str):  # Missing ctx!
    ...

# Correct
@loopback_tool
async def my_tool(ctx: ToolEnv, order_id: Annotated[str, "Order ID"]):
    ...
```

### Missing event Parameter (Handoff Tools)

**Error:**

```
TypeError: Handoff tool 'transfer_to_support' must have 'event' parameter
```

**Fix:** Handoff tools require an `event` parameter:

```python
# Wrong
@handoff_tool
async def transfer_to_support(ctx: ToolEnv, reason: str):
    ...

# Correct
@handoff_tool
async def transfer_to_support(ctx: ToolEnv, reason: Annotated[str, "Reason"], event):
    if isinstance(event, AgentHandedOff):
        ...
```

### Wrong Parameter Order

**Fix:** `ctx` must be the first parameter. `event` is required but can be in any position after `ctx`:

```python
# Wrong - ctx is not first
@handoff_tool
async def my_handoff(reason: str, ctx: ToolEnv, event):
    ...

# Correct - ctx first, event last (SDK convention)
@handoff_tool
async def my_handoff(ctx: ToolEnv, reason: Annotated[str, "Reason"], event):
    ...
```

## Model Compatibility Issues

### Unknown Model Error

**Error:**

```
litellm.exceptions.BadRequestError: Unknown model: my-model
```

**Fix:** Use correct LiteLLM model string format:

```python
# Provider prefixes
"gpt-4o"                                   # OpenAI (no prefix)
"anthropic/claude-sonnet-4-20250514"       # Anthropic
"gemini/gemini-2.5-flash-preview-09-2025"  # Google
"azure/my-deployment"                       # Azure OpenAI

# Full model list: https://models.litellm.ai/
```

### Missing API Key

**Error:**

```
AuthenticationError: No API key provided
```

**Fix:** Set the appropriate environment variable:

```bash
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=AI...
```

Or pass directly:

```python
LlmAgent(
    model="gpt-4o",
    api_key=os.getenv("OPENAI_API_KEY"),  # Explicit key
    ...
)
```

### Web Search Not Working

Some models don't support native web search. Line SDK auto-detects and falls back to DuckDuckGo:

```
INFO: Model gpt-4o doesn't support native web search, using fallback tool
```

For the fallback, install: `pip install duckduckgo-search`

## DTMF Issues

### Invalid DTMF Button

**Error:**

```
ValueError: Invalid DTMF button: 10
```

**Fix:** DTMF buttons must be strings, not integers:

```python
# Wrong
AgentSendDtmf(button=5)    # Integer
AgentSendDtmf(button="10") # Invalid - not a single button

# Correct
AgentSendDtmf(button="5")  # String
AgentSendDtmf(button="*")  # Star
AgentSendDtmf(button="#")  # Hash
```

Valid buttons: `"0"`, `"1"`, `"2"`, `"3"`, `"4"`, `"5"`, `"6"`, `"7"`, `"8"`, `"9"`, `"*"`, `"#"`

## Phone Number Format

### Transfer Call Fails

**Error:**

```
I'm sorry, that phone number appears to be invalid.
```

**Fix:** Use E.164 format (international format with +):

```python
# Wrong
AgentTransferCall(target_phone_number="555-123-4567")
AgentTransferCall(target_phone_number="5551234567")

# Correct
AgentTransferCall(target_phone_number="+15551234567")
AgentTransferCall(target_phone_number="+44207946090")  # UK
```

## Introduction Not Playing

### Agent Stays Silent at Start

**Possible causes:**

1. **Empty introduction:**

```python
# This makes agent wait for user
config=LlmConfig(introduction="")

# Use a greeting instead
config=LlmConfig(introduction="Hello! How can I help?")
```

1. **Introduction is None:**

```python
# None also skips introduction
config=LlmConfig(introduction=None)
```

1. **CallStarted not triggering:**
Check your run filter includes `CallStarted`:

```python
def run_filter(event):
    return isinstance(event, (CallStarted, UserTurnEnded, CallEnded))
```

## Optional Parameters

### LLM Not Recognizing Optional Parameter

**Issue:** LLM always provides a value even when optional

**Fix:** Use default values, not just `Optional[T]`:

```python
# Wrong - Optional doesn't make it optional for the LLM schema
@loopback_tool
async def search(ctx: ToolEnv, query: Annotated[Optional[str], "Search query"]):
    ...

# Correct - default value makes it optional
@loopback_tool
async def search(
    ctx: ToolEnv,
    query: Annotated[str, "Search query"],
    limit: Annotated[int, "Max results"] = 10,  # Optional with default
):
    ...
```

## Tool Not Being Called

### LLM Ignores Available Tool

**Possible causes:**

1. **Poor description:** Make the tool description clear and action-oriented:

```python
# Vague
"""Get data."""

# Clear
"""Look up the current status and location of a customer's order by order ID."""
```

1. **Missing from system prompt:** Guide the LLM to use the tool:

```python
config=LlmConfig(
    system_prompt="""You are an order assistant.
When users ask about their orders, use the get_order_status tool.
Always confirm the order ID before looking it up."""
)
```

1. **Wrong tool type:** Ensure tool type matches use case:
   - `@loopback_tool` - Results inform LLM response
   - `@passthrough_tool` - Actions bypass LLM
   - `@handoff_tool` - Transfer to another agent

## Background Tool Issues

### Background Tool Results Lost

**Issue:** Background tool completes but user waits in silence

**Fix:** Use `yield` for interim status messages. A plain `return` does work (the SDK normalizes it to a single yield), but it provides no interim feedback:

```python
# Works, but no interim feedback - user waits in silence
@loopback_tool(is_background=True)
async def slow_lookup(ctx: ToolEnv, id: Annotated[str, "ID"]):
    result = await slow_api.fetch(id)
    return result  # Triggers one completion, but no interim status

# Better - yield interim status so agent can speak while waiting
@loopback_tool(is_background=True)
async def slow_lookup(ctx: ToolEnv, id: Annotated[str, "ID"]):
    yield "Looking that up..."        # Immediate feedback
    result = await slow_api.fetch(id)
    yield result                      # Final result
```

## Handoff Issues

### Handoff Target Never Receives Events

**Issue:** After handoff, subsequent events don't reach the target agent

**Fix:** Ensure handoff tool stores the target correctly:

```python
@handoff_tool
async def transfer(ctx: ToolEnv, event):
    if isinstance(event, AgentHandedOff):
        # Handle initial handoff
        yield AgentSendText(text="Transferring...")
        async for output in target_agent.process(ctx.turn_env, CallStarted()):
            yield output
        return  # Important: return after initial handling

    # Handle subsequent events
    async for output in target_agent.process(ctx.turn_env, event):
        yield output
```

## WebSocket Connection Issues

### Connection Closes Immediately

**Check:**

1. Server is running: `curl http://localhost:8000/status`
2. WebSocket URL is correct (from `/chats` response)
3. No exceptions in `get_agent`:

```python
async def get_agent(env, call_request):
    try:
        return LlmAgent(...)
    except Exception as e:
        logger.error(f"Failed to create agent: {e}")
        raise
```

### Connection Drops Mid-Call

**Check logs for:**

- `Error in agent.process` - Exception in your agent code
- `Error in websocket loop` - Message processing error
- `WebSocket disconnected` - Client disconnected

## Debugging Tips

### Enable Verbose Logging

```python
from loguru import logger
import sys

# Configure loguru for detailed output
logger.remove()
logger.add(sys.stderr, level="DEBUG")
```

### Log Tool Calls

```python
@loopback_tool
async def my_tool(ctx: ToolEnv, param: Annotated[str, "Param"]):
    """My tool."""
    logger.info(f"my_tool called with param={param}")
    result = await do_something(param)
    logger.info(f"my_tool returning: {result}")
    return result
```

### Test Tools Directly

```python
# Test tool outside of agent
from line.llm_agent import ToolEnv
from line.agent import TurnEnv

async def test_tool():
    ctx = ToolEnv(turn_env=TurnEnv())

    # For tools that use `return`:
    result = await my_tool.func(ctx, param="test")
    print(f"Result: {result}")

    # For tools that use `yield` (async generators):
    async for result in my_generator_tool.func(ctx, param="test"):
        print(f"Result: {result}")

import asyncio
asyncio.run(test_tool())
```

### Check Event History

```python
async def process(self, env, event):
    # Debug: print full history
    for i, e in enumerate(event.history or []):
        logger.debug(f"History[{i}]: {type(e).__name__} - {e}")

    async for output in self.inner.process(env, event):
        yield output
```
