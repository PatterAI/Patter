"""Generic OpenAI-compatible LLM provider for Patter's pipeline mode.

Drives *any* OpenAI-compatible ``/chat/completions`` endpoint — an agent
runtime (Hermes, OpenClaw) or a local inference gateway (Ollama, vLLM,
LM Studio). Patter owns the carrier + STT + turn-taking + TTS; this
provider turns each conversation turn into a single
``POST {base_url}/chat/completions`` request and speaks the response.

It subclasses :class:`getpatter.services.llm_loop.OpenAILLMProvider` exactly
like :mod:`getpatter.providers.groq_llm` / :mod:`getpatter.providers.cerebras_llm`:
``super().__init__()`` initialises the inherited SSE streaming loop, the
sampling kwargs, and ``_user_agent``; then ``self._client`` is replaced with
an ``AsyncOpenAI`` pointed at ``base_url`` with the long configurable timeout
the parent does not set today.

Two additions over the parent:

* **Long timeout.** Agent runtimes execute tools / memory / skills before
  replying, so a turn can take 30-90 s. The default is 60 s here (the presets
  raise it to 120 s); the base provider keeps no timeout, which is correct for
  raw inference.
* **Session continuity.** Three independent, opt-in signals let a runtime
  scope one session per phone call (and, optionally, a long-term memory
  namespace). Each is decoupled from the others:

  - ``session_user_prefix`` → emits the OpenAI ``user`` field as
    ``f"{prefix}{call_id}"`` (some gateways derive a session from ``user``).
  - ``session_id_header`` + ``session_id_prefix`` → emits a per-call header
    ``f"{session_id_prefix}{call_id}"`` for per-call session / transcript
    continuity (the mechanism stateless runtimes such as Hermes key off).
  - ``session_key_header`` + ``session_key`` → emits a *static* header for
    long-term memory scoping. The value is credential-grade and is never
    logged.

  All three are OFF by default — when none is configured the emitted request
  is byte-identical to the parent (no ``user``, no extra headers).

Keyless gateways (Ollama / vLLM / LM Studio accept no key) are supported: the
conventional ``"EMPTY"`` sentinel is passed to ``AsyncOpenAI`` (whose
constructor rejects ``None``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Callable, ClassVar

from getpatter.models import SessionContext, hash_caller
from getpatter.services.llm_loop import OpenAILLMProvider

__all__ = ["OpenAICompatibleLLMProvider", "LLM"]

logger = logging.getLogger("getpatter.llm.openai_compatible")

# AsyncOpenAI rejects ``api_key=None`` — keyless gateways (Ollama / vLLM /
# LM Studio) accept any bearer (or none), so we pass this conventional sentinel.
_EMPTY_KEY_SENTINEL = "EMPTY"


class OpenAICompatibleLLMProvider(OpenAILLMProvider):
    """LLM provider for any OpenAI-compatible ``/chat/completions`` endpoint.

    Streams in the same ``{"type": "text" | "tool_call" | "usage"}`` chunk
    format as :class:`OpenAILLMProvider`. All OpenAI-spec sampling kwargs
    accepted by the parent (``response_format``, ``parallel_tool_calls``,
    ``tool_choice``, ``seed``, ``top_p``, ``frequency_penalty``,
    ``presence_penalty``, ``stop``, ``temperature``, ``max_tokens``) are
    forwarded transparently.

    Args:
        api_key: Bearer token. If omitted and ``api_key_env`` is given, read
            from that environment variable. May resolve to ``None`` for
            keyless local gateways — the ``"EMPTY"`` sentinel is sent so the
            ``AsyncOpenAI`` constructor (which rejects ``None``) is satisfied.
        base_url: OpenAI-compatible base URL ending in ``/v1`` — the whole
            point of this provider, so it is **required**. Operator-controlled
            config, never derived from caller / transcript input.
        model: Model / agent target — **required**.
        api_key_env: Environment variable to read the bearer from when
            ``api_key`` is not given (e.g. ``"OPENCLAW_API_KEY"``).
        timeout: Per-request timeout in seconds. Default ``60.0`` (the base
            OpenAI provider sets no timeout — raised here because agent
            runtimes run tools before replying).
        extra_headers: Extra headers merged into ``default_headers`` *after*
            the ``User-Agent`` so the SDK attribution is not silently
            clobbered (a caller can still override ``User-Agent`` explicitly).
        session_user_prefix: When set, emits the OpenAI ``user`` field as
            ``f"{prefix}{call_id}"`` for per-call session continuity. ``None``
            (default) means no ``user`` field is sent.
        session_id_header: Optional header NAME carrying the per-call session
            id, e.g. ``"X-Hermes-Session-Id"`` / ``"x-openclaw-session-key"``.
            When set (and a ``call_id`` is available) the header value is
            ``f"{session_id_prefix}{call_id}"``. ``None`` (default) means off.
        session_id_prefix: Prefix for the ``session_id_header`` VALUE. Defaults
            to ``""`` (the raw call id). Independent of ``session_user_prefix``.
        session_key_header: Optional header NAME for long-term memory scope,
            e.g. ``"X-Hermes-Session-Key"``. The value is static (does not vary
            per call). ``None`` (default) means off.
        session_key: Static value emitted in ``session_key_header``. It is a
            credential-grade memory scope and is NEVER logged. ``None``
            (default) means the header is omitted even if
            ``session_key_header`` is set.
        session_key_factory: Optional callable that derives the
            ``session_key_header`` VALUE per call from a
            :class:`getpatter.models.SessionContext` (carrying ``call_id`` /
            ``caller`` / ``callee`` / ``caller_hash``). When set it takes
            PRECEDENCE over the static ``session_key``: at request-build time
            the factory is called and its return value is emitted in
            ``session_key_header``. A falsy return (``None`` / ``""``) omits the
            header for that call. The static ``session_key`` remains the simple
            fallback used when no factory is configured. The returned value is a
            credential-grade memory scope and is NEVER logged. ``None``
            (default) means the static path is used.
        session_key_from: Convenience selector for a built-in per-call key
            derivation (requires ``session_key_header``). Set to
            ``"caller_hash"`` to derive the session key per call as
            ``f"patter-caller-{ctx.caller_hash}"`` (a stable, non-reversible
            hash of the caller — never the raw number), enabling per-caller
            cross-call memory on any runtime that scopes memory by header.
            ``None`` (default) uses the static ``session_key`` path. Ignored
            when ``session_key_factory`` is given explicitly. Same semantics as
            the Hermes preset's selector.
        **kwargs: Sampling kwargs forwarded to :class:`OpenAILLMProvider`.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "openai_compatible"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str,
        model: str,
        api_key_env: str | None = None,
        timeout: float = 60.0,
        extra_headers: dict[str, str] | None = None,
        session_user_prefix: str | None = None,
        session_id_header: str | None = None,
        session_id_prefix: str = "",
        session_key_header: str | None = None,
        session_key: str | None = None,
        session_key_factory: Callable[[SessionContext], str | None] | None = None,
        session_key_from: str | None = None,
        **kwargs,
    ) -> None:
        # ``session_key_from="caller_hash"`` installs a default factory that
        # scopes durable memory per caller via the non-reversible caller hash
        # (never the raw number). An explicit ``session_key_factory`` always
        # wins over this convenience selector. Same logic as the Hermes preset.
        if session_key_factory is None and session_key_from == "caller_hash":
            session_key_factory = lambda ctx: (
                f"patter-caller-{ctx.caller_hash}" if ctx.caller_hash else None
            )
        elif session_key_from is not None and session_key_from != "caller_hash":
            raise ValueError(
                "session_key_from must be 'caller_hash' or None, "
                f"got {session_key_from!r}"
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise RuntimeError(
                "The 'openai' package is required for "
                "OpenAICompatibleLLMProvider. Install it with: pip install openai"
            ) from e

        # Resolve the bearer: explicit api_key wins, then api_key_env, else
        # None (keyless local gateway). Never logged.
        key = api_key or (os.environ.get(api_key_env) if api_key_env else None)

        # Initialise parent state (model, sampling kwargs, _user_agent) without
        # using its OpenAI-pointed client. We swap in a base_url-pointed client
        # below with the same User-Agent the parent computed plus the long
        # configurable timeout the parent does not set.
        super().__init__(api_key=key or _EMPTY_KEY_SENTINEL, model=model, **kwargs)

        default_headers = {"User-Agent": self._user_agent, **(extra_headers or {})}
        # Bound a connect that never lands (gateway down) to ~10 s while keeping
        # the long read budget for tool-running turns. A scalar timeout would
        # apply the full (120 s) ceiling to connect too, so a dead Hermes would
        # hang the first turn for the full budget instead of failing fast.
        import httpx as _httpx

        _client_timeout: Any = _httpx.Timeout(timeout, connect=min(timeout, 10.0))
        self._client: Any = AsyncOpenAI(
            api_key=key or _EMPTY_KEY_SENTINEL,
            base_url=base_url,
            timeout=_client_timeout,
            default_headers=default_headers,
        )

        self._session_user_prefix = session_user_prefix
        self._session_id_header = session_id_header
        self._session_id_prefix = session_id_prefix
        self._session_key_header = session_key_header
        # Credential-grade memory scope — never logged.
        self._session_key = session_key
        # When set, derives the session_key_header value per call (caller-hash,
        # etc.) and overrides the static session_key. Never logged.
        self._session_key_factory = session_key_factory

    async def warmup(self) -> None:
        """Pre-call DNS / TLS warmup that omits ``Authorization`` for keyless gateways.

        Overrides :meth:`OpenAILLMProvider.warmup`, which sends
        ``Authorization: Bearer {api_key}`` unconditionally — for keyless
        gateways (Ollama / vLLM / LM Studio) that becomes
        ``Bearer EMPTY``, which some gateways reject. Here the header is sent
        only when a real key is present, matching the TS provider's warmup.
        Best-effort: 5 s timeout, all exceptions swallowed at DEBUG.
        """
        try:
            base_url = str(getattr(self._client, "base_url", "") or "").rstrip("/")
            if not base_url:
                return
            import httpx

            headers: dict[str, str] = {"User-Agent": self._user_agent}
            key = getattr(self._client, "api_key", None)
            if key and key != _EMPTY_KEY_SENTINEL:
                headers["Authorization"] = f"Bearer {key}"
            async with httpx.AsyncClient(timeout=5.0) as http:
                await http.get(f"{base_url}/models", headers=headers)
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("OpenAI-compatible LLM warmup failed (best-effort): %s", exc)

    def _record_completion_cost(
        self, *, prompt_tokens: int, completion_tokens: int
    ) -> None:
        """Stamp ``patter.cost.llm_*_tokens`` with the provider key tag."""
        try:
            from getpatter.observability.attributes import record_patter_attrs

            record_patter_attrs(
                {
                    "patter.cost.llm_input_tokens": prompt_tokens,
                    "patter.cost.llm_output_tokens": completion_tokens,
                    "patter.llm.provider": self.provider_key,
                }
            )
        except Exception:  # pragma: no cover — defense in depth
            logger.debug("_record_completion_cost failed", exc_info=True)

    def _resolve_session_key(
        self,
        *,
        call_id: str | None,
        caller: str | None,
        callee: str | None,
    ) -> str | None:
        """Resolve the ``session_key_header`` VALUE for this call.

        When a ``session_key_factory`` is configured it is called with a
        :class:`SessionContext` (the raw ``caller`` plus its non-reversible
        :func:`hash_caller`) and its return value wins — a falsy return omits
        the header. Otherwise the static ``session_key`` is used. The result is
        a credential-grade memory scope and is never logged.
        """
        if self._session_key_factory is not None:
            ctx = SessionContext(
                call_id=call_id,
                caller=caller,
                callee=callee,
                caller_hash=hash_caller(caller),
            )
            return self._session_key_factory(ctx)
        return self._session_key

    def _build_completion_kwargs(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        *,
        call_id: str | None = None,
        caller: str | None = None,
        callee: str | None = None,
    ) -> dict[str, Any]:
        """Assemble ``chat.completions.create`` kwargs, adding session continuity.

        Extends the parent builder with up to three INDEPENDENT, opt-in
        session signals — the OpenAI ``user`` field, a per-call session-id
        header, and a memory-scope header (static ``session_key`` OR a per-call
        value from ``session_key_factory``). Each is gated separately, so e.g. a
        runtime can take the per-call header without the ``user`` field.
        Per-call ``user`` / session-id signals require a ``call_id``; the
        memory-scope header does not (a factory may key off the caller hash
        alone). When none applies the result is byte-identical to the parent
        (no ``user``, no ``extra_headers``).
        """
        kwargs = super()._build_completion_kwargs(messages, tools)
        extra: dict[str, str] = {}
        if self._session_user_prefix is not None and call_id:
            kwargs["user"] = f"{self._session_user_prefix}{call_id}"
        if self._session_id_header is not None and call_id:
            extra[self._session_id_header] = f"{self._session_id_prefix}{call_id}"
        if self._session_key_header is not None:
            # Truthy check (not ``is not None``): an empty-string session key is
            # not a meaningful memory scope — treat it as unset rather than
            # emitting a confusing empty header on the wire. The factory (when
            # configured) takes precedence over the static session_key.
            session_key_value = self._resolve_session_key(
                call_id=call_id, caller=caller, callee=callee
            )
            if session_key_value:
                extra[self._session_key_header] = session_key_value
        if extra:
            # Merge over any pre-existing extra_headers (the parent never sets
            # this today, but the spread keeps it future-safe and clobber-free).
            kwargs["extra_headers"] = {**kwargs.get("extra_headers", {}), **extra}
        return kwargs

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        cancel_event: asyncio.Event | None = None,
        call_id: str | None = None,
        caller: str | None = None,
        callee: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream chunks, threading per-call context into the session fields.

        Mirrors :meth:`OpenAILLMProvider.stream` but routes ``call_id`` (plus
        ``caller`` / ``callee`` when a ``session_key_factory`` needs them) into
        ``_build_completion_kwargs`` so the per-call ``user`` / session headers
        are emitted. All three are optional — unset means the parent-identical
        no-session path. ``caller`` is used only to compute the session-key
        scope (and its non-reversible hash); it is never logged here.
        """
        kwargs = self._build_completion_kwargs(
            messages, tools, call_id=call_id, caller=caller, callee=callee
        )
        # Agent runtimes run tools for tens of seconds before the first token;
        # race create()/first-byte against the cancel signal + spawn a close()
        # watchdog so a barge-in during that pre-first-token window aborts the
        # request promptly instead of blocking the next turn (see base
        # ``OpenAILLMProvider._open_stream_with_cancel`` / ``_abort_on_cancel``).
        response = await self._open_stream_with_cancel(kwargs, cancel_event)
        if response is None:
            return
        abort_watcher = (
            asyncio.ensure_future(self._abort_on_cancel(cancel_event, response))
            if cancel_event is not None
            else None
        )

        last_usage = None
        try:
            async for chunk in response:
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        await response.close()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
                    return
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
                # Mirror OpenAILLMProvider.stream exactly: prompt_tokens is the
                # TOTAL input (uncached + cached); subtract cached so input_tokens
                # is the uncached portion and cost isn't double-billed.
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
        except asyncio.CancelledError:
            raise
        except Exception:
            # close()-induced read error during a barge-in is a clean stop, not
            # an LLM error — swallow when cancelling so llm_error_message does
            # not fire. Genuine upstream errors (cancel not set) propagate.
            if cancel_event is not None and cancel_event.is_set():
                return
            raise
        finally:
            if abort_watcher is not None:
                abort_watcher.cancel()
                try:
                    await abort_watcher
                except (asyncio.CancelledError, Exception):
                    pass


class LLM(OpenAICompatibleLLMProvider):
    """Public alias of :class:`OpenAICompatibleLLMProvider` for the
    ``from getpatter.llm import openai_compatible`` namespace.

    Example::

        from getpatter.llm import openai_compatible

        # Ollama / vLLM / LM Studio (keyless local gateway):
        llm = openai_compatible.LLM(
            base_url="http://127.0.0.1:11434/v1", model="llama3.1",
        )
    """

    provider_key: ClassVar[str] = "openai_compatible"
