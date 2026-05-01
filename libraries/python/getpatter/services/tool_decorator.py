"""Decorator that turns a typed Python function into a ToolDefinition dict.

Usage::

    from getpatter import tool

    @tool
    async def get_weather(location: str, unit: str = "celsius") -> str:
        \"\"\"Get the current weather for a location.

        Args:
            location: City name or zip code
            unit: Temperature unit (celsius or fahrenheit)
        \"\"\"
        return f"Sunny, 22°{unit[0].upper()}"

The decorated name resolves to a ``dict`` with keys ``name``,
``description``, ``parameters`` (JSON Schema), and ``handler``.
"""

from __future__ import annotations

import inspect
import re
import typing
from typing import Any, Callable, get_args, get_origin

# ── Python type → JSON Schema type mapping ──────────────────────────────

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _json_schema_type(annotation: Any) -> tuple[str, bool]:
    """Return ``(json_schema_type, is_optional)`` for a type annotation.

    Handles bare types (``str``, ``int``, ...), generic aliases
    (``list[int]``), and ``Optional[X]`` / ``X | None``.
    """
    origin = get_origin(annotation)

    # Optional[X] is Union[X, None]
    if origin is typing.Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            base_type, _ = _json_schema_type(args[0])
            return base_type, True
        return "object", True

    # Generic aliases like list[int], dict[str, Any]
    if origin is not None:
        raw = origin
    else:
        raw = annotation

    schema_type = _TYPE_MAP.get(raw, "object")
    return schema_type, False


# ── Docstring parser (Google-style Args section) ────────────────────────

_ARGS_SECTION_RE = re.compile(
    r"^\s*Args:\s*$",
    re.MULTILINE,
)

_ARG_LINE_RE = re.compile(
    r"^\s{2,}(\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)",
)


def _parse_docstring(docstring: str | None) -> tuple[str, dict[str, str]]:
    """Parse a Google-style docstring into a summary and per-arg descriptions.

    Returns ``(summary, {param_name: description})``.
    """
    if not docstring:
        return "", {}

    lines = docstring.strip().splitlines()

    # Summary: first non-blank line(s) before any section header.
    summary_parts: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            break
        if stripped == "" and summary_parts:
            break
        if stripped:
            summary_parts.append(stripped)
    summary = " ".join(summary_parts)

    # Args section
    arg_descriptions: dict[str, str] = {}
    match = _ARGS_SECTION_RE.search(docstring)
    if match is None:
        return summary, arg_descriptions

    after_args = docstring[match.end() :]
    current_name: str | None = None
    current_desc_parts: list[str] = []

    for line in after_args.splitlines():
        # Stop at the next section header (e.g. "Returns:", "Raises:")
        if re.match(r"^\s{0,1}\S", line) and line.strip() and not line.strip().startswith("-"):
            break

        arg_match = _ARG_LINE_RE.match(line)
        if arg_match:
            # Save previous arg
            if current_name is not None:
                arg_descriptions[current_name] = " ".join(current_desc_parts).strip()
            current_name = arg_match.group(1)
            current_desc_parts = [arg_match.group(2).strip()]
        elif current_name is not None:
            # Continuation line
            stripped = line.strip()
            if stripped:
                current_desc_parts.append(stripped)

    if current_name is not None:
        arg_descriptions[current_name] = " ".join(current_desc_parts).strip()

    return summary, arg_descriptions


# ── @tool decorator ─────────────────────────────────────────────────────

ToolDefinition = dict[str, Any]


def tool(fn: Callable[..., Any]) -> ToolDefinition:
    """Decorator that converts a typed function into a ``ToolDefinition`` dict.

    The returned dict has the shape::

        {
            "name": "<function_name>",
            "description": "<first line of docstring>",
            "parameters": { <JSON Schema> },
            "handler": <original_function>,
        }
    """
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    summary, arg_descriptions = _parse_docstring(inspect.getdoc(fn))

    properties: dict[str, dict[str, str]] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        annotation = hints.get(name)
        if annotation is None:
            schema_type = "string"
            is_optional = False
        else:
            schema_type, is_optional = _json_schema_type(annotation)

        prop: dict[str, str] = {"type": schema_type}
        desc = arg_descriptions.get(name, "")
        if desc:
            prop["description"] = desc

        properties[name] = prop

        has_default = param.default is not inspect.Parameter.empty
        if not has_default and not is_optional:
            required.append(name)

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        parameters["required"] = required

    # Adapter between the decorated user function (signature
    # ``check_order(order_id: str)``) and the runtime tool executor which
    # always invokes handlers as ``handler(arguments_dict, call_context_dict)``.
    # The adapter inspects the original signature: if the function already
    # takes ``(arguments, call_context)`` positionally we pass through,
    # otherwise we unpack ``arguments`` as keyword-args into the real call.
    # Without this adapter every ``@tool`` function fails at runtime with
    # ``takes 1 positional argument but 2 were given``.
    _param_names = tuple(sig.parameters.keys())
    _is_legacy_twoarg = (
        len(_param_names) == 2 and _param_names == ("arguments", "call_context")
    )
    import asyncio as _asyncio

    async def _adapter(arguments: dict, call_context: dict):
        if _is_legacy_twoarg:
            result = fn(arguments, call_context)
        else:
            filtered = {
                k: v for k, v in (arguments or {}).items() if k in _param_names
            }
            result = fn(**filtered)
        if _asyncio.iscoroutine(result) or _asyncio.isfuture(result):
            result = await result
        return result

    _adapter.__wrapped__ = fn  # type: ignore[attr-defined]

    return {
        "name": fn.__name__,
        "description": summary,
        "parameters": parameters,
        "handler": _adapter,
    }
