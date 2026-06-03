"""Public dataclasses and runtime models for the Patter SDK.

These are the primary types users construct (``Agent``, ``Guardrail``,
``PipelineHooks``, ``STTConfig``, ``TTSConfig``) and observe
(``CallMetrics``, ``LatencyBreakdown``, ``CostBreakdown``, ``CallControl``).
All public configs are frozen dataclasses for immutability — see
``.claude/rules/immutability.md``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from getpatter.providers.base import AudioFilter, BackgroundAudioPlayer, VADProvider
    from getpatter.services.llm_loop import LLMProvider

logger = logging.getLogger("getpatter")

# Closed set of provider modes the SDK dispatches on. Matches the TypeScript
# string union ``'openai_realtime' | 'elevenlabs_convai' | 'pipeline'`` in
# ``types.ts``. Tightened from a free ``str`` so editors autocomplete and
# typos surface at type-check time instead of at call time.
ProviderMode = Literal["openai_realtime", "elevenlabs_convai", "pipeline"]


@dataclass(frozen=True)
class Guardrail:
    """Output guardrail — filters AI responses before TTS.

    Args:
        name: Identifier used in log messages when the guardrail fires.
        check: Optional callable ``(text: str) -> bool`` that returns ``True``
            when the response should be blocked.
        blocked_terms: Optional tuple of words/phrases; any match blocks the
            response (case-insensitive substring check).
        replacement: What the agent says instead when a response is blocked.
    """

    name: str
    check: Callable[[str], bool] | None = None
    blocked_terms: tuple[str, ...] | None = None
    replacement: str = "I'm sorry, I can't respond to that."

    def __post_init__(self) -> None:
        # Backward-compat: accept a list of terms but store an immutable
        # tuple so the frozen dataclass holds no mutable shared state.
        if self.blocked_terms is not None and not isinstance(self.blocked_terms, tuple):
            object.__setattr__(self, "blocked_terms", tuple(self.blocked_terms))


@dataclass(frozen=True)
class HookContext:
    """Context passed to pipeline hooks."""

    call_id: str
    caller: str
    callee: str
    history: tuple[dict, ...] = ()


@dataclass(frozen=True)
class PipelineHooks:
    """Pipeline hooks for intercepting data at each stage (pipeline mode only).

    Each hook receives the data and a :class:`HookContext`. Return ``None``
    to skip the downstream step (or, for ``before_llm`` / ``after_llm``,
    keep the original value). Hooks may be sync or async.

    Attributes:
        before_send_to_stt: Called with the raw PCM audio chunk before it is
            forwarded to the STT provider. Return ``None`` to drop the chunk
            (e.g., to implement custom VAD gating).
        after_transcribe: Called after STT, before LLM. Return ``None`` to skip turn.
        before_llm: Called with the messages list before the LLM call.
            Return ``None`` to keep them, or return a new list to replace
            (useful for prompt injection, message filtering, RAG augmentation).
        after_llm: Called with the final assistant text after the LLM stream
            completes. Return ``None`` to keep, or return a new string to
            replace (useful for output validation, redaction, post-processing).
        before_synthesize: Called before TTS, per-sentence in streaming mode.
            Return ``None`` to skip TTS for this sentence.
        after_synthesize: Called after TTS produces an audio chunk.
            Return ``None`` to discard the chunk.
    """

    before_send_to_stt: Callable | None = None
    after_transcribe: Callable | None = None
    before_llm: Callable | None = None
    after_llm: Callable | None = None
    before_synthesize: Callable | None = None
    after_synthesize: Callable | None = None


# --- OpenAI-compatible consult target (OpenClaw / vLLM / Ollama / Groq …) -----

# Default OpenClaw gateway base URL. The gateway binds to loopback by default and
# serves the OpenAI-compatible endpoint at ``{base}/chat/completions``.
_OPENCLAW_DEFAULT_BASE_URL = "http://127.0.0.1:18789/v1"
_OPENCLAW_API_KEY_ENV = "OPENCLAW_API_KEY"
# Explicit, deprecation-proof per-call session knob (the call id is also sent as
# the OpenAI ``user`` field as a harmless secondary).
_OPENCLAW_SESSION_HEADER = "x-openclaw-session-key"
# Consult-biased ("substantive") default description: steer the agent to ALWAYS
# consult for account-specific facts instead of answering from memory — the one
# mitigation for a local LLM confabulating a real appointment/customer record.
_OPENCLAW_DESCRIPTION = (
    "Consult your OpenClaw agent for anything account-specific — appointments, "
    "customer records, schedules, or actions in the back-office system. NEVER "
    "state an appointment time, customer detail, or schedule fact from your own "
    "memory; ALWAYS call this tool for those and read back what it returns."
)
_OPENCLAW_REASSURANCE = "Let me check on that for you, one moment."

# Agent ids are interpolated into the model string and cross into the gateway, so
# restrict to a safe set (letters, digits, and the separators OpenClaw accepts).
_OPENCLAW_AGENT_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")


def _is_loopback_or_private_host(base_url: str) -> bool:
    """True if *base_url*'s host is loopback / private / link-local.

    Auto-enables ``allow_loopback`` for the OpenClaw preset, whose default
    gateway lives on ``127.0.0.1`` (the intended co-located deployment). A public
    hostname returns ``False`` so the strict SSRF guard is preserved.
    """
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(base_url).hostname or "").lower()
    if host in ("localhost", "0.0.0.0"):  # noqa: S104 — host check, not a bind
        return True
    if host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


@dataclass(frozen=True)
class OpenAICompatibleConsult:
    """Native :class:`ConsultConfig` target that speaks an OpenAI-compatible
    ``/chat/completions`` endpoint directly — no hand-written adapter.

    Lets ``consult`` reach an OpenClaw agent (or any OpenAI-compatible gateway:
    vLLM, Ollama, Groq, …). The consult handler builds a standard chat-completions
    request (``model`` + ``messages`` + ``user``) and speaks
    ``choices[0].message.content``.

    Prefer :meth:`ConsultConfig.openclaw` for the OpenClaw preset rather than
    constructing this directly.

    Args:
        base_url: OpenAI-compatible base URL ending in ``/v1`` (the handler POSTs
            to ``{base_url}/chat/completions``), e.g.
            ``http://127.0.0.1:18789/v1``.
        model: Model / agent target. For OpenClaw this is the namespaced agent id,
            e.g. ``"openclaw/receptionist"``.
        api_key: Bearer token. Prefer ``api_key_env`` so the secret stays out of
            source. For OpenClaw this is an OPERATOR-grade credential — never
            logged.
        api_key_env: Environment variable to read the bearer from when ``api_key``
            is not given (e.g. ``"OPENCLAW_API_KEY"``).
        session_header: Optional header carrying the per-call session id (the call
            id), e.g. ``"x-openclaw-session-key"``. The call id is also sent as the
            OpenAI ``user`` field.
    """

    base_url: str
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    session_header: str | None = None

    def __post_init__(self) -> None:
        from urllib.parse import urlparse

        if not self.base_url:
            raise ValueError("OpenAICompatibleConsult requires a non-empty base_url")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("https", "http"):
            raise ValueError(
                "OpenAICompatibleConsult base_url must be http(s), got "
                f"{parsed.scheme!r}"
            )
        if not parsed.hostname:
            raise ValueError("OpenAICompatibleConsult base_url is missing a hostname")
        if not self.model:
            raise ValueError("OpenAICompatibleConsult requires a non-empty model")


@dataclass(frozen=True)
class ConsultConfig:
    """Configuration for the built-in ``consult`` escalation tool.

    When set on an :class:`Agent`, Patter auto-injects a tool (default name
    ``consult_agent``) that the in-call agent can invoke mid-call to reach the
    caller's own back-office agent over HTTP for deeper reasoning, fresh
    information, or an action beyond the call. Patter keeps STT + LLM/voice +
    TTS + carrier; the back-office agent is consulted only on demand (it is
    never on the per-turn path). The tool POSTs
    ``{"request", "call_id", "caller", "callee"}`` to :attr:`url`; the endpoint
    returns JSON with a ``reply`` / ``response`` / ``text`` string (or any JSON
    / plain text) and the agent speaks it.

    Injected in **Realtime** and **Pipeline** modes only. ElevenLabs ConvAI
    tools live on the ElevenLabs-hosted agent, so ``consult`` does not apply
    there (a warning is emitted if you set it with that provider).

    Args:
        url: HTTP(S) endpoint Patter POSTs to when the tool is invoked.
            SSRF-validated at call start (private/loopback/link-local hosts and
            non-HTTP schemes are rejected).
        headers: Optional headers sent with the POST (e.g. an ``Authorization``
            bearer for the orchestrator). Never logged.
        timeout_s: Per-consult HTTP timeout in seconds. Higher than the generic
            webhook-tool default (10 s) because a consult may run deeper
            reasoning. Default ``30.0``.
        tool_name: Name the LLM sees for the tool. Default ``"consult_agent"``.
        description: Description the LLM sees — tune to steer when the agent
            escalates.
        allow_loopback: Opt-in escape hatch for pointing ``consult`` at a
            **trusted, developer-configured local agent** (e.g. a back-office
            orchestrator on ``127.0.0.1`` or an RFC1918 private host). Default
            ``False`` keeps the strict SSRF guard (loopback / private /
            link-local hosts rejected). When ``True``, those host checks are
            relaxed for the consult URL only — non-HTTP(S) schemes are STILL
            rejected, and every other webhook path stays strict. Because the
            consult URL is SDK-user configuration (not caller input), relaxing
            it is safe; note that cloud-metadata endpoints (hostnames and the
            IMDS IP ``169.254.169.254``) also become reachable when opted in —
            only enable this for URLs you control.
        openai_compatible: Native target that speaks an OpenAI-compatible
            ``/chat/completions`` endpoint directly (e.g. an OpenClaw agent) — no
            hand-written adapter. Mutually exclusive with ``url``: set exactly one.
            Use :meth:`ConsultConfig.openclaw` for the OpenClaw preset.
        reassurance: Optional filler the agent speaks while the consult runs
            (Realtime mode only) so a multi-second back-office call is not dead
            air. ``None`` (default) plays no filler; the ``openclaw`` preset sets a
            sensible default.
    """

    url: str | None = None
    openai_compatible: "OpenAICompatibleConsult | None" = None
    headers: dict | None = None
    timeout_s: float = 30.0
    tool_name: str = "consult_agent"
    description: str = (
        "Consult your back-office agent for deeper reasoning, fresh "
        "information, or actions beyond this call. Use when the caller asks "
        "something you cannot answer directly."
    )
    reassurance: "str | dict | None" = None
    allow_loopback: bool = False

    def __post_init__(self) -> None:
        from urllib.parse import urlparse

        # Exactly one target: the generic webhook url OR the native
        # openai_compatible codec.
        if (self.url is None) == (self.openai_compatible is None):
            raise ValueError(
                "ConsultConfig requires exactly one of url or openai_compatible"
            )
        if self.url is not None:
            if not self.url:
                raise ValueError("ConsultConfig requires a non-empty url")
            parsed = urlparse(self.url)
            if parsed.scheme not in ("https", "http"):
                raise ValueError(
                    f"ConsultConfig url must be http(s), got {parsed.scheme!r}"
                )
            if not parsed.hostname:
                raise ValueError("ConsultConfig url is missing a hostname")
        if not self.tool_name:
            raise ValueError("ConsultConfig requires a non-empty tool_name")

    @classmethod
    def openclaw(
        cls,
        agent: str,
        *,
        base_url: str = _OPENCLAW_DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        tool_name: str = "consult_agent",
        description: str | None = None,
        reassurance: "str | dict | None" = _OPENCLAW_REASSURANCE,
        headers: dict | None = None,
        allow_loopback: bool | None = None,
    ) -> "ConsultConfig":
        """Consult a specific OpenClaw agent directly (no hand-written adapter).

        ``agent`` is the OpenClaw agent id (e.g. ``"receptionist"``) → targets
        ``model="openclaw/<agent>"``. An already-namespaced target
        (``"openclaw/x"``, ``"openclaw:x"``, ``"agent:x"``) is passed through.

        ``allow_loopback`` defaults to ``True`` when ``base_url`` is
        loopback/private (the intended co-located deployment), so the SSRF guard
        does not reject the local gateway. The OpenClaw gateway bearer is read
        from ``api_key`` or the ``OPENCLAW_API_KEY`` env var (operator-grade —
        never logged). Sized at the phone-safe ``timeout_s=30.0`` default; raise
        only for batch-style agents, never above 30 s on a live call.

        Requires OpenClaw's OpenAI-compatible endpoint to be enabled
        (``gateway.http.endpoints.chatCompletions.enabled = true``) and the
        gateway bound to loopback/private. Pass only a least-privileged agent id
        — selecting an agent is routing, not a security boundary.
        """
        if not agent or not _OPENCLAW_AGENT_RE.fullmatch(agent):
            raise ValueError(
                "OpenClaw agent must be a non-empty id of letters, digits, and "
                "._:/- only"
            )
        model = agent if (":" in agent or "/" in agent) else f"openclaw/{agent}"
        if allow_loopback is None:
            allow_loopback = _is_loopback_or_private_host(base_url)
        return cls(
            openai_compatible=OpenAICompatibleConsult(
                base_url=base_url,
                model=model,
                api_key=api_key,
                api_key_env=_OPENCLAW_API_KEY_ENV,
                session_header=_OPENCLAW_SESSION_HEADER,
            ),
            timeout_s=timeout_s,
            tool_name=tool_name,
            description=description or _OPENCLAW_DESCRIPTION,
            reassurance=reassurance,
            headers=headers,
            allow_loopback=allow_loopback,
        )


@dataclass(frozen=True)
class RealtimeTurnDetection:
    """OpenAI Realtime turn-detection tuning.

    Raise the VAD ``threshold`` (server_vad) or switch to ``semantic_vad`` with
    ``eagerness='low'`` to stop speakerphone / conference-room noise (mouse
    clicks, phone shifts, background chatter) from being mistaken for the
    caller speaking and cutting the agent off.

    Each unset field falls back to the adapter's current default (server_vad,
    threshold ``0.5``, prefix_padding_ms ``300``, silence_duration_ms ``300``).
    ``type='semantic_vad'`` emits ``{type, eagerness}`` only — OpenAI rejects
    ``threshold`` / ``prefix_padding_ms`` / ``silence_duration_ms`` on the
    semantic detector. ``create_response`` / ``interrupt_response`` are NOT
    exposed (Patter keeps its client-gated barge-in safety values).

    Args:
        type: ``"server_vad"`` (default) or ``"semantic_vad"``.
        threshold: server_vad only — 0..1, higher rejects more background
            noise. ``None`` keeps the adapter default (0.5).
        prefix_padding_ms: server_vad only. ``None`` keeps the default (300).
        silence_duration_ms: server_vad only. ``None`` keeps the adapter
            default.
        eagerness: semantic_vad only — ``"low"`` lets the caller finish (least
            likely to interrupt), through ``"high"`` / ``"auto"``.
    """

    type: str = "server_vad"
    threshold: float | None = None
    prefix_padding_ms: int | None = None
    silence_duration_ms: int | None = None
    eagerness: str | None = None

    def __post_init__(self) -> None:
        if self.type not in ("server_vad", "semantic_vad"):
            raise ValueError(
                "RealtimeTurnDetection.type must be 'server_vad' or "
                f"'semantic_vad', got {self.type!r}"
            )
        if self.eagerness is not None and self.eagerness not in (
            "low",
            "medium",
            "high",
            "auto",
        ):
            raise ValueError(
                "RealtimeTurnDetection.eagerness must be one of "
                f"low|medium|high|auto, got {self.eagerness!r}"
            )
        if self.eagerness is not None and self.type != "semantic_vad":
            raise ValueError(
                "RealtimeTurnDetection.eagerness is only valid when type='semantic_vad'"
            )


@dataclass(frozen=True)
class Agent:
    """Configuration for a local-mode voice AI agent.

    Several fields are also carried by engine markers
    (``engines.openai.Realtime``, ``engines.elevenlabs.ConvAI``) and by the
    server-instantiated adapters (``providers.openai_realtime.OpenAIRealtimeAdapter``,
    ``providers.elevenlabs_convai.ElevenLabsConvAIAdapter``). When the same
    setting is set in two places, precedence is:

    1. **Explicit kwarg on** ``Patter.agent(voice=..., model=..., language=...)``
       always wins.
    2. Otherwise, when an ``engine=`` is passed, the engine's value populates
       the Agent (see ``Patter._unpack_engine``).
    3. Otherwise, the Agent default is used.

    The server passes the resolved Agent down to the adapter at call time, so
    the adapter's own ``voice``/``model``/``language`` arguments mirror the
    Agent's — they are not independent overrides at runtime.
    """

    system_prompt: str
    voice: str = "alloy"
    model: str = "gpt-realtime-mini"
    language: str = "en"
    first_message: str = ""
    tools: tuple[dict, ...] | None = None
    provider: ProviderMode = "openai_realtime"
    stt: STTConfig | None = None  # which STT provider to use in pipeline mode
    tts: TTSConfig | None = None  # which TTS provider to use in pipeline mode
    variables: dict | None = (
        None  # Dynamic variables for ``{placeholder}`` substitution in system_prompt
    )
    guardrails: tuple[Guardrail | dict, ...] | None = (
        None  # Tuple of Guardrail objects or guardrail dicts
    )
    hooks: PipelineHooks | None = None  # Pipeline hooks for pipeline mode
    text_transforms: tuple[Callable, ...] | None = (
        None  # Text transforms applied to LLM output before TTS
    )
    vad: "VADProvider | None" = (
        None  # Optional server-side VAD (e.g., Silero) — pipeline mode only
    )
    audio_filter: "AudioFilter | None" = (
        None  # Optional pre-STT audio filter (noise cancel) — pipeline mode only
    )
    background_audio: "BackgroundAudioPlayer | None" = (
        None  # Optional background audio mixer — pipeline mode only
    )
    llm: "LLMProvider | None" = (
        None  # Optional built-in LLM provider for pipeline mode (e.g., AnthropicLLM())
    )
    # Model Context Protocol (MCP) servers to plug into this agent. Each
    # entry is either a ``str`` URL (shorthand) or a dict with at least
    # ``url`` and optional ``headers`` / ``name``. At call start, the SDK
    # queries each server's ``tools/list`` and merges the discovered
    # tools into ``tools`` with synthetic handlers that dispatch to
    # ``tools/call``. Requires the optional ``mcp`` package — install
    # via ``pip install getpatter[mcp]``. ``None`` (default) disables MCP.
    mcp_servers: tuple | None = None
    # Optional back-office "consult" escalation. When set, Patter auto-injects
    # a ``consult_agent`` tool (Realtime + Pipeline modes) that the in-call
    # agent can invoke to reach the caller's own orchestrator over HTTP for
    # deeper reasoning / fresh info, then speak the reply. The orchestrator
    # stays off the per-turn path — consulted only on demand. ``None`` (default)
    # disables it. See :class:`ConsultConfig`.
    consult: "ConsultConfig | None" = None
    # Minimum sustained voice (ms) before treating caller audio as a barge-in
    # and interrupting TTS. ``0`` disables barge-in entirely — useful on noisy
    # links (ngrok tunnels, speakerphone) where the agent can hear itself.
    barge_in_threshold_ms: int = 300
    # When ``True``, the sentence chunker emits the first clause of each
    # response on a soft punctuation boundary (",", em-dash, en-dash) once
    # ~40 chars have accumulated. Saves 200-500 ms TTFA on the first
    # sentence of each turn at the cost of slightly clipping prosody on the
    # very first chunk. Hard-disabled when ``language`` starts with ``"it"``
    # (Italian decimal comma would split mid-number). Default: ``False``.
    aggressive_first_flush: bool = False
    # Patter prepends a default phone-friendly preamble to ``system_prompt``
    # so the LLM responds in spoken-language style (no markdown, emojis,
    # bullet lists, code blocks; numbers and dates spelled out; replies kept
    # short). Set to ``True`` to disable and ship ``system_prompt`` verbatim.
    disable_phone_preamble: bool = False
    # When set, Patter prepends a native "# Preambles" guidance block to the
    # Realtime session ``instructions`` so the model speaks one short,
    # action-describing sentence ("I'll check that order now.") before a tool
    # call that may take a moment. Most effective on ``gpt-realtime-2`` where
    # preambles are first-class. ``False`` (default) leaves the prompt byte-
    # identical to today. ``True`` prepends the built-in block. A ``str`` is
    # used verbatim as the full block (override). Realtime modes only;
    # pipeline mode already has its own phone preamble (see services/llm_loop).
    tool_call_preambles: bool | str = False
    # Acoustic echo cancellation. When ``True`` (pipeline mode only) the
    # SDK instantiates an :class:`getpatter.audio.aec.NlmsEchoCanceller`
    # that subtracts the agent's own TTS bleed from the inbound mic
    # stream before VAD/STT see it. Strongly recommended for speakerphone
    # / tunnel deployments where the bleed otherwise keeps VAD
    # permanently in "speaking" state and barge-in only fires during
    # natural TTS pauses. Off by default — handset / headset deployments
    # don't have the bleed, and the 0.5–2 s convergence period would
    # briefly attenuate caller speech if they spoke before any TTS played.
    echo_cancellation: bool = False
    # OpenAI Realtime — reasoning-effort tier (``gpt-realtime-2`` only).
    # Threaded through from ``engines.openai.Realtime(reasoning_effort=...)``
    # so the high-level engine wrapper has the same expressivity as the
    # underlying ``OpenAIRealtimeAdapter``. ``None`` (default) leaves the
    # ``session.reasoning`` field unset and the server default applies.
    openai_realtime_reasoning_effort: str | None = None
    # OpenAI Realtime — override for ``input_audio_transcription.model``.
    # ``None`` (default) keeps the adapter default (``whisper-1``). Set to
    # e.g. ``"gpt-realtime-whisper"`` for low-latency transcript partials.
    openai_realtime_input_audio_transcription_model: str | None = None
    # OpenAI Realtime — input noise reduction for speakerphone / conference
    # audio. ``None`` (default) omits the field entirely (no reduction —
    # today's behavior). ``"far_field"`` is recommended for phone /
    # speakerphone calls; ``"near_field"`` for a handset close to the mouth.
    openai_realtime_noise_reduction: Literal["near_field", "far_field"] | None = None
    # OpenAI Realtime — turn-detection tuning (raise the VAD threshold to
    # reject speakerphone noise, or switch to ``semantic_vad`` with
    # ``eagerness='low'``). ``None`` (default) keeps the adapter's current
    # hardcoded turn_detection. See :class:`RealtimeTurnDetection`.
    realtime_turn_detection: "RealtimeTurnDetection | None" = None
    # Opt-in barge-in confirmation strategies (pipeline mode). With the
    # default empty tuple the SDK falls back to the legacy "interrupt
    # immediately on VAD speech_start" behaviour. When at least one
    # strategy is provided, a VAD speech_start during TTS marks the
    # barge-in as *pending* — the agent's TTS continues streaming
    # naturally and its in-flight LLM stream is preserved — and the
    # strategies are consulted on every STT transcript. The first strategy that returns ``True`` confirms
    # the barge-in (cancels TTS, flushes the inbound ring buffer); if
    # none confirm within ``barge_in_confirm_ms`` the pending state is
    # dropped and TTS resumes. See
    # ``getpatter.services.barge_in_strategies`` for the
    # :class:`BargeInStrategy` protocol and the
    # :class:`MinWordsStrategy` reference implementation.
    barge_in_strategies: tuple["BargeInStrategy", ...] = ()
    # Maximum time (ms) to wait for at least one strategy to confirm a
    # pending barge-in before discarding the pending state and resuming
    # TTS. Only consulted when ``barge_in_strategies`` is non-empty.
    barge_in_confirm_ms: int = 1500
    # When ``True`` (default), ``Patter.call`` warms up the STT, TTS, and LLM
    # provider connections in parallel with the carrier-side ``initiate_call``
    # request so DNS, TLS, and HTTP/2 handshakes are already complete by the
    # time the callee answers. Adapters expose ``warmup()`` returning ``None``
    # by default — providers can override to dial open a persistent connection
    # ahead of the WebSocket bridge. The window is bounded by ``ring_timeout``
    # so a never-answered call doesn't tie up provider sockets indefinitely.
    # Best-effort: warmup failures are logged at DEBUG and never abort the
    # call. See ``docs/python-sdk/latency.mdx`` for the cold-start latency
    # rationale.
    prewarm: bool = True
    # When ``True``, ``Patter.call`` pre-renders ``first_message`` to TTS
    # audio bytes during the ringing window and streams the cached buffer
    # immediately when the carrier emits ``start``. Eliminates the
    # 200-700 ms TTS first-byte latency on the greeting that dominates
    # the first-turn ``p95`` on pipeline calls.
    #
    # Dataclass default stays ``False`` to preserve backwards-compatible
    # behaviour for callers who construct ``Agent(...)`` directly without
    # going through :meth:`Patter.agent`. The recommended factory
    # :meth:`Patter.agent` flips the default to ``True`` automatically
    # when ``provider == "pipeline"`` (since 0.6.2) — parity with the
    # TypeScript ``phone.agent({...})`` factory. Opt out from the factory
    # by passing ``prewarm_first_message=False`` (e.g. for very
    # high-volume outbound where un-answered TTS spend matters); cost
    # trade-off is typically $0.001-$0.005 per ringing call depending on
    # TTS provider.
    #
    # **Pipeline mode only.** Realtime / ConvAI provider modes never
    # consume the prewarm cache (the StreamHandler for those modes runs
    # its first-message emit through the provider's own audio path), so
    # ``Patter.call`` refuses to spawn the prewarm task and emits a WARN
    # when ``provider != "pipeline"``.
    prewarm_first_message: bool = False


@dataclass(frozen=True)
class CallEvent:
    """Call lifecycle event."""

    call_id: str
    caller: str = ""
    callee: str = ""
    direction: str = ""


@dataclass(frozen=True)
class IncomingMessage:
    """Inbound user utterance forwarded to ``on_message`` handlers."""

    text: str
    call_id: str
    caller: str


@dataclass(frozen=True)
class MachineDetectionResult:
    """Normalised AMD (answering-machine detection) result emitted to the
    ``on_machine_detection`` callback once the carrier reports back.

    The ``raw`` field preserves the provider value verbatim; ``classification``
    is the carrier-agnostic projection that test/acceptance code should check.
    Mirrors ``MachineDetectionResult`` in ``libraries/typescript/src/types.ts``.
    """

    call_id: str
    carrier: str  # "twilio" | "telnyx"
    classification: str  # "human" | "machine" | "fax" | "unknown"
    raw: str
    detected_at: float  # unix epoch seconds


@dataclass(frozen=True)
class STTConfig:
    """Pipeline-mode STT provider selection (provider key + credentials + options).

    ``options`` is caller-immutable by convention — do not mutate the dict after
    passing it to the constructor; frozen only prevents attribute reassignment, not
    in-place dict mutation.
    """

    provider: str
    api_key: str
    language: str = "en"
    # Provider-specific tuning knobs (e.g. Deepgram endpointing). Unknown keys
    # are silently ignored so older SDK versions stay forward-compatible.
    options: dict | None = None

    def to_dict(self) -> dict:
        """Serialize to a plain dict for transport.

        WARNING: the returned dict contains ``api_key`` in plain text.
        Never pass the output of this method to a logger.
        """
        out = {
            "provider": self.provider,
            "api_key": self.api_key,
            "language": self.language,
        }
        if self.options:
            out["options"] = dict(self.options)
        return out


@dataclass(frozen=True)
class TTSConfig:
    """Pipeline-mode TTS provider selection (provider key + credentials + voice).

    ``options`` is caller-immutable by convention — do not mutate the dict after
    passing it to the constructor; frozen only prevents attribute reassignment, not
    in-place dict mutation.
    """

    provider: str
    api_key: str
    voice: str = "alloy"
    options: dict | None = None

    def to_dict(self) -> dict:
        """Serialize to a plain dict for transport.

        WARNING: the returned dict contains ``api_key`` in plain text.
        Never pass the output of this method to a logger.
        """
        out = {"provider": self.provider, "api_key": self.api_key, "voice": self.voice}
        if self.options:
            out["options"] = dict(self.options)
        return out


@dataclass(frozen=True)
class CostBreakdown:
    """Per-call cost breakdown by segment (USD)."""

    stt: float = 0.0
    tts: float = 0.0
    llm: float = 0.0
    telephony: float = 0.0
    total: float = 0.0
    # Amount saved on LLM cost thanks to OpenAI Realtime prompt caching.
    # ``llm`` above is the net cost AFTER this discount. Dashboards can
    # render "saved $X (pct%)" next to the LLM line when > 0.
    llm_cached_savings: float = 0.0


@dataclass(frozen=True)
class LatencyBreakdown:
    """Per-turn latency breakdown (milliseconds)."""

    # STT finalization time: end-of-speech (VAD stop or STT speech_final)
    # → final transcript delivery. This is the engineering metric — pure STT
    # processing latency, independent of how long the user spoke. Industry
    # benchmarks (Picovoice, Deepgram, Gladia, Speechmatics) all report this
    # number as "STT latency". Falls back to turn_start when the endpoint
    # signal is unavailable (degraded provider, batch STT, etc.).
    stt_ms: float = 0.0
    llm_ms: float = 0.0
    tts_ms: float = 0.0
    total_ms: float = 0.0
    # Duration of the user's utterance (turn_start → end-of-speech). Useful
    # to distinguish "user spoke for 4s" from "STT took 4s to finalize" —
    # they used to be conflated in stt_ms before 0.6.1. ``None`` when the
    # endpoint signal is unavailable.
    user_speech_duration_ms: float | None = None
    # Time-to-first-token for the LLM (stt_complete → first streaming token).
    # ``None`` in Realtime / non-streaming paths where the LLM doesn't expose
    # TTFT separately. Populated by ``CallMetricsAccumulator`` from
    # ``record_llm_first_token``.
    llm_ttft_ms: float | None = None
    # Total LLM generation time (stt_complete → llm_complete). Distinct from
    # ``llm_ms`` (TTFT-style first-token latency) — this captures the full
    # token-stream duration which is useful for cost / throughput analysis.
    llm_total_ms: float | None = None
    # Endpoint latency: time from end-of-user-speech (VAD stop or STT
    # ``speech_final``) to LLM dispatch. Captures the silence-detection +
    # transcript-finalization gap. ``None`` when the source signal is missing.
    endpoint_ms: float | None = None
    # Barge-in latency: time from user-interrupt detection to TTS playback
    # actually halting (i.e. after ``audio_sender.send_clear()`` returned).
    # ``None`` outside of an interrupted turn.
    bargein_ms: float | None = None
    # Total TTS time: LLM-first-token (or first-sentence boundary) to last
    # TTS audio byte sent. ``None`` when TTS never completed.
    tts_total_ms: float | None = None
    # **User-perceived agent response latency** — the metric to watch on
    # SLO / p95 dashboards. Computed as ``endpoint_ms + llm_ttft_ms +
    # tts_ms`` when all three signals are available, ``None`` otherwise.
    # Unlike ``total_ms`` (which includes the user's entire utterance and
    # therefore grows with how long they spoke), ``agent_response_ms``
    # isolates the system-controlled latency: silence detection + LLM TTFT
    # + TTS first byte.
    agent_response_ms: float | None = None


@dataclass(frozen=True)
class TurnMetrics:
    """Metrics for a single conversation turn."""

    turn_index: int
    user_text: str
    agent_text: str
    latency: LatencyBreakdown
    stt_audio_seconds: float = 0.0
    tts_characters: int = 0
    timestamp: float = 0.0


@dataclass(frozen=True)
class CallMetrics:
    """Accumulated metrics for an entire call."""

    call_id: str
    duration_seconds: float
    turns: tuple[TurnMetrics, ...]
    cost: CostBreakdown
    latency_avg: LatencyBreakdown
    latency_p95: LatencyBreakdown
    provider_mode: str
    stt_provider: str = ""
    tts_provider: str = ""
    llm_provider: str = ""
    telephony_provider: str = ""
    # Model identifiers per provider (e.g. "ink-whisper", "eleven_flash_v2_5",
    # "gpt-oss-120b"). Surface them on the dashboard cost breakdown so
    # operators can attribute per-call spend to a specific model without
    # cross-referencing the deployment config.
    stt_model: str = ""
    tts_model: str = ""
    llm_model: str = ""
    # Additional percentiles exposed for richer latency dashboards.
    # Default to zero so older consumers still construct CallMetrics cleanly.
    latency_p50: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    latency_p90: LatencyBreakdown = field(default_factory=LatencyBreakdown)
    latency_p99: LatencyBreakdown = field(default_factory=LatencyBreakdown)


# Carrier-agnostic terminal outcomes for an outbound call. ``answered`` means a
# human (or at least a live connection) picked up and the conversation ran;
# ``voicemail`` means AMD classified the callee as a machine; the remaining
# three come straight from the carrier status callback when the call never
# reaches the media stream. Mirrors ``CallOutcome`` in
# ``libraries/typescript/src/types.ts``.
CallOutcome = Literal["answered", "voicemail", "no_answer", "busy", "failed"]


@dataclass(frozen=True)
class CallResult:
    """Structured outcome of an outbound call placed with ``call(wait=True)``.

    Returned only when ``call(..., wait=True)`` is awaited — a fire-and-forget
    ``call()`` (the default, ``wait=False``) still returns ``None`` for
    backward compatibility. Every field is derived from a real carrier signal:
    ``answered`` / ``voicemail`` from the AMD result + media-stream end,
    ``no_answer`` / ``busy`` / ``failed`` from the carrier status callback when
    the call terminates before any media flows.

    Mirrors ``CallResult`` in ``libraries/typescript/src/types.ts`` (camelCase
    fields there, same positions).
    """

    call_id: str
    outcome: CallOutcome
    # Carrier-raw final status verbatim (e.g. "completed", "no-answer",
    # "busy", "failed"). ``outcome`` is the carrier-agnostic projection to
    # check in code; ``status`` is preserved for logging / debugging.
    status: str
    duration_seconds: float = 0.0
    transcript: tuple[dict, ...] = ()
    # Populated only when the call connected (``answered`` / ``voicemail``).
    # ``cost.total`` is the headline USD figure. ``None`` for calls that never
    # reached media (``no_answer`` / ``busy`` / ``failed``).
    cost: CostBreakdown | None = None
    metrics: CallMetrics | None = None


class CallControl:
    """In-call control interface passed to ``on_message`` handlers.

    Allows the handler to transfer the call, hang up, or send DTMF tones
    without needing direct access to the telephony provider.

    Usage::

        async def handle(data, call: CallControl):
            if needs_transfer:
                await call.transfer("+15551234567")
            elif is_done:
                await call.hangup()
            else:
                return "Hello!"
    """

    def __init__(
        self,
        call_id: str,
        caller: str,
        callee: str,
        telephony_provider: str,
        *,
        _transfer_fn=None,
        _hangup_fn=None,
        _send_dtmf_fn=None,
    ):
        self.call_id = call_id
        self.caller = caller
        self.callee = callee
        self.telephony_provider = telephony_provider
        self._transfer_fn = _transfer_fn
        self._hangup_fn = _hangup_fn
        self._send_dtmf_fn = _send_dtmf_fn
        self._transferred = asyncio.Event()
        self._hung_up = asyncio.Event()

    @property
    def is_transferred(self) -> bool:
        """True if transfer() was called."""
        return self._transferred.is_set()

    @property
    def is_hung_up(self) -> bool:
        """True if hangup() was called."""
        return self._hung_up.is_set()

    @property
    def ended(self) -> bool:
        """True if transfer() or hangup() was called."""
        return self._transferred.is_set() or self._hung_up.is_set()

    async def transfer(self, number: str) -> None:
        """Transfer the call to another phone number (E.164 format)."""
        if self._transfer_fn is not None:
            await self._transfer_fn(number)
            self._transferred.set()
        else:
            logger.warning("transfer() not available for this provider mode")

    async def hangup(self) -> None:
        """End the call."""
        if self._hangup_fn is not None:
            await self._hangup_fn()
            self._hung_up.set()
        else:
            logger.warning("hangup() not available for this provider mode")

    async def send_dtmf(self, digits: str, *, delay_ms: int = 300) -> None:
        """Send DTMF digits (for IVR navigation, e.g. "1234#").

        Args:
            digits: String of DTMF digits (0-9, *, #).
            delay_ms: Delay in milliseconds between consecutive digits.
        """
        if self._send_dtmf_fn is not None:
            await self._send_dtmf_fn(digits, delay_ms)
        else:
            logger.warning("send_dtmf() not available for this provider mode")
