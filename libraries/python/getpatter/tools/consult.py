"""Built-in ``consult`` tool — lets the in-call agent escalate to the
caller's own back-office agent over HTTP for deeper reasoning or fresh
information, then speak the answer.

This is the *dispatch + consult* pattern: Patter conducts the call (STT +
LLM/voice + TTS + carrier); when the in-call agent hits something it cannot
answer directly, it invokes this tool, which POSTs the request to the
configured orchestrator endpoint and returns the reply for the agent to speak.
The orchestrator stays off the per-turn path — it is consulted only on demand,
so ordinary turns keep their low latency.

The tool is a normal handler-tool (it rides the existing tool-dispatch path in
both Realtime and Pipeline modes); the handler does the HTTP call itself so the
per-consult timeout and auth headers from :class:`getpatter.models.ConsultConfig`
are honoured (the generic webhook-tool path uses a fixed 10 s timeout and sends
no headers).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import httpx

from getpatter.tools.tool_executor import _validate_webhook_url

if TYPE_CHECKING:
    from getpatter.models import ConsultConfig

logger = logging.getLogger("getpatter")

# Cap the orchestrator response we feed back to the LLM, mirroring the
# webhook-tool executor's 1 MB ceiling.
_MAX_RESPONSE_BYTES = 1_000_000

# Reply fields checked (in order) when the orchestrator returns a JSON object.
_REPLY_KEYS = ("reply", "response", "text", "result", "answer", "message")


def build_consult_tool(config: "ConsultConfig") -> dict:
    """Build the consult tool dict (schema + handler) for *config*.

    Validates the orchestrator URL for SSRF at build time (raises
    ``ValueError`` on a private/loopback/link-local host or non-HTTP scheme),
    then returns a tool dict in the same shape the built-in
    ``transfer_call`` / ``end_call`` tools use — ``{name, description,
    parameters, handler}`` — so it merges into ``agent.tools`` and is dispatched
    by the existing ``ToolExecutor`` in both Realtime and Pipeline modes.

    When ``config.allow_loopback`` is ``True`` the loopback / private / link-local
    host checks are relaxed for this URL only (for trusted, developer-configured
    local agents); the non-HTTP(S) scheme check still applies.
    """
    _validate_webhook_url(
        config.url, allow_loopback=config.allow_loopback
    )  # raises on SSRF / bad scheme

    url = config.url
    headers = dict(config.headers or {})
    timeout_s = float(config.timeout_s)

    parameters = {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": (
                    "The question or task to send to your back-office agent "
                    "for deeper reasoning, fresh information, or an action "
                    "beyond this call."
                ),
            }
        },
        "required": ["request"],
    }

    async def _consult_handler(arguments: dict, call_context: dict) -> str:
        request_text = (arguments or {}).get("request", "")
        ctx = call_context or {}
        payload = {
            "request": request_text,
            "call_id": ctx.get("call_id", ""),
            "caller": ctx.get("caller", ""),
            "callee": ctx.get("callee", ""),
        }
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.content[:_MAX_RESPONSE_BYTES]
        except Exception as exc:
            # Never log the URL or headers (may carry a secret); type only.
            logger.warning(
                "consult tool: orchestrator call failed: %s", type(exc).__name__
            )
            return "I wasn't able to reach the system to get that answer right now."

        try:
            data = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            return body.decode("utf-8", errors="replace")
        if isinstance(data, dict):
            for key in _REPLY_KEYS:
                value = data.get(key)
                if isinstance(value, str):
                    return value
        return json.dumps(data)

    return {
        "name": config.tool_name,
        "description": config.description,
        "parameters": parameters,
        "handler": _consult_handler,
    }
