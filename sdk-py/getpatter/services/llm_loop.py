"""Built-in LLM loop for pipeline mode when no on_message handler is provided.

Uses a pluggable ``LLMProvider`` protocol so callers can supply OpenAI,
Anthropic, Gemini, or any custom provider.  The default provider is
``OpenAILLMProvider`` which preserves full backward compatibility.
"""

from __future__ import annotations

__all__ = ["LLMLoop", "LLMProvider", "OpenAILLMProvider"]

import json
import logging
from typing import AsyncGenerator, AsyncIterator, Protocol, runtime_checkable

from getpatter.observability.tracing import SPAN_LLM, start_span

logger = logging.getLogger("patter")


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol that any LLM provider must satisfy.

    Implementors yield streaming chunks as dicts.  Each chunk must include a
    ``"type"`` key:

    * ``{"type": "text", "content": "..."}`` — a text token.
    * ``{"type": "tool_call", "index": int, "id": str | None,
       "name": str | None, "arguments": str | None}`` — a (partial) tool
       invocation.  Chunks with the same ``index`` are concatenated.
    * ``{"type": "done"}`` — signals the end of the stream (optional).
    """

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Yield streaming chunks for the given messages and tools."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Built-in OpenAI provider
# ---------------------------------------------------------------------------


class OpenAILLMProvider:
    """LLM provider backed by OpenAI Chat Completions (streaming).

    Args:
        api_key: OpenAI API key.
        model: Chat model ID (e.g. ``"gpt-4o-mini"``).
    """

    def __init__(self, api_key: str, model: str) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError(
                "The 'openai' package is required for the built-in OpenAI LLM "
                "provider. Install it with: pip install openai"
            )

        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Yield normalised chunks from OpenAI Chat Completions."""
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools if tools else None,
            stream=True,
        )

        async for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue

            if delta.content:
                yield {"type": "text", "content": delta.content}

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    yield {
                        "type": "tool_call",
                        "index": tc.index,
                        "id": tc.id,
                        "name": tc.function.name if tc.function else None,
                        "arguments": tc.function.arguments if tc.function else None,
                    }


# ---------------------------------------------------------------------------
# LLM loop
# ---------------------------------------------------------------------------


class LLMLoop:
    """Streaming LLM with tool calling for pipeline mode.

    When ``agent.provider == "pipeline"`` and no ``on_message`` callback is
    provided, this class handles the LLM interaction internally.

    Args:
        openai_key: OpenAI API key (used when *llm_provider* is not supplied).
        model: Chat model ID (e.g. ``"gpt-4o-mini"``).
        system_prompt: System instructions for the agent.
        tools: Tool definitions from the agent (may include local handlers
            and/or webhook URLs).
        tool_executor: A ``ToolExecutor`` instance for running tools.
        llm_provider: An optional custom :class:`LLMProvider`.  When omitted
            an :class:`OpenAILLMProvider` is created using *openai_key* and
            *model* (backward compatible).
    """

    def __init__(
        self,
        openai_key: str,
        model: str,
        system_prompt: str,
        tools: list[dict] | None = None,
        tool_executor=None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        if llm_provider is not None:
            self._provider = llm_provider
        else:
            self._provider = OpenAILLMProvider(api_key=openai_key, model=model)

        self._system_prompt = system_prompt
        self._tools = tools
        self._tool_executor = tool_executor

        # Build OpenAI-format tool definitions (without handler/webhook_url)
        self._openai_tools: list[dict] | None = None
        if tools:
            self._openai_tools = []
            for t in tools:
                fn = {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                }
                self._openai_tools.append({"type": "function", "function": fn})

        # Map tool name -> original tool dict (for handler/webhook_url lookup)
        self._tool_map: dict[str, dict] = {}
        if tools:
            for t in tools:
                self._tool_map[t["name"]] = t

    async def run(
        self,
        user_text: str,
        history: list[dict],
        call_context: dict,
    ) -> AsyncGenerator[str, None]:
        """Stream LLM response tokens, handling tool calls automatically.

        Builds messages from history + user_text, streams via the configured
        :class:`LLMProvider`.  If the model emits tool calls, executes them
        via ``ToolExecutor`` and re-submits to the LLM until a text response
        is produced.

        Args:
            user_text: The user's latest transcribed utterance.
            history: Conversation history as ``[{role, text, timestamp}]``.
            call_context: Dict with ``call_id``, ``caller``, ``callee``.

        Yields:
            Text tokens as they arrive from the LLM.
        """
        messages = self._build_messages(history, user_text)

        # Loop to handle tool calls — the LLM may call tools multiple times
        max_iterations = 10
        for iteration in range(max_iterations):
            tool_calls_accumulated: dict[int, dict] = {}
            text_parts: list[str] = []
            has_tool_calls = False

            # Open a span around the provider streaming call. Kept as an
            # explicit __enter__/__exit__ (rather than ``with``) because we
            # need to ``yield`` from inside the span which ``with`` + async
            # generators makes awkward.
            _span_cm = start_span(
                SPAN_LLM,
                {
                    "patter.llm.iteration": iteration,
                    "patter.llm.history_size": len(history),
                    "patter.call.id": call_context.get("call_id", ""),
                },
            )
            _span_cm.__enter__()
            try:
                async for chunk in self._provider.stream(messages, self._openai_tools):
                    chunk_type = chunk.get("type")

                    if chunk_type == "text":
                        content = chunk.get("content", "")
                        if content:
                            text_parts.append(content)
                            yield content

                    elif chunk_type == "tool_call":
                        has_tool_calls = True
                        idx = chunk["index"]
                        if idx not in tool_calls_accumulated:
                            tool_calls_accumulated[idx] = {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            }
                        if chunk.get("id"):
                            tool_calls_accumulated[idx]["id"] = chunk["id"]
                        if chunk.get("name"):
                            tool_calls_accumulated[idx]["name"] = chunk["name"]
                        if chunk.get("arguments"):
                            tool_calls_accumulated[idx]["arguments"] += chunk["arguments"]
            finally:
                _span_cm.__exit__(None, None, None)

            # If no tool calls, we're done
            if not has_tool_calls:
                return

            # Execute tool calls and add results to messages
            assistant_msg: dict = {"role": "assistant", "content": "".join(text_parts) or None}
            assistant_tool_calls = []
            for idx in sorted(tool_calls_accumulated.keys()):
                tc = tool_calls_accumulated[idx]
                assistant_tool_calls.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                })
            assistant_msg["tool_calls"] = assistant_tool_calls
            messages.append(assistant_msg)

            for tc_data in assistant_tool_calls:
                tool_name = tc_data["function"]["name"]
                try:
                    arguments = json.loads(tc_data["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                result = await self._execute_tool(tool_name, arguments, call_context)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_data["id"],
                    "content": result,
                })

            # Re-submit to LLM with tool results — next iteration will
            # either produce text or more tool calls

        logger.warning("LLM loop hit max iterations (%d)", max_iterations)

    async def _execute_tool(
        self, tool_name: str, arguments: dict, call_context: dict
    ) -> str:
        """Execute a tool via ToolExecutor."""
        tool_def = self._tool_map.get(tool_name, {})
        handler = tool_def.get("handler")
        webhook_url = tool_def.get("webhook_url", "")

        if self._tool_executor is not None:
            return await self._tool_executor.execute(
                tool_name=tool_name,
                arguments=arguments,
                call_context=call_context,
                webhook_url=webhook_url,
                handler=handler,
            )

        return json.dumps({"error": f"No executor available for tool '{tool_name}'"})

    def _build_messages(
        self, history: list[dict], user_text: str
    ) -> list[dict]:
        """Build OpenAI messages array from conversation history."""
        messages: list[dict] = [
            {"role": "system", "content": self._system_prompt},
        ]
        for entry in history:
            role = entry.get("role", "user")
            text = entry.get("text", "")
            if role == "assistant":
                messages.append({"role": "assistant", "content": text})
            else:
                messages.append({"role": "user", "content": text})

        # Add the current user message
        messages.append({"role": "user", "content": user_text})
        return messages
