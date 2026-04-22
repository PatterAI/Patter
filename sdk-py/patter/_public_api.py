"""Public top-level API surface for Patter — Phase 1a of the v0.5.0 refactor.

This module centralises the new LiveKit-style, user-facing primitives so they
can be re-exported from :mod:`patter` without cluttering the historical module
layout. The Phase 1a goals are:

* Expose a dedicated ``Tool`` dataclass (distinct from the legacy
  ``ToolDefinition`` dict produced by :func:`patter.services.tool_decorator.tool`).
* Provide a unified ``tool(...)`` factory that works both as a decorator on a
  typed Python function and as a kwargs constructor.
* Re-export :class:`patter.models.Guardrail` and a ``guardrail(...)`` factory
  for building guardrails without an initialised :class:`patter.client.Patter`
  client.

No existing runtime code paths depend on the new ``Tool`` dataclass — it is
consumed by the Phase 2 dispatch layer, which is handled separately.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from patter.models import Guardrail
from patter.services.tool_decorator import tool as _legacy_tool_decorator

__all__ = ["Tool", "Guardrail", "tool", "guardrail"]


@dataclass(frozen=True)
class Tool:
    """Declarative tool definition for use with a Patter agent.

    Exactly one of ``handler`` and ``webhook_url`` must be supplied:

    * ``handler``: an async or sync callable invoked in-process.
    * ``webhook_url``: an HTTPS URL the Patter server POSTs to when the LLM
      invokes the tool.

    Args:
        name: Tool name (visible to the LLM).
        description: Human description shown to the LLM.
        parameters: JSON Schema describing the tool arguments.
        handler: Callable executed when the tool is invoked. Mutually exclusive
            with ``webhook_url``.
        webhook_url: HTTPS URL POSTed to when the tool is invoked. Mutually
            exclusive with ``handler``.
    """

    name: str
    description: str = ""
    parameters: dict | None = None
    handler: Callable[..., Any] | None = None
    webhook_url: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Tool requires a non-empty name")
        has_handler = self.handler is not None
        has_webhook = bool(self.webhook_url)
        if has_handler and has_webhook:
            raise ValueError(
                "Tool accepts exactly one of handler or webhook_url, not both"
            )
        if not has_handler and not has_webhook:
            raise ValueError(
                "Tool requires either a handler callable or a webhook_url"
            )


def tool(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str = "",
    parameters: dict | None = None,
    handler: Callable[..., Any] | None = None,
    webhook_url: str = "",
) -> Tool:
    """Create a :class:`Tool` instance.

    Two calling conventions:

    * **Decorator** — apply to a typed Python function. The function's name,
      docstring, and type hints are introspected via the legacy
      :func:`patter.services.tool_decorator.tool` to build the JSON Schema::

          @tool
          async def get_weather(location: str) -> str:
              \"\"\"Get weather for a location.\"\"\"
              ...

    * **Keyword constructor** — build a :class:`Tool` explicitly::

          tool(
              name="transfer_call",
              description="Transfer the call to another number.",
              parameters={"type": "object", "properties": {...}},
              handler=my_handler,
          )

    Exactly one of ``handler`` or ``webhook_url`` must be provided in the
    keyword form. In decorator form the decorated function is used as the
    handler.
    """
    if fn is not None:
        # Decorator form: @tool on a typed callable.
        if not callable(fn):
            raise TypeError(
                "tool() positional argument must be a callable (decorator use); "
                "call tool(name=..., handler=...) for the keyword form"
            )
        legacy = _legacy_tool_decorator(fn)
        return Tool(
            name=legacy["name"],
            description=legacy["description"],
            parameters=legacy["parameters"],
            handler=legacy["handler"],
        )

    # Keyword form: tool(name=..., handler=...) or tool(name=..., webhook_url=...).
    if not name:
        raise ValueError("tool() requires a 'name' when called with keyword arguments")
    return Tool(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        webhook_url=webhook_url,
    )


def guardrail(
    name: str,
    blocked_terms: list[str] | None = None,
    check: Callable[[str], bool] | None = None,
    replacement: str = "I'm sorry, I can't respond to that.",
) -> Guardrail:
    """Create a :class:`Guardrail` instance.

    Use when building guardrails for :meth:`patter.client.Patter.agent` without
    needing an initialised :class:`patter.client.Patter` client.

    Args:
        name: Identifier used in log messages when the guardrail fires.
        blocked_terms: List of words/phrases; any case-insensitive substring
            match blocks the response.
        check: Optional callable ``(text: str) -> bool`` returning ``True``
            when the response should be blocked.
        replacement: Text spoken instead when a response is blocked.
    """
    return Guardrail(
        name=name,
        blocked_terms=blocked_terms,
        check=check,
        replacement=replacement,
    )
