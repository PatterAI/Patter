# Advanced Patterns

Advanced techniques for Line SDK voice agents including background tools, state management, events, and complex workflows.

## Two-Tier Agent Pattern

Use a fast model for responsive conversation while accessing a powerful model for complex reasoning via background tools:

```python
import os
from typing import Annotated, Optional
from line.agent import AgentClass, TurnEnv
from line.events import AgentSendText, CallEnded, InputEvent, UserTextSent
from line.llm_agent import LlmAgent, LlmConfig, ToolEnv, end_call, loopback_tool


class ChatSupervisorAgent(AgentClass):
    """Fast Haiku for chat + powerful Opus for complex questions via background tool."""

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")

        # Supervisor: powerful model for deep reasoning (only called via tool)
        self._supervisor = LlmAgent(
            model="anthropic/claude-opus-4-5",
            api_key=self._api_key,
            config=LlmConfig(system_prompt="You are a deep reasoning assistant..."),
        )

        # Chatter: fast model for responsive conversation
        self._chatter = LlmAgent(
            model="anthropic/claude-haiku-4-5-20251001",
            api_key=self._api_key,
            tools=[self.ask_supervisor, end_call],
            config=LlmConfig(
                system_prompt="You handle conversations. Use ask_supervisor for complex questions.",
                introduction="Hello! How can I help?",
            ),
        )

    async def process(self, env: TurnEnv, event: InputEvent):
        if isinstance(event, CallEnded):
            await self._cleanup()
            return
        async for output in self._chatter.process(env, event):
            yield output

    async def _cleanup(self):
        await self._chatter.cleanup()
        await self._supervisor.cleanup()

    @loopback_tool(is_background=True)
    async def ask_supervisor(
        self,
        ctx: ToolEnv,
        question: Annotated[str, "The complex question requiring deep reasoning"],
    ):
        """Consult powerful model for complex questions. Runs in background."""
        yield "Pondering your question deeply, will get back to you shortly"

        # Call supervisor with the question
        supervisor_event = UserTextSent(content=question, history=[])
        full_response = ""
        async for output in self._supervisor.process(ctx.turn_env, supervisor_event):
            if isinstance(output, AgentSendText):
                full_response += output.text
        yield full_response
```

### Key Benefits

- **Responsive conversation**: Fast model (Haiku/Flash/Mini) handles most interactions
- **Deep reasoning on demand**: Powerful model (Opus/GPT-4o) only called when needed
- **Non-blocking**: Background tool allows conversation to continue during reasoning
- **Cost effective**: Only uses expensive model for complex questions

## Background Tools

Background tools run asynchronously without blocking the conversation:

```python
from line.llm_agent import loopback_tool, ToolEnv

@loopback_tool(is_background=True)
async def slow_database_lookup(
    ctx: ToolEnv,
    customer_id: Annotated[str, "Customer ID"],
):
    """Look up customer data (may take several seconds)."""
    # First yield - immediate response so agent can speak
    yield "Looking up your account information..."

    # Slow operation runs in background
    data = await slow_external_api.fetch_customer(customer_id)

    # Second yield - result when ready
    yield f"Found your account: {data.name}, balance: ${data.balance}"
```

### Background Tool Lifecycle

1. User asks "What's my account balance?"
2. LLM calls `slow_database_lookup(customer_id="123")`
3. Tool yields "Looking up..." immediately
4. LLM receives result, says "Let me check on that"
5. Meanwhile, slow API call runs in background
6. **Option A**: User says "thanks" during wait
   - LLM responds to user
   - When lookup completes, result triggers new completion
   - LLM says "Your balance is $1,234"
7. **Option B**: User stays silent
   - When lookup completes, result triggers completion
   - LLM says "Thank you for waiting. Your balance is $1,234"

### Key Properties

- `is_background=True` on `@loopback_tool` decorator
- Each yield triggers a new LLM completion
- Tool continues running even if user interrupts
- Results are added to conversation history

## Event System

### Input Events (harness -> agent)

Events received by `agent.process(env, event)`:

```python
from line.events import (
    # Call lifecycle
    CallStarted,      # Call connected
    CallEnded,        # Call disconnected

    # User turn events
    UserTurnStarted,  # User began speaking
    UserTextSent,     # Transcribed speech (content: str)
    UserDtmfSent,     # DTMF button press (button: str)
    UserTurnEnded,    # User stopped speaking (content: list)

    # Agent turn events (from TTS confirmation)
    AgentTurnStarted, # Agent began speaking
    AgentTextSent,    # Confirmed speech (content: str)
    AgentDtmfSent,    # Confirmed DTMF (button: str)
    AgentTurnEnded,   # Agent stopped speaking

    # Handoff
    AgentHandedOff,   # Control transferred to handoff tool
)
```

### Output Events (agent -> harness)

Events yielded from `agent.process()`:

```python
from line.events import (
    # Speech and actions
    AgentSendText,      # Speak text: AgentSendText(text="Hello")
    AgentSendDtmf,      # Send DTMF: AgentSendDtmf(button="5")
    AgentEndCall,       # End call: AgentEndCall()
    AgentTransferCall,  # Transfer: AgentTransferCall(target_phone_number="+1...")

    # Tool tracking
    AgentToolCalled,    # Tool invoked (tool_call_id, tool_name, tool_args)
    AgentToolReturned,  # Tool result (tool_call_id, tool_name, tool_args, result)

    # Dynamic call settings
    AgentUpdateCall,    # Update voice: AgentUpdateCall(voice_id="...", pronunciation_dict_id="...")

    # Logging
    LogMetric,          # Log metric: LogMetric(name="latency", value=1.5)
    LogMessage,         # Log message: LogMessage(name="event", level="info", message="...")
)
```

### Event History

Each input event includes full conversation history:

```python
async def process(self, env: TurnEnv, event: InputEvent):
    # event.history contains all prior events
    for e in event.history:
        if isinstance(e, UserTextSent):
            print(f"User said: {e.content}")
        elif isinstance(e, AgentTextSent):
            print(f"Agent said: {e.content}")
```

## State Management

### Per-Call State in Wrapper

```python
class StatefulAgent:
    def __init__(self, inner_agent):
        self.inner = inner_agent
        self.turn_count = 0
        self.user_name = None
        self.context = {}

    async def process(self, env: TurnEnv, event: InputEvent):
        if isinstance(event, UserTurnEnded):
            self.turn_count += 1

            # Extract and store info
            user_text = self._get_user_text(event)
            if "my name is" in user_text.lower():
                # Simple extraction - use LLM for robust extraction
                self.user_name = user_text.split("my name is")[-1].strip()

        async for output in self.inner.process(env, event):
            yield output
```

### Tool State with Classes

```python
class FormCollector:
    """Stateful form collection tool."""

    def __init__(self):
        self.fields = {}
        self.current_field = "name"
        self.field_order = ["name", "email", "phone"]

    @loopback_tool
    async def record_field(
        self,
        ctx: ToolEnv,
        value: Annotated[str, "The value provided by the user"],
    ) -> dict:
        """Record a form field value."""
        recorded_field = self.current_field
        self.fields[recorded_field] = value

        # Move to next field
        idx = self.field_order.index(recorded_field)
        if idx + 1 < len(self.field_order):
            self.current_field = self.field_order[idx + 1]
            return {
                "recorded": recorded_field,
                "next_question": f"What is your {self.current_field}?",
                "complete": False,
            }
        return {
            "recorded": recorded_field,
            "complete": True,
            "collected": self.fields,
        }

# Usage
form = FormCollector()
agent = LlmAgent(
    tools=[form.record_field, end_call],
    ...
)
```

## VoiceAgentApp Configuration

### Pre-Call Handler

Run logic before accepting a call:

```python
from line.voice_agent_app import VoiceAgentApp, CallRequest, PreCallResult

async def pre_call_handler(call_request: CallRequest) -> PreCallResult:
    """Validate and configure calls before connecting."""
    # Check allowlist
    if call_request.from_ not in ALLOWED_NUMBERS:
        return None  # Reject call (raises 403)

    # Look up caller info
    caller = await db.get_caller(call_request.from_)

    # Return metadata and config
    return PreCallResult(
        metadata={"caller_name": caller.name, "account_id": caller.id},
        config={"voice": "sonic-english-us"}  # Passed back to client
    )

app = VoiceAgentApp(
    get_agent=get_agent,
    pre_call_handler=pre_call_handler,
)
```

### Accessing Call Metadata

```python
async def get_agent(env: AgentEnv, call_request: CallRequest):
    # Access metadata set by pre_call_handler
    caller_name = call_request.metadata.get("caller_name", "there")
    account_id = call_request.metadata.get("account_id")

    return LlmAgent(
        config=LlmConfig(
            introduction=f"Hello {caller_name}! How can I help with your account today?",
        ),
        ...
    )
```

## Custom Agent Implementation

Build agents from scratch implementing the Agent protocol:

```python
from typing import AsyncIterable
from line.agent import TurnEnv
from line.events import (
    InputEvent, OutputEvent,
    CallStarted, CallEnded, UserTurnEnded,
    AgentSendText, AgentEndCall,
)

class CustomAgent:
    """Minimal custom agent implementation."""

    def __init__(self):
        self.intro_sent = False

    async def process(
        self,
        env: TurnEnv,
        event: InputEvent,
    ) -> AsyncIterable[OutputEvent]:
        """Process input events and yield output events."""

        if isinstance(event, CallStarted):
            if not self.intro_sent:
                self.intro_sent = True
                yield AgentSendText(text="Hello! I'm a custom agent.")
            return

        if isinstance(event, CallEnded):
            # Cleanup resources
            return

        if isinstance(event, UserTurnEnded):
            # Extract user text
            user_text = " ".join(
                item.content for item in event.content
                if hasattr(item, 'content')
            )

            # Simple echo response
            yield AgentSendText(text=f"You said: {user_text}")

            # End on "goodbye"
            if "goodbye" in user_text.lower():
                yield AgentSendText(text="Goodbye!")
                yield AgentEndCall()
```

## Run/Cancel Filters

Control when the agent processes events:

```python
from line.events import CallStarted, UserTurnEnded, UserTurnStarted, CallEnded

def custom_run_filter(event):
    """Events that trigger agent processing."""
    return isinstance(event, (CallStarted, UserTurnEnded, CallEnded))

def custom_cancel_filter(event):
    """Events that cancel current agent processing (interruption)."""
    return isinstance(event, UserTurnStarted)

# Return (agent, run_filter, cancel_filter) tuple
async def get_agent(env, call_request):
    agent = LlmAgent(...)
    return (agent, custom_run_filter, custom_cancel_filter)
```

Default filters:
- **Run**: `CallStarted`, `UserTurnEnded`, `CallEnded`
- **Cancel**: `UserTurnStarted` (user interruption)

## Interruption Handling

By default, `UserTurnStarted` cancels the current agent response by raising `asyncio.CancelledError`. To detect when an interruption occurred:

```python
import asyncio

class InterruptionAwareAgent:
    def __init__(self, inner_agent):
        self.inner = inner_agent
        self.was_interrupted = False

    async def process(self, env, event):
        try:
            if self.was_interrupted:
                self.was_interrupted = False
                # Interruption occurred on previous turn

            async for output in self.inner.process(env, event):
                yield output
        except asyncio.CancelledError:
            self.was_interrupted = True
            raise  # Re-raise to allow proper cleanup
```

## Logging and Metrics

Use `loguru.logger` for logging from tools. Note that `LogMetric` and `LogMessage` are OutputEvent types that can only be emitted from `@passthrough_tool` or wrapper agents that yield OutputEvents directly — yielding them from a `@loopback_tool` would serialize them as tool result strings rather than processing them as log events.

```python
from loguru import logger

@loopback_tool
async def timed_operation(ctx: ToolEnv, query: Annotated[str, "Query"]) -> str:
    """Operation with timing."""
    import time
    start = time.time()

    result = await slow_operation(query)

    elapsed = time.time() - start
    logger.info(f"operation_complete: query={query} elapsed={elapsed:.2f}s")
    return result
```

To emit `LogMetric` or `LogMessage` as actual OutputEvents, use a `@passthrough_tool` or a wrapper agent:

```python
from line.events import LogMetric, LogMessage

@passthrough_tool
async def timed_action(ctx: ToolEnv, query: Annotated[str, "Query"]):
    """Action with structured logging (passthrough — results bypass LLM)."""
    import time
    start = time.time()

    await perform_action(query)

    elapsed = time.time() - start
    yield LogMetric(name="action_latency_ms", value=elapsed * 1000)
    yield LogMessage(
        name="action_complete",
        level="info",
        message=f"Completed query in {elapsed:.2f}s",
        metadata={"query": query},
    )
```
