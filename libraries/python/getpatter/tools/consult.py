"""Built-in ``consult`` tool — lets the in-call agent escalate to the
caller's own back-office agent for deeper reasoning or fresh information,
then speak the answer.

This is the *dispatch + consult* pattern: Patter conducts the call (STT +
LLM/voice + TTS + carrier); when the in-call agent hits something it cannot
answer directly, it invokes this tool, which reaches the configured back-office
agent and returns the reply for the agent to speak. The back-office agent stays
off the per-turn path — it is consulted only on demand, so ordinary turns keep
their low latency.

Two targets are supported (see :class:`getpatter.models.ConsultConfig`):

* ``url`` — the generic webhook path: POSTs ``{request, call_id, caller,
  callee}`` to your endpoint and reads a ``reply`` field back.
* ``openai_compatible`` — speaks an OpenAI-compatible ``/chat/completions``
  endpoint directly (e.g. an OpenClaw agent, or vLLM / Ollama / Groq) with no
  hand-written adapter: POSTs ``{model, messages, user}`` and speaks
  ``choices[0].message.content``. Use :meth:`ConsultConfig.openclaw`.

The tool is a normal handler-tool (it rides the existing tool-dispatch path in
both Realtime and Pipeline modes); the handler does the HTTP call itself so the
per-consult timeout and auth from :class:`getpatter.models.ConsultConfig` are
honoured. ``config.reassurance``, when set, is attached so the agent speaks a
filler while the consult runs (Realtime mode only).
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx

from getpatter.tools.tool_executor import _validate_webhook_url

if TYPE_CHECKING:
    from getpatter.models import ConsultConfig, OpenAICompatibleConsult

logger = logging.getLogger("getpatter")

# Cap the response we feed back to the LLM, mirroring the webhook-tool
# executor's 1 MB ceiling.
_MAX_RESPONSE_BYTES = 1_000_000

# Reply fields checked (in order) when a generic webhook returns a JSON object.
_REPLY_KEYS = ("reply", "response", "text", "result", "answer", "message")

# Spoken-friendly fallback when the back-office agent is unreachable or errors —
# never crash the live call.
_GRACEFUL_FALLBACK = "I wasn't able to reach the system to get that answer right now."

_Handler = Callable[[dict, dict], Awaitable[str]]

_PARAMETERS = {
    "type": "object",
    "properties": {
        "request": {
            "type": "string",
            "description": (
                "The question or task to send to your back-office agent for "
                "deeper reasoning, fresh information, or an action beyond this "
                "call. State it self-containedly — the dialog history is not "
                "forwarded with the consult."
            ),
        }
    },
    "required": ["request"],
}


def build_consult_tool(config: "ConsultConfig") -> dict:
    """Build the consult tool dict (schema + handler) for *config*.

    The orchestrator URL is SSRF-validated at build time (raises ``ValueError``
    on a private/loopback/link-local host or non-HTTP scheme unless
    ``config.allow_loopback`` relaxes the host check). Returns a tool dict in the
    same shape the built-in ``transfer_call`` / ``end_call`` tools use — ``{name,
    description, parameters, handler}`` (plus ``reassurance`` when configured) —
    so it merges into ``agent.tools`` and is dispatched by the existing
    ``ToolExecutor`` in both Realtime and Pipeline modes.
    """
    headers = dict(config.headers or {})
    timeout_s = float(config.timeout_s)

    handler: _Handler
    if config.openai_compatible is not None:
        handler = _build_openai_handler(
            config.openai_compatible, headers, timeout_s, config.allow_loopback
        )
    else:
        handler = _build_webhook_handler(
            config.url or "", headers, timeout_s, config.allow_loopback
        )

    tool: dict = {
        "name": config.tool_name,
        "description": config.description,
        "parameters": _PARAMETERS,
        "handler": handler,
    }
    if config.reassurance is not None:
        tool["reassurance"] = config.reassurance
    return tool


def _build_webhook_handler(
    url: str, headers: dict, timeout_s: float, allow_loopback: bool
) -> _Handler:
    """Generic webhook target: POST ``{request, call_id, caller, callee}``."""
    _validate_webhook_url(url, allow_loopback=allow_loopback)  # raises on SSRF

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
            return _GRACEFUL_FALLBACK

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

    return _consult_handler


def _build_openai_handler(
    oc: "OpenAICompatibleConsult",
    headers: dict,
    timeout_s: float,
    allow_loopback: bool,
) -> _Handler:
    """OpenAI-compatible target: POST ``{model, messages, user}`` to
    ``{base_url}/chat/completions`` and speak ``choices[0].message.content``."""
    endpoint = oc.base_url.rstrip("/") + "/chat/completions"
    _validate_webhook_url(endpoint, allow_loopback=allow_loopback)  # raises on SSRF
    # Resolve the bearer once (explicit wins over env). Operator-grade — the
    # value is never logged.
    api_key = oc.api_key or (os.environ.get(oc.api_key_env) if oc.api_key_env else None)
    model = oc.model
    session_header = oc.session_header

    async def _openai_handler(arguments: dict, call_context: dict) -> str:
        request_text = (arguments or {}).get("request", "")
        ctx = call_context or {}
        call_id = ctx.get("call_id", "")
        caller = ctx.get("caller", "")
        callee = ctx.get("callee", "")

        context_lines = [
            "You are answering an inbound phone call relayed by a voice agent.",
        ]
        if caller:
            context_lines.append(f"Caller: {caller}")
        if callee:
            context_lines.append(f"Line dialed: {callee}")
        context_lines.append(
            "Reply concisely in a spoken, conversational style — it is read "
            "aloud to the caller."
        )

        req_headers = dict(headers)
        if api_key:
            req_headers["Authorization"] = f"Bearer {api_key}"
        if session_header and call_id:
            req_headers[session_header] = call_id

        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": "\n".join(context_lines)},
                {"role": "user", "content": request_text},
            ],
            "stream": False,
        }
        if call_id:
            # Harmless secondary to session_header; lets gateways that key on the
            # OpenAI ``user`` field derive a stable per-call session.
            payload["user"] = call_id

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(endpoint, json=payload, headers=req_headers)
                if resp.status_code == 404:
                    logger.warning(
                        "consult tool: OpenAI-compatible endpoint returned 404 — "
                        "is it enabled? (OpenClaw: set "
                        "gateway.http.endpoints.chatCompletions.enabled = true)"
                    )
                    return _GRACEFUL_FALLBACK
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            # Never log the endpoint/headers/key — type only.
            logger.warning(
                "consult tool: openai-compatible call failed: %s",
                type(exc).__name__,
            )
            return _GRACEFUL_FALLBACK

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.warning("consult tool: response missing choices[0].message.content")
            return _GRACEFUL_FALLBACK
        if isinstance(content, str) and content.strip():
            return content.strip()[:_MAX_RESPONSE_BYTES]
        return _GRACEFUL_FALLBACK

    return _openai_handler


# --- Post-call notify (on_call_end → OpenClaw) ------------------------------

# Default instruction prepended to the post-call record sent to OpenClaw.
_POSTCALL_INSTRUCTION = (
    "A phone call handled by the voice agent has just ended. Here is the record "
    "of the call. Log it and follow up if anything needs action."
)
# Cap the transcript we forward so a very long call doesn't bloat the request.
_POSTCALL_MAX_TRANSCRIPT_CHARS = 12_000


def _build_postcall_record(data: dict, include_transcript: bool) -> str:
    """Render the ``on_call_end`` payload into a spoken-call record string."""
    data = data or {}
    lines: list[str] = []
    caller = data.get("caller")
    callee = data.get("callee")
    if caller:
        lines.append(f"Caller: {caller}")
    if callee:
        lines.append(f"Line dialed: {callee}")
    metrics = data.get("metrics")
    duration = getattr(metrics, "duration_seconds", None)
    if isinstance(duration, (int, float)):
        lines.append(f"Duration: {round(duration)}s")
    if include_transcript:
        entries = data.get("transcript") or []
        rendered = "\n".join(
            f"{e.get('role', '?')}: {e.get('text', '')}"
            for e in entries
            if isinstance(e, dict)
        )
        if rendered:
            lines.append("Transcript:\n" + rendered[:_POSTCALL_MAX_TRANSCRIPT_CHARS])
    return "\n".join(lines) if lines else "(no call details available)"


def openclaw_post_call_notifier(
    agent: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout_s: float = 30.0,
    allow_loopback: bool | None = None,
    include_transcript: bool = True,
    instruction: str = _POSTCALL_INSTRUCTION,
):
    """Return an ``on_call_end`` callback that posts the finished call's record
    to a specific OpenClaw agent, so the brain has the record and can follow up.

    Wire it on ``serve``:

        phone.serve(agent, on_call_end=openclaw_post_call_notifier("receptionist"))

    The record is POSTed to the same OpenClaw agent over its OpenAI-compatible
    ``/chat/completions`` gateway, keyed to the call id (the ``user`` field +
    ``x-openclaw-session-key`` header) so it lands in the SAME OpenClaw session
    as the in-call ``consult`` turns. It is fire-and-forget: any error is logged
    by type only (never the URL / headers / key) and never raised into teardown.

    Args mirror :meth:`getpatter.models.ConsultConfig.openclaw`. ``base_url`` /
    ``allow_loopback`` default to the OpenClaw preset (loopback gateway,
    auto-relaxed SSRF); the bearer is read from ``api_key`` or ``OPENCLAW_API_KEY``
    (operator-grade — never logged).
    """
    from getpatter.models import ConsultConfig

    cfg_kwargs: dict = {"api_key": api_key, "timeout_s": timeout_s}
    if base_url is not None:
        cfg_kwargs["base_url"] = base_url
    if allow_loopback is not None:
        cfg_kwargs["allow_loopback"] = allow_loopback
    cfg = ConsultConfig.openclaw(agent, **cfg_kwargs)
    oc = cfg.openai_compatible
    assert oc is not None  # openclaw() always sets it

    endpoint = oc.base_url.rstrip("/") + "/chat/completions"
    _validate_webhook_url(endpoint, allow_loopback=cfg.allow_loopback)
    resolved_key = oc.api_key or (
        os.environ.get(oc.api_key_env) if oc.api_key_env else None
    )
    model = oc.model
    session_header = oc.session_header
    timeout = float(cfg.timeout_s)

    async def _notify(data: dict) -> None:
        call_id = (data or {}).get("call_id", "")
        record = _build_postcall_record(data, include_transcript)
        headers = {"Content-Type": "application/json"}
        if resolved_key:
            headers["Authorization"] = f"Bearer {resolved_key}"
        if session_header and call_id:
            headers[session_header] = call_id
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": record},
            ],
            "stream": False,
        }
        if call_id:
            payload["user"] = call_id
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
        except Exception as exc:
            # Fire-and-forget: never raise into call teardown; log type only.
            logger.warning("openclaw post-call notify failed: %s", type(exc).__name__)

    return _notify
