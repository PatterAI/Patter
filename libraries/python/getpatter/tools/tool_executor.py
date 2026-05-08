"""Tool webhook executor with SSRF protection and response-size guard.

Tools registered via ``@tool`` (function-backed) execute inline; tools backed
by an external HTTP webhook are dispatched through this module. The dispatcher
validates the URL against an SSRF blocklist (private/loopback IPs, cloud
metadata endpoints, hostname aliases like ``localhost``), enforces a 1 MB
response cap to protect downstream LLM token budgets, and emits an OTel span
for each call.

Concurrency: the executor is fully async and reuses one ``httpx.AsyncClient``
per call to avoid the connection-setup tax on tool-heavy turns.
"""

import asyncio
import ipaddress
import json
import logging
import random
from typing import Any
from urllib.parse import urlparse

import httpx

from getpatter.observability.tracing import SPAN_TOOL, start_span
from getpatter.tools.circuit_breaker import (
    CircuitBreakerOptions,
    CircuitBreakerRegistry,
)

logger = logging.getLogger("getpatter")

# Maximum size of a tool webhook response (1 MB).  Responses larger than this
# are rejected to prevent OOM when the result is forwarded to OpenAI.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024


def _backoff_delay_s(base_s: float, attempt: int) -> float:
    """Exponential backoff with cap + small jitter. Mirrors the TS
    ``backoffDelayMs`` helper. Attempts: 0 → base, 1 → base*2, etc.,
    capped at 5 s with up to 60 ms of jitter."""
    cap = 5.0
    exp = min(cap, base_s * (2**attempt))
    return exp + random.random() * 0.06


async def _invoke_handler(
    handler: object,
    arguments: dict,
    call_context: dict,
    on_progress: Any | None = None,
) -> Any:
    """Invoke a tool handler that may be a plain async function (returns
    a result) or an async generator (yields progress, returns final).

    Generator yields are inspected:
      - ``{"progress": str}`` → forwarded to ``on_progress`` if set.
      - ``{"result": str}`` → captured as the final result; subsequent
        yields are ignored. The generator's ``return`` value (if any)
        overrides this.
      - any other value → JSON-serialised and forwarded as best-effort
        progress so unknown shapes still surface to the caller.
    """
    invoked = handler(arguments, call_context)
    # Async generator detection: ``inspect.isasyncgen`` reliably
    # discriminates ``async def`` from ``async def``-with-``yield``.
    import inspect

    if inspect.isasyncgen(invoked):
        last_result: str = ""
        async for yielded in invoked:
            if isinstance(yielded, dict):
                if isinstance(yielded.get("progress"), str):
                    if on_progress is not None:
                        await _maybe_await(on_progress(yielded["progress"]))
                    continue
                if isinstance(yielded.get("result"), str):
                    last_result = yielded["result"]
                    continue
            # Unknown shape — forward as best-effort progress.
            if on_progress is not None and yielded is not None:
                text = (
                    yielded
                    if isinstance(yielded, str)
                    else json.dumps(yielded, default=str)
                )
                await _maybe_await(on_progress(text))
        # Async generators in Python don't surface ``return`` values like
        # JS generators do — the StopAsyncIteration value is ignored. So
        # the agreed protocol is to capture the final yield's
        # ``{"result": ...}`` (if any) as the result.
        return last_result or "{}"
    # Plain coroutine — await it.
    if asyncio.iscoroutine(invoked) or asyncio.isfuture(invoked):
        return await invoked
    # Synchronous handler (rare but supported).
    return invoked


async def _maybe_await(value: Any) -> None:
    """Await ``value`` only if it is a coroutine. Lets ``on_progress``
    be either a sync or async callable."""
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        await value


# Hostnames that must never be targeted by a webhook, even when they are not
# literal IPs (DNS-based SSRF to cloud metadata endpoints or localhost aliases).
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata",
    }
)


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
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
        ):
            raise ValueError(
                f"Webhook URL points to a private/reserved address: {hostname!r}"
            )
    except ValueError as exc:
        # Re-raise only our own ValueError (private IP rejection), not the
        # ip_address() parsing error which just means it's a hostname.
        if "private" in str(exc) or "reserved" in str(exc):
            raise


class ToolExecutor:
    """Executes agent tools via local handler or webhook with retry,
    exponential backoff, and a per-tool circuit breaker.

    Failure modes return ``{"error": ..., "fallback": True}`` so the
    model can recover gracefully (e.g. respond "I couldn't reach the
    booking system, can I take your number to call you back?") instead
    of hanging on an exception that never surfaces.
    """

    MAX_RETRIES = 2
    RETRY_DELAY = 0.5  # base delay in seconds; doubles each attempt

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        circuit_breaker: CircuitBreakerOptions | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._breaker = CircuitBreakerRegistry(circuit_breaker)

    @property
    def circuit_breaker(self) -> CircuitBreakerRegistry:
        """Expose the breaker for tests + dashboard observability."""
        return self._breaker

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
        on_progress: Any | None = None,
    ) -> str:
        """Execute a tool and return the result as a JSON string.

        If *handler* is provided, it is called directly (sync or async).
        Otherwise, falls back to POSTing to *webhook_url*. Both paths
        get retry-with-exponential-backoff and circuit-breaker
        protection.
        """
        # Reject early when the breaker is OPEN — returns a structured
        # fallback JSON so the model can recover instead of waiting.
        if not self._breaker.allow(tool_name):
            retry_after_ms = self._breaker.time_until_half_open_ms(tool_name)
            return json.dumps(
                {
                    "error": f"Tool '{tool_name}' is temporarily unavailable (circuit open).",
                    "fallback": True,
                    "circuit_state": "open",
                    "retry_after_ms": int(retry_after_ms),
                }
            )

        with start_span(
            SPAN_TOOL,
            {
                "patter.tool.name": tool_name,
                "patter.tool.transport": "handler"
                if handler is not None
                else ("webhook" if webhook_url else "none"),
                "patter.call.id": call_context.get("call_id", ""),
            },
        ):
            if handler is not None:
                return await self._execute_handler(
                    tool_name, arguments, call_context, handler, on_progress
                )
            if webhook_url:
                return await self._execute_webhook(
                    tool_name, arguments, call_context, webhook_url
                )
            return json.dumps(
                {
                    "error": f"Tool '{tool_name}' has no handler or webhook_url",
                    "fallback": True,
                }
            )

    async def _execute_handler(
        self,
        tool_name: str,
        arguments: dict,
        call_context: dict,
        handler: object,
        on_progress: Any | None = None,
    ) -> str:
        """Call a local Python function as a tool handler. Supports
        plain async functions AND async generators that yield
        ``{"progress": "..."}`` for streaming updates. Retries with
        exponential backoff on exception (parity with webhook path).
        Previously a single failure became a hard fault; a transient DB
        blip would silently kill the turn."""
        last_err: Exception | None = None
        total_attempts = self.MAX_RETRIES + 1
        for attempt in range(total_attempts):
            try:
                result = await _invoke_handler(
                    handler, arguments, call_context, on_progress
                )
                self._breaker.record_success(tool_name)
                if isinstance(result, str):
                    return result
                return json.dumps(result)
            except Exception as e:  # noqa: BLE001 - intentional broad catch
                last_err = e
                if attempt < total_attempts - 1:
                    logger.warning(
                        "Tool handler '%s' failed (attempt %d/%d), retrying: %s",
                        tool_name,
                        attempt + 1,
                        total_attempts,
                        e,
                    )
                    await asyncio.sleep(_backoff_delay_s(self.RETRY_DELAY, attempt))
        logger.error("Tool handler '%s' raised: %s", tool_name, last_err)
        self._breaker.record_failure(tool_name)
        return json.dumps(
            {
                "error": f"Tool handler error after {total_attempts} attempts: {last_err}",
                "fallback": True,
            }
        )

    async def _execute_webhook(
        self,
        tool_name: str,
        arguments: dict,
        call_context: dict,
        webhook_url: str,
    ) -> str:
        """POST to user webhook and return result as string for OpenAI.

        Retries up to MAX_RETRIES times on failure with exponential
        backoff before returning an error JSON with ``fallback=True``.
        Records success/failure on the per-tool circuit breaker so a
        flapping endpoint trips OPEN instead of being retried forever.
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
                    self._breaker.record_failure(tool_name)
                    return json.dumps(
                        {
                            "error": f"Webhook response too large: {content_length} bytes (max {_MAX_RESPONSE_BYTES})",
                            "fallback": True,
                        }
                    )
                self._breaker.record_success(tool_name)
                return json.dumps(response.json())
            except Exception as e:  # noqa: BLE001 - intentional broad catch
                if attempt < self.MAX_RETRIES:
                    logger.warning(
                        "Tool webhook '%s' failed (attempt %d/%d), retrying: %s",
                        tool_name,
                        attempt + 1,
                        self.MAX_RETRIES + 1,
                        e,
                    )
                    await asyncio.sleep(_backoff_delay_s(self.RETRY_DELAY, attempt))
                else:
                    logger.error(
                        "Tool webhook '%s' failed after %d attempts: %s",
                        tool_name,
                        self.MAX_RETRIES + 1,
                        e,
                    )
                    self._breaker.record_failure(tool_name)
                    return json.dumps(
                        {
                            "error": f"Tool failed after {self.MAX_RETRIES + 1} attempts: {str(e)}",
                            "fallback": True,
                        }
                    )
        # Should never reach here, but satisfy type checker
        return json.dumps({"error": "unexpected", "fallback": True})
