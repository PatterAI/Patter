import asyncio
import ipaddress
import json
import logging
from urllib.parse import urlparse

import httpx

from getpatter.observability.tracing import SPAN_TOOL, start_span

logger = logging.getLogger("patter")

# Maximum size of a tool webhook response (1 MB).  Responses larger than this
# are rejected to prevent OOM when the result is forwarded to OpenAI.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024

# Hostnames that must never be targeted by a webhook, even when they are not
# literal IPs (DNS-based SSRF to cloud metadata endpoints or localhost aliases).
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata",
})


def _validate_webhook_url(url: str) -> None:
    """Block SSRF — reject private IPs, loopback, non-HTTP(S) schemes.

    NOTE: This check is a best-effort filter.  DNS rebinding attacks can
    bypass it because the hostname is resolved at validation time, not at
    request time.  The real protection is that tool webhook URLs are
    supplied by the SDK user (not by callers), so they are trusted
    configuration values.  Do not expose this function to untrusted input.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme!r}")
    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("Webhook URL is missing a hostname")
    # Reject known-dangerous hostnames up front, before any IP parsing, so
    # that aliases like `localhost` or cloud metadata endpoints are blocked
    # even when they do not resolve to a literal IP in the URL string.
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise ValueError(f"Webhook URL points to a blocked hostname: {hostname!r}")
    # Block literal private/loopback IP addresses in the URL itself.
    # We intentionally avoid blocking based on DNS resolution here because
    # synchronous socket.gethostbyname() would block the async event loop.
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError(f"Webhook URL points to a private/reserved address: {hostname!r}")
    except ValueError as exc:
        # Re-raise only our own ValueError (private IP rejection), not the
        # ip_address() parsing error which just means it's a hostname.
        if "private" in str(exc) or "reserved" in str(exc):
            raise


class ToolExecutor:
    """Executes agent tools via local handler or webhook with retry/fallback."""

    MAX_RETRIES = 2
    RETRY_DELAY = 0.5  # seconds

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None

    async def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owns_client:
            await self._client.aclose()

    async def execute(
        self,
        tool_name: str,
        arguments: dict,
        call_context: dict,
        webhook_url: str = "",
        handler: object = None,
    ) -> str:
        """Execute a tool and return the result as a JSON string.

        If *handler* is provided, it is called directly (sync or async).
        Otherwise, falls back to POSTing to *webhook_url*.
        """
        with start_span(
            SPAN_TOOL,
            {
                "patter.tool.name": tool_name,
                "patter.tool.transport": "handler" if handler is not None else ("webhook" if webhook_url else "none"),
                "patter.call.id": call_context.get("call_id", ""),
            },
        ):
            if handler is not None:
                return await self._execute_handler(tool_name, arguments, call_context, handler)
            if webhook_url:
                return await self._execute_webhook(tool_name, arguments, call_context, webhook_url)
            return json.dumps({"error": f"Tool '{tool_name}' has no handler or webhook_url", "fallback": True})

    async def _execute_handler(
        self,
        tool_name: str,
        arguments: dict,
        call_context: dict,
        handler: object,
    ) -> str:
        """Call a local Python function as a tool handler."""
        try:
            result = handler(arguments, call_context)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result
            if isinstance(result, str):
                return result
            return json.dumps(result)
        except Exception as e:
            logger.error("Tool handler '%s' raised: %s", tool_name, e)
            return json.dumps({"error": f"Tool handler error: {str(e)}", "fallback": True})

    async def _execute_webhook(
        self,
        tool_name: str,
        arguments: dict,
        call_context: dict,
        webhook_url: str,
    ) -> str:
        """POST to user webhook and return result as string for OpenAI.

        Retries up to MAX_RETRIES times on failure before returning an error
        JSON with fallback=True.
        """
        _validate_webhook_url(webhook_url)
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = await self._client.post(
                    webhook_url,
                    json={
                        "tool": tool_name,
                        "arguments": arguments,
                        "call_id": call_context.get("call_id", ""),
                        "caller": call_context.get("caller", ""),
                        "callee": call_context.get("callee", ""),
                        "attempt": attempt + 1,
                    },
                )
                response.raise_for_status()
                content_length = len(response.content)
                if content_length > _MAX_RESPONSE_BYTES:
                    raise ValueError(
                        f"Webhook response too large: {content_length} bytes "
                        f"(max {_MAX_RESPONSE_BYTES})"
                    )
                return json.dumps(response.json())
            except Exception as e:
                if attempt < self.MAX_RETRIES:
                    logger.warning(
                        "Tool webhook failed (attempt %d), retrying in %ss: %s",
                        attempt + 1,
                        self.RETRY_DELAY,
                        e,
                    )
                    await asyncio.sleep(self.RETRY_DELAY)
                else:
                    logger.error(
                        "Tool webhook failed after %d attempts: %s",
                        self.MAX_RETRIES + 1,
                        e,
                    )
                    return json.dumps(
                        {
                            "error": f"Tool failed after {self.MAX_RETRIES + 1} attempts: {str(e)}",
                            "fallback": True,
                        }
                    )
        # Should never reach here, but satisfy type checker
        return json.dumps({"error": "unexpected", "fallback": True})
