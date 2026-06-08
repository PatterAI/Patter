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
import inspect
import json
import logging
from dataclasses import dataclass
from typing import (
    Any,
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    ClassVar,
    Literal,
    Protocol,
    runtime_checkable,
)

from getpatter.observability.tracing import SPAN_LLM, SPAN_TOOL, start_span

logger = logging.getLogger("getpatter")


# Per-provider-TYPE memo of whether ``stream`` accepts a ``call_id`` keyword.
# Built-in providers declare ``call_id`` (or ``**kwargs``) and hit the fast
# path after the first call; a user's minimal custom provider whose ``stream``
# is ``(self, messages, tools=None, *, cancel_event=None)`` is detected once and
# called WITHOUT ``call_id`` thereafter — otherwise it would raise TypeError.
_provider_accepts_call_id: dict[type, bool] = {}


def _stream_accepts_call_id(provider: object) -> bool:
    """Whether ``provider.stream`` tolerates a ``call_id`` keyword argument.

    True when the signature declares a parameter named ``call_id`` OR accepts
    ``**kwargs`` (``VAR_KEYWORD``). Cached per provider type to keep the hot
    path cheap. Some callables (C-level, ``functools.partial`` without
    ``__wrapped__``) refuse introspection — those default to ``False`` so the
    safe no-``call_id`` path is taken rather than risking a new crash site.
    """
    provider_type = type(provider)
    cached = _provider_accepts_call_id.get(provider_type)
    if cached is not None:
        return cached
    accepts = False
    try:
        sig = inspect.signature(provider.stream)
        for param in sig.parameters.values():
            if param.name == "call_id" or param.kind is inspect.Parameter.VAR_KEYWORD:
                accepts = True
                break
    except (ValueError, TypeError):  # pragma: no cover - exotic callables
        accepts = False
    _provider_accepts_call_id[provider_type] = accepts
    return accepts


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
        Base delay for exponential backoff between attempts, in seconds.
        Actual delay = min(5.0, base * 2^attempt) + jitter (0–60 ms).
    request_timeout_s:
        Per-request timeout for webhook calls, in seconds.
    circuit_breaker:
        Optional :class:`~getpatter.tools.circuit_breaker.CircuitBreakerOptions`
        to enable per-tool circuit breaking.  When the breaker is OPEN the
        tool is rejected immediately without a network call, preventing
        repeated calls to a known-bad endpoint.  Mirrors the TypeScript
        ``DefaultToolExecutor`` which owns a ``CircuitBreakerRegistry``.
        Defaults to ``None`` (no circuit breaker) for backward compatibility.
    """

    def __init__(
        self,
        *,
        max_retries: int = _DEFAULT_TOOL_MAX_RETRIES,
        retry_delay_s: float = _DEFAULT_TOOL_RETRY_DELAY_S,
        request_timeout_s: float = _DEFAULT_TOOL_TIMEOUT_S,
        circuit_breaker=None,
    ) -> None:
        self._max_retries = max_retries
        self._retry_delay_s = retry_delay_s
        self._request_timeout_s = request_timeout_s
        if circuit_breaker is not None:
            from getpatter.tools.circuit_breaker import CircuitBreakerRegistry

            self._breaker: Any = CircuitBreakerRegistry(circuit_breaker)
        else:
            self._breaker = None

    async def execute(
        self,
        *,
        tool_name: str,
        arguments: dict,
        call_context: dict,
        webhook_url: str = "",
        handler: Any = None,
        tool_timeout_s: float | None = None,
    ) -> str:
        """Dispatch a tool call and return a JSON-stringifiable result.

        Errors are returned as JSON like
        ``{"error": "...", "fallback": True}`` rather than raised, so the
        LLM loop can surface them to the model and continue.

        ``tool_timeout_s`` is the per-tool execution timeout in seconds. When
        set it bounds BOTH the handler await and the webhook request so a long
        external-API tool (30-60s) isn't cut at the 10s default. ``None`` keeps
        the executor's ``request_timeout_s`` default. The per-tool timeout
        governs tool execution and is independent of any LLM provider's own
        ``stream()`` ceiling.
        """
        if self._breaker is not None and not self._breaker.allow(tool_name):
            retry_ms = int(self._breaker.time_until_half_open_ms(tool_name))
            return json.dumps(
                {
                    "error": f"Tool '{tool_name}' is temporarily unavailable (circuit open).",
                    "fallback": True,
                    "circuit_state": "open",
                    "retry_after_ms": retry_ms,
                }
            )

        # Clamp to a sane ceiling; ``None`` -> use the configured default.
        effective_timeout: float | None
        if tool_timeout_s is not None:
            effective_timeout = max(0.1, min(float(tool_timeout_s), 300.0))
        else:
            effective_timeout = None

        if handler is not None:
            try:
                result_or_coro = handler(arguments, call_context)
                if asyncio.iscoroutine(result_or_coro) or asyncio.isfuture(
                    result_or_coro
                ):
                    if effective_timeout is not None:
                        result = await asyncio.wait_for(
                            result_or_coro, effective_timeout
                        )
                    else:
                        result = await result_or_coro
                else:
                    result = result_or_coro
                if isinstance(result, str):
                    return result
                return json.dumps(result)
            except (asyncio.TimeoutError, TimeoutError):
                logger.error(
                    "Tool handler '%s' timed out after %ss",
                    tool_name,
                    effective_timeout,
                )
                return json.dumps(
                    {
                        "error": f"Tool '{tool_name}' timed out after {effective_timeout}s",
                        "fallback": True,
                    }
                )
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
                tool_name,
                arguments,
                call_context,
                webhook_url,
                timeout_s=effective_timeout,
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
        *,
        timeout_s: float | None = None,
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
            # Per-tool timeout overrides the executor default so a long
            # external-API tool isn't cut at the 10s default.
            client_timeout = (
                timeout_s if timeout_s is not None else self._request_timeout_s
            )
            async with httpx.AsyncClient(timeout=client_timeout) as client:
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
                            import random

                            _backoff = (
                                min(5.0, self._retry_delay_s * (2**attempt))
                                + random.random() * 0.06
                            )
                            await asyncio.sleep(_backoff)
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
        call_id: str | None = None,
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

        ``call_id`` is the stable per-call identifier (optional). Agent-runtime
        providers (Hermes / OpenClaw / any OpenAI-compatible gateway) thread it
        into the OpenAI ``user`` field so the runtime derives one session per
        phone call. Existing providers ignore it harmlessly — it is purely
        additive and OFF unless a provider opts in via ``session_user_prefix``.
        """
        ...  # pragma: no cover

    # Optional: ``async def warmup(self) -> None`` — best-effort pre-call
    # DNS / TLS / HTTP-keepalive warmup invoked by ``Patter.call`` when the
    # agent has ``prewarm=True``. Concrete providers (OpenAI, Anthropic,
    # Google, Cerebras, Groq) define this method to issue a lightweight
    # HTTPS GET to their inference endpoint so by the time the first
    # ``stream()`` call lands, the connection pool already has a warm
    # socket. Detected via duck-typed ``getattr(provider, "warmup", None)``
    # in the client so plain mocks / older providers without ``warmup``
    # still satisfy this protocol — kept off the Protocol surface to
    # preserve backward-compat with ``runtime_checkable.isinstance``.


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

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "openai"

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

    async def warmup(self) -> None:
        """Pre-call DNS / TLS / HTTP-keepalive warmup.

        Issues a lightweight ``GET <base_url>/models`` so the underlying
        ``httpx.AsyncClient`` (owned by the OpenAI SDK) opens a socket and
        completes the TLS handshake during the carrier ringing window.
        By the time the first ``chat.completions.create`` call lands, the
        connection pool has a warm socket and the first chunk arrives a
        DNS+TLS round-trip earlier (~150-400 ms saved on cold start).

        Note: an HTTPS GET warms DNS + TLS + connection pool but does NOT
        warm the inference path itself; for true inference warmup a real
        low-token request is needed, left as a follow-up. STT / TTS providers ship concrete
        WebSocket-based prewarms (Cartesia / Deepgram / AssemblyAI for
        STT; ElevenLabs WS for TTS) which save 200-500 ms each — those
        dominate the cold-start latency budget.

        Best-effort: timeouts and any other exception are swallowed at
        DEBUG. Mirrors the warmup contract documented on the
        :class:`LLMProvider` protocol.
        """
        try:
            base_url = str(getattr(self._client, "base_url", "") or "").rstrip("/")
            if not base_url:
                return
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as http:
                await http.get(
                    f"{base_url}/models",
                    headers={
                        "Authorization": f"Bearer {self._client.api_key}",
                        "User-Agent": self._user_agent,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("LLM warmup failed (best-effort): %s", exc)

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
        call_id: str | None = None,
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

        ``call_id`` (optional) is accepted for protocol parity with
        session-aware providers but ignored here — the base OpenAI provider
        emits no per-call ``user`` field. Subclasses (e.g. the
        OpenAI-compatible agent-runtime provider) override ``stream`` to thread
        it into ``_build_completion_kwargs``.
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
            completion_tokens = getattr(last_usage, "completion_tokens", 0) or 0
            self._record_completion_cost(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            yield {
                "type": "usage",
                "input_tokens": uncached_input,
                "output_tokens": completion_tokens,
                "cache_read_tokens": cache_read,
            }

    def _record_completion_cost(
        self, *, prompt_tokens: int, completion_tokens: int
    ) -> None:
        """Stamp ``patter.cost.llm_*_tokens`` on the current span.

        Subclasses (Groq, Cerebras) inherit this — the ``patter.llm.provider``
        tag is overridden in the subclass to identify the upstream vendor.
        Provider-specific subclasses with a different response shape (Anthropic,
        Google) override this directly.
        """
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.llm_input_tokens": prompt_tokens,
                    "patter.cost.llm_output_tokens": completion_tokens,
                    "patter.llm.provider": "openai",
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_completion_cost failed", exc_info=True)


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
        on_tool_call: (Callable[[str, dict, Any], Awaitable[None]] | None) = None,
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
        # Optional async callback fired after a successful tool execution.
        # When set, the StreamHandler can surface tool calls into the
        # transcript timeline / ``on_transcript`` callback so pipeline mode
        # achieves observability parity with realtime mode (which routes
        # tool calls through ``_emit_tool_event`` directly on the handler).
        self._on_tool_call: Callable[[str, dict, Any], Awaitable[None]] | None = (
            on_tool_call
        )
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

        # Diagnostics for the char/4 fallback billing path (see _run_completion).
        # Counted per-LLMLoop instance (i.e. per call). Surfaced only via logs
        # — keeps record_llm_usage's public signature unchanged.
        self._usage_missing_count = 0
        self._logged_usage_fallback = False

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

    def set_on_tool_call(
        self,
        callback: Callable[[str, dict, Any], Awaitable[None]] | None,
    ) -> None:
        """Set or replace the post-tool-execution observer.

        The callback is awaited after every successful tool execution with
        ``(tool_name, arguments, result)``. Set to ``None`` to disable.
        Mirrors the TypeScript ``setOnToolCall`` setter so callers (e.g.
        :class:`PipelineStreamHandler`) can wire the loop after
        construction without touching private fields.
        """
        self._on_tool_call = callback

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
            usage_chunk_received = False

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
            _span_exc_info: tuple = (None, None, None)
            try:
                # Only thread ``call_id`` into providers whose ``stream``
                # accepts it (or ``**kwargs``). A user's minimal custom provider
                # with ``(messages, tools=None, *, cancel_event=None)`` would
                # otherwise raise TypeError on the added keyword. ``cancel_event``
                # predates this and every Protocol implementer tolerates it.
                if _stream_accepts_call_id(self._provider):
                    stream_iter = self._provider.stream(
                        messages,
                        self._openai_tools,
                        cancel_event=cancel_event,
                        call_id=call_context.get("call_id"),
                    )
                else:
                    stream_iter = self._provider.stream(
                        messages,
                        self._openai_tools,
                        cancel_event=cancel_event,
                    )
                async for chunk in stream_iter:
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
                        usage_chunk_received = True
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
            except BaseException:
                import sys

                _span_exc_info = sys.exc_info()
                raise
            finally:
                _span_cm.__exit__(*_span_exc_info)

            # Fallback billing: some providers (Cerebras streaming has been
            # observed to do this on certain chunk-shape variants) don't
            # emit a ``usage`` chunk even with ``stream_options={"include_usage":
            # True}``. Without this fallback the LLM cost silently shows ~0
            # for the whole call. char/4 is the canonical OpenAI-tokenizer
            # rough estimate; conservative-upward is preferable to silent zero.
            if not usage_chunk_received and self._metrics is not None:
                input_chars = sum(
                    len(m.get("content", "") or "")
                    for m in messages
                    if isinstance(m, dict)
                )
                output_chars = sum(len(p) for p in text_parts)
                estimated_input = max(1, input_chars // 4)
                estimated_output = max(1, output_chars // 4)
                self._metrics.record_llm_usage(
                    provider=self._provider_name,
                    model=self._model,
                    input_tokens=estimated_input,
                    output_tokens=estimated_output,
                )
                self._usage_missing_count += 1
                # First fallback in this call → INFO so the operator sees it once.
                # Subsequent iterations only DEBUG to avoid spamming logs on
                # long tool-loop turns where every iteration is char/4-billed.
                if not self._logged_usage_fallback:
                    self._logged_usage_fallback = True
                    logger.info(
                        "llm_usage_fallback provider=%s model=%s input_chars=%d "
                        "output_chars=%d est_input_tokens=%d est_output_tokens=%d",
                        self._provider_name,
                        self._model,
                        input_chars,
                        output_chars,
                        estimated_input,
                        estimated_output,
                    )
                else:
                    logger.debug(
                        "llm_usage_fallback provider=%s model=%s iteration=%d "
                        "input_chars=%d output_chars=%d est_input_tokens=%d "
                        "est_output_tokens=%d total_missing=%d",
                        self._provider_name,
                        self._model,
                        iteration,
                        input_chars,
                        output_chars,
                        estimated_input,
                        estimated_output,
                        self._usage_missing_count,
                    )

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
                except json.JSONDecodeError as _je:
                    logger.warning(
                        "Tool '%s' returned malformed arguments JSON (falling back to {}): %s",
                        tool_name,
                        _je,
                    )
                    arguments = {}

                result = await self._execute_tool(tool_name, arguments, call_context)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_data["id"],
                        "content": result,
                    }
                )
                # Surface successful tool execution to the host SDK
                # (StreamHandler in pipeline mode) so it can emit a
                # ``role=tool`` transcript entry mirroring realtime mode.
                # Failures in the observer must NOT abort the LLM loop —
                # log and continue. ``getattr`` with default keeps
                # subclasses / test doubles that bypass ``__init__`` working.
                on_tool_call = getattr(self, "_on_tool_call", None)
                if on_tool_call is not None:
                    try:
                        await on_tool_call(tool_name, arguments, result)
                    except Exception:  # pragma: no cover - defensive
                        logger.exception(
                            "on_tool_call observer failed for tool '%s'",
                            tool_name,
                        )

            # Re-submit to LLM with tool results — next iteration will
            # either produce text or more tool calls

        logger.warning("LLM loop hit max iterations (%d)", max_iterations)

    async def _execute_tool(
        self, tool_name: str, arguments: dict, call_context: dict
    ) -> str:
        """Execute a tool via ToolExecutor.

        Forwards the tool's per-tool ``timeout_s`` (when declared) so a long
        browser-automation / external-API tool isn't cut at the 10s default.
        The per-tool timeout governs tool *execution* only — it is independent
        of any LLM provider's own ``stream()`` ceiling (Python providers honour
        ``cancel_event`` and have no separate 30s tool-stream cap on the inline
        path, so no further realignment is needed here).
        """
        tool_def = self._tool_map.get(tool_name, {})
        handler = tool_def.get("handler")
        webhook_url = tool_def.get("webhook_url", "")
        tool_timeout_s = tool_def.get("timeout_s")

        if self._tool_executor is not None:
            return await self._tool_executor.execute(
                tool_name=tool_name,
                arguments=arguments,
                call_context=call_context,
                webhook_url=webhook_url,
                handler=handler,
                tool_timeout_s=tool_timeout_s,
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
