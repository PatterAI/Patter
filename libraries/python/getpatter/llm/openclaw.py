"""OpenClaw agent-runtime LLM for Patter pipeline mode.

Thin preset over :class:`getpatter.llm.openai_compatible.OpenAICompatibleLLMProvider`
that targets a specific OpenClaw agent directly. Patter is the voice shell
(carrier + STT + turn-taking + TTS); the OpenClaw agent is the brain on the
line — each turn is one ``POST {base_url}/chat/completions`` against the local
OpenClaw gateway with ``model="openclaw/<agent>"``.

Naming and agent-target semantics are aligned byte-for-byte with the shipped
consult preset (:meth:`getpatter.models.ConsultConfig.openclaw`): same
``:18789/v1`` base URL, same ``OPENCLAW_API_KEY`` env, same agent-id charset and
``openclaw/<agent>`` namespacing rule, same ``x-openclaw-session-key`` session
header. The constants are imported from :mod:`getpatter.models` so the two paths
can never drift — an agent id valid for consult is valid here.

Unlike the consult preset (whose ``timeout_s=30`` is a phone-safe filler
default for an on-demand escalation), the LLM-provider default is ``120`` s
because here the runtime *is* the per-turn brain.
"""

from __future__ import annotations

from typing import ClassVar

from getpatter.llm.openai_compatible import OpenAICompatibleLLMProvider
from getpatter.models import (
    _OPENCLAW_AGENT_RE,
    _OPENCLAW_API_KEY_ENV,
    _OPENCLAW_DEFAULT_BASE_URL,
    _OPENCLAW_SESSION_HEADER,
)

__all__ = ["LLM"]


class LLM(OpenAICompatibleLLMProvider):
    """OpenClaw agent-runtime LLM provider.

    Example::

        from getpatter.llm import openclaw

        llm = openclaw.LLM(agent="receptionist")       # → model "openclaw/receptionist"
        llm = openclaw.LLM(agent="openclaw/custom")     # already-namespaced, passed through
        llm = openclaw.LLM(agent="receptionist", api_key="...")

    Args:
        agent: OpenClaw agent id (e.g. ``"receptionist"``) → targets
            ``model="openclaw/<agent>"``. An already-namespaced target
            (``"openclaw/x"``, ``"openclaw:x"``, ``"agent:x"``) is passed
            through unchanged. Validated against the same charset rule the
            shipped consult preset uses (``^[A-Za-z0-9._:/-]+$``).
        base_url: OpenClaw gateway base URL. Defaults to
            ``http://127.0.0.1:18789/v1``.
        api_key: OPERATOR-grade bearer (never logged). Defaults to the
            ``OPENCLAW_API_KEY`` env var.
        timeout: Per-request timeout in seconds. Default ``120.0``.

    Defaults:

    * ``session_user_prefix`` → ``"patter-call-"``
    * ``session_id_header`` → ``"x-openclaw-session-key"`` carrying the raw
      ``call_id`` (``session_id_prefix=""``). OpenClaw keys sessions off both
      the ``user`` field and this header.
    """

    provider_key: ClassVar[str] = "openclaw"

    def __init__(
        self,
        agent: str,
        *,
        base_url: str = _OPENCLAW_DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = 120.0,
        **kwargs,
    ) -> None:
        if not agent or not _OPENCLAW_AGENT_RE.fullmatch(agent):
            raise ValueError(
                "OpenClaw agent must be a non-empty id of letters, digits, and "
                "._:/- only"
            )
        model = agent if (":" in agent or "/" in agent) else f"openclaw/{agent}"
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            api_key_env=_OPENCLAW_API_KEY_ENV,
            timeout=timeout,
            session_user_prefix="patter-call-",
            session_id_header=_OPENCLAW_SESSION_HEADER,
            session_id_prefix="",
            **kwargs,
        )
