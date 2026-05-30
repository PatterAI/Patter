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
from dataclasses import asdict, dataclass, field, is_dataclass
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


@dataclass(frozen=True)
class PatterToolResult:
    """Structured result returned by ``PatterTool.execute``."""

    call_id: str
    status: str
    duration_seconds: float
    # Carrier-agnostic outcome (answered / voicemail / no_answer / busy /
    # failed) lifted from the SDK ``CallResult``. Defaulted for backward
    # compatibility with any code constructing this envelope positionally.
    outcome: str = ""
    transcript: tuple[dict[str, Any], ...] = field(default_factory=tuple)
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
        """Start the underlying Patter server. Idempotent.

        ``execute()`` relies on ``Patter.call(wait=True)``, which requires an
        active server to receive the carrier completion webhooks — that's what
        ``serve()`` provides here. No ``on_call_end`` callback is wired: the
        SDK's own per-call_id completion registry resolves the result, so the
        user's ``on_call_end`` slot is left free.
        """
        if self._started:
            return
        if not self._agent_spec:
            raise ValueError(
                "PatterTool.start: `agent` config is required. Pass "
                "`{'stt': ..., 'llm': ..., 'tts': ...}` or an `engine` "
                "(e.g. OpenAIRealtime) when constructing PatterTool."
            )
        built_agent = self._phone.agent(**self._agent_spec)
        await self._phone.serve(agent=built_agent, recording=self._recording)
        self._started = True

    async def stop(self) -> None:
        """Best-effort shutdown — tear the Patter server down via disconnect()."""
        if not self._started:
            return
        if hasattr(self._phone, "disconnect"):
            try:
                await self._phone.disconnect()
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    "PatterTool.stop: phone.disconnect() failed", exc_info=True
                )
        self._started = False

    # --- Execution ---------------------------------------------------------

    async def execute(
        self,
        to: str,
        goal: str | None = None,
        first_message: str | None = None,
        max_duration_sec: int | None = None,
    ) -> PatterToolResult:
        """Dial outbound, wait for the call to end, return a structured result.

        Thin wrapper over ``Patter.call(wait=True)``: the SDK now owns the
        dial → call_id → terminal-signal correlation, so this just bounds the
        wait with ``max_duration_sec`` and maps the ``CallResult`` into the
        tool's public envelope.
        """
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

        try:
            result = await asyncio.wait_for(
                self._phone.call(to=to, agent=override_agent, wait=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"PatterTool.execute: call to {to} exceeded {timeout}s timeout"
            ) from exc

        return _result_from_call_result(result)

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


def _result_from_call_result(result: Any) -> PatterToolResult:
    """Map an SDK ``CallResult`` into the tool's public envelope.

    Reads structured attributes off the dataclass directly — ``cost.total``
    and ``duration_seconds`` are real numbers here (the previous dict-probing
    implementation silently dropped both because the live payload delivers a
    ``CallMetrics`` dataclass, not a dict). ``metrics`` is flattened to a plain
    dict so ``to_dict()`` stays JSON-friendly for the Hermes/MCP wire envelope.
    """
    cost_obj = getattr(result, "cost", None)
    cost_total = getattr(cost_obj, "total", None)
    cost_usd = float(cost_total) if isinstance(cost_total, (int, float)) else None

    metrics_obj = getattr(result, "metrics", None)
    if is_dataclass(metrics_obj):
        metrics_dict: dict[str, Any] | None = asdict(metrics_obj)
    elif isinstance(metrics_obj, dict):
        metrics_dict = metrics_obj
    else:
        metrics_dict = None

    transcript = tuple(getattr(result, "transcript", ()) or ())
    return PatterToolResult(
        call_id=getattr(result, "call_id", ""),
        status=str(getattr(result, "status", "") or "completed"),
        outcome=str(getattr(result, "outcome", "") or ""),
        duration_seconds=float(getattr(result, "duration_seconds", 0.0) or 0.0),
        cost_usd=cost_usd,
        transcript=transcript,
        metrics=metrics_dict,
    )
