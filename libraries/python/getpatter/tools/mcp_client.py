"""Model Context Protocol (MCP) client integration for Patter (Python
parity with TS ``libraries/typescript/src/tools/mcp-client.ts``).

Lets users plug a Patter agent into MCP servers (Google Workspace,
PayPal, Postgres, GitHub, ...) without writing a wrapper handler per
service::

    phone.agent(
        ...,
        mcp_servers=[
            "https://mcp.googleworkspace.com/sse",
            {"url": "https://mcp.paypal.com/sse", "headers": {"Authorization": "..."}},
        ],
    )

At call start, the SDK queries each server's ``tools/list``, registers
the discovered tools with synthetic handlers that dispatch to
``tools/call``, and merges them into the agent's tool list before the
underlying model sees them.

Lazy import: ``mcp`` is an optional dependency declared in the
``[mcp]`` extra. Users who do not configure ``mcp_servers`` never pay
the install cost.

Limitations of the MVP (will iterate):

  * Per-call connection (handshake on every call). Caching the
    discovered tool list process-wide is a follow-up.
  * Streamable HTTP transport only — ``stdio`` and the legacy ``SSE``
    fallback are not exposed yet.
  * No tool-name conflict resolution: if an MCP tool collides with a
    user-supplied ``agent.tools`` entry, MCP is rejected at startup.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("getpatter")

#: Public MCP server config. ``str`` is shorthand for ``{"url": <str>}``.
MCPServerConfig = str | dict


def _resolve_config(input_: MCPServerConfig, index: int) -> dict:
    if isinstance(input_, str):
        return {"url": input_, "headers": {}, "name": f"mcp[{index}]"}
    if not isinstance(input_, dict):
        raise TypeError(
            f"mcp_servers[{index}] must be a str URL or dict (got {type(input_).__name__})"
        )
    url = input_.get("url")
    if not url:
        raise ValueError(f"mcp_servers[{index}]: missing required 'url' field")
    return {
        "url": url,
        "headers": input_.get("headers") or {},
        "name": input_.get("name") or f"mcp[{index}]",
    }


class MCPManager:
    """Manages a set of MCP server connections for a single Patter call.

    Lifecycle: ``connect()`` once, returns the discovered tools as
    Patter tool dicts; ``close()`` on call end.
    """

    def __init__(self, servers: list[MCPServerConfig] | None) -> None:
        servers = servers or []
        self._configs = [_resolve_config(s, i) for i, s in enumerate(servers)]
        self._sessions: list[Any] = []
        self._exit_stacks: list[Any] = []

    @property
    def has_servers(self) -> bool:
        return len(self._configs) > 0

    async def connect(self) -> list[dict]:
        """Connect to every configured server and discover their tools.

        Returns a list of Patter-shape tool dicts with synthetic
        handlers wired to MCP ``tools/call``.
        """
        if not self._configs:
            return []

        try:
            from mcp import ClientSession  # type: ignore[import-not-found]
            from mcp.client.streamable_http import streamablehttp_client  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "mcp_servers configured but the `mcp` package is not "
                "installed. Run `pip install getpatter[mcp]` to enable "
                f"MCP support. (import error: {exc})"
            ) from exc

        from contextlib import AsyncExitStack

        from getpatter.tools.tool_executor import _validate_webhook_url

        aggregated: list[dict] = []
        for cfg in self._configs:
            # SSRF guard: refuse to connect to MCP servers on internal /
            # loopback / link-local / private targets (cloud metadata at
            # 169.254.169.254, localhost/127.0.0.0/8, 10/8, 172.16/12,
            # 192.168/16, ::1, …). Parity with the TS ``validateWebhookUrl``
            # check in mcp-client.ts. Best-effort (DNS rebinding can still
            # bypass) but MCP URLs are user-supplied config, not caller input.
            try:
                _validate_webhook_url(cfg["url"])
            except ValueError as exc:
                logger.error(
                    "MCP server '%s' (%s) rejected by SSRF guard: %s",
                    cfg["name"],
                    cfg["url"],
                    exc,
                )
                continue
            stack = AsyncExitStack()
            try:
                read_stream, write_stream, _ = await stack.enter_async_context(
                    streamablehttp_client(cfg["url"], headers=cfg["headers"])
                )
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MCP server '%s' (%s) connect failed: %s",
                    cfg["name"],
                    cfg["url"],
                    exc,
                )
                await stack.aclose()
                continue
            self._sessions.append(session)
            self._exit_stacks.append(stack)

            try:
                listed = await session.list_tools()
            except Exception as exc:  # noqa: BLE001
                logger.error("MCP server '%s' tools/list failed: %s", cfg["name"], exc)
                continue
            tools = listed.tools if hasattr(listed, "tools") else []
            for t in tools:
                name = getattr(t, "name", None)
                if not name:
                    continue
                description = getattr(t, "description", "") or ""
                input_schema = getattr(t, "inputSchema", None) or {
                    "type": "object",
                    "properties": {},
                }
                aggregated.append(
                    _build_tool_entry(session, name, description, input_schema)
                )
            logger.info(
                "MCP server '%s' registered %d tool(s)", cfg["name"], len(tools)
            )
        return aggregated

    @staticmethod
    def assert_no_conflicts(
        user_tools: list[dict] | None, mcp_tools: list[dict]
    ) -> None:
        """Raise on tool-name collisions between user-supplied and
        MCP-discovered tools."""
        if not user_tools or not mcp_tools:
            return
        user_names = {t.get("name") for t in user_tools}
        for mcp_t in mcp_tools:
            if mcp_t.get("name") in user_names:
                raise ValueError(
                    f"MCP tool '{mcp_t['name']}' collides with a user-supplied "
                    "tool of the same name. Rename one of them or remove the "
                    "duplicate from agent.tools."
                )

    async def close(self) -> None:
        """Close every open MCP connection. Idempotent; logs but does
        not raise on individual failures."""
        stacks = self._exit_stacks
        self._exit_stacks = []
        self._sessions = []
        for stack in stacks:
            try:
                await stack.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("MCP close error (ignored): %s", exc)


def _build_tool_entry(
    session: Any, name: str, description: str, input_schema: dict
) -> dict:
    """Wrap an MCP tool descriptor into a Patter tool dict with a
    synthetic handler that dispatches to ``tools/call``."""

    async def _handler(args: dict, _ctx: dict) -> str:
        try:
            result = await session.call_tool(name, arguments=args)
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {"error": f"MCP tool '{name}' error: {exc}", "fallback": True}
            )
        # MCP responses carry a ``content`` list of typed blocks.
        text_parts: list[str] = []
        content = getattr(result, "content", None) or []
        for block in content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")
            else:
                # Non-text blocks are JSON-serialised verbatim so the
                # model still sees something useful.
                try:
                    text_parts.append(json.dumps(block.__dict__))
                except Exception:  # noqa: BLE001
                    text_parts.append(str(block))
        text = "\n".join(text_parts)
        if getattr(result, "isError", False):
            return json.dumps({"error": text or "MCP tool error", "fallback": True})
        return text or "{}"

    return {
        "name": name,
        "description": description,
        "parameters": input_schema,
        "handler": _handler,
    }
