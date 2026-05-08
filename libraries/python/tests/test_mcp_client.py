"""Tests for ``getpatter.tools.mcp_client.MCPManager``.

Covers config validation, ``has_servers`` getter, ``tools/list``
discovery, ``tools/call`` dispatch, tool-name collision detection, auth
header propagation, per-call lifecycle (connect/close idempotency), and
the optional-dependency-absent error path.

The ``mcp`` SDK is patched at the module level — no real MCP server is
ever contacted. All tests are tagged ``@pytest.mark.mocked`` per
``.claude/rules/authentic-tests.md`` because the outer
``mcp.ClientSession`` boundary is faked.
"""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from getpatter.tools.mcp_client import (
    MCPManager,
    _resolve_config,
)


# ---------------------------------------------------------------------------
# Helpers — fake `mcp` package wired into ``sys.modules`` for the duration
# of a test so the lazy import inside ``MCPManager.connect`` resolves.
# ---------------------------------------------------------------------------


def _make_fake_mcp_modules(
    *,
    list_tools_payload: Any,
    call_tool_payload: Any | None = None,
    call_tool_side_effect: Exception | None = None,
    captured_headers: dict[str, dict[str, str]] | None = None,
) -> dict[str, ModuleType]:
    """Return a dict of fake module objects suitable for ``sys.modules``.

    ``captured_headers`` (if provided) is mutated in place: ``["headers"]``
    receives whatever ``streamablehttp_client`` was constructed with, so
    tests can assert auth header propagation.
    """

    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=list_tools_payload)
    if call_tool_side_effect is not None:
        session.call_tool = AsyncMock(side_effect=call_tool_side_effect)
    else:
        session.call_tool = AsyncMock(return_value=call_tool_payload)

    @asynccontextmanager
    async def _session_ctx(*_args: Any, **_kwargs: Any):
        yield session

    client_session_cls = MagicMock(side_effect=_session_ctx)

    @asynccontextmanager
    async def _streamable_ctx(url: str, headers: dict[str, str] | None = None):
        if captured_headers is not None:
            captured_headers["url"] = url
            captured_headers["headers"] = headers or {}
        # The real factory yields (read_stream, write_stream, get_session_id).
        yield (object(), object(), lambda: "fake-session-id")

    streamable_factory = MagicMock(side_effect=_streamable_ctx)

    mcp_pkg = ModuleType("mcp")
    mcp_pkg.ClientSession = client_session_cls  # type: ignore[attr-defined]

    mcp_client_pkg = ModuleType("mcp.client")
    mcp_streamable = ModuleType("mcp.client.streamable_http")
    mcp_streamable.streamablehttp_client = streamable_factory  # type: ignore[attr-defined]

    # Expose the session for tests that want to assert against it.
    mcp_pkg._test_session = session  # type: ignore[attr-defined]
    mcp_pkg._test_client_session_cls = client_session_cls  # type: ignore[attr-defined]
    mcp_pkg._test_streamable_factory = streamable_factory  # type: ignore[attr-defined]

    return {
        "mcp": mcp_pkg,
        "mcp.client": mcp_client_pkg,
        "mcp.client.streamable_http": mcp_streamable,
    }


def _install_fake_mcp(modules: dict[str, ModuleType]) -> dict[str, ModuleType | None]:
    """Install ``modules`` into ``sys.modules``; return originals for restore."""
    saved: dict[str, ModuleType | None] = {}
    for name, mod in modules.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    return saved


def _restore_modules(saved: dict[str, ModuleType | None]) -> None:
    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


def _make_tool(
    name: str, description: str = "", input_schema: dict | None = None
) -> Any:
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.inputSchema = input_schema or {"type": "object", "properties": {}}
    return tool


def _make_text_block(text: str) -> Any:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


# ---------------------------------------------------------------------------
# 1. Config validation
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestConfigValidation:
    def test_string_shorthand_resolves_to_url_dict(self) -> None:
        cfg = _resolve_config("https://mcp.example.com/sse", 0)
        assert cfg == {
            "url": "https://mcp.example.com/sse",
            "headers": {},
            "name": "mcp[0]",
        }

    def test_full_options_object_preserved(self) -> None:
        cfg = _resolve_config(
            {
                "url": "https://mcp.paypal.com/sse",
                "headers": {"Authorization": "Bearer xyz"},
                "name": "paypal",
            },
            2,
        )
        assert cfg["url"] == "https://mcp.paypal.com/sse"
        assert cfg["headers"] == {"Authorization": "Bearer xyz"}
        assert cfg["name"] == "paypal"

    def test_missing_url_raises_value_error_with_index(self) -> None:
        with pytest.raises(ValueError, match=r"mcp_servers\[3\].*url"):
            _resolve_config({"headers": {"x": "y"}}, 3)

    def test_invalid_type_raises_type_error_with_index(self) -> None:
        with pytest.raises(TypeError, match=r"mcp_servers\[1\]"):
            _resolve_config(42, 1)  # type: ignore[arg-type]

    def test_constructor_validates_each_entry_with_index(self) -> None:
        with pytest.raises(ValueError, match=r"mcp_servers\[1\]"):
            MCPManager(["https://ok.example.com/sse", {"headers": {}}])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 2. has_servers getter
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestHasServers:
    def test_has_servers_false_when_none(self) -> None:
        assert MCPManager(None).has_servers is False

    def test_has_servers_false_when_empty_list(self) -> None:
        assert MCPManager([]).has_servers is False

    def test_has_servers_true_when_configs_present(self) -> None:
        mgr = MCPManager(["https://mcp.example.com/sse"])
        assert mgr.has_servers is True


# ---------------------------------------------------------------------------
# 3. tools/list discovery
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestToolsListDiscovery:
    async def test_discovered_tools_wrapped_as_tool_definition(self) -> None:
        listed = MagicMock()
        listed.tools = [
            _make_tool(
                "search_email",
                "Search Gmail",
                {"type": "object", "properties": {"q": {"type": "string"}}},
            ),
            _make_tool("send_email", "Send a message"),
        ]
        modules = _make_fake_mcp_modules(list_tools_payload=listed)
        saved = _install_fake_mcp(modules)
        try:
            mgr = MCPManager(["https://mcp.googleworkspace.com/sse"])
            tools = await mgr.connect()
        finally:
            await mgr.close()
            _restore_modules(saved)

        assert len(tools) == 2
        assert tools[0]["name"] == "search_email"
        assert tools[0]["description"] == "Search Gmail"
        assert tools[0]["parameters"] == {
            "type": "object",
            "properties": {"q": {"type": "string"}},
        }
        assert callable(tools[0]["handler"])
        assert tools[1]["name"] == "send_email"
        # Missing inputSchema falls back to an empty object schema.
        assert tools[1]["parameters"] == {"type": "object", "properties": {}}

    async def test_unnamed_tools_are_skipped(self) -> None:
        listed = MagicMock()
        unnamed = MagicMock()
        unnamed.name = None
        listed.tools = [unnamed, _make_tool("real_tool")]
        modules = _make_fake_mcp_modules(list_tools_payload=listed)
        saved = _install_fake_mcp(modules)
        try:
            mgr = MCPManager(["https://mcp.example.com/sse"])
            tools = await mgr.connect()
        finally:
            await mgr.close()
            _restore_modules(saved)

        assert [t["name"] for t in tools] == ["real_tool"]


# ---------------------------------------------------------------------------
# 4. tools/call dispatch
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestToolsCallDispatch:
    async def test_synthetic_handler_routes_to_call_tool_and_returns_text(self) -> None:
        listed = MagicMock()
        listed.tools = [_make_tool("echo_tool")]
        call_result = MagicMock()
        call_result.content = [_make_text_block("hello back")]
        call_result.isError = False
        modules = _make_fake_mcp_modules(
            list_tools_payload=listed, call_tool_payload=call_result
        )
        saved = _install_fake_mcp(modules)
        try:
            mgr = MCPManager(["https://mcp.example.com/sse"])
            tools = await mgr.connect()
            handler = tools[0]["handler"]
            result = await handler({"msg": "hi"}, {})
        finally:
            await mgr.close()
            _restore_modules(saved)

        assert result == "hello back"
        # Arguments forwarded verbatim to call_tool.
        session = modules["mcp"]._test_session  # type: ignore[attr-defined]
        session.call_tool.assert_awaited_once_with("echo_tool", arguments={"msg": "hi"})

    async def test_handler_serialises_is_error_response_as_fallback_envelope(
        self,
    ) -> None:
        listed = MagicMock()
        listed.tools = [_make_tool("flaky_tool")]
        err_result = MagicMock()
        err_result.content = [_make_text_block("upstream 500")]
        err_result.isError = True
        modules = _make_fake_mcp_modules(
            list_tools_payload=listed, call_tool_payload=err_result
        )
        saved = _install_fake_mcp(modules)
        try:
            mgr = MCPManager(["https://mcp.example.com/sse"])
            tools = await mgr.connect()
            result = await tools[0]["handler"]({}, {})
        finally:
            await mgr.close()
            _restore_modules(saved)

        parsed = json.loads(result)
        assert parsed["fallback"] is True
        assert "upstream 500" in parsed["error"]

    async def test_handler_catches_call_tool_exception_and_returns_fallback(
        self,
    ) -> None:
        listed = MagicMock()
        listed.tools = [_make_tool("boom_tool")]
        modules = _make_fake_mcp_modules(
            list_tools_payload=listed,
            call_tool_side_effect=RuntimeError("transport blew up"),
        )
        saved = _install_fake_mcp(modules)
        try:
            mgr = MCPManager(["https://mcp.example.com/sse"])
            tools = await mgr.connect()
            result = await tools[0]["handler"]({}, {})
        finally:
            await mgr.close()
            _restore_modules(saved)

        parsed = json.loads(result)
        assert parsed["fallback"] is True
        assert "transport blew up" in parsed["error"]


# ---------------------------------------------------------------------------
# 5. Tool-name collision
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestToolNameCollision:
    def test_assert_no_conflicts_raises_with_offending_name(self) -> None:
        user_tools = [{"name": "send_email"}, {"name": "list_inbox"}]
        mcp_tools = [{"name": "search_email"}, {"name": "send_email"}]
        with pytest.raises(ValueError, match="send_email"):
            MCPManager.assert_no_conflicts(user_tools, mcp_tools)

    def test_assert_no_conflicts_no_op_when_no_user_tools(self) -> None:
        # Should NOT raise — nothing to collide with.
        MCPManager.assert_no_conflicts(None, [{"name": "x"}])
        MCPManager.assert_no_conflicts([], [{"name": "x"}])

    def test_assert_no_conflicts_no_op_when_no_mcp_tools(self) -> None:
        MCPManager.assert_no_conflicts([{"name": "x"}], [])

    def test_assert_no_conflicts_passes_when_disjoint(self) -> None:
        MCPManager.assert_no_conflicts([{"name": "local_a"}], [{"name": "remote_b"}])


# ---------------------------------------------------------------------------
# 6. Auth headers propagation
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestAuthHeadersPropagation:
    async def test_authorization_header_reaches_streamable_factory(self) -> None:
        listed = MagicMock()
        listed.tools = []
        captured: dict[str, dict[str, str]] = {}
        modules = _make_fake_mcp_modules(
            list_tools_payload=listed, captured_headers=captured
        )
        saved = _install_fake_mcp(modules)
        try:
            mgr = MCPManager(
                [
                    {
                        "url": "https://mcp.paypal.com/sse",
                        "headers": {"Authorization": "Bearer secret_xyz"},
                        "name": "paypal",
                    }
                ]
            )
            await mgr.connect()
        finally:
            await mgr.close()
            _restore_modules(saved)

        assert captured["url"] == "https://mcp.paypal.com/sse"
        assert captured["headers"] == {"Authorization": "Bearer secret_xyz"}

    async def test_string_shorthand_propagates_empty_headers(self) -> None:
        listed = MagicMock()
        listed.tools = []
        captured: dict[str, dict[str, str]] = {}
        modules = _make_fake_mcp_modules(
            list_tools_payload=listed, captured_headers=captured
        )
        saved = _install_fake_mcp(modules)
        try:
            mgr = MCPManager(["https://mcp.example.com/sse"])
            await mgr.connect()
        finally:
            await mgr.close()
            _restore_modules(saved)

        assert captured["headers"] == {}


# ---------------------------------------------------------------------------
# 7. Per-call lifecycle — connect / close / idempotent close
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestLifecycle:
    async def test_connect_returns_empty_when_no_servers_configured(self) -> None:
        mgr = MCPManager(None)
        # Critically: connect() with zero configs MUST NOT import `mcp` —
        # so we don't even need to install the fake. Failing to import
        # here would prove the zero-cost guarantee is broken.
        assert "mcp" not in sys.modules or sys.modules.get("mcp") is not None
        result = await mgr.connect()
        assert result == []
        await mgr.close()  # No-op; should not raise.

    async def test_close_is_idempotent(self) -> None:
        listed = MagicMock()
        listed.tools = [_make_tool("noop_tool")]
        modules = _make_fake_mcp_modules(list_tools_payload=listed)
        saved = _install_fake_mcp(modules)
        try:
            mgr = MCPManager(["https://mcp.example.com/sse"])
            await mgr.connect()
            await mgr.close()
            # Second close MUST be safe.
            await mgr.close()
        finally:
            _restore_modules(saved)


# ---------------------------------------------------------------------------
# 8. Optional dependency absent
# ---------------------------------------------------------------------------


@pytest.mark.mocked
class TestOptionalDepAbsent:
    async def test_raises_clear_error_when_mcp_not_installed_and_servers_configured(
        self,
    ) -> None:
        # Force the lazy import inside `MCPManager.connect` to fail by
        # making the relevant submodule unimportable.
        original_mcp = sys.modules.pop("mcp", None)
        original_streamable = sys.modules.pop("mcp.client.streamable_http", None)

        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "mcp" or name.startswith("mcp."):
                raise ImportError(f"No module named '{name}'")
            return _real_import(name, *args, **kwargs)

        import builtins

        _real_import = builtins.__import__
        try:
            mgr = MCPManager(["https://mcp.example.com/sse"])
            with patch("builtins.__import__", side_effect=_fake_import):
                with pytest.raises(RuntimeError) as excinfo:
                    await mgr.connect()
            msg = str(excinfo.value)
            assert "mcp_servers configured" in msg
            assert "pip install getpatter[mcp]" in msg
        finally:
            if original_mcp is not None:
                sys.modules["mcp"] = original_mcp
            if original_streamable is not None:
                sys.modules["mcp.client.streamable_http"] = original_streamable

    async def test_zero_cost_when_mcp_servers_omitted(self) -> None:
        # Even with `mcp` unimportable, connect() with zero configs MUST
        # NOT trigger any import attempt — proving the optional dep is
        # truly opt-in.
        import builtins

        _real_import = builtins.__import__
        attempts: list[str] = []

        def _tracking_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "mcp" or name.startswith("mcp."):
                attempts.append(name)
                raise ImportError(f"No module named '{name}'")
            return _real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_tracking_import):
            mgr = MCPManager(None)
            result = await mgr.connect()

        assert result == []
        assert attempts == [], f"unexpected mcp imports: {attempts}"
