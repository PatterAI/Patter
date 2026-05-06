"""Tool JSON-schema validation for Patter agents (Python parity with TS
``libraries/typescript/src/tools/schema-validation.ts``).

Two layers:

  - **Always-on structural sanity**: every tool's ``parameters`` must look
    like a valid OpenAI function-tool schema (``type: "object"``,
    ``properties`` is a dict, ``required`` is a list if present). Catches
    typos at build time instead of letting them blow up mid-call.

  - **Strict mode**: when a tool sets ``strict: True``, the schema must
    additionally satisfy OpenAI's strict-mode requirements
    (``additionalProperties: False`` on every nested object, every
    property in ``required``, no truly optional fields). Strict mode is
    opt-in — backward-compatible.

Both layers run inside ``Patter.agent(...)`` so user mistakes are
surfaced immediately, not on the first inbound call.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


class ToolSchemaError(ValueError):
    """Raised by :func:`validate_tool_schema` for a malformed tool schema."""


def _tool_label(tool: Any) -> str:
    """Return a short tag for log/error messages — uses ``name`` for both
    :class:`Tool` dataclass instances and raw dict tools."""
    name = getattr(tool, "name", None)
    if name is None and isinstance(tool, dict):
        name = tool.get("name")
    return f"tool '{name or '<unnamed>'}'"


def _get(tool: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from either a Tool dataclass or a dict."""
    if isinstance(tool, dict):
        return tool.get(key, default)
    return getattr(tool, key, default)


def validate_tool_schema(tool: Any) -> None:
    """Validate a tool's ``parameters`` schema. Raises
    :class:`ToolSchemaError` with a clear message on the first violation;
    otherwise returns normally. Idempotent and pure — safe to call from
    constructors."""
    params = _get(tool, "parameters")
    tag = _tool_label(tool)

    if not isinstance(params, dict):
        raise ToolSchemaError(
            f"{tag}: `parameters` must be a JSON Schema dict (got {type(params).__name__})."
        )
    if params.get("type") != "object":
        raise ToolSchemaError(
            f"{tag}: `parameters['type']` must be \"object\" "
            f"(got {json.dumps(params.get('type'))}). OpenAI function tools "
            "require an object root."
        )
    properties = params.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise ToolSchemaError(
            f"{tag}: `parameters['properties']` must be a dict mapping "
            "field → JSON Schema."
        )
    required = params.get("required")
    if required is not None and not isinstance(required, list):
        raise ToolSchemaError(
            f"{tag}: `parameters['required']` must be a list of field names."
        )
    if isinstance(required, list) and isinstance(properties, dict):
        for field_name in required:
            if not isinstance(field_name, str):
                raise ToolSchemaError(
                    f"{tag}: `parameters['required']` entries must be strings "
                    f"(got {type(field_name).__name__})."
                )
            if field_name not in properties:
                raise ToolSchemaError(
                    f"{tag}: `parameters['required']` lists \"{field_name}\" "
                    "but it is not declared in `parameters['properties']`."
                )

    if _get(tool, "strict") is True:
        _validate_strict_mode_schema(_tool_label(tool), params, [])


def _validate_strict_mode_schema(
    tag: str,
    schema: dict,
    path_parts: list[str],
) -> None:
    """Verify a schema satisfies OpenAI strict mode's structural rules:
    recursive ``additionalProperties: False`` and ``required`` covering
    every property at each object level."""
    here = "parameters" if not path_parts else "parameters." + ".".join(path_parts)
    schema_type = schema.get("type")

    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            raise ToolSchemaError(
                f"{tag}: strict mode requires `{here}.additionalProperties: False` "
                f"on every object — got {json.dumps(schema.get('additionalProperties'))}."
            )
        properties: dict = schema.get("properties") or {}
        required: list = schema.get("required") or []
        for prop_name in properties.keys():
            if prop_name not in required:
                raise ToolSchemaError(
                    f"{tag}: strict mode requires every property to be listed in "
                    f'`required` — "{here}.{prop_name}" is missing. Use a '
                    'nullable type (e.g. ["string", "null"]) instead of an '
                    "optional field."
                )
        for prop_name, prop_schema in properties.items():
            if isinstance(prop_schema, dict):
                _validate_strict_mode_schema(
                    tag, prop_schema, path_parts + ["properties", prop_name]
                )
    elif schema_type == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            _validate_strict_mode_schema(tag, items, path_parts + ["items"])


def validate_all_tool_schemas(tools: Iterable[Any] | None) -> None:
    """Validate a sequence of tools; re-raises the first
    :class:`ToolSchemaError`."""
    if not tools:
        return
    for tool in tools:
        validate_tool_schema(tool)
