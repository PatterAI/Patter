"""Hermes agent-runtime LLM for Patter pipeline mode.

Thin preset over :class:`getpatter.llm.openai_compatible.OpenAICompatibleLLMProvider`
that defaults the base URL, model, env-key name, and timeout for the Hermes
agent runtime. Patter is the voice shell (carrier + STT + turn-taking + TTS);
Hermes is the brain on the line — each turn is one
``POST {base_url}/chat/completions`` against the local Hermes gateway.

Hermes is stateless and keys continuity off HEADERS, not the OpenAI ``user``
field. Patter sends ``X-Hermes-Session-Id: patter-call-<call_id>`` on every
turn so one phone call maps to one Hermes session / transcript (on by default).
For long-term memory scoping, set ``session_key`` to emit a static
``X-Hermes-Session-Key`` header (off by default). The OpenAI ``user`` field is
still sent (``patter-call-<call_id>``) as a harmless upstream-log correlation
id, but it is not what drives the session.
"""

from __future__ import annotations

import os
from typing import Callable, ClassVar

from getpatter.llm.openai_compatible import OpenAICompatibleLLMProvider
from getpatter.models import SessionContext

__all__ = ["LLM"]

# Hermes gateway default (loopback; operator-co-located deployment).
_BASE_URL = "http://127.0.0.1:8642/v1"
_DEFAULT_MODEL = "hermes-agent"

# Hermes is stateless — continuity is carried in headers.
_SESSION_USER_PREFIX = "patter-call-"
_SESSION_ID_HEADER = "X-Hermes-Session-Id"
_SESSION_ID_PREFIX = "patter-call-"
_SESSION_KEY_HEADER = "X-Hermes-Session-Key"


class LLM(OpenAICompatibleLLMProvider):
    """Hermes agent-runtime LLM provider.

    Example::

        from getpatter.llm import hermes

        llm = hermes.LLM()                         # all env-defaulted
        llm = hermes.LLM(model="hermes-7b")        # explicit model override
        llm = hermes.LLM(api_key="...", base_url="http://host:8642/v1")

    Defaults:

    * ``base_url`` → ``http://127.0.0.1:8642/v1``
    * ``model`` → ``API_SERVER_MODEL_NAME`` env if set, else ``"hermes-agent"``
    * ``api_key`` → ``api_key`` arg or ``API_SERVER_KEY`` env (may be absent for
      a keyless local Hermes)
    * ``timeout`` → ``120.0`` s (runtimes run tools / memory / skills before
      replying, so turns can take 30-90 s)
    * per-call continuity → ``X-Hermes-Session-Id: patter-call-<call_id>``
      (always sent with a call id — the primary mechanism)
    * long-term memory → ``X-Hermes-Session-Key: <session_key>`` (only sent
      when ``session_key`` / ``session_key_from`` / ``session_key_factory`` is
      configured)

    Args:
        session_key: Optional STATIC long-term memory scope. When set, every
            turn emits ``X-Hermes-Session-Key: <session_key>`` so Hermes
            namespaces persistent memory across calls. Credential-grade — never
            logged. ``None`` (default) means the header is not sent.
        session_key_from: Convenience selector for a built-in per-call key
            derivation. Set to ``"caller_hash"`` to derive the session key per
            call as ``f"patter-caller-{ctx.caller_hash}"`` (a stable,
            non-reversible hash of the caller — never the raw number), enabling
            per-caller cross-call memory. ``None`` (default) uses the static
            ``session_key`` path. Ignored when ``session_key_factory`` is given
            explicitly.
        session_key_factory: Custom callable deriving the
            ``X-Hermes-Session-Key`` value per call from a
            :class:`getpatter.models.SessionContext`. Takes precedence over both
            ``session_key`` and ``session_key_from``. A falsy return omits the
            header for that call. Credential-grade — never logged.
    """

    provider_key: ClassVar[str] = "hermes"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = _BASE_URL,
        model: str | None = None,
        timeout: float = 120.0,
        session_key: str | None = None,
        session_key_from: str | None = None,
        session_key_factory: Callable[[SessionContext], str | None] | None = None,
        **kwargs,
    ) -> None:
        resolved_model = model or os.environ.get(
            "API_SERVER_MODEL_NAME", _DEFAULT_MODEL
        )
        # ``session_key_from`` / ``session_key_factory`` resolution (the
        # "caller_hash" selector, factory precedence, validation) lives in the
        # generic OpenAICompatibleLLMProvider — this preset just forwards.
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=resolved_model,
            api_key_env="API_SERVER_KEY",
            timeout=timeout,
            session_user_prefix=_SESSION_USER_PREFIX,
            session_id_header=_SESSION_ID_HEADER,
            session_id_prefix=_SESSION_ID_PREFIX,
            session_key_header=_SESSION_KEY_HEADER,
            session_key=session_key,
            session_key_from=session_key_from,
            session_key_factory=session_key_factory,
            **kwargs,
        )
