"""Google Gemini LLM provider for Patter's pipeline mode.

Backed by the ``google-genai`` SDK (``google.genai.Client``) which
supports both the Gemini Developer API and Vertex AI.  This provider
bridges Patter's OpenAI-style chunk protocol with Gemini's
``generate_content_stream`` event format.

Portions adapted from LiveKit Agents
(https://github.com/livekit/agents, commit 78a66bcf79c5cea82989401c408f1dff4b961a5b,
file livekit-plugins/livekit-plugins-google/livekit/plugins/google/llm.py),
licensed under Apache License 2.0. Copyright 2023 LiveKit, Inc.

Adaptations from the LiveKit source:
  * Collapsed the ``llm.LLM`` / ``llm.LLMStream`` pair into a single
    :class:`GoogleLLMProvider` that satisfies Patter's ``LLMProvider``
    Protocol (``async def stream(messages, tools)``).
  * Translates OpenAI-formatted messages into Gemini ``Content`` turns
    (including ``function_call`` / ``function_response`` parts) so
    callers don't need a second message format.
  * Maps Gemini stream events (``text`` parts, ``function_call`` parts)
    to the Patter chunk protocol ``{"type": "text"|"tool_call"|"done"}``.
  * Dropped LiveKit-specific machinery (``thought_signatures``,
    ``APIConnectOptions``, metrics/usage events, retry wrapping) that
    don't apply to Patter's thinner runtime.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

logger = logging.getLogger("patter")

__all__ = ["GoogleLLMProvider"]


_DEFAULT_MODEL = "gemini-2.5-flash"


class GoogleLLMProvider:
    """LLM provider backed by Google Gemini (``google-genai`` SDK).

    Supports both the Gemini Developer API (with an API key) and
    Vertex AI (with Google Cloud credentials).  Streams chunks in
    Patter's ``{"type": "text" | "tool_call" | "done", ...}`` protocol.

    Args:
        api_key: Google API key for the Gemini Developer API. If
            omitted, ``GOOGLE_API_KEY`` is read from the environment.
            Ignored when ``vertexai=True``.
        model: Gemini model ID. Defaults to ``gemini-2.5-flash``.
        vertexai: If True, use Vertex AI instead of the Developer API.
        project: GCP project (Vertex AI only).
        location: GCP location (Vertex AI only). Defaults to
            ``us-central1``.
        temperature: Optional sampling temperature.
        max_output_tokens: Optional output token cap.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        vertexai: bool = False,
        project: str | None = None,
        location: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        try:
            from google.genai import Client
        except ImportError as e:
            raise RuntimeError(
                "The 'google-genai' package is required for GoogleLLMProvider. "
                "Install it with: pip install 'getpatter[google]'"
            ) from e

        use_vertexai = (
            vertexai
            if vertexai
            else os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "0").lower() in ["true", "1"]
        )

        resolved_key: str | None = None
        gcp_project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        gcp_location = location or os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1"

        if use_vertexai:
            if not gcp_project:
                raise ValueError(
                    "Project is required for Vertex AI, either via the 'project' "
                    "argument or the GOOGLE_CLOUD_PROJECT environment variable."
                )
        else:
            resolved_key = api_key or os.environ.get("GOOGLE_API_KEY")
            if not resolved_key:
                raise ValueError(
                    "Google API key is required, either as the 'api_key' argument "
                    "or via the GOOGLE_API_KEY environment variable."
                )
            gcp_project = None
            gcp_location = None

        self._client = Client(
            api_key=resolved_key,
            vertexai=use_vertexai,
            project=gcp_project,
            location=gcp_location,
        )
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Stream chunks from Gemini's ``generate_content_stream``."""
        from google.genai import types

        system_instruction, contents = _to_gemini_contents(messages)
        gemini_tools = _to_gemini_tools(tools) if tools else None

        config_kwargs: dict[str, Any] = {}
        if system_instruction:
            config_kwargs["system_instruction"] = [types.Part(text=system_instruction)]
        if gemini_tools is not None:
            config_kwargs["tools"] = gemini_tools
        if self._temperature is not None:
            config_kwargs["temperature"] = self._temperature
        if self._max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = self._max_output_tokens

        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

        stream = await self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        )

        # Gemini does not provide a stable per-function-call index across
        # chunks, so we assign a monotonically increasing index per
        # function_call part that we see.
        next_index = 0

        last_usage = None
        async for response in stream:
            # Gemini emits ``usage_metadata`` cumulatively on each chunk.
            # Capture only the most recent value so we yield ONE usage
            # event with the final totals (avoids double-counting).
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                last_usage = usage

            if not getattr(response, "candidates", None):
                continue
            candidate = response.candidates[0]
            content = getattr(candidate, "content", None)
            if not content or not getattr(content, "parts", None):
                continue

            for part in content.parts:
                function_call = getattr(part, "function_call", None)
                if function_call:
                    args = getattr(function_call, "args", {}) or {}
                    call_id = (
                        getattr(function_call, "id", None)
                        or f"gemini_call_{next_index}"
                    )
                    yield {
                        "type": "tool_call",
                        "index": next_index,
                        "id": call_id,
                        "name": getattr(function_call, "name", "") or "",
                        "arguments": json.dumps(args),
                    }
                    next_index += 1
                    continue

                text = getattr(part, "text", None)
                if text:
                    yield {"type": "text", "content": text}

        if last_usage is not None:
            yield {
                "type": "usage",
                "input_tokens": getattr(last_usage, "prompt_token_count", 0) or 0,
                "output_tokens": getattr(last_usage, "candidates_token_count", 0) or 0,
                "cache_read_tokens": getattr(last_usage, "cached_content_token_count", 0) or 0,
            }

        yield {"type": "done"}


# ---------------------------------------------------------------------------
# Message / tool translation (OpenAI format -> google.genai types)
# ---------------------------------------------------------------------------


def _to_gemini_tools(tools: list[dict]) -> list[Any]:
    """Convert OpenAI-style tool definitions to Gemini ``Tool`` objects."""
    from google.genai import types

    function_decls: list[types.FunctionDeclaration] = []
    for tool in tools:
        fn = tool.get("function", tool)
        function_decls.append(
            types.FunctionDeclaration(
                name=fn["name"],
                description=fn.get("description", ""),
                parameters=fn.get("parameters", {"type": "object", "properties": {}}),
            )
        )
    if not function_decls:
        return []
    return [types.Tool(function_declarations=function_decls)]


def _to_gemini_contents(messages: list[dict]) -> tuple[str, list[Any]]:
    """Convert OpenAI-style messages to (system_instruction, Gemini Contents).

    Gemini expects:
      * ``system`` as a top-level ``GenerateContentConfig.system_instruction``.
      * ``user`` / ``model`` turns with ``Part`` lists.
      * Tool calls are ``Part(function_call=...)`` on ``model`` turns.
      * Tool results are ``Part(function_response=...)`` on ``user`` turns.
    """
    from google.genai import types

    system_parts: list[str] = []
    contents: list[types.Content] = []

    for msg in messages:
        role = msg.get("role")

        if role == "system":
            content = msg.get("content")
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue

        if role == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                contents.append(
                    types.Content(role="user", parts=[types.Part(text=content)])
                )
            continue

        if role == "assistant":
            parts: list[types.Part] = []
            text = msg.get("content")
            if isinstance(text, str) and text:
                parts.append(types.Part(text=text))

            for tc in msg.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "") or "{}")
                except json.JSONDecodeError:
                    args = {}
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            name=fn.get("name", ""),
                            args=args,
                            id=tc.get("id"),
                        )
                    )
                )

            if parts:
                contents.append(types.Content(role="model", parts=parts))
            continue

        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            raw = msg.get("content", "")
            try:
                response_dict = (
                    json.loads(raw) if isinstance(raw, str) else dict(raw)
                )
                if not isinstance(response_dict, dict):
                    response_dict = {"result": response_dict}
            except (json.JSONDecodeError, TypeError):
                response_dict = {"result": raw}
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=msg.get("name", "") or tool_call_id,
                                response=response_dict,
                                id=tool_call_id or None,
                            )
                        )
                    ],
                )
            )
            continue

    return "\n\n".join(system_parts), contents
