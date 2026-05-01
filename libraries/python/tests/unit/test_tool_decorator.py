"""Unit tests for getpatter.services.tool_decorator — @tool decorator."""

from __future__ import annotations

from typing import Optional

import pytest

from getpatter.services.tool_decorator import tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@tool
async def _weather(location: str, unit: str = "celsius") -> str:
    """Get the current weather for a location.

    Args:
        location: City name or zip code
        unit: Temperature unit (celsius or fahrenheit)
    """
    return f"Sunny, 22°{unit[0].upper()}"


@tool
async def _no_docstring(x: int, y: int) -> int:
    return x + y  # type: ignore[return-value]


@tool
def _sync_handler(name: str) -> str:
    """Say hello.

    Args:
        name: The name to greet
    """
    return f"Hello, {name}!"


@tool
async def _all_types(
    s: str,
    i: int,
    f: float,
    b: bool,
    lst: list,
    d: dict,
) -> str:
    """Accepts all mapped types.

    Args:
        s: A string
        i: An integer
        f: A float
        b: A boolean
        lst: A list
        d: A dictionary
    """
    return "ok"


@tool
async def _optional_param(name: str, nickname: Optional[str] = None) -> str:
    """Greet someone.

    Args:
        name: Full name
        nickname: Optional nickname
    """
    return f"Hi {nickname or name}"


@tool
async def _all_defaults(x: int = 0, y: int = 0) -> int:
    """Add two numbers.

    Args:
        x: First number
        y: Second number
    """
    return x + y  # type: ignore[return-value]


# ===========================================================================
# Test cases
# ===========================================================================


@pytest.mark.unit
class TestToolDecoratorBasic:
    """Basic @tool decorator functionality."""

    def test_returns_dict(self) -> None:
        assert isinstance(_weather, dict)

    def test_name(self) -> None:
        assert _weather["name"] == "_weather"

    def test_description_from_docstring(self) -> None:
        assert _weather["description"] == "Get the current weather for a location."

    def test_parameters_is_json_schema_object(self) -> None:
        params = _weather["parameters"]
        assert params["type"] == "object"
        assert "properties" in params

    def test_required_params(self) -> None:
        assert _weather["parameters"]["required"] == ["location"]

    def test_property_types(self) -> None:
        props = _weather["parameters"]["properties"]
        assert props["location"]["type"] == "string"
        assert props["unit"]["type"] == "string"

    def test_property_descriptions(self) -> None:
        props = _weather["parameters"]["properties"]
        assert props["location"]["description"] == "City name or zip code"
        assert props["unit"]["description"] == "Temperature unit (celsius or fahrenheit)"

    def test_handler_is_callable(self) -> None:
        assert callable(_weather["handler"])


@pytest.mark.unit
class TestToolDecoratorHandler:
    """Handler invocation."""

    # Post-BUG-#21 the ``handler`` stored in the ToolDefinition is an
    # adapter with the signature ``(arguments_dict, call_context_dict)``;
    # the original user function is on ``handler.__wrapped__``.

    @pytest.mark.asyncio
    async def test_async_handler_works(self) -> None:
        result = await _weather["handler"]({"location": "New York"}, {})
        assert "Sunny" in result

    @pytest.mark.asyncio
    async def test_async_handler_with_default(self) -> None:
        result = await _weather["handler"](
            {"location": "London", "unit": "fahrenheit"}, {}
        )
        assert "F" in result

    @pytest.mark.asyncio
    async def test_sync_handler_works(self) -> None:
        # Sync user functions are awaited via the adapter in an event loop.
        result = await _sync_handler["handler"]({"name": "World"}, {})
        assert result == "Hello, World!"


@pytest.mark.unit
class TestToolDecoratorDefaultParams:
    """Parameters with defaults are not required."""

    def test_default_param_not_required(self) -> None:
        assert "unit" not in _weather["parameters"].get("required", [])

    def test_no_required_when_all_have_defaults(self) -> None:
        assert "required" not in _all_defaults["parameters"]

    def test_default_param_still_in_properties(self) -> None:
        assert "unit" in _weather["parameters"]["properties"]


@pytest.mark.unit
class TestToolDecoratorOptionalType:
    """Optional[X] types."""

    def test_optional_type_is_string(self) -> None:
        props = _optional_param["parameters"]["properties"]
        assert props["nickname"]["type"] == "string"

    def test_optional_param_not_required(self) -> None:
        required = _optional_param["parameters"].get("required", [])
        assert "nickname" not in required

    def test_non_optional_param_required(self) -> None:
        required = _optional_param["parameters"]["required"]
        assert "name" in required


@pytest.mark.unit
class TestToolDecoratorTypeMapping:
    """Python types map to correct JSON Schema types."""

    def test_str_to_string(self) -> None:
        assert _all_types["parameters"]["properties"]["s"]["type"] == "string"

    def test_int_to_integer(self) -> None:
        assert _all_types["parameters"]["properties"]["i"]["type"] == "integer"

    def test_float_to_number(self) -> None:
        assert _all_types["parameters"]["properties"]["f"]["type"] == "number"

    def test_bool_to_boolean(self) -> None:
        assert _all_types["parameters"]["properties"]["b"]["type"] == "boolean"

    def test_list_to_array(self) -> None:
        assert _all_types["parameters"]["properties"]["lst"]["type"] == "array"

    def test_dict_to_object(self) -> None:
        assert _all_types["parameters"]["properties"]["d"]["type"] == "object"


@pytest.mark.unit
class TestToolDecoratorNoDocstring:
    """Functions without docstrings."""

    def test_empty_description(self) -> None:
        assert _no_docstring["description"] == ""

    def test_no_param_descriptions(self) -> None:
        props = _no_docstring["parameters"]["properties"]
        assert "description" not in props["x"]
        assert "description" not in props["y"]

    def test_required_still_computed(self) -> None:
        assert _no_docstring["parameters"]["required"] == ["x", "y"]


@pytest.mark.unit
class TestToolDecoratorValidToolDefinition:
    """The result conforms to the ToolDefinition shape."""

    def test_has_all_required_keys(self) -> None:
        for key in ("name", "description", "parameters", "handler"):
            assert key in _weather

    def test_parameters_has_type_object(self) -> None:
        assert _weather["parameters"]["type"] == "object"

    def test_parameters_has_properties(self) -> None:
        assert isinstance(_weather["parameters"]["properties"], dict)

    def test_parameters_required_is_list(self) -> None:
        assert isinstance(_weather["parameters"]["required"], list)
