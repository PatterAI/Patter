"""Unit tests for getpatter.tools.schema_validation.

Parity with libraries/typescript/tests/tool-schema-validation.test.ts.
"""

from __future__ import annotations

import pytest

from getpatter.tools.schema_validation import (
    ToolSchemaError,
    validate_all_tool_schemas,
    validate_tool_schema,
)


def _tool(**overrides) -> dict:
    """Build a minimal tool dict for testing."""
    base = {
        "name": "test_tool",
        "description": "desc",
        "parameters": {"type": "object", "properties": {}},
    }
    base.update(overrides)
    return base


class TestStructuralValidation:
    def test_accepts_minimal_valid_object_schema(self) -> None:
        validate_tool_schema(_tool(parameters={"type": "object"}))

    def test_rejects_parameters_not_an_object(self) -> None:
        with pytest.raises(ToolSchemaError):
            validate_tool_schema(_tool(parameters="oops"))

    def test_rejects_wrong_root_type(self) -> None:
        with pytest.raises(ToolSchemaError, match='must be "object"'):
            validate_tool_schema(_tool(parameters={"type": "string"}))

    def test_rejects_properties_not_an_object(self) -> None:
        with pytest.raises(ToolSchemaError, match="dict mapping"):
            validate_tool_schema(
                _tool(parameters={"type": "object", "properties": ["not", "a", "dict"]})
            )

    def test_rejects_required_not_a_list(self) -> None:
        with pytest.raises(ToolSchemaError, match="must be a list"):
            validate_tool_schema(
                _tool(
                    parameters={
                        "type": "object",
                        "properties": {},
                        "required": "name",
                    }
                )
            )

    def test_rejects_required_field_not_in_properties(self) -> None:
        with pytest.raises(ToolSchemaError, match='lists "bar"'):
            validate_tool_schema(
                _tool(
                    parameters={
                        "type": "object",
                        "properties": {"foo": {"type": "string"}},
                        "required": ["foo", "bar"],
                    }
                )
            )


class TestStrictMode:
    def test_accepts_compliant_strict_schema(self) -> None:
        validate_tool_schema(
            _tool(
                strict=True,
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "age": {"type": "number"},
                    },
                    "required": ["name", "age"],
                    "additionalProperties": False,
                },
            )
        )

    def test_rejects_strict_without_additional_properties_false(self) -> None:
        with pytest.raises(ToolSchemaError, match="additionalProperties: False"):
            validate_tool_schema(
                _tool(
                    strict=True,
                    parameters={
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        # additionalProperties intentionally omitted
                    },
                )
            )

    def test_rejects_strict_with_optional_field(self) -> None:
        with pytest.raises(
            ToolSchemaError, match="strict mode requires every property"
        ):
            validate_tool_schema(
                _tool(
                    strict=True,
                    parameters={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "age": {"type": "number"},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                )
            )

    def test_recurses_into_nested_object(self) -> None:
        with pytest.raises(
            ToolSchemaError,
            match=r"parameters\.properties\.address\.additionalProperties: False",
        ):
            validate_tool_schema(
                _tool(
                    strict=True,
                    parameters={
                        "type": "object",
                        "properties": {
                            "address": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                                # missing additionalProperties: False
                            },
                        },
                        "required": ["address"],
                        "additionalProperties": False,
                    },
                )
            )

    def test_recurses_into_array_items(self) -> None:
        with pytest.raises(
            ToolSchemaError, match=r"parameters\.properties\.tags\.items"
        ):
            validate_tool_schema(
                _tool(
                    strict=True,
                    parameters={
                        "type": "object",
                        "properties": {
                            "tags": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"key": {"type": "string"}},
                                    "required": ["key"],
                                    # missing additionalProperties: False
                                },
                            },
                        },
                        "required": ["tags"],
                        "additionalProperties": False,
                    },
                )
            )

    def test_skips_strict_checks_when_unset(self) -> None:
        # No additionalProperties, no required — would fail strict mode but
        # passes when strict is unset (default).
        validate_tool_schema(
            _tool(
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                }
            )
        )


class TestValidateAll:
    def test_handles_empty_and_none(self) -> None:
        validate_all_tool_schemas(None)
        validate_all_tool_schemas([])

    def test_rethrows_first_violation(self) -> None:
        with pytest.raises(ToolSchemaError, match="tool 'bad'"):
            validate_all_tool_schemas(
                [
                    _tool(name="ok", parameters={"type": "object"}),
                    _tool(name="bad", parameters="oops"),
                ]
            )
