"""Built-in LLM loop for pipeline mode when no on_message handler is provided.

Uses a pluggable ``LLMProvider`` protocol so callers can supply OpenAI,
Anthropic, Gemini, or any custom provider.  The default provider is
``OpenAILLMProvider`` which preserves full backward compatibility.
"""

from __future__ import annotations

__all__ = [
    "LLMLoop",
    "LLMProvider",
    "OpenAILLMProvider",
    "LLMChunk",
    "DefaultToolExecutor",
]

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import (
    Any,
    AsyncGenerator,
    AsyncIterator,
    Literal,
    Protocol,
    runtime_checkable,
)

from getpatter.observability.tracing import SPAN_LLM, SPAN_TOOL, start_span

logger = logging.getLogger("getpatter")


# ---------------------------------------------------------------------------
# Streaming chunk type — public mirror of TypeScript ``LLMChunk``
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMChunk:
    """A single streaming chunk emitted by an :class:`LLMProvider`.

    Mirrors the TypeScript ``LLMChunk`` interface in
    ``libraries/typescript/src/llm-loop.ts``. The Python ``LLMProvider``
    historically yields plain ``dict`` chunks for backward compatibility;
    this dataclass is a typed wrapper that callers may use when they want
    type-checker support. ``LLMLoop`` itself accepts both dicts and
    :class:`LLMChunk` instances.

    Attributes
    ----------
    type:
        ``"text"`` (token), ``"tool_call"`` (partial tool invocation),
        ``"done"`` (end-of-stream sentinel), or ``"usage"`` (final token
        accounting chunk emitted by providers that expose token counts).
    content:
        Text payload — set when ``type == "text"``.
    index:
        Tool-call index (chunks with the same index concatenate).
    id:
        Tool-call id — set on the first chunk for a given index.
    name:
        Tool name — set on the first chunk for a given index.
    arguments:
        Partial JSON-encoded tool arguments — concatenated across chunks.
    input_tokens:
        Uncached prompt tokens (only set on ``"usage"`` chunks).
    output_tokens:
        Completion tokens (only set on ``"usage"`` chunks).
    cache_read_tokens:
        Cached prompt tokens read from the provider's prompt cache.
    cache_write_tokens:
        Cached prompt tokens newly written to the provider's prompt cache.
    """

    type: Literal["text", "tool_call", "done", "usage"]
    content: str | None = None
    index: int | None = None
    id: str | None = None
    name: str | None = None
    arguments: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the equivalent dict representation used by ``LLMProvider.stream``.

        Only fields that are not ``None`` are emitted, matching the shape
        produced by :class:`OpenAILLMProvider`.
        """
        out: dict[str, Any] = {"type": self.type}
        if self.content is not None:
            out["content"] = self.content
        if self.index is not None:
            out["index"] = self.index
        if self.id is not None:
            out["id"] = self.id
        if self.name is not None:
            out["name"] = self.name
        if self.arguments is not None:
            out["arguments"] = self.arguments
        if self.input_tokens is not None:
            out["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            out["output_tokens"] = self.output_tokens
        if self.cache_read_tokens is not None:
            out["cache_read_tokens"] = self.cache_read_tokens
        if self.cache_write_tokens is not None:
            out["cache_write_tokens"] = self.cache_write_tokens
        return out


# ---------------------------------------------------------------------------
# DefaultToolExecutor — public, async, retry/fallback aware
# ---------------------------------------------------------------------------


_DEFAULT_TOOL_MAX_RETRIES = 2
_DEFAULT_TOOL_RETRY_DELAY_S = 0.5
_DEFAULT_TOOL_TIMEOUT_S = 10.0
_TOOL_MAX_RESPONSE_BYTES = 1 * 1024 * 1024


class DefaultToolExecutor:
    """Default async tool executor — webhook with retry/fallback.

    Mirrors the TypeScript ``DefaultToolExecutor`` in
    ``libraries/typescript/src/llm-loop.ts``. Resolves a tool dispatch by:

    1. Calling ``tool_def["handler"]`` directly when present (sync or
       async). Handler exceptions are caught and returned as a JSON error
       payload with ``fallback=True``.
    2. Falling back to ``tool_def["webhook_url"]`` with SSRF validation,
       per-attempt timeout, retry/backoff, and a 1 MB response cap.
    3. Returning a structured JSON error when neither path is available.

    The executor exposes the same shape as :class:`getpatter.tools.tool_executor.ToolExecutor`
    so it is a drop-in replacement at the :class:`LLMLoop` boundary.

    Parameters
    ----------
    max_retries:
        Total attempts = ``max_retries + 1``. Defaults to 2 (i.e. 3 attempts).
    retry_delay_s:
        Delay between attempts, in seconds.
    request_timeout_s:
        Per-request timeout for webhook calls, in seconds.
    """

    def __init__(
        self,
        *,
        max_retries: int = _DEFAULT_TOOL_MAX_RETRIES,
        retry_delay_s: float = _DEFAULT_TOOL_RETRY_DELAY_S,
        request_timeout_s: float = _DEFAULT_TOOL_TIMEOUT_S,
    ) -> None:
        self._max_retries = max_retries
        self._retry_delay_s = retry_delay_s
        self._request_timeout_s = request_timeout_s

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: dict,
        call_context: dict,
        webhook_url: str = "",
        handler: Any = None,
    ) -> str:
        """Dispatch a tool call and return a JSON-stringifiable result.

        Errors are returned as JSON like
        ``{"error": "...", "fallback": True}`` rather than raised, so the
        LLM loop can surface them to the model and continue.
        """
        if handler is not None:
            try:
                result = handler(arguments, call_context)
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    result = await result
                if isinstance(result, str):
                    return result
                return json.dumps(result)
            except Exception as exc:  # noqa: BLE001 — surface every error
                logger.error("Tool handler '%s' raised: %s", tool_name, exc)
                return json.dumps(
                    {
                        "error": f"Tool handler error: {exc}",
                        "fallback": True,
                    }
                )

        if webhook_url:
            return await self._dispatch_webhook(
                tool_name, arguments, call_context, webhook_url
            )

        return json.dumps(
            {
                "error": f"No handler or webhook_url for tool '{tool_name}'",
                "fallback": True,
            }
        )

    async def _dispatch_webhook(
        self,
        tool_name: str,
        arguments: dict,
        call_context: dict,
        webhook_url: str,
    ) -> str:
        # Validate the URL up-front. Local import avoids a circular
        # dependency between ``services.llm_loop`` and ``server``.
        from getpatter.server import validate_webhook_url

        if not validate_webhook_url(webhook_url):
            return json.dumps(
                {
                    "error": f"Tool webhook URL rejected: {webhook_url!r}",
                    "fallback": True,
                }
            )

        import httpx  # local — keep top-level import-time light

        total_attempts = self._max_retries + 1
        with start_span(
            SPAN_TOOL,
            {
                "patter.tool.name": tool_name,
                "patter.tool.transport": "webhook",
                "patter.call.id": call_context.get("call_id", ""),
            },
        ):
            async with httpx.AsyncClient(timeout=self._request_timeout_s) as client:
                for attempt in range(total_attempts):
                    try:
                        response = await client.post(
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
                        if len(response.content) > _TOOL_MAX_RESPONSE_BYTES:
                            return json.dumps(
                                {
                                    "error": (
                                        f"Webhook response too large: "
                                        f"{len(response.content)} bytes "
                                        f"(max {_TOOL_MAX_RESPONSE_BYTES})"
                                    ),
                                    "fallback": True,
                                }
                            )
                        return json.dumps(response.json())
                    except Exception as exc:  # noqa: BLE001
                        if attempt < total_attempts - 1:
                            logger.warning(
                                "Tool webhook '%s' failed (attempt %d), retrying: %s",
                                tool_name,
                                attempt + 1,
                                exc,
                            )
                            await asyncio.sleep(self._retry_delay_s)
                        else:
                            logger.error(
                                "Tool webhook '%s' failed after %d attempts: %s",
                                tool_name,
                                total_attempts,
                                exc,
                            )
                            return json.dumps(
                                {
                                    "error": (
                                        f"Tool failed after {total_attempts} "
                                        f"attempts: {exc}"
                                    ),
                                    "fallback": True,
                                }
                            )
        # Unreachable — the loop above always returns.
        return json.dumps(
            {
                "error": f"Tool '{tool_name}' exited retry loop unexpectedly",
                "fallback": True,
            }
        )


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
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict]:
        """Yield streaming chunks for the given messages and tools.

        ``cancel_event`` is a per-turn signal that the stream handler trips
        on barge-in. Implementors should check ``cancel_event.is_set()``
        between iterations of their inner ``async for`` over the upstream
        SDK / WS / HTTP stream and break out as soon as it fires —
        otherwise a barge-in mid-fetch leaves the network call open until
        its own timeout (often 30 s) elapses, which blocks the next user
        transcript and produces the "agent stays silent after
        interruption" symptom. Optional for backward compatibility;
        providers that don't honour it are still usable but the user-facing
        interrupt-then-respond loop will be slower.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Built-in OpenAI provider
# ---------------------------------------------------------------------------


class OpenAILLMProvider:
    """LLM provider backed by OpenAI Chat Completions (streaming).

    Subclasses (Cerebras, Groq, ...) inherit the SSE streaming loop and the
    optional sampling kwargs forwarded into ``chat.completions.create``.
    Provider-specific subclasses only need to override the OpenAI client
    construction (e.g. base URL, compression layer).

    Args:
        api_key: OpenAI API key.
        model: Chat model ID (e.g. ``"gpt-4o-mini"``).
        response_format: Optional OpenAI-style ``response_format`` dict for
            JSON mode / structured outputs (e.g.
            ``{"type": "json_schema", "json_schema": {...}}``).
        parallel_tool_calls: Whether to allow the model to emit multiple
            tool calls in parallel.
        tool_choice: ``"auto" | "none" | "required"`` or a specific tool dict.
        seed: Sampling seed for reproducible outputs.
        top_p: Nucleus sampling cutoff in [0, 1].
        frequency_penalty: Penalty in [-2, 2] applied to repeated tokens.
        presence_penalty: Penalty in [-2, 2] applied to seen tokens.
        stop: Stop sequence(s) — string or list of strings.
        temperature: Sampling temperature [0, 2].
        max_tokens: Max tokens in the assistant response. Forwarded as
            ``max_completion_tokens`` on the wire (current OpenAI spec —
            ``max_tokens`` is now legacy and Cerebras/Groq mirror this).
        user_agent: Optional User-Agent header. Defaults to
            ``f"getpatter/{__version__}"`` for upstream attribution.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        response_format: dict | None = None,
        parallel_tool_calls: bool | None = None,
        tool_choice: str | dict | None = None,
        seed: int | None = None,
        top_p: float | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        stop: str | list[str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        user_agent: str | None = None,
    ) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError(
                "The 'openai' package is required for the built-in OpenAI LLM "
                "provider. Install it with: pip install openai"
            )

        # Default User-Agent identifies the SDK in upstream logs / rate-limit
        # attribution. Imported lazily to avoid an ``__init__.py`` cycle.
        if user_agent is None:
            from getpatter import __version__ as _patter_version

            user_agent = f"getpatter/{_patter_version}"

        self._client = AsyncOpenAI(
            api_key=api_key,
            default_headers={"User-Agent": user_agent},
        )
        self._model = model
        self._user_agent = user_agent
        self._response_format = response_format
        self._parallel_tool_calls = parallel_tool_calls
        self._tool_choice = tool_choice
        self._seed = seed
        self._top_p = top_p
        self._frequency_penalty = frequency_penalty
        self._presence_penalty = presence_penalty
        self._stop = stop
        self._temperature = temperature
        self._max_tokens = max_tokens

    def _build_completion_kwargs(
        self,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> dict[str, Any]:
        """Assemble the kwargs dict forwarded to ``chat.completions.create``.

        Sampling kwargs are only included when the user supplied a non-None
        value, so the upstream provider applies its own defaults otherwise.
        ``max_tokens`` is mapped to ``max_completion_tokens`` (current OpenAI
        spec; ``max_tokens`` is now legacy).
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools
        if self._response_format is not None:
            kwargs["response_format"] = self._response_format
        if self._parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = self._parallel_tool_calls
        if self._tool_choice is not None:
            kwargs["tool_choice"] = self._tool_choice
        if self._seed is not None:
            kwargs["seed"] = self._seed
        if self._top_p is not None:
            kwargs["top_p"] = self._top_p
        if self._frequency_penalty is not None:
            kwargs["frequency_penalty"] = self._frequency_penalty
        if self._presence_penalty is not None:
            kwargs["presence_penalty"] = self._presence_penalty
        if self._stop is not None:
            kwargs["stop"] = self._stop
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if self._max_tokens is not None:
            kwargs["max_completion_tokens"] = self._max_tokens
        return kwargs

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict]:
        """Yield normalised chunks from OpenAI Chat Completions.

        Emits a final ``{"type": "usage", ...}`` chunk when the upstream
        response includes a ``usage`` field (enabled via
        ``stream_options={"include_usage": True}``). Downstream callers
        use this to attribute real input/output token counts to the call
        instead of estimating from text length.

        All sampling kwargs configured on the instance (``temperature``,
        ``response_format``, ``seed``, ...) are forwarded conditionally —
        unset values are omitted so upstream defaults apply.

        ``cancel_event`` (optional, set on barge-in by the stream handler)
        is checked between upstream chunks and short-circuits the stream
        immediately so the next user transcript is not blocked behind a
        long-running fetch.
        """
        kwargs = self._build_completion_kwargs(messages, tools)
        response = await self._client.chat.completions.create(**kwargs)

        last_usage = None
        async for chunk in response:
            if cancel_event is not None and cancel_event.is_set():
                # Best-effort cancel of the upstream stream so the underlying
                # HTTP connection is freed instead of waiting for the server
                # to close. ``response.close()`` is sync on AsyncOpenAI and
                # may raise if the stream already ended — best-effort.
                try:
                    await response.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
                return
            # Usage chunks have empty ``choices`` and a populated ``usage``.
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                last_usage = usage

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

        if last_usage is not None:
            cache_read = 0
            details = getattr(last_usage, "prompt_tokens_details", None)
            if details is not None:
                cache_read = getattr(details, "cached_tokens", 0) or 0
            # OpenAI's prompt_tokens is the TOTAL input (uncached + cached).
            # Subtract cached so input_tokens represents only the uncached
            # portion and calculate_llm_cost doesn't bill cached tokens at
            # the full input rate (mirrors libraries/typescript/src/llm-loop.ts:296-305).
            prompt_tokens = getattr(last_usage, "prompt_tokens", 0) or 0
            uncached_input = max(0, prompt_tokens - cache_read)
            yield {
                "type": "usage",
                "input_tokens": uncached_input,
                "output_tokens": getattr(last_usage, "completion_tokens", 0) or 0,
                "cache_read_tokens": cache_read,
            }


# ---------------------------------------------------------------------------
# LLM loop
# ---------------------------------------------------------------------------


DEFAULT_PHONE_PREAMBLE = (
    "You are speaking on a live phone call. Respond concisely. "
    "Do not use markdown, headers, bullet lists, code fences, or emojis. "
    "Spell out numbers, currencies, dates, and units in natural spoken language. "
    "Keep replies under 2 sentences unless the caller asks for detail."
)


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
        metrics=None,
        event_bus=None,
        disable_phone_preamble: bool = False,
    ) -> None:
        if llm_provider is not None:
            self._provider = llm_provider
        else:
            self._provider = OpenAILLMProvider(api_key=openai_key, model=model)

        if disable_phone_preamble:
            self._system_prompt = system_prompt
        else:
            self._system_prompt = (
                f"{DEFAULT_PHONE_PREAMBLE}\n\n{system_prompt}"
                if system_prompt
                else DEFAULT_PHONE_PREAMBLE
            )
        self._tools = tools
        self._tool_executor = tool_executor
        self._metrics = metrics
        self._event_bus = event_bus
        self._model = model
        # Resolve the provider key for cost attribution. Prefer the
        # ``provider_key`` ClassVar declared by wrapper classes (stable,
        # matches ``pricing.py``); fall back to the legacy ``__name__``
        # strip for custom user-defined providers.
        if llm_provider is not None:
            cls = type(llm_provider)
            explicit = getattr(cls, "provider_key", None)
            if explicit:
                self._provider_name = explicit
            else:
                raw = cls.__name__.lower()
                for suffix in ("llmprovider", "provider", "llm"):
                    raw = raw.replace(suffix, "")
                self._provider_name = raw or "custom"
        else:
            self._provider_name = "openai"

        # Build OpenAI-format tool definitions (without handler/webhook_url)
        self._openai_tools: list[dict] | None = None
        if tools:
            self._openai_tools = []
            for t in tools:
                fn = {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
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
        hook_executor=None,
        hook_ctx=None,
        *,
        cancel_event: asyncio.Event | None = None,
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
            hook_executor: Optional :class:`PipelineHookExecutor` — when
                supplied, ``before_llm`` runs against the messages list
                before each provider call, and ``after_llm`` runs against
                the final assistant text once streaming completes.
            hook_ctx: Optional :class:`HookContext` — required when
                ``hook_executor`` is supplied.

        Yields:
            Text tokens as they arrive from the LLM.
        """
        messages = self._build_messages(history, user_text)
        # before_llm hook runs once on the initial message list. Subsequent
        # tool-call iterations re-submit augmented messages and skip the
        # hook (running the hook on every iteration would let a poorly
        # written hook trigger an infinite re-write loop).
        # Tier 3 (``on_response``) — and the deprecated legacy callable that
        # maps to it — buffer streaming tokens, run the hook against the
        # final assistant text, and yield the (possibly rewritten) text as
        # a single chunk. Tier 1 (``on_chunk``) and tier 2 (``on_sentence``)
        # keep streaming. Tier 1 transform is applied inline below; tier 2
        # runs in the sentence chunker / stream-handler downstream.
        has_after_llm_response = bool(
            hook_executor is not None
            and hook_ctx is not None
            and hook_executor.has_after_llm_response()
        )
        has_after_llm_chunk = bool(
            hook_executor is not None and hook_executor.has_after_llm_chunk()
        )
        if hook_executor is not None and hook_ctx is not None:
            messages = await hook_executor.run_before_llm(messages, hook_ctx)
        # Accumulate yielded text across iterations for after_llm hook.
        all_emitted_text: list[str] = []

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
                async for chunk in self._provider.stream(
                    messages, self._openai_tools, cancel_event=cancel_event
                ):
                    chunk_type = chunk.get("type")

                    if chunk_type == "text":
                        content = chunk.get("content", "")
                        if content:
                            # Tier 1 — per-token sync transform. Cheap, no buffering.
                            if has_after_llm_chunk:
                                content = hook_executor.run_after_llm_chunk(content)
                            text_parts.append(content)
                            if self._event_bus is not None:
                                self._event_bus.emit(
                                    "llm_chunk",
                                    {"text": content, "iteration": iteration},
                                )
                            if has_after_llm_response:
                                # Buffer; yield after the on_response hook runs.
                                all_emitted_text.append(content)
                            else:
                                yield content

                    elif chunk_type == "usage":
                        if self._metrics is not None:
                            self._metrics.record_llm_usage(
                                provider=self._provider_name,
                                model=self._model,
                                input_tokens=chunk.get("input_tokens", 0),
                                output_tokens=chunk.get("output_tokens", 0),
                                cache_read_tokens=chunk.get("cache_read_tokens", 0),
                                cache_write_tokens=chunk.get("cache_write_tokens", 0),
                            )

                    elif chunk_type == "tool_call":
                        has_tool_calls = True
                        idx = chunk["index"]
                        if idx not in tool_calls_accumulated:
                            tool_calls_accumulated[idx] = {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            }
                            # Emit tool_call_started the first time we see
                            # a given index. ``args`` may still be empty —
                            # streamed tool args arrive incrementally.
                            if self._event_bus is not None:
                                self._event_bus.emit(
                                    "tool_call_started",
                                    {
                                        "index": idx,
                                        "name": chunk.get("name") or "",
                                        "args": chunk.get("arguments") or "",
                                    },
                                )
                        if chunk.get("id"):
                            tool_calls_accumulated[idx]["id"] = chunk["id"]
                        if chunk.get("name"):
                            tool_calls_accumulated[idx]["name"] = chunk["name"]
                        if chunk.get("arguments"):
                            tool_calls_accumulated[idx]["arguments"] += chunk[
                                "arguments"
                            ]
            finally:
                _span_cm.__exit__(None, None, None)

            # If no tool calls, we're done
            if not has_tool_calls:
                if has_after_llm_response:
                    final_text = "".join(all_emitted_text)
                    rewritten = await hook_executor.run_after_llm_response(
                        final_text, hook_ctx
                    )
                    if rewritten:
                        yield rewritten
                return

            # Execute tool calls and add results to messages
            assistant_msg: dict = {
                "role": "assistant",
                "content": "".join(text_parts) or None,
            }
            assistant_tool_calls = []
            for idx in sorted(tool_calls_accumulated.keys()):
                tc = tool_calls_accumulated[idx]
                assistant_tool_calls.append(
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                )
            assistant_msg["tool_calls"] = assistant_tool_calls
            messages.append(assistant_msg)

            for tc_data in assistant_tool_calls:
                tool_name = tc_data["function"]["name"]
                try:
                    arguments = json.loads(tc_data["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                result = await self._execute_tool(tool_name, arguments, call_context)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_data["id"],
                        "content": result,
                    }
                )

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

    def _build_messages(self, history: list[dict], user_text: str) -> list[dict]:
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
