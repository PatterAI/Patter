"""Custom LLM — point Patter's pipeline at ANY OpenAI-compatible endpoint.

The industry-standard "Custom LLM" pattern (the name the voice-AI
ecosystem uses for this concept): Patter owns the phone leg
— carrier, STT, turn-taking, barge-in, TTS — and POSTs each conversation
turn to YOUR ``/chat/completions`` endpoint. That endpoint can be:

* an **agent runtime** (Hermes, OpenClaw — prefer the dedicated presets
  :mod:`getpatter.llm.hermes` / :mod:`getpatter.llm.openclaw`, which are
  thin subclasses of this same engine with the right defaults baked in),
* a **local inference gateway** (Ollama, vLLM, LM Studio — keyless OK),
* or **your own service** that speaks the OpenAI Chat Completions protocol
  (SSE streaming, optional tool calls).

``custom.LLM`` is the canonical name for the generic engine
(:class:`getpatter.llm.openai_compatible.OpenAICompatibleLLMProvider`):
same streaming loop, same barge-in cancellation, same opt-in session
continuity (per-call ``user`` field, per-call session-id header, and a
static or factory-derived memory-scope header).
"""

from __future__ import annotations

from typing import ClassVar

from getpatter.llm.openai_compatible import OpenAICompatibleLLMProvider

__all__ = ["LLM"]


class LLM(OpenAICompatibleLLMProvider):
    """Generic "Custom LLM" provider for any OpenAI-compatible endpoint.

    Examples::

        from getpatter.llm import custom

        # Your own agent service (any OpenAI-compatible /chat/completions):
        llm = custom.LLM(
            base_url="http://127.0.0.1:9000/v1",
            model="my-agent",
            api_key_env="MY_AGENT_KEY",
            timeout=120.0,          # agent runtimes run tools before replying
        )

        # Keyless local gateway (Ollama / vLLM / LM Studio):
        llm = custom.LLM(base_url="http://127.0.0.1:11434/v1", model="llama3.1")

        # Per-call session continuity + per-caller long-term memory, on a
        # runtime that scopes sessions/memory by header:
        llm = custom.LLM(
            base_url="http://127.0.0.1:9000/v1",
            model="my-agent",
            session_id_header="X-My-Session-Id",    # value = <prefix><call_id>
            session_id_prefix="patter-call-",
            session_key_header="X-My-Memory-Key",
            session_key_from="caller_hash",          # patter-caller-<hash>
        )

    All constructor arguments are inherited from
    :class:`~getpatter.llm.openai_compatible.OpenAICompatibleLLMProvider`
    (``base_url`` and ``model`` are required). The Hermes / OpenClaw presets
    are subclasses of the same engine — use them when they exist; use this
    for everything else.
    """

    #: Stable pricing/dashboard key — read by stream-handler/metrics.
    provider_key: ClassVar[str] = "custom"
