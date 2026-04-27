# Tool Patterns Deep Dive

Detailed documentation for implementing custom tools in Line SDK voice agents.

## Parameter Syntax

Tools use `Annotated[type, "description"]` to define parameters visible to the LLM:

```python
from typing import Annotated, Literal, Optional
from line.llm_agent import loopback_tool, ToolEnv

@loopback_tool
async def example_tool(
    ctx: ToolEnv,  # REQUIRED: Must be first parameter

    # Required parameter with description
    name: Annotated[str, "The customer's full name"],

    # Optional parameter (has default value)
    priority: Annotated[str, "Priority level"] = "normal",

    # Enum constraint using Literal
    category: Annotated[Literal["sales", "support", "billing"], "Department"] = "support",

    # Optional that allows None (still needs default to be optional)
    notes: Annotated[Optional[str], "Additional notes"] = None,
) -> str:
    """Tool description from docstring - shown to the LLM."""
    ...
```

**Key rules:**
1. First parameter MUST be `ctx: ToolEnv`
2. Description from docstring (not annotation)
3. `Optional[T]` does NOT make a parameter optional - use a default value
4. Use `Literal["a", "b", "c"]` for enum constraints

## Loopback Tools

Loopback tools return data to the LLM for continued reasoning.

### Basic Pattern

```python
@loopback_tool
async def get_account_balance(
    ctx: ToolEnv,
    account_id: Annotated[str, "The account ID"],
) -> str:
    """Get the current balance for an account."""
    balance = await db.get_balance(account_id)
    return f"Account {account_id} balance: ${balance:.2f}"
```

### Returning Structured Data

Return dicts or lists - they're JSON serialized for the LLM:

```python
@loopback_tool
async def search_orders(
    ctx: ToolEnv,
    customer_id: Annotated[str, "Customer ID"],
    status: Annotated[str, "Order status filter"] = "all",
) -> dict:
    """Search for customer orders."""
    orders = await db.search_orders(customer_id, status)
    return {
        "total": len(orders),
        "orders": [
            {"id": o.id, "status": o.status, "total": o.total}
            for o in orders[:5]  # Limit for voice context
        ]
    }
```

### Generator Pattern (Multiple Results)

Yield multiple values - each triggers a new LLM completion:

```python
@loopback_tool
async def stream_updates(
    ctx: ToolEnv,
    ticket_id: Annotated[str, "Support ticket ID"],
):
    """Get real-time updates for a ticket."""
    async for update in ticket_service.subscribe(ticket_id):
        yield f"Update: {update.message}"
```

### Background Tools

For slow operations, use `is_background=True` to avoid blocking:

```python
@loopback_tool(is_background=True)
async def check_bank_balance(
    ctx: ToolEnv,
    account_id: Annotated[str, "Bank account ID"],
):
    """Check bank balance (may take a few seconds)."""
    yield "Looking up your balance now..."  # Immediate response
    balance = await slow_bank_api.get_balance(account_id)  # Slow operation
    yield f"Your balance is ${balance:.2f}"  # Final result
```

**Background tool behavior:**
1. Tool runs asynchronously without blocking LLM/other tools
2. Each yielded value triggers a new LLM completion
3. Tool is NOT cancelled on user interruption
4. Results are incorporated into conversation history

**Use cases:**
- External API calls with unpredictable latency
- Database queries that might be slow
- Any operation where you want to say "please wait" first

## Passthrough Tools

Passthrough tools bypass the LLM and send output directly to the user/system.

### Basic Pattern

```python
from line.events import AgentSendText, AgentEndCall
from line.llm_agent import passthrough_tool, ToolEnv

@passthrough_tool
async def end_conversation(ctx: ToolEnv):
    """End the call politely."""
    yield AgentSendText(text="Thank you for calling. Goodbye!")
    yield AgentEndCall()
```

### Transfer with Message

```python
from line.events import AgentSendText, AgentTransferCall

@passthrough_tool
async def transfer_to_sales(
    ctx: ToolEnv,
    product: Annotated[str, "Product the customer is interested in"],
):
    """Transfer to sales team for product inquiries."""
    yield AgentSendText(text=f"I'll connect you with our sales team to discuss {product}.")
    yield AgentTransferCall(target_phone_number="+18005551234")
```

### DTMF Navigation

```python
from line.events import AgentSendDtmf

@passthrough_tool
async def navigate_ivr(
    ctx: ToolEnv,
    option: Annotated[str, "IVR menu option (1-9, *, #)"],
):
    """Press a button on the phone menu."""
    yield AgentSendDtmf(button=option)
```

### Output Event Types

```python
from line.events import (
    AgentSendText,      # Speak text: AgentSendText(text="Hello")
    AgentEndCall,       # End call: AgentEndCall()
    AgentTransferCall,  # Transfer: AgentTransferCall(target_phone_number="+1...")
    AgentSendDtmf,      # DTMF tone: AgentSendDtmf(button="5")
)
```

## Error Handling

Handle errors gracefully in tools:

```python
@loopback_tool
async def get_order(
    ctx: ToolEnv,
    order_id: Annotated[str, "Order ID"],
) -> str:
    """Look up an order by ID."""
    try:
        order = await db.get_order(order_id)
        if not order:
            return f"No order found with ID {order_id}"
        return f"Order {order_id}: {order.status}"
    except DatabaseError as e:
        logger.error(f"Database error: {e}")
        return "Sorry, I'm having trouble accessing order information right now."
```

## Tool Classes

For complex tools with shared state, use class methods:

```python
class OrderService:
    def __init__(self, db_connection):
        self.db = db_connection
        self._cache = {}

    @loopback_tool
    async def get_order(
        self,  # 'self' is allowed as first param for methods
        ctx: ToolEnv,
        order_id: Annotated[str, "Order ID"],
    ) -> str:
        """Get order details."""
        if order_id in self._cache:
            return self._cache[order_id]
        order = await self.db.get_order(order_id)
        self._cache[order_id] = f"Order {order_id}: {order.status}"
        return self._cache[order_id]

# Usage
service = OrderService(db)
agent = LlmAgent(
    model="gpt-4o",
    tools=[service.get_order, end_call],
    ...
)
```

## Input Validation

Validate inputs before processing:

```python
import re

@loopback_tool
async def lookup_email(
    ctx: ToolEnv,
    email: Annotated[str, "Customer email address"],
) -> str:
    """Look up customer by email."""
    # Validate email format
    if not re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email):
        return f"'{email}' doesn't look like a valid email address. Please provide a valid email."

    customer = await db.find_by_email(email)
    if not customer:
        return f"No customer found with email {email}"
    return f"Found customer: {customer.name} (ID: {customer.id})"
```

## Combining Tool Types

An agent can use all three tool types together:

```python
from line.llm_agent import (
    LlmAgent, LlmConfig,
    loopback_tool, passthrough_tool,
    end_call, ToolEnv
)

@loopback_tool
async def check_balance(ctx: ToolEnv, account: Annotated[str, "Account number"]) -> str:
    """Check account balance."""
    return f"Balance: $1,234.56"

@passthrough_tool
async def emergency_transfer(ctx: ToolEnv):
    """Transfer to emergency support immediately."""
    yield AgentSendText(text="Connecting you to emergency support now.")
    yield AgentTransferCall(target_phone_number="+18009119111")

agent = LlmAgent(
    model="gpt-4o",
    tools=[
        check_balance,       # Loopback - results go back to LLM
        emergency_transfer,  # Passthrough - bypasses LLM
        end_call,           # Built-in passthrough
    ],
    config=LlmConfig(
        system_prompt="You are a banking assistant. Use emergency_transfer only for fraud or safety issues."
    ),
)
```
