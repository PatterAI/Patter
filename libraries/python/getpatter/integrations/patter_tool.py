"""PatterTool — wrap a live Patter instance as a tool callable from external
agent frameworks (OpenAI Assistants, Anthropic Claude tool-use, LangChain,
Hermes Agent, MCP, generic OpenAI-compatible endpoints).

See ``libraries/typescript/src/integrations/patter-tool.ts`` for the matching TS module —
the wire contracts (parameter schema, result envelope, ``hermes_handler``
return shape) are kept identical so a customer can swap SDKs at any time.

Usage (Hermes Agent integration)::

    # in your tools/patter.py file (auto-discovered by Hermes)
    from tools.registry import registry
    from getpatter import Patter, Twilio, DeepgramSTT, GroqLLM, ElevenLabsTTS
    from getpatter.integrations import PatterTool

    phone = Patter(
        carrier=Twilio(),
        phone_number=os.environ["TWILIO_PHONE_NUMBER"],
        webhook_url="agent.example.com",
    )

    tool = PatterTool(
        phone=phone,
        agent={"stt": DeepgramSTT(), "llm": GroqLLM(), "tts": ElevenLabsTTS()},
    )

    # One-line registration with Hermes' registry:
    tool.register_hermes(registry)

Usage (OpenAI / Anthropic / generic)::

    tool = PatterTool(phone=phone, agent={...})
    schemas = {"openai": tool.openai_schema(), "anthropic": tool.anthropic_schema()}

    # When the LLM emits a tool_call:
    result = await tool.execute(to="+15551234567", goal="Book a dentist appointment")
    # → {"call_id": "...", "status": "completed", "duration_seconds": 12.3, ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("getpatter.integrations.patter_tool")


# JSON-Schema for the call args. Identical wire shape across openai/anthropic/hermes.
_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": (
                'Destination phone number in E.164 format (e.g. "+15551234567"). Required.'
            ),
        },
        "goal": {
            "type": "string",
            "description": (
                "What the agent should accomplish on the call. Becomes the in-call "
                "agent's system prompt for this single call."
            ),
        },
        "first_message": {
            "type": "string",
            "description": (
                "Optional first message the agent speaks when the callee answers. "
                "Defaults to a generic greeting."
            ),
        },
        "max_duration_sec": {
            "type": "integer",
            "description": (
                "Hard timeout for the call in seconds. Default 180. The call is "
                "force-ended at this deadline whether or not it has resolved."
            ),
            "minimum": 5,
            "maximum": 1800,
        },
    },
    "required": ["to"],
}

_DEFAULT_NAME = "make_phone_call"
_DEFAULT_DESCRIPTION = (
    "Place a real outbound phone call. Returns a JSON object with the full "
    "transcript, call status, duration in seconds, and cost. Use this when the "
    "user asks you to call someone, schedule appointments by phone, or otherwise "
    "reach a human via voice."
)


@dataclass
class PatterToolResult:
    """Structured result returned by ``PatterTool.execute``."""

    call_id: str
    status: str
    duration_seconds: float
    transcript: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float | None = None
    metrics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PatterTool:
    """Wrap a live ``Patter`` instance as a tool callable from external agent
    frameworks. Schema-stable across OpenAI / Anthropic / Hermes; ``execute()``
    runs an outbound dial and waits for completion.
    """

    def __init__(
        self,
        phone: Any,
        agent: dict[str, Any] | None = None,
        name: str = _DEFAULT_NAME,
        description: str = _DEFAULT_DESCRIPTION,
        max_duration_sec: int = 180,
        recording: bool = False,
    ) -> None:
        if phone is None:
            raise ValueError("PatterTool: `phone` (a Patter instance) is required.")
        self._phone = phone
        self._agent_spec = agent
        self.name = name
        self.description = description
        self._max_duration_sec = max(5, min(1800, int(max_duration_sec)))
        self._recording = recording
        self._started = False
        # Map of call_id -> asyncio.Future awaiting the call_end payload.
        self._pending: dict[str, asyncio.Future] = {}
        # Single-slot future for the next dial's call_id (FIFO via Lock).
        self._next_call_id: asyncio.Future[str] | None = None
        self._dial_lock: asyncio.Lock | None = None

    # --- Schema exporters --------------------------------------------------

    def openai_schema(self) -> dict[str, Any]:
        """OpenAI Chat Completions / Assistants tool spec."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _PARAMETERS_SCHEMA,
            },
        }

    def anthropic_schema(self) -> dict[str, Any]:
        """Anthropic Messages API tool spec."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _PARAMETERS_SCHEMA,
        }

    def hermes_schema(self) -> dict[str, Any]:
        """Hermes Agent (Nous Research) registry schema. Same JSON-Schema as
        Anthropic's, exposed under ``parameters`` to match Hermes' contract."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _PARAMETERS_SCHEMA,
        }

    # --- Hermes registry helper -------------------------------------------

    def register_hermes(self, registry: Any, toolset: str = "patter") -> None:
        """Register this tool with a Hermes ``tools.registry.Registry`` instance.

        Hermes' tool contract is ``handler(args: dict, **kw) -> str`` returning
        a JSON string (errors as ``{"error": "..."}``). We bridge it to
        ``execute()``.
        """
        if not hasattr(registry, "register"):
            raise TypeError(
                "PatterTool.register_hermes: argument must be a Hermes Registry "
                "(needs a `.register()` method)."
            )
        handler = self.hermes_handler()
        registry.register(
            name=self.name,
            toolset=toolset,
            schema=self.hermes_schema(),
            handler=handler,
        )

    # --- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the underlying Patter server. Idempotent."""
        if self._started:
            return
        if not self._agent_spec:
            raise ValueError(
                "PatterTool.start: `agent` config is required. Pass "
                "`{'stt': ..., 'llm': ..., 'tts': ...}` or an `engine` "
                "(e.g. OpenAIRealtime) when constructing PatterTool."
            )
        self._dial_lock = asyncio.Lock()
        built_agent = self._phone.agent(**self._agent_spec)
        await self._phone.serve(
            agent=built_agent,
            recording=self._recording,
            on_call_end=self._on_call_end,
        )
        # Subscribe to the metrics store SSE stream so we can correlate
        # outbound dials (`call_initiated`) with the call_id Patter assigns
        # at dial time.
        store = self._phone.metrics_store
        if store is None:
            raise RuntimeError(
                "PatterTool.start: phone.metrics_store is None after serve() — "
                "is the dashboard disabled?"
            )
        # The Python MetricsStore exposes an asyncio.Queue subscription.
        queue = store.subscribe()
        asyncio.create_task(self._consume_metrics_events(queue, store))
        self._started = True

    async def stop(self) -> None:
        """Best-effort shutdown — fail any pending calls and stop the server."""
        if not self._started:
            return
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("PatterTool: shutdown while call pending"))
        self._pending.clear()
        if hasattr(self._phone, "stop"):
            try:
                await self._phone.stop()
            except Exception:  # pragma: no cover - defensive
                logger.debug("PatterTool.stop: phone.stop() failed", exc_info=True)
        self._started = False

    # --- Execution ---------------------------------------------------------

    async def execute(
        self,
        to: str,
        goal: str | None = None,
        first_message: str | None = None,
        max_duration_sec: int | None = None,
    ) -> PatterToolResult:
        """Dial outbound, wait for the call to end, return a structured result."""
        if not isinstance(to, str) or not to.startswith("+"):
            raise ValueError(
                'PatterTool.execute: `to` must be an E.164 phone number (e.g. "+15551234567").'
            )
        if not self._started:
            await self.start()
        timeout = max(5, min(1800, int(max_duration_sec or self._max_duration_sec)))

        agent_kwargs = dict(self._agent_spec or {})
        if goal is not None:
            agent_kwargs["system_prompt"] = goal
        if first_message is not None:
            agent_kwargs["first_message"] = first_message
        override_agent = self._phone.agent(**agent_kwargs)

        # Acquire the dial lock so concurrent execute() calls don't fight over
        # which one captures the next call_initiated event. Use try/finally
        # to guarantee `_next_call_id` is cleared on every exit path —
        # otherwise a TimeoutError leaves a completed-with-exception future
        # in place, and the SSE consumer task would call `set_result` on it
        # (raising InvalidStateError, silently swallowed) while the next
        # legitimate execute() would wait on a stale future.
        assert self._dial_lock is not None
        async with self._dial_lock:
            loop = asyncio.get_running_loop()
            self._next_call_id = loop.create_future()
            try:
                await self._phone.call(to=to, agent=override_agent)
                call_id: str = await asyncio.wait_for(
                    self._next_call_id, timeout=10.0
                )
            finally:
                self._next_call_id = None
            end_future: asyncio.Future = loop.create_future()
            self._pending[call_id] = end_future

        try:
            data = await asyncio.wait_for(end_future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(call_id, None)
            raise TimeoutError(
                f"PatterTool.execute: call {call_id} exceeded {timeout}s timeout"
            ) from exc

        return _build_result(call_id, data)

    def hermes_handler(self) -> Callable[..., Awaitable[str]]:
        """Return a Hermes-compatible handler ``(args, **kw) -> Awaitable[str]``.

        The handler returns a JSON string with the result envelope, or
        ``{"error": "..."}`` if execution failed. Matches Hermes' contract
        (see ``hermes-agent.nousresearch.com/docs/developer-guide/adding-tools``).
        """

        async def handler(args: dict[str, Any], **_kw: Any) -> str:
            try:
                result = await self.execute(
                    to=args.get("to") or "",
                    goal=args.get("goal"),
                    first_message=args.get("first_message"),
                    max_duration_sec=args.get("max_duration_sec"),
                )
                return json.dumps(result.to_dict(), default=str)
            except Exception as exc:
                return json.dumps({"error": str(exc)})

        return handler

    # --- Internal: SSE consumer + onCallEnd hook --------------------------

    async def _consume_metrics_events(self, queue: asyncio.Queue, store: Any) -> None:
        try:
            while True:
                event = await queue.get()
                if (
                    event.get("type") == "call_initiated"
                    and self._next_call_id is not None
                    and not self._next_call_id.done()
                ):
                    call_id = event.get("data", {}).get("call_id") or ""
                    if call_id:
                        self._next_call_id.set_result(call_id)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                store.unsubscribe(queue)
            except Exception:
                pass

    async def _on_call_end(self, data: dict[str, Any]) -> None:
        call_id = data.get("call_id") or ""
        if not call_id:
            return
        future = self._pending.pop(call_id, None)
        if future is None or future.done():
            return
        future.set_result(data)


def _build_result(call_id: str, data: dict[str, Any]) -> PatterToolResult:
    """Translate Patter's ``onCallEnd`` payload into the public envelope."""
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else None
    cost: float | None = None
    duration: float = 0.0
    if metrics is not None:
        # `metrics` may be a dataclass-like object or a plain dict depending on
        # how the underlying transport serialised it.
        cost_obj = metrics.get("cost") if isinstance(metrics, dict) else None
        if isinstance(cost_obj, dict) and isinstance(cost_obj.get("total"), (int, float)):
            cost = float(cost_obj["total"])
        dur_raw = metrics.get("duration_seconds") if isinstance(metrics, dict) else None
        if isinstance(dur_raw, (int, float)):
            duration = float(dur_raw)
    transcript = data.get("transcript") if isinstance(data.get("transcript"), list) else []
    return PatterToolResult(
        call_id=call_id,
        status=str(data.get("status") or "completed"),
        duration_seconds=duration,
        cost_usd=cost,
        transcript=list(transcript),
        metrics=metrics,
    )
