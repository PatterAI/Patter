---
name: line-voice-agent
description: Build voice agents with the Cartesia Line SDK. Supports 100+ LLM providers via LiteLLM with tool calling, multi-agent handoffs, and real-time interruption handling.
---

# Line SDK Voice Agent Guide

Build production voice agents with the Cartesia Line SDK. This guide covers agent creation, tool patterns, multi-agent workflows, and LLM provider configuration.

## How Line Works

Line is Cartesia's voice agent deployment platform. You write Python agent code using the Line SDK, deploy it to Cartesia's managed cloud via the `cartesia` CLI, and Cartesia hosts it with auto-scaling. Cartesia handles STT (Ink), TTS (Sonic), telephony, and audio orchestration. Only one deployment per agent is active at a time; once deployed, your agent receives calls automatically.

```
┌─────────────────────────────────────────────────────────────────┐
│                     Cartesia Line Platform                       │
│  ┌──────────┐    ┌──────────────┐    ┌──────────┐              │
│  │   Ink    │───▶│  Your Agent  │───▶│  Sonic   │              │
│  │  (STT)   │    │  (Line SDK)  │    │  (TTS)   │              │
│  └──────────┘    └──────────────┘    └──────────┘              │
│       ▲                                    │                    │
│       │         Audio Orchestration        │                    │
│       └────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────────┘
        ▲                                    │
        │            WebSocket               ▼
┌───────┴────────────────────────────────────┴───────┐
│              Client (Phone / Web / Mobile)          │
└─────────────────────────────────────────────────────┘
```

**Your code handles:**
- LLM reasoning and conversation flow
- Tool execution (API calls, database lookups)
- Multi-agent coordination and handoffs

**Cartesia handles:**
- Speech-to-text (Ink)
- Text-to-speech (Sonic)
- Real-time audio streaming
- Turn-taking and interruption detection
- Deployment and auto-scaling

**Audio Input Options:**
- [Cartesia Telephony](https://docs.cartesia.ai/build-with-line/integrations/telephony/phone-numbers) - Managed phone numbers
- [Calls API](references/calls-api.md) - Web apps, mobile apps, custom telephony

## Prerequisites

- **Python 3.9+** and [uv](https://docs.astral.sh/uv/) (recommended package manager)
- **Cartesia API key** — get one at [play.cartesia.ai/keys](https://play.cartesia.ai/keys) (used by the CLI and for deployment)
- **LLM API key** — for whichever LLM provider your agent calls (e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`)
- **Cartesia CLI** — install with:
  ```bash
  curl -fsSL https://cartesia.sh | sh
  ```

## Cartesia CLI Reference

```bash
# Authentication
cartesia auth login              # Login with Cartesia API key
cartesia auth status             # Check auth status

# Project Setup
cartesia create [project-name]   # Create project from template
cartesia init                    # Link existing directory to an agent

# Local Development
cartesia chat <port>             # Chat with local agent (text mode)

# Deployment
cartesia deploy                  # Deploy to Cartesia cloud
cartesia status                  # Check deployment status

# Environment Variables (encrypted, stored on Cartesia)
cartesia env set KEY=VALUE       # Set a single env var
cartesia env set --from .env     # Import all vars from .env file
cartesia env rm <name>           # Remove an env var

# Agents & Calls
cartesia agents ls               # List all agents
cartesia deployments ls          # List deployments
cartesia call <phone> [agent-id] # Make outbound call
```

## Quick Start

### 1. Create Project

```bash
cartesia auth login
cartesia create my-agent
cd my-agent
```

### 2. Write Agent Code

`main.py`:

```python
import os
from line.llm_agent import LlmAgent, LlmConfig, end_call
from line.voice_agent_app import AgentEnv, CallRequest, VoiceAgentApp

async def get_agent(env: AgentEnv, call_request: CallRequest):
    return LlmAgent(
        model="anthropic/claude-haiku-4-5-20251001",
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        tools=[end_call],
        config=LlmConfig(
            system_prompt="You are a helpful voice assistant.",
            introduction="Hello! How can I help you today?",
        ),
    )

app = VoiceAgentApp(get_agent=get_agent)

if __name__ == "__main__":
    app.run()
```

### 3. Test Locally

```bash
ANTHROPIC_API_KEY=your-key python main.py
cartesia chat 8000  # Text chat with your running agent
```

### 4. Deploy

```bash
cartesia env set ANTHROPIC_API_KEY=your-key  # Encrypted, stored on Cartesia
cartesia deploy
cartesia status  # Verify deployment is active
```

### 5. Make a Call

```bash
cartesia call +1234567890  # Outbound call via CLI
```

Or trigger calls from the [Cartesia dashboard](https://play.cartesia.ai).

## Project Structure

**Every Line agent project MUST have:**

```
my_agent/
├── main.py          # VoiceAgentApp entry point (REQUIRED)
├── cartesia.toml    # Deployment config, created by cartesia init or cartesia create (REQUIRED)
└── pyproject.toml   # Dependencies: cartesia-line
```

## Core Concepts

### LlmAgent

The main agent class that wraps LLM providers via LiteLLM:

```python
from line.llm_agent import LlmAgent, LlmConfig

agent = LlmAgent(
    model="gemini/gemini-2.5-flash-preview-09-2025",  # LiteLLM model string
    api_key=os.getenv("GEMINI_API_KEY"),              # Provider API key
    tools=[end_call, my_custom_tool],                  # List of tools
    config=LlmConfig(...),                             # Agent configuration
    max_tool_iterations=10,                            # Max tool call loops (default: 10)
)
```

### LlmConfig

Configuration for agent behavior and LLM sampling:

```python
from line.llm_agent import LlmConfig

config = LlmConfig(
    # Agent behavior
    system_prompt="You are a helpful assistant.",
    introduction="Hello! How can I help?",  # Set to "" to wait for user first

    # Sampling parameters (optional)
    temperature=0.7,
    max_tokens=1024,
    top_p=0.9,

    # Resilience (optional)
    num_retries=2,
    timeout=30.0,
    fallbacks=["gpt-4o-mini"],  # Fallback models
)
```

### Dynamic Configuration from CallRequest

Use `LlmConfig.from_call_request()` to pull configuration from the incoming call:

```python
async def get_agent(env: AgentEnv, call_request: CallRequest):
    return LlmAgent(
        model="anthropic/claude-sonnet-4-20250514",
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        tools=[end_call],
        config=LlmConfig.from_call_request(
            call_request,
            fallback_system_prompt="Default system prompt if not in request.",
            fallback_introduction="Default introduction if not in request.",
            temperature=0.7,  # Additional LlmConfig options
        ),
    )
```

Priority order: CallRequest value > fallback argument > SDK default

### VoiceAgentApp

The application harness that manages HTTP endpoints and WebSocket connections:

```python
from line.voice_agent_app import VoiceAgentApp, AgentEnv, CallRequest

async def get_agent(env: AgentEnv, call_request: CallRequest):
    # env.loop - asyncio event loop
    # call_request.call_id - unique call identifier
    # call_request.agent.system_prompt - from request
    # call_request.agent.introduction - from request
    # call_request.metadata - custom metadata dict
    return LlmAgent(...)

app = VoiceAgentApp(get_agent=get_agent)
app.run(host="0.0.0.0", port=8000)
```

## Built-in Tools

Import from `line.llm_agent`:

```python
from line.llm_agent import end_call, send_dtmf, transfer_call, web_search
```

### end_call

End the current call. Tell the LLM to say goodbye before calling this.

```python
tools=[end_call]
# System prompt: "Say goodbye before ending the call with end_call."
```

### send_dtmf

Send DTMF tones (touch-tone buttons). Useful for IVR navigation.

```python
tools=[send_dtmf]
# Buttons: "0"-"9", "*", "#" (strings, not integers!)
```

### transfer_call

Transfer to another phone number (E.164 format required).

```python
tools=[transfer_call]
# Example: +14155551234
```

### web_search

Search the web for real-time information. Uses native LLM web search when available, falls back to DuckDuckGo.

```python
# Default settings
tools=[web_search]

# Custom settings
tools=[web_search(search_context_size="high")]  # "low", "medium", "high"
```

## Custom Tool Types

Three tool paradigms for different use cases:

| Type | Decorator | Use Case | Result Handling |
|------|-----------|----------|-----------------|
| **Loopback** | `@loopback_tool` | API calls, database lookups | Result sent back to LLM |
| **Passthrough** | `@passthrough_tool` | End call, transfer, DTMF | Bypasses LLM, goes to user |
| **Handoff** | `@handoff_tool` | Multi-agent workflows | Transfers control to another agent |

### Tool Type Decision Tree

```
Does the result need LLM processing?
├─ YES → @loopback_tool
│   └─ Is it long-running (>1s)? → @loopback_tool(is_background=True)
│       └─ Yield interim status, then final result
├─ NO, deterministic action → @passthrough_tool
│   └─ Yields OutputEvent objects directly (AgentSendText, AgentEndCall, etc.)
└─ Transfer to another agent → @handoff_tool or agent_as_handoff()
```

### Loopback Tools

Results are sent back to the LLM to inform the next response:

```python
from typing import Annotated
from line.llm_agent import loopback_tool, ToolEnv

@loopback_tool
async def get_order_status(
    ctx: ToolEnv,
    order_id: Annotated[str, "The order ID to look up"],
) -> str:
    """Look up the current status of an order."""
    order = await db.get_order(order_id)
    return f"Order {order_id} status: {order.status}, ETA: {order.eta}"
```

**Parameter syntax:**
- First parameter MUST be `ctx: ToolEnv`
- Use `Annotated[type, "description"]` for LLM-visible parameters
- Tool description comes from the docstring
- Optional parameters need default values (not just `Optional[T]`)

```python
@loopback_tool
async def search_products(
    ctx: ToolEnv,
    query: Annotated[str, "Search query"],
    category: Annotated[str, "Product category"] = "all",  # Optional with default
    limit: Annotated[int, "Max results"] = 10,
) -> str:
    """Search the product catalog."""
    ...
```

### Passthrough Tools

Results bypass the LLM and go directly to the user/system:

```python
from line.events import AgentSendText, AgentTransferCall
from line.llm_agent import passthrough_tool, ToolEnv

@passthrough_tool
async def transfer_to_support(
    ctx: ToolEnv,
    reason: Annotated[str, "Reason for transfer"],
):
    """Transfer the call to the support team."""
    yield AgentSendText(text="Let me transfer you to our support team now.")
    yield AgentTransferCall(target_phone_number="+18005551234")
```

**Output event types** (from `line.events`):
- `AgentSendText(text="...")` - Speak text to user
- `AgentEndCall()` - End the call
- `AgentTransferCall(target_phone_number="+1...")` - Transfer call
- `AgentSendDtmf(button="5")` - Send DTMF tone

### Handoff Tools

Transfer control to another agent. See [Multi-Agent Workflows](references/multi-agent-workflows.md).

## Model Selection Strategy

**Use FAST models for the main conversational agent:**
- `gemini/gemini-2.5-flash-preview-09-2025` (recommended)
- `anthropic/claude-haiku-4-5-20251001`
- `gpt-4o-mini`

**Use POWERFUL models only via background tool calls** for complex reasoning:
- `anthropic/claude-opus-4-5`
- `gpt-4o`

This pattern keeps conversations responsive while accessing deep reasoning when needed. See the Two-Tier Agent Pattern in [Advanced Patterns](references/advanced-patterns.md) for implementation.

## LLM Providers

Line SDK uses LiteLLM model strings. Common formats:

| Provider | Format | Example |
|----------|--------|---------|
| OpenAI | `model_name` | `gpt-4o`, `gpt-4o-mini` |
| Anthropic | `anthropic/model_name` | `anthropic/claude-sonnet-4-20250514` |
| Google Gemini | `gemini/model_name` | `gemini/gemini-2.5-flash-preview-09-2025` |
| Azure OpenAI | `azure/deployment_name` | `azure/my-gpt4-deployment` |

Set the appropriate API key environment variable:
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`
- `AZURE_API_KEY`

Full list: https://docs.litellm.ai/docs/providers

## Common Patterns

### Agent with Custom Tools

```python
from typing import Annotated
from line.llm_agent import LlmAgent, LlmConfig, loopback_tool, end_call, ToolEnv

@loopback_tool
async def check_appointment(
    ctx: ToolEnv,
    date: Annotated[str, "Date in YYYY-MM-DD format"],
) -> str:
    """Check available appointment slots for a given date."""
    slots = await calendar.get_available_slots(date)
    return f"Available slots on {date}: {', '.join(slots)}"

@loopback_tool
async def book_appointment(
    ctx: ToolEnv,
    date: Annotated[str, "Date in YYYY-MM-DD format"],
    time: Annotated[str, "Time in HH:MM format"],
    name: Annotated[str, "Customer name"],
) -> str:
    """Book an appointment slot."""
    result = await calendar.book(date, time, name)
    return f"Appointment booked for {name} on {date} at {time}. Confirmation: {result.id}"

async def get_agent(env: AgentEnv, call_request: CallRequest):
    return LlmAgent(
        model="gemini/gemini-2.5-flash-preview-09-2025",
        api_key=os.getenv("GEMINI_API_KEY"),
        tools=[check_appointment, book_appointment, end_call],
        config=LlmConfig(
            system_prompt="""You are an appointment scheduling assistant.
Help users check availability and book appointments.
Always confirm the booking details before finalizing.""",
            introduction="Hi! I can help you schedule an appointment. What date works for you?",
        ),
    )
```

### Wait for User to Speak First

Set `introduction=""` to have the agent wait for the user:

```python
config=LlmConfig(
    system_prompt="You are a helpful assistant.",
    introduction="",  # Empty string = wait for user
)
```

### Form Filling Pattern

See the form filler example for collecting structured data via voice. Key pattern:

```python
@loopback_tool
async def record_answer(
    ctx: ToolEnv,
    answer: Annotated[str, "The user's answer"],
) -> dict:
    """Record an answer to the current question."""
    # Process and validate answer
    # Return next question or completion status
    return {"next_question": "What is your email?", "is_complete": False}
```

## Common Mistakes to Avoid

1. **Missing `end_call` tool** - If not included (or a similar custom tool), the agent cannot end the call on its own and must wait for the user to hang up

2. **Raising exceptions in tools** - Return user-friendly error strings:
   ```python
   # BAD
   raise ValueError("Invalid order ID")

   # GOOD
   return "I couldn't find that order. Please check the ID and try again."
   ```

3. **Forgetting `ctx` parameter** - First parameter must be `ctx: ToolEnv`:
   ```python
   # GOOD
   @loopback_tool
   async def my_tool(ctx: ToolEnv, order_id: Annotated[str, "Order ID"]): ...
   ```

4. **Forgetting `event` in handoff tools** - Handoff tools MUST have `event` parameter:
   ```python
   # GOOD
   @handoff_tool
   async def my_handoff(ctx: ToolEnv, param: Annotated[str, "desc"], event): ...
   ```

5. **Missing Annotated descriptions** - LLM needs parameter descriptions:
   ```python
   # GOOD
   async def my_tool(ctx, order_id: Annotated[str, "The order ID to look up"]): ...
   ```

6. **Blocking on long operations** - Use `is_background=True` and yield interim status:
   ```python
   @loopback_tool(is_background=True)
   async def slow_search(ctx: ToolEnv, query: Annotated[str, "Query"]):
       yield "Searching..."  # Immediate feedback
       result = await slow_operation()
       yield result
   ```

7. **Using sync APIs directly** - Wrap sync calls with `asyncio.to_thread()`:
   ```python
   result = await asyncio.to_thread(sync_api_call, params)
   ```

8. **Using slow models for main conversation** - Use fast models (haiku, flash, mini) for the main agent, powerful models only via background tools.

## Reference Documentation

- [Tool Patterns](references/tool-patterns.md) - Deep dive on tool implementation
- [Multi-Agent Workflows](references/multi-agent-workflows.md) - Handoffs, wrappers, guardrails
- [Advanced Patterns](references/advanced-patterns.md) - Background tools, state, events
- [Calls API](references/calls-api.md) - WebSocket integration for web/mobile apps
- [Troubleshooting](references/troubleshooting.md) - Common issues and debugging

## Key Imports

```python
# Core
from line.llm_agent import LlmAgent, LlmConfig
from line.voice_agent_app import VoiceAgentApp, AgentEnv, CallRequest

# Built-in tools
from line.llm_agent import end_call, send_dtmf, transfer_call, web_search

# Tool decorators
from line.llm_agent import loopback_tool, passthrough_tool, handoff_tool

# Tool context
from line.llm_agent import ToolEnv

# Multi-agent
from line.llm_agent import agent_as_handoff

# Events (for passthrough/handoff tools and custom agents)
from line.events import (
    AgentSendText,
    AgentEndCall,
    AgentTransferCall,
    AgentSendDtmf,
    AgentUpdateCall,
)
```

## Key Reference Files

When implementing Line SDK agents, reference these example files:
- `examples/basic_chat/main.py` - Simplest agent pattern
- `examples/form_filler/` - Loopback tools with state
- `examples/chat_supervisor/main.py` - Background tools with two-tier model strategy
- `examples/transfer_agent/main.py` - Multi-agent handoffs
- `examples/echo/tools.py` - Custom handoff tools
