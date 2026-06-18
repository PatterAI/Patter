"""Parity test for _sanitize_gemini_schema (TS gemini-schema-sanitize.test.ts).

REGRESSION (prod 2026-06-18 demo silence): the sanitizer recursed into the
`properties` map and stripped user property NAMES (treating them as schema
keywords), leaving properties={} while `required` still listed them. Gemini
Live rejected setup (WS 1007: "required[0]: property is not defined") -> no
audio. Property names under `properties` must be preserved.
"""

import pytest

from getpatter.providers.google_llm import _sanitize_gemini_schema


@pytest.mark.unit
def test_preserves_property_names_under_properties():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    out = _sanitize_gemini_schema(schema)
    assert out["properties"] == {"query": {"type": "string"}}
    assert out["required"] == ["query"]
    # Server invariant: every required name must exist in properties.
    for name in out["required"]:
        assert name in out["properties"]


@pytest.mark.unit
def test_strips_disallowed_keywords_but_keeps_property_names():
    schema = {
        "type": "object",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "additionalProperties": False,
        "properties": {
            "to": {"type": "string", "additionalProperties": False},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["to", "subject", "body"],
    }
    out = _sanitize_gemini_schema(schema)
    assert "$schema" not in out
    assert "additionalProperties" not in out
    assert out["properties"]["to"] == {"type": "string"}
    assert out["required"] == ["to", "subject", "body"]
    for name in out["required"]:
        assert name in out["properties"]


@pytest.mark.unit
def test_recurses_into_nested_object_properties():
    schema = {
        "type": "object",
        "properties": {
            "filters": {"type": "object", "properties": {"since": {"type": "string"}}, "required": ["since"]},
        },
        "required": ["filters"],
    }
    out = _sanitize_gemini_schema(schema)
    assert out["properties"]["filters"]["properties"]["since"] == {"type": "string"}
    assert out["properties"]["filters"]["required"] == ["since"]


@pytest.mark.unit
def test_handles_array_items_subschema():
    schema = {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}, "required": ["tags"]}
    out = _sanitize_gemini_schema(schema)
    assert out["properties"]["tags"]["items"] == {"type": "string"}
