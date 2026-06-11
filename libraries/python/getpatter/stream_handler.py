"""Base and concrete StreamHandler classes for provider-mode-specific stream handling.

Each handler encapsulates: provider initialization, audio routing, transcript
handling, conversation history, metrics, guardrails, tool calling, and call
control for a single provider mode (openai_realtime, elevenlabs_convai, pipeline).

The telephony-specific handlers (twilio_handler, telnyx_handler) remain thin
adapters that parse WebSocket messages, transcode audio if needed, and delegate
to the appropriate StreamHandler.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from getpatter._speech_events import EouTrigger
from getpatter.models import HookContext, _invoke_transfer_fn
from getpatter.observability.tracing import (
    SPAN_BARGEIN,
    SPAN_ENDPOINT,
    SPAN_LLM,
    SPAN_STT,
    SPAN_TTS,
    start_span,
)
from getpatter.services.input_chain import InputProcessingChain
from getpatter.services.pipeline_hooks import PipelineHookExecutor
from getpatter.services.sentence_chunker import SentenceChunker
from getpatter.telephony.common import (
    _create_stt_from_config,
    _create_tts_from_config,
    _resolve_variables,
    _sanitize_variable_value,
    _validate_e164,
)
from getpatter.utils.log_sanitize import mask_phone_number, sanitize_log_value

logger = logging.getLogger("getpatter")


from getpatter.utils.ws import is_ws_alive as _is_parked_ws_alive  # noqa: E402


# Minimum wall-clock duration (seconds) the agent must have been speaking
# before barge-in is allowed to fire. AEC variant (1.0 s) covers the
# filter convergence window. NO_AEC variant raised 0.1 → 0.5 s on
# 2026-05-19 after the 0.6.2 acceptance run showed a phantom VAD
# speech_start firing on the very first inbound frame, cancelling the
# prewarmed firstMessage and leaving the turn-state machine wedged
# (``_turn_already_closed=True``). 0.5 s filters those phantoms while
# still allowing real interruptions to land within half a second of
# agent onset.
MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_AEC = 1.0
MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_NO_AEC = 0.5
# Backwards-compat alias used by tests; matches AEC variant.
MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN = MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_AEC


# ---------------------------------------------------------------------------
# Shared tool definitions injected into every agent
# ---------------------------------------------------------------------------

# Non-speech artifacts that Whisper (and, less often, Deepgram) routinely
# emit when fed silence or TTS echo on mulaw 8 kHz. Issue #154: the filter is
# now DISPLAY-ONLY — it no longer gates or cancels the model response (the GA
# session sets ``create_response: True`` so the server replies independently of
# the Whisper transcript). Because the only effect of a match is to DROP the
# user's displayed transcript line (``record_stt_complete`` never fires →
# empty ``user_text`` → the dashboard skips the user line), the set must NOT
# contain real conversational words — dropping 'yes' / 'no' / 'okay' / 'right'
# would silently delete legitimate user turns from the transcript. Keep ONLY
# unambiguous non-speech artifacts: YouTube caption credits + sign-offs,
# music / sound markers, and silence markers. Parity with TS ``HALLUCINATIONS``.
#
# Whisper was trained heavily on captioned video, so on silence / PSTN echo it
# falls back to the most common caption credits + sign-offs. Curated from
# widely-reported Whisper-on-silence outputs across the open-source ASR
# community. Comparison happens against the lower-cased + stripped form, so add
# the canonical lowercase spelling here.
_STT_HALLUCINATIONS: frozenset[str] = frozenset(
    {
        # Caption credits / sign-offs (YouTube training-set bias).
        "thank you for watching",
        "thank you for watching!",
        "thanks for watching",
        "thanks for watching!",
        "thank you so much for watching",
        "thank you for watching please subscribe",
        "thanks for watching please subscribe",
        "thanks for listening",
        "please subscribe",
        "please subscribe to my channel",
        "don't forget to subscribe",
        "like and subscribe",
        "subscribe",
        "subtitles by the amara.org community",
        "subtitles by the amara org community",
        "subtitles by",
        "transcribed by",
        "transcription by castingwords",
        "the end",
        "we'll see you next time",
        "see you next time",
        "bye bye",
        # Music / sound markers.
        "music",
        "[music]",
        "piano music",
        "applause",
        "[applause]",
        "♪",
        # Silence markers.
        "[no audio]",
        "[silence]",
        "[blank_audio]",
        "(silence)",
    }
)


# Sentence-ending characters used to split multi-closer hallucination segments
# ("We'll see you next time. Bye bye.") without importing ``re``.
_SENTENCE_ENDERS = ".!?…。！？"

# Fraction of a candidate transcript's words that must appear in the agent's
# in-flight spoken text for it to be treated as the agent's own TTS echoing
# back (rather than real caller speech). 0.6 keeps real replies that merely
# share a couple of words while catching garbled echo fragments. Language-
# agnostic — unlike the English-only ``_STT_HALLUCINATIONS`` set.
_ECHO_WORD_OVERLAP_THRESHOLD = 0.6
# Minimum word count before a candidate can be classified as echo. Real TTS
# bleed is a long, near-complete fragment of the agent's speech; a 1-3 word
# caller reply that happens to repeat the agent's offered words ("lunedì",
# "yes", "Monday at two") is a legitimate answer and must NEVER be dropped.
# Short echo blips on a no-AEC link are left to AEC / barge_in_strategies.
_ECHO_MIN_CANDIDATE_WORDS = 4


def _normalize_for_echo(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for echo comparison."""
    out = []
    for ch in text.lower():
        out.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return " ".join("".join(out).split())


def _looks_like_echo(candidate: str, agent_text: str) -> bool:
    """True when ``candidate`` looks like a fragment of ``agent_text`` — i.e. the
    agent's own TTS bleeding into STT (forwarded during TTS without effective
    AEC) rather than real caller speech. Substring match OR high word-overlap.
    """
    a = _normalize_for_echo(agent_text)
    c = _normalize_for_echo(candidate)
    if not a or not c:
        return False
    words = c.split()
    # Never classify a short reply as echo — exempts single-word / few-word
    # caller answers that legitimately repeat the agent's offered words.
    if len(words) < _ECHO_MIN_CANDIDATE_WORDS:
        return False
    if c in a:  # candidate is verbatim a long fragment of what the agent said
        return True
    agent_words = set(a.split())
    overlap = sum(1 for w in words if w in agent_words) / len(words)
    return overlap >= _ECHO_WORD_OVERLAP_THRESHOLD


def _is_near_duplicate(a: str, b: str) -> bool:
    """True when two normalised finals are the same utterance double-emitted
    (identical, or one a WORD-PREFIX of the other — Deepgram's
    ``speech_final``+``is_final`` pair) — used to drop the back-to-back pair
    WITHOUT swallowing a genuinely different utterance that merely arrives
    quickly. Word-boundary aware so a character infix ("no" in "nothing
    else") is NOT treated as a duplicate."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer.startswith(shorter + " ")


def _is_stt_hallucination(text: str) -> bool:
    """True when *text* is — or is composed entirely of — known STT
    hallucinations.

    Beyond an exact set lookup it (a) strips trailing punctuation, since Whisper
    appends it ("Thank you.", "Bye bye.") and that alone defeats an exact match,
    and (b) splits multi-closer segments ("We'll see you next time. Bye bye.")
    on sentence boundaries, dropping the turn only when EVERY piece is a known
    hallucination — so a real sentence that merely contains a filler word is
    never falsely dropped. Parity with TS ``isSttHallucination``.
    """
    stripped = (text or "").strip().lower().rstrip(".,!?;:…。！？ ").strip()
    if not stripped:
        return True
    if stripped in _STT_HALLUCINATIONS:
        return True
    pieces = stripped
    for ch in _SENTENCE_ENDERS:
        pieces = pieces.replace(ch, "\n")
    parts = [p.strip() for p in pieces.split("\n") if p.strip()]
    return len(parts) > 1 and all(p in _STT_HALLUCINATIONS for p in parts)


def _ends_with_sentence_final_punct(text: str) -> bool:
    """True when *text* (whitespace-stripped) ends with sentence-final
    punctuation — the fast-path confidence signal for preemptive generation.
    Parity with TS ``endsWithSentenceFinalPunct``."""
    stripped = (text or "").rstrip()
    return bool(stripped) and stripped[-1] in _SENTENCE_ENDERS


def _speculation_matches(interim: str, final: str) -> bool:
    """Whether a committed FINAL transcript matches the INTERIM a speculative
    turn was generated from, i.e. the speculation can be released.

    Both sides are normalized via :func:`_normalize_for_echo` (lowercase,
    punctuation stripped, whitespace collapsed), so a final that merely adds
    trailing punctuation / capitalization to the interim still matches.
    Parity with TS ``speculationMatches``.
    """
    a = _normalize_for_echo(interim)
    b = _normalize_for_echo(final)
    return bool(a) and a == b


#: Hard cap (seconds of playout) on TTS audio buffered by a speculative turn.
#: Overflow aborts the speculation — an unbounded buffer on a long LLM reply
#: would hold tens of MB per call. ~15 s covers any reasonable spoken reply
#: prefix while the user finishes their utterance. Parity with TS
#: ``PREEMPTIVE_MAX_BUFFER_MS``.
_PREEMPTIVE_MAX_BUFFER_S = 15.0


class _SpeculativeTurn:
    """In-flight PREEMPTIVE GENERATION state for one speculated user turn.

    Created by ``PipelineStreamHandler._start_speculation`` on a confident
    interim transcript. The owning task runs the LLM + sentence-chunked TTS
    but HOLDS all audio in ``buffered`` until the final transcript commits:

    * release (final matches): ``released=True`` + ``release_event`` set —
      the task flushes ``buffered`` to the carrier and continues live; it IS
      the real turn from then on (history/metrics recorded by the releaser
      and the task).
    * discard (mismatch / barge-in / replaced / overflow / teardown):
      ``cancel_event`` set — the task unwinds without ever touching the
      carrier, conversation history, or per-turn metrics.

    Mirrors TS ``SpeculativeTurn``.
    """

    __slots__ = (
        "interim_text",
        "norm_text",
        "cancel_event",
        "release_event",
        "released",
        "flushed",
        "failed",
        "interrupted",
        "final_text",
        "buffered",
        "buffered_bytes",
        "response_parts",
        "first_tts_chunk",
        "llm_first_token_recorded",
        "task",
    )

    def __init__(self, interim_text: str) -> None:
        self.interim_text = interim_text
        self.norm_text = _normalize_for_echo(interim_text)
        # Per-speculation LLM cancel signal (same machinery the live path
        # hands to ``LLMLoop.run``). On release this becomes the handler's
        # ``_llm_cancel_event`` so the existing barge-in cancel paths reach
        # the speculative stream.
        self.cancel_event: asyncio.Event = asyncio.Event()
        # Set once the commit-time decision is known (either way); the task
        # parks on it when generation finishes before the final commits.
        self.release_event: asyncio.Event = asyncio.Event()
        self.released = False
        # True once buffered audio has been flushed to the carrier (release).
        self.flushed = False
        # True when the speculation can no longer be released (LLM error,
        # buffer overflow, internal failure) — the commit path must dispatch
        # normally.
        self.failed = False
        # Barge-in after release cut the live continuation short.
        self.interrupted = False
        # Stamped at release with the committed final transcript.
        self.final_text = ""
        # Per-sentence audio held until release: (sentence_text, chunks).
        # The chunks list is registered BEFORE synthesis so a mid-sentence
        # release flushes the partial sentence too.
        self.buffered: list[tuple[str, list[bytes]]] = []
        self.buffered_bytes = 0
        self.response_parts: list[str] = []
        # Same single-element-list flag shape ``_synthesize_sentence`` uses,
        # shared across the buffered flush and the live continuation so the
        # per-turn first-byte metric stays idempotent.
        self.first_tts_chunk: list[bool] = [True]
        self.llm_first_token_recorded = False
        self.task: asyncio.Task | None = None


def _summarize_realtime_error(ev_data: Any) -> str:
    """Build a PII-free one-line summary of a Realtime ``error`` event.

    Surfaces only the error ``type`` / ``code`` / ``message`` fields — never
    audio or transcript bodies. Parity with TS ``onAdapterError``. Accepts
    either a flat error dict (``{"type", "code", "message"}``) or a wrapper
    (``{"error": {...}}``) and degrades gracefully on unexpected shapes.
    """
    err: Any = ev_data
    if isinstance(ev_data, dict) and isinstance(ev_data.get("error"), dict):
        err = ev_data["error"]
    if isinstance(err, dict):
        err_type = err.get("type")
        err_code = err.get("code")
        err_message = err.get("message")
        return (
            f"type={err_type} code={err_code} message={sanitize_log_value(err_message)}"
        )
    return f"message={sanitize_log_value(err)}"


TRANSFER_CALL_TOOL: dict = {
    "name": "transfer_call",
    "description": "Transfer the call to a human agent at the specified phone number",
    "parameters": {
        "type": "object",
        "properties": {
            "number": {
                "type": "string",
                "description": "Phone number to transfer to (E.164 format)",
            },
            "mode": {
                "type": "string",
                "enum": ["cold", "warm"],
                "description": (
                    "Transfer mode. 'cold' (default) redirects the caller "
                    "immediately. 'warm' puts the caller on hold music, dials "
                    "the human agent, announces the summary to them, then "
                    "bridges everyone together."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "Warm mode only — one or two sentences announced to the "
                    "human agent before the caller is bridged (who is calling "
                    "and what they need)."
                ),
            },
        },
        "required": ["number"],
    },
}

# Valid values for the ``mode`` argument of the built-in ``transfer_call``
# tool. Anything else is rejected with an error envelope (never silently
# coerced) so a hallucinated mode cannot trigger an unintended blind redirect.
_TRANSFER_MODES = ("cold", "warm")

#: Name of the built-in multi-agent handoff tool injected when
#: ``Agent.handoffs`` is configured.
HANDOFF_TOOL_NAME = "handoff_to"


def build_handoff_tool(handoff_names) -> dict:
    """Build the ``handoff_to`` tool schema for the given target-agent names.

    The names are surfaced both as a JSON-schema ``enum`` (so the model can
    only pick a configured target) and in the description. Sorted for a
    deterministic schema. Parity with TS ``buildHandoffTool``.
    """
    names = sorted(str(n) for n in handoff_names)
    return {
        "name": HANDOFF_TOOL_NAME,
        "description": (
            "Hand the conversation off to another specialized agent. The call "
            "continues seamlessly with the new agent's instructions and tools. "
            "Available agents: " + ", ".join(names)
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": names,
                    "description": "Name of the agent to hand the conversation to",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason for the handoff",
                },
            },
            "required": ["name"],
        },
    }


def _apply_handoff_target(current, target):
    """Return a copy of ``current`` with the LLM-visible configuration of the
    handoff ``target`` applied.

    Only conversational config swaps: ``system_prompt``, ``tools``,
    ``variables``, ``guardrails``, ``text_transforms``, ``consult``,
    ``handoffs`` (so chained handoffs follow the target's own map),
    ``disable_phone_preamble`` and ``tool_call_preambles``. Live audio
    infrastructure established at call start — STT/TTS/VAD instances, engine
    connection, carrier codec settings, and therefore the voice on engines
    that cannot switch voice mid-session — is intentionally retained from
    ``current``. ``Agent`` is a frozen dataclass, so a new instance is
    returned via :func:`dataclasses.replace`.
    """
    import dataclasses

    return dataclasses.replace(
        current,
        system_prompt=target.system_prompt,
        tools=target.tools,
        variables=target.variables,
        guardrails=target.guardrails,
        text_transforms=target.text_transforms,
        consult=target.consult,
        handoffs=target.handoffs,
        disable_phone_preamble=target.disable_phone_preamble,
        tool_call_preambles=target.tool_call_preambles,
    )


def _handoff_history_text(name: str, reason: str) -> str:
    """Render the system-style transcript line recording a handoff."""
    text = f"[handoff] Conversation handed to agent '{name}'"
    if reason:
        text += f" — {reason}"
    return text

END_CALL_TOOL: dict = {
    "name": "end_call",
    "description": "End the current phone call. Use when the conversation is complete or the user says goodbye.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Reason for ending the call (e.g., 'conversation_complete', 'user_requested', 'no_response')",
            }
        },
    },
}


def _augment_with_builtin_handoff_tools(
    user_tools: list[dict] | None,
    *,
    transfer_fn: Any | None,
    hangup_fn: Any | None,
) -> list[dict]:
    """Return ``user_tools`` with the built-in ``transfer_call`` and
    ``end_call`` tools appended, each wired with a handler closure that
    routes to the telephony-level ``_transfer_fn`` / ``_hangup_fn``
    already attached to the stream handler.

    Used by pipeline mode to match the realtime path's tool surface
    (see ``OpenAIRealtimeStreamHandler.start`` where the same two
    built-ins are injected into ``session.update``). Without this the
    pipeline LLM never sees the built-in tools and cannot initiate a
    transfer or hangup regardless of system-prompt instructions.

    Tools are appended (not prepended) so user-provided tools keep their
    original order. The handler signature ``(arguments, call_context)``
    matches the calling convention used by ``ToolExecutor._invoke_handler``.
    """
    out: list[dict] = list(user_tools or [])
    if transfer_fn is not None:

        async def _transfer_handler(arguments: dict, call_context: dict) -> str:
            args = arguments or {}
            number = args.get("number", "")
            mode = args.get("mode") or "cold"
            summary = args.get("summary") or ""
            if mode not in _TRANSFER_MODES:
                return json.dumps(
                    {
                        "error": f"Invalid transfer mode {mode!r} — use 'cold' or 'warm'",
                        "status": "rejected",
                    }
                )
            # Validate BEFORE attempting the transfer (both modes): the
            # carrier helpers silently no-op on a non-E.164 target, so the
            # old path told the LLM "Transferring to 555-1234" while nothing
            # happened and the call sat in limbo. Mirrors the realtime
            # path's rejection envelope and the TS pipeline.
            if not _validate_e164(number):
                logger.warning(
                    "transfer_call rejected: invalid number %s",
                    mask_phone_number(number),
                )
                return json.dumps(
                    {"error": "Invalid phone number format", "status": "rejected"}
                )
            if mode == "warm":
                outcome = await _invoke_transfer_fn(
                    transfer_fn, number, mode="warm", summary=summary
                )
                if isinstance(outcome, dict):
                    return json.dumps(outcome)
                return json.dumps(
                    {"status": "transferring", "mode": "warm", "to": number}
                )
            # Cold mode: byte-identical to the historical behaviour.
            await transfer_fn(number)
            return f"Transferring to {number}"

        out.append({**TRANSFER_CALL_TOOL, "handler": _transfer_handler})
    if hangup_fn is not None:

        async def _hangup_handler(arguments: dict, call_context: dict) -> str:
            await hangup_fn()
            return "Call ended"

        out.append({**END_CALL_TOOL, "handler": _hangup_handler})
    return out


def _inject_consult_tool(agent):
    """Return *agent* with the built-in ``consult`` tool merged into its tool
    list when ``agent.consult`` is set; otherwise return *agent* unchanged.

    Mirrors :meth:`_init_mcp_tools` — ``Agent`` is frozen, so a copy with the
    merged tools is returned via :func:`dataclasses.replace`. Called from both
    the Realtime and Pipeline start paths so the consult tool's schema reaches
    the model and its handler reaches the ``ToolExecutor`` uniformly. Idempotent:
    a no-op if a tool with the same name is already present.
    """
    consult = getattr(agent, "consult", None)
    if consult is None:
        return agent
    from getpatter.tools.consult import build_consult_tool

    consult_tool = build_consult_tool(consult)
    existing = list(agent.tools or [])
    if any(
        isinstance(t, dict) and t.get("name") == consult_tool["name"] for t in existing
    ):
        return agent
    import dataclasses

    return dataclasses.replace(agent, tools=tuple(existing) + (consult_tool,))


# ---------------------------------------------------------------------------
# Audio sender protocol — abstracts Twilio vs Telnyx audio output
# ---------------------------------------------------------------------------


class AudioSender(ABC):
    """Protocol for sending audio back to a telephony WebSocket."""

    @abstractmethod
    async def send_audio(self, pcm_audio: bytes) -> None:
        """Send PCM 16 kHz audio to the telephony provider.

        The sender is responsible for any transcoding (e.g. mulaw for Twilio).
        """

    @abstractmethod
    async def send_clear(self) -> None:
        """Clear/stop any currently playing audio."""

    @abstractmethod
    async def send_mark(self, mark_name: str) -> None:
        """Send a playback mark (Twilio-specific; no-op on Telnyx)."""

    def reset_pcm_carry(self) -> None:
        """Drop any buffered odd byte from the PCM16 alignment carry.

        Call at the start/end of a TTS synthesis block so a crash or
        cancellation mid-sentence never bleeds a partial sample into the
        next sentence. Default is a no-op; subclasses that keep a carry
        buffer (e.g. ``TwilioAudioSender``) override this. Matches TS
        parity where ``ttsByteCarry = null`` is reset at every synth
        boundary.
        """
        return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def resolve_agent_prompt(agent, custom_params: dict | None = None) -> str:
    """Resolve dynamic variables in the agent's system prompt."""
    resolved = agent.system_prompt
    agent_variables: dict = getattr(agent, "variables", None) or {}
    all_variables = {**agent_variables}
    if custom_params:
        for k, v in custom_params.items():
            all_variables[k] = _sanitize_variable_value(v)
    if all_variables:
        resolved = _resolve_variables(resolved, all_variables)
    return resolved


# Native "# Preambles" guidance block prepended to the Realtime session
# ``instructions`` when ``Agent.tool_call_preambles is True``. Steers the
# reasoning model to speak one short, action-describing sentence in its own
# voice immediately before a slow tool call. This is OpenAI's recommended,
# first-class answer (most effective on ``gpt-realtime-2``) to the "let me
# check while a slow tool runs" UX — no API field, no client timer.
#
# This literal MUST stay byte-identical to the TypeScript
# ``DEFAULT_TOOL_CALL_PREAMBLE_BLOCK`` in ``stream-handler.ts`` (cross-SDK
# parity). No trailing newline beyond the block content.
DEFAULT_TOOL_CALL_PREAMBLE_BLOCK = """# Preambles

Use short preambles only when they help the user understand that work is happening. A preamble is one short spoken update describing the action you are about to take — not hidden reasoning, and never a claim about the result.

## When to use a preamble
Use a preamble when:
- you are about to call a tool that may take noticeable time;
- you need to reason through a multi-step request;
- you are checking records, availability, account state, or policy details;
- you are preparing an escalation or handoff;
- silence would make the assistant feel unresponsive.

When a preamble is needed, output it immediately before the reasoning or tool call.

## When to NOT use a preamble
Do not use a preamble when:
- the answer is direct and can be given immediately;
- the user is only confirming, correcting, or declining something;
- the audio is unclear and you need clarification instead;
- the tool call is lightweight and the user would not benefit from an update.

## Style
- Keep it to one short sentence (two only before a high-impact action).
- Vary the wording across turns; do not reuse the same opener.
- Describe the action, not the internal reasoning.
- Never imply success or failure before the tool returns.

Prefer:
- "I'll check that order now."
- "I'll look up your appointment details."
- "I'll verify that before we make any changes."
- "I'll check the policy and then give you the next step."
- "I'll pull that up so we can make sure it's the right account."

Avoid:
- "Let me think about that for a second."
- "Please wait while I process your request."
- "I'm going to use my tools now."
- "Hmm..." / "One moment while I process that...\""""


def apply_tool_call_preambles(prompt: str, knob: bool | str) -> str:
    """Prepend the Preambles guidance block to a Realtime system prompt.

    ``False`` returns ``prompt`` unchanged (byte-identical — same object).
    ``True`` prepends the built-in :data:`DEFAULT_TOOL_CALL_PREAMBLE_BLOCK`.
    A ``str`` is prepended verbatim (full override). Realtime modes only —
    pipeline mode has its own phone preamble.
    """
    if not knob:
        return prompt
    block = knob if isinstance(knob, str) else DEFAULT_TOOL_CALL_PREAMBLE_BLOCK
    return f"{block}\n\n{prompt}" if prompt else block


def apply_call_overrides(agent, overrides: dict):
    """Return a new Agent with per-call config overrides applied."""
    from getpatter.models import (
        STTConfig as _STTCfg,
    )
    from getpatter.models import (
        TTSConfig as _TTSCfg,
    )

    fields: dict = {}
    for k in (
        "system_prompt",
        "voice",
        "model",
        "language",
        "first_message",
        "provider",
    ):
        if k in overrides:
            fields[k] = overrides[k]
    if "stt_config" in overrides and isinstance(overrides["stt_config"], dict):
        fields["stt"] = _STTCfg(**overrides["stt_config"])
    if "tts_config" in overrides and isinstance(overrides["tts_config"], dict):
        fields["tts"] = _TTSCfg(**overrides["tts_config"])
    if "tools" in overrides:
        fields["tools"] = overrides["tools"]
    if "variables" in overrides:
        fields["variables"] = overrides["variables"]
    if fields:
        # ``dataclasses.replace`` — NOT an asdict() round-trip: asdict
        # recursively converted nested configs (STTConfig/TTSConfig/
        # PipelineHooks) into plain dicts and deep-copied live provider/VAD
        # objects holding sockets and ONNX sessions, so ANY per-call
        # override crashed the call later with AttributeError on the
        # dict-ified config.
        from dataclasses import replace as _dc_replace

        agent = _dc_replace(agent, **fields)
        logger.debug("Per-call config overrides applied: %s", list(fields.keys()))
    return agent


def create_metrics_accumulator(
    call_id: str,
    provider: str,
    telephony_provider: str,
    agent,
    deepgram_key: str,
    elevenlabs_key: str,
    pricing: dict | None,
    report_only_initial_ttfb: bool = False,
):
    """Create and return a CallMetricsAccumulator for the call."""
    from getpatter.services.metrics import CallMetricsAccumulator

    stt_name = ""
    tts_name = ""
    stt_model = ""
    tts_model = ""
    realtime_model = ""
    if provider == "pipeline":
        # Prefer the explicit ``provider_key`` ClassVar declared by
        # wrapper classes (stable, matches ``pricing.py`` keys); fall
        # back to the legacy ``provider`` instance attribute.
        if agent.stt is not None:
            stt_name = getattr(type(agent.stt), "provider_key", None) or getattr(
                agent.stt, "provider", ""
            )
            # Adapter ``model`` attribute powers per-model rate resolution
            # in pricing.calculate_stt_cost. Empty string → provider default.
            stt_model = str(getattr(agent.stt, "model", "") or "")
        else:
            stt_name = "deepgram" if deepgram_key else ""
        if agent.tts is not None:
            tts_name = getattr(type(agent.tts), "provider_key", None) or getattr(
                agent.tts, "provider", ""
            )
            tts_model = str(getattr(agent.tts, "model", "") or "")
        else:
            tts_name = "elevenlabs" if elevenlabs_key else ""
    elif provider in ("openai_realtime", "openai_realtime_2"):
        stt_name = "openai"
        tts_name = "openai"
        # Realtime collapses STT+LLM+TTS into one model — capture it so the
        # token-based cost calc picks the right per-model rate (e.g. gpt-
        # realtime-2 vs gpt-realtime-mini). Use the agent's declared model
        # when set; fall back to the adapter default.
        realtime_model = str(getattr(agent, "model", "") or "") or "gpt-realtime-mini"
    elif provider == "elevenlabs_convai":
        stt_name = "elevenlabs"
        tts_name = "elevenlabs"
    if provider in ("openai_realtime", "openai_realtime_2"):
        llm_name = "openai"
    elif provider == "elevenlabs_convai":
        llm_name = "elevenlabs"
    else:
        # Resolve the provider key. Prefer the ``provider_key`` ClassVar
        # declared by wrapper classes (stable, matches ``pricing.py``);
        # fall back to the legacy ``__name__`` strip for custom adapters.
        _agent_llm = getattr(agent, "llm", None)
        if _agent_llm is not None:
            _cls = type(_agent_llm)
            _explicit = getattr(_cls, "provider_key", None)
            if _explicit:
                llm_name = _explicit
            else:
                _raw = _cls.__name__.lower()
                for _suffix in ("llmprovider", "provider", "llm"):
                    _raw = _raw.replace(_suffix, "")
                llm_name = _raw or "custom"
        else:
            llm_name = "custom"
    return CallMetricsAccumulator(
        call_id=call_id,
        provider_mode=provider,
        telephony_provider=telephony_provider,
        stt_provider=stt_name,
        tts_provider=tts_name,
        llm_provider=llm_name,
        pricing=pricing,
        report_only_initial_ttfb=report_only_initial_ttfb,
        stt_model=stt_model,
        tts_model=tts_model,
        realtime_model=realtime_model,
    )


def evaluate_guardrails(agent, response_text: str) -> tuple[bool, str]:
    """Evaluate output guardrails against response text.

    Returns (blocked, guard_name). If blocked is True, the response should
    be suppressed.
    """
    guardrails = getattr(agent, "guardrails", None) or []
    for guard in guardrails:
        blocked = False
        blocked_terms = (
            guard.get("blocked_terms")
            if isinstance(guard, dict)
            else getattr(guard, "blocked_terms", None)
        )
        check_fn = (
            guard.get("check")
            if isinstance(guard, dict)
            else getattr(guard, "check", None)
        )
        guard_name = (
            guard.get("name")
            if isinstance(guard, dict)
            else getattr(guard, "name", "unnamed")
        )
        if blocked_terms:
            blocked = any(
                term.lower() in response_text.lower() for term in blocked_terms
            )
        if check_fn and not blocked:
            try:
                blocked = bool(check_fn(response_text))
            except Exception as exc:
                logger.warning("Guardrail '%s' check error: %s", guard_name, exc)
        if blocked:
            logger.warning(
                "Guardrail '%s' triggered on: %.50s", guard_name, response_text
            )
            return True, guard_name
    return False, ""


def get_guardrail_replacement(agent, guard_name: str) -> str:
    """Get the replacement text for a triggered guardrail by name.

    Returns the replacement text from the specific guard that fired,
    falling back to a default message.
    """
    guardrails = getattr(agent, "guardrails", None) or []
    for guard in guardrails:
        name = (
            guard.get("name")
            if isinstance(guard, dict)
            else getattr(guard, "name", "unnamed")
        )
        if name == guard_name:
            r = (
                guard.get("replacement")
                if isinstance(guard, dict)
                else getattr(guard, "replacement", None)
            )
            if r:
                return r
    return "I'm sorry, I can't respond to that."


async def _safe_close_parked_handle(handle: object) -> None:
    """Best-effort async close of a parked provider handle that the
    StreamHandler chose NOT to adopt (cache miss, parked WS already
    dead, unknown shape, etc.).

    Handles all flavours used by the SDK:
    - tuple ``(session, ws)`` from Cartesia STT.
    - object with ``.ws`` attribute (e.g. ``ElevenLabsParkedWS``).
    - bare WebSocket / ``WebSocketClientProtocol``.
    """
    try:
        if isinstance(handle, tuple) and len(handle) == 2:
            session, ws = handle
            try:
                await ws.close()
            except Exception:
                pass
            try:
                await session.close()
            except Exception:
                pass
            return
        ws = getattr(handle, "ws", None)
        if ws is not None:
            await ws.close()
            return
        await handle.close()  # type: ignore[attr-defined]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Base StreamHandler
# ---------------------------------------------------------------------------


class StreamHandler(ABC):
    """Base class for provider-mode-specific stream handling.

    Subclasses implement the core logic for OpenAI Realtime, ElevenLabs ConvAI,
    or Pipeline mode. The telephony handler creates the appropriate subclass
    and delegates audio/lifecycle events.
    """

    def __init__(
        self,
        agent,
        audio_sender: AudioSender,
        call_id: str,
        caller: str,
        callee: str,
        resolved_prompt: str,
        metrics,
        *,
        on_transcript=None,
        on_message=None,
        on_metrics=None,
        on_transcript_line=None,
        conversation_history: deque | None = None,
        transcript_entries: deque | None = None,
        speech_events: Any = None,
    ) -> None:
        self.agent = agent
        self.audio_sender = audio_sender
        self.call_id = call_id
        self.caller = caller
        self.callee = callee
        self.resolved_prompt = resolved_prompt
        self.metrics = metrics
        self.on_transcript = on_transcript
        self.on_message = on_message
        self.on_metrics = on_metrics
        # FIX-5 (issue #154): live per-line transcript callback. Wired by the
        # server to ``store.record_transcript_line`` so each user/assistant line
        # is broadcast over SSE the moment it is known (keyed by the reserved
        # turn index), letting the dashboard order lines by
        # ``(turn_index, user<assistant)`` even when the user line arrives after
        # the assistant line. ``None`` == no live-line emission (back-compat).
        # Parity with TS handler's direct ``metricsStore.recordTranscriptLine``.
        self.on_transcript_line = on_transcript_line
        # FIX-5 (issue #154): monotonic turn index reserved at turn-open
        # (``speech_stopped``) via ``metrics.reserve_turn_index``. Stamped onto
        # every transcript line (user + assistant) so the dashboard can order
        # lines by (turn_index, role) even when the user line arrives after the
        # assistant line. ``None`` until the first turn opens. Lives on the base
        # so every realtime-style subclass (OpenAI Realtime + ConvAI) shares it.
        # Parity with TS ``currentTurnIndex``.
        self._current_turn_index: int | None = None
        # ``is not None`` — NOT ``or``: the bridges pass freshly-created EMPTY
        # deques (falsy!), so ``or`` silently allocated private replacements
        # and the bridge-side deques stayed empty forever — every
        # ``on_call_end`` payload carried an empty transcript/history.
        self.conversation_history: deque = (
            conversation_history if conversation_history is not None else deque(maxlen=200)
        )
        self.transcript_entries: deque = (
            transcript_entries if transcript_entries is not None else deque(maxlen=200)
        )
        # Optional `SpeechEvents` dispatcher. When set, the handler emits
        # turn-taking edges (VAD start/stop, EOU commit, agent first/last
        # wire chunk) as the call progresses. None == prior behaviour.
        self.speech_events = speech_events
        # Tracks the wall-clock when the user's current speaking segment
        # began, so `fire_user_speech_ended` can include `speech_duration_ms`.
        self._user_speech_start_ms: float | None = None
        # Tracks the wall-clock when the agent's current turn began
        # speaking on the wire, for `fire_agent_speech_ended.speech_duration_ms`.
        self._agent_turn_start_ms: float | None = None
        self._background_task: asyncio.Task | None = None
        # MCP server connection manager — populated lazily in
        # ``_init_mcp_tools`` when the agent declares ``mcp_servers``.
        # Closed in ``cleanup``/``fire_call_end`` to free open MCP
        # WebSocket / HTTP connections. Parity with TS field.
        self._mcp_manager: Any = None
        # Carrier-neutral local call recorder (``LocalCallRecorder`` from
        # ``getpatter.audio.call_recorder``) — wired by the telephony bridge
        # right after handler construction when ``serve(local_recording=...)``
        # is on (next to the ``_patter_side`` assignment). ``None`` (default)
        # keeps every tap a no-op. Closed via ``_close_local_recorder`` in
        # each subclass ``cleanup``. Parity with TS ``StreamHandler.localRecorder``.
        self.local_recorder: Any = None

        # Set by Patter._attach_span_exporter via attach_span_exporter; "uut" by default.
        # Read once at handler start; later changes via the same Patter instance
        # will not retroactively affect this handler's spans.
        self._patter_side: str = getattr(self, "_patter_side", "uut")

        # Create one EventBus per handler instance and wire it to metrics.
        from getpatter.observability.event_bus import EventBus as _EventBus

        self._event_bus: _EventBus = _EventBus()
        if self.metrics is not None and hasattr(self.metrics, "attach_event_bus"):
            self.metrics.attach_event_bus(self._event_bus)

    async def _init_mcp_tools(self) -> None:
        """Connect to every configured MCP server, discover their tools
        via ``tools/list``, and merge them into ``agent.tools`` before
        the adapter is built. The synthetic handlers dispatch back
        through the MCP client so ``ToolExecutor`` can invoke them like
        any other handler-tool. No-op when ``agent.mcp_servers`` is
        empty or the optional ``mcp`` package is not installed."""
        servers = getattr(self.agent, "mcp_servers", None)
        if not servers:
            return
        from getpatter.tools.mcp_client import MCPManager

        manager = MCPManager(servers)
        try:
            discovered = await manager.connect()
        except Exception as exc:
            logger.error("MCP connect failed (continuing without MCP tools): %s", exc)
            return
        if not discovered:
            return
        existing = list(self.agent.tools or [])
        MCPManager.assert_no_conflicts(existing, discovered)
        # ``Agent`` is a frozen dataclass — replace it with a copy that
        # has the merged tool list so the adapter and ToolExecutor see
        # the discovered tools alongside user-defined ones.
        import dataclasses

        self.agent = dataclasses.replace(self.agent, tools=tuple(existing + discovered))
        self._mcp_manager = manager
        logger.info("MCP: merged %d tool(s) into agent", len(discovered))

    async def _close_mcp(self) -> None:
        """Close MCP connections opened by :meth:`_init_mcp_tools`."""
        manager = self._mcp_manager
        self._mcp_manager = None
        if manager is None:
            return
        try:
            await manager.close()
        except Exception as exc:
            logger.debug("MCP close error (ignored): %s", exc)

    def _close_local_recorder(self) -> None:
        """Finalize the local recording WAV (if any). Idempotent + guarded —
        called from every subclass ``cleanup`` so abnormal teardown paths
        (carrier WS drop, stream error) still patch the WAV header and leave
        a parseable file. The recorder reference is kept so the bridge can
        read ``handler.local_recorder.path`` for the ``on_call_end`` payload.
        """
        recorder = getattr(self, "local_recorder", None)
        if recorder is None:
            return
        try:
            recorder.close()
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.debug("Local recorder close failed: %s", exc)

    def _tap_caller_audio(self, data: bytes, encoding: str) -> None:
        """Feed a caller-side chunk to the local recorder (no-op when off)."""
        recorder = getattr(self, "local_recorder", None)
        if recorder is not None:
            recorder.add_caller_audio(data, encoding=encoding)

    def _tap_agent_audio(self, data: bytes, encoding: str) -> None:
        """Feed an agent-side chunk to the local recorder (no-op when off)."""
        recorder = getattr(self, "local_recorder", None)
        if recorder is not None:
            recorder.add_agent_audio(data, encoding=encoding)

    def add_observer(self, fn) -> None:
        """Register *fn* as an observer for all ``metrics_collected`` events.

        Convenience wrapper around :meth:`EventBus.on` that exposes a stable
        public API for external monitoring tools::

            handler.add_observer(lambda payload: print(payload))

        Returns ``None``; to unsubscribe, call :meth:`EventBus.on` directly.

        Args:
            fn: Callable that accepts a single payload dict. May be sync or
                async (async callables are scheduled via asyncio.create_task).
        """
        self._event_bus.on("metrics_collected", fn)

    # ------------------------------------------------------------------
    # Speech-event helpers — no-op when no SpeechEvents dispatcher is set.
    # ------------------------------------------------------------------

    async def _emit_user_speech_started(self) -> None:
        if getattr(self, "speech_events", None) is None:
            return
        self._user_speech_start_ms = time.time() * 1000
        await self.speech_events.fire_user_speech_started()

    async def _emit_user_speech_ended(self) -> None:
        if getattr(self, "speech_events", None) is None:
            return
        now_ms = time.time() * 1000
        duration_ms = (
            int(now_ms - self._user_speech_start_ms)
            if self._user_speech_start_ms is not None
            else 0
        )
        self._user_speech_start_ms = None
        await self.speech_events.fire_user_speech_ended(
            speech_duration_ms=max(0, duration_ms)
        )

    async def _emit_user_speech_eos(
        self, *, trigger: str, transcript_so_far: str | None = None
    ) -> None:
        if getattr(self, "speech_events", None) is None:
            return
        await self.speech_events.fire_user_speech_eos(
            trigger=trigger, transcript_so_far=transcript_so_far
        )

    async def _emit_agent_speech_started(self, *, engine: str | None = None) -> None:
        if getattr(self, "speech_events", None) is None:
            return
        self._agent_turn_start_ms = time.time() * 1000
        tts_provider = self._infer_tts_provider()
        await self.speech_events.fire_agent_speech_started(
            tts_provider=tts_provider, engine=engine
        )

    async def _emit_agent_speech_ended(self, *, interrupted: bool = False) -> None:
        if getattr(self, "speech_events", None) is None:
            return
        now_ms = time.time() * 1000
        duration_ms = (
            int(now_ms - self._agent_turn_start_ms)
            if self._agent_turn_start_ms is not None
            else 0
        )
        self._agent_turn_start_ms = None
        await self.speech_events.fire_agent_speech_ended(
            speech_duration_ms=max(0, duration_ms), interrupted=interrupted
        )

    async def _emit_llm_first_token(
        self, *, llm_provider: str, model: str | None = None
    ) -> None:
        """Fire the per-turn TTFT marker. Idempotent within a turn —
        :class:`SpeechEvents` guards on ``_first_token_for_turn``.
        """
        if getattr(self, "speech_events", None) is None:
            return
        await self.speech_events.fire_llm_first_token(
            llm_provider=llm_provider, model=model or ""
        )

    async def _emit_audio_out(self, *, tts_provider: str | None = None) -> None:
        """Fire the per-turn first-TTS-chunk marker. Idempotent within a
        turn — :class:`SpeechEvents` guards on ``_first_audio_for_turn``.
        ``tts_provider`` defaults to the inferred TTS class name (Pipeline
        mode) or the engine name (Realtime / ConvAI).
        """
        if getattr(self, "speech_events", None) is None:
            return
        provider = tts_provider or self._infer_tts_provider() or "unknown"
        await self.speech_events.fire_audio_out(tts_provider=provider)

    def _infer_tts_provider(self) -> str | None:
        """Best-effort TTS provider name for event payloads. Returns None
        when the handler has no TTS (Realtime / ConvAI engines) or the
        provider can't be classified."""
        tts = getattr(self, "tts_provider", None) or getattr(self, "_tts", None)
        if tts is None:
            return None
        cls_name = type(tts).__name__.lower()
        # Heuristic: provider classes are named like ``ElevenLabsTTS``,
        # ``OpenAITTS``, ``CartesiaTTS`` etc.
        for known in (
            "elevenlabs",
            "openai",
            "cartesia",
            "rime",
            "lmnt",
            "inworld",
            "telnyx",
        ):
            if known in cls_name:
                return known
        return cls_name.replace("tts", "") or None

    def _infer_llm_provider(self) -> str:
        """Best-effort LLM provider name for event payloads. Returns the
        agent's configured LLM provider class name lower-cased, or
        ``"openai"`` when only the OpenAI key path is in use."""
        llm = getattr(self.agent, "llm", None)
        if llm is None:
            return "openai"
        cls_name = type(llm).__name__.lower()
        for known in (
            "anthropic",
            "cerebras",
            "groq",
            "google",
            "gemini",
            "openai",
            "azure",
            "mistral",
            "deepseek",
        ):
            if known in cls_name:
                return known
        return cls_name.replace("llmprovider", "").replace("llm", "") or "custom"

    @abstractmethod
    async def start(self) -> None:
        """Initialize provider connections and start background tasks."""

    @abstractmethod
    async def on_audio_received(self, audio_bytes: bytes) -> None:
        """Handle incoming audio from the telephony provider (already decoded)."""

    async def on_dtmf(self, digit: str) -> None:
        """Handle DTMF keypress. Override in subclasses that support it."""

    async def on_mark(self, mark_name: str) -> None:
        """Handle playback mark confirmation. Override if needed."""

    @abstractmethod
    async def cleanup(self) -> None:
        """Close provider connections and cancel background tasks."""

    # Safety: auto-hangup ceiling to prevent runaway billing on calls that
    # never receive a carrier stop (mirrors TS MAX_CALL_DURATION_MS = 1 h).
    _MAX_CALL_DURATION_S: float = 60 * 60

    def _arm_max_call_watchdog(self) -> None:
        """Start the 1-hour auto-hangup watchdog (idempotent).

        TS has had this guard since early on; Python calls without a carrier
        stop event could previously run (and bill) forever.
        """
        existing = getattr(self, "_max_call_watchdog", None)
        if existing is not None and not existing.done():
            return

        async def _watchdog() -> None:
            await asyncio.sleep(self._MAX_CALL_DURATION_S)
            logger.warning(
                "Call %s hit max duration (%d min) — terminating",
                self.call_id,
                int(self._MAX_CALL_DURATION_S / 60),
            )
            hangup = getattr(self, "_hangup_fn", None)
            if hangup is not None:
                try:
                    await hangup()
                except Exception as exc:  # noqa: BLE001 - best effort
                    logger.debug("max-duration hangup failed: %s", exc)

        self._max_call_watchdog = asyncio.create_task(_watchdog())

    def _cancel_max_call_watchdog(self) -> None:
        task = getattr(self, "_max_call_watchdog", None)
        if task is not None and not task.done():
            task.cancel()
        self._max_call_watchdog = None


    async def _safe_on_transcript(self, payload: dict) -> None:
        """Invoke the user's ``on_transcript`` with exception containment.

        A raise from user code inside the realtime event-forwarding loop
        permanently killed it — inbound audio kept flowing to the provider
        while nothing came back (zombie call). Observer callbacks must never
        break the pipeline.
        """
        if not self.on_transcript:
            return
        try:
            await self.on_transcript(payload)
        except Exception:  # noqa: BLE001 - user callback containment
            logger.exception("on_transcript callback failed")

    async def _safe_on_metrics(self, payload: dict) -> None:
        """Invoke the user's ``on_metrics`` with exception containment."""
        if not self.on_metrics:
            return
        try:
            await self.on_metrics(payload)
        except Exception:  # noqa: BLE001 - user callback containment
            logger.exception("on_metrics callback failed")

    async def _emit_turn_metrics(self, turn, *, call_id: str | None = None) -> None:
        """Emit a completed turn to the user-supplied on_metrics callback.

        All emit sites share the same payload shape
        (``{call_id, turn, cost_so_far}``). Callers remain responsible for
        appending transcript entries / storing the turn; only the user-facing
        callback is centralised here for parity with TS ``emitTurnMetrics``.
        """
        # Stamp patter.latency.{ttfb_ms,turn_ms} on the active span before the
        # user callback runs. ``ttfb_ms`` maps to ``total_ms`` (turn_start →
        # first TTS audio byte — the user-perceptible "time to first byte"
        # for the response). ``turn_ms`` maps to ``tts_total_ms`` when set
        # (LLM-first-token → last TTS byte) and falls back to ``total_ms``.
        if turn is not None and getattr(turn, "latency", None) is not None:
            try:
                from getpatter.services.pipeline_hooks import PipelineHookExecutor

                ttfb_ms = float(turn.latency.total_ms or 0.0)
                turn_ms = float(
                    turn.latency.tts_total_ms
                    if turn.latency.tts_total_ms is not None
                    else (turn.latency.total_ms or 0.0)
                )
                PipelineHookExecutor(hooks=None).record_turn_latency(
                    ttfb_ms=ttfb_ms, turn_ms=turn_ms
                )
            except Exception:  # pragma: no cover — observability must never break calls
                logger.debug("record_turn_latency failed", exc_info=True)

        if not self.on_metrics or turn is None or self.metrics is None:
            return
        await self._safe_on_metrics(
            {
                "call_id": call_id if call_id is not None else self.call_id,
                "turn": turn,
                "cost_so_far": self.metrics.get_cost_so_far(),
                # Fix 5: expose LLM TTFT separately from full-generation llm_ms.
                "llm_ttft_ms": self.metrics.last_turn_llm_ttft_ms,
            }
        )


# ---------------------------------------------------------------------------
# OpenAI Realtime StreamHandler
# ---------------------------------------------------------------------------


#: Hard cap on how long the Realtime path waits for the user transcript to
#: arrive before flushing the buffered assistant turn alone. 3 s covers
#: OpenAI Whisper's typical 200-800 ms post-response delay with substantial
#: headroom; beyond this we accept the order will look "assistant-only"
#: rather than block the dashboard transcript display indefinitely.
_REALTIME_USER_TRANSCRIPT_WAIT_S = 3.0


class OpenAIRealtimeStreamHandler(StreamHandler):
    """Handles the openai_realtime provider mode."""

    def __init__(
        self,
        agent,
        audio_sender: AudioSender,
        call_id: str,
        caller: str,
        callee: str,
        resolved_prompt: str,
        metrics,
        *,
        openai_key: str,
        transfer_fn=None,
        hangup_fn=None,
        on_transcript=None,
        on_metrics=None,
        on_transcript_line=None,
        conversation_history: deque | None = None,
        transcript_entries: deque | None = None,
        audio_format: str = "pcm16",
        input_transcode: str | None = None,
        speech_events=None,
        pop_prewarmed_connections=None,
    ) -> None:
        super().__init__(
            agent=agent,
            audio_sender=audio_sender,
            call_id=call_id,
            caller=caller,
            callee=callee,
            resolved_prompt=resolved_prompt,
            metrics=metrics,
            on_transcript=on_transcript,
            on_metrics=on_metrics,
            on_transcript_line=on_transcript_line,
            conversation_history=conversation_history,
            transcript_entries=transcript_entries,
            speech_events=speech_events,
        )
        self._openai_key = openai_key
        self._transfer_fn = transfer_fn
        self._hangup_fn = hangup_fn
        self._audio_format = audio_format
        # Callback supplied by the telephony adapter so we can adopt a
        # Realtime WS that ``Patter._park_provider_connections`` opened
        # during the ringing window. ``None`` skips adoption — we fall
        # back to a cold ``connect()``.
        self._pop_prewarmed_connections = pop_prewarmed_connections
        # OpenAI Realtime API uses a single codec for both input and output
        # (``audio_format`` becomes both ``input_audio_format`` and
        # ``output_audio_format`` in the session). When the telephony leg
        # delivers a different codec than what we want to send back (e.g.
        # Telnyx inbound = PCM16 16 kHz, outbound = PCMU 8 kHz), set
        # ``input_transcode`` to convert inbound bytes to match ``audio_format``
        # before forwarding to OpenAI.
        #
        # Supported values:
        #   ``"pcm16_16k_to_g711_ulaw"`` — Telnyx inbound PCM16 16 kHz →
        #       mulaw 8 kHz (matches ``audio_format="g711_ulaw"``).
        self._input_transcode = input_transcode
        self._adapter = None
        # Per-handler StatefulResampler for pcm16_16k_to_g711_ulaw transcoding.
        self._resampler_16k_to_8k = None
        # Realtime turn ordering buffer. OpenAI Realtime emits the
        # user-transcript-completion event AFTER response_done, because
        # Whisper transcription runs in parallel with — and slower than —
        # the model response. Without this buffer the conversation_history
        # push order is [assistant, user, ...] which renders out-of-order
        # in the dashboard. See TS parity in stream-handler.ts.
        self._user_transcript_pending = False
        self._pending_assistant_turn: str | None = None
        self._pending_assistant_timer: asyncio.Task | None = None
        # ``self._current_turn_index`` is initialised on the base
        # ``StreamHandler.__init__`` (shared with the ConvAI handler).

    async def _flush_assistant_turn(self, text: str) -> None:
        """Push an assistant turn into history, fire ``on_transcript``, and
        emit turn-complete metrics. Shared between the immediate path (no
        user transcript pending) and the buffered path (flushed after the
        user transcript arrives or the fallback timer fires)."""
        self.conversation_history.append(
            {"role": "assistant", "text": text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "assistant", "text": text})
        if self.on_transcript:
            # FIX-5: stamp the reserved turn index so the dashboard can order
            # this assistant line relative to its user line by (turn_index,
            # role) even when the lines arrive out of order.
            await self._safe_on_transcript(
                {
                    "role": "assistant",
                    "text": text,
                    "call_id": self.call_id,
                    "turnIndex": self._current_turn_index,
                    "history": list(self.conversation_history),
                }
            )
        # FIX-5 (issue #154): emit the live assistant transcript line keyed by
        # the same reserved turn index as its paired user line BEFORE the metrics
        # turn is recorded, so the dashboard renders it as soon as it is known.
        await self._emit_transcript_line("assistant", text)
        if self.metrics is not None:
            # Pass the pre-reserved index so the recorded turn carries the same
            # stable ``turn_index`` as the live transcript lines.
            turn = self.metrics.record_turn_complete(
                text, pre_reserved_index=self._current_turn_index
            )
            await self._emit_turn_metrics(turn)

    async def _emit_transcript_line(self, role: str, text: str) -> None:
        """Emit a single live transcript line to the dashboard store.

        FIX-5 (issue #154): fires ``on_transcript_line`` (wired by the server to
        ``store.record_transcript_line``) with the reserved turn index so the
        dashboard can order lines by ``(turn_index, user<assistant)``. No-op when
        no callback is wired or no turn index has been reserved. Parity with TS
        handler's direct ``metricsStore.recordTranscriptLine`` calls.
        """
        if self.on_transcript_line is None or self._current_turn_index is None:
            return
        if not text:
            return
        try:
            await self.on_transcript_line(
                {
                    "call_id": self.call_id,
                    "turnIndex": self._current_turn_index,
                    "role": role,
                    "text": text,
                }
            )
        except Exception:  # pragma: no cover — observability must never break calls
            logger.debug("on_transcript_line failed", exc_info=True)

    async def _assistant_buffer_timeout(self) -> None:
        """Fallback flush: if the user transcript never arrives, surface
        the assistant turn alone after the wait window."""
        try:
            await asyncio.sleep(_REALTIME_USER_TRANSCRIPT_WAIT_S)
        except asyncio.CancelledError:
            return
        buffered = self._pending_assistant_turn
        self._pending_assistant_turn = None
        self._pending_assistant_timer = None
        self._user_transcript_pending = False
        if buffered is not None:
            try:
                await self._flush_assistant_turn(buffered)
            except Exception:
                logger.exception("Assistant buffer flush (timeout) failed")

    def _schedule_reassurance(
        self, tool_def: dict, tool_name: str
    ) -> asyncio.Task | None:
        """Schedule a reassurance filler message if the tool has one
        configured. Bridges the silence when a slow tool call would
        otherwise leave the caller hanging. Returns the task so the
        caller can cancel it on tool completion. Parity with TS
        ``handleFunctionCall`` reassurance scheduling."""
        config = tool_def.get("reassurance")
        if not config:
            return None
        if isinstance(config, str):
            message = config
            after_ms = 1500
        elif isinstance(config, dict):
            message = config.get("message", "")
            after_ms = int(config.get("after_ms", 1500))
        else:
            return None
        if not message:
            return None

        adapter = self._adapter
        if adapter is None or not (
            hasattr(adapter, "send_reassurance") or hasattr(adapter, "send_text")
        ):
            return None

        async def _fire() -> None:
            try:
                await asyncio.sleep(after_ms / 1000.0)
                # Speak the filler WITHOUT injecting a phantom ``role:user``
                # turn. ``send_reassurance`` emits a bare ``response.create``
                # (assistant-attributed, same no-fake-turn shape as
                # ``send_first_message``); ``send_text`` would corrupt the
                # transcript with a fake caller line. Fall back to
                # ``send_text`` only for adapters lacking the new method.
                if hasattr(adapter, "send_reassurance"):
                    await adapter.send_reassurance(message)
                else:
                    await adapter.send_text(message)
            except asyncio.CancelledError:
                # Tool returned before the grace window — nothing to do.
                raise
            except Exception as exc:
                logger.warning(
                    "Reassurance message failed for tool '%s': %s", tool_name, exc
                )

        return asyncio.create_task(_fire())

    async def _emit_tool_event(
        self,
        name: str,
        args: dict | None,
        result: str | None,
    ) -> None:
        """Surface a tool invocation into the transcript timeline. Pushes
        ``role="tool"`` into history (for the dashboard) and fires
        ``on_transcript`` so the host application can log / persist /
        render it. Result is truncated for log readability — the full
        payload is in history."""
        args_text = json.dumps(args or {})
        if result is None:
            text = f"{name}({args_text})"
        else:
            displayed = result if len(result) <= 200 else result[:200] + "…"
            text = f"{name}({args_text}) → {displayed}"
        self.conversation_history.append(
            {"role": "tool", "text": text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "tool", "text": text})
        if self.on_transcript:
            await self._safe_on_transcript(
                {
                    "role": "tool",
                    "text": text,
                    "call_id": self.call_id,
                    "tool_name": name,
                    "tool_args": args or {},
                    "tool_result": result,
                }
            )

    async def _handle_handoff_function_call(self, func_data: dict) -> None:
        """Dispatch the built-in ``handoff_to`` tool on the Realtime path.

        Swaps the live session to the target agent's configuration via a
        mid-session ``session.update`` (new ``instructions`` + ``tools``),
        updates ``self.agent`` so subsequent tool dispatch resolves against
        the target's tool list, and records a system-style history entry so
        transcripts show the handoff. ALWAYS sends a function result — an
        unknown name / malformed args produce an error envelope, never
        silence (a missing function result would wedge the model).

        Voice is intentionally NOT swapped: OpenAI Realtime rejects a voice
        change once the session has produced audio, so the session keeps the
        voice established at call start (documented limitation; an INFO log
        is emitted when the target requested a different voice).
        """
        raw_args = func_data.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (json.JSONDecodeError, ValueError):
            args = None
        if not isinstance(args, dict):
            result = json.dumps(
                {"error": "Malformed handoff_to arguments", "status": "rejected"}
            )
            await self._adapter.send_function_result(func_data["call_id"], result)
            await self._emit_tool_event(HANDOFF_TOOL_NAME, {}, result)
            return
        name = args.get("name", "")
        reason = args.get("reason") or ""
        handoffs: dict = getattr(self.agent, "handoffs", None) or {}
        target = handoffs.get(name)
        if target is None:
            result = json.dumps(
                {
                    "error": f"Unknown handoff agent {name!r}",
                    "available": sorted(handoffs.keys()),
                }
            )
            await self._adapter.send_function_result(func_data["call_id"], result)
            await self._emit_tool_event(HANDOFF_TOOL_NAME, args, result)
            return

        if target.voice and target.voice != self.agent.voice:
            logger.info(
                "handoff_to %r: voice change is not supported mid-session on "
                "OpenAI Realtime — keeping the current voice.",
                name,
            )

        # Swap the LLM-visible config (frozen dataclass → dataclasses.replace
        # inside _apply_handoff_target) and re-inject the consult tool when the
        # target configures one.
        self.agent = _inject_consult_tool(_apply_handoff_target(self.agent, target))
        self.resolved_prompt = resolve_agent_prompt(self.agent)

        # Build the new wire tool list: target tools + built-ins (+ onward
        # handoff tool when the target has its own handoff map). Mirrors the
        # construction in ``start()``.
        new_tools: list[dict] = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {}),
                **({"strict": True} if t.get("strict") is True else {}),
            }
            for t in (self.agent.tools or [])
        ]
        new_tools += [TRANSFER_CALL_TOOL, END_CALL_TOOL]
        if getattr(self.agent, "handoffs", None):
            new_tools.append(build_handoff_tool(self.agent.handoffs.keys()))

        new_instructions = apply_tool_call_preambles(
            self.resolved_prompt,
            getattr(self.agent, "tool_call_preambles", False),
        )
        # session.update FIRST, then the function result — the result triggers
        # the next ``response.create``, which must already run under the new
        # instructions so the model replies as the target agent.
        await self._adapter.update_session(
            instructions=new_instructions, tools=new_tools
        )

        handoff_text = _handoff_history_text(name, reason)
        self.conversation_history.append(
            {"role": "system", "text": handoff_text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "system", "text": handoff_text})
        if self.on_transcript:
            await self.on_transcript(
                {
                    "role": "system",
                    "text": handoff_text,
                    "call_id": self.call_id,
                }
            )

        result = json.dumps({"status": "handed_off", "to": name})
        await self._adapter.send_function_result(func_data["call_id"], result)
        await self._emit_tool_event(HANDOFF_TOOL_NAME, args, result)

    async def start(self) -> None:
        """Connect to OpenAI Realtime, register tools, and begin event forwarding."""
        self._arm_max_call_watchdog()
        # Both ``openai_realtime`` and ``openai_realtime_2`` engines now
        # route through the GA-compatible ``OpenAIRealtime2Adapter`` —
        # OpenAI deprecated the Beta Realtime API on 2026-05, returning
        # `invalid_model` to the legacy ``session.update`` shape and the
        # ``OpenAI-Beta: realtime=v1`` header. Only the default model
        # string differs between the two engines (mini vs flagship);
        # everything else (session shape, MIME types, event names) is
        # identical and lives in the GA adapter.
        from getpatter.providers.openai_realtime_2 import (  # type: ignore[import]
            OpenAIRealtime2Adapter,
        )

        _adapter_cls = OpenAIRealtime2Adapter

        # Resolve MCP servers BEFORE the adapter is built so the
        # discovered tools are visible in the first ``session.update``.
        # Failures are logged but not fatal — a dead MCP server should
        # not kill the entire call. Parity with TS ``initMcpTools``.
        await self._init_mcp_tools()
        # Merge the built-in consult tool (if configured) so its schema reaches
        # the Realtime session and its handler reaches the ToolExecutor.
        self.agent = _inject_consult_tool(self.agent)

        preambles_on = bool(getattr(self.agent, "tool_call_preambles", False))
        agent_tools: list[dict] = []
        for t in self.agent.tools or []:
            description = t.get("description", "")
            # Per-tool nicety: when preambles are on AND the tool carries a
            # ``reassurance`` string, surface it to the model as a sample
            # preamble phrasing on a COPY of the description (never mutate the
            # frozen Agent / tool dict). Non-breaking and opt-in.
            sample = t.get("reassurance")
            if preambles_on and isinstance(sample, str) and sample:
                description = f"{description}\n\nPreamble sample phrases:\n- {sample}"
            entry: dict = {
                "name": t["name"],
                "description": description,
                "parameters": t.get("parameters", {}),
            }
            # Propagate strict-mode opt-in to the OpenAI session.update
            # wire format. Schema is already validated at agent() build
            # time so we can pass it through without re-checking.
            if t.get("strict") is True:
                entry["strict"] = True
            agent_tools.append(entry)
        openai_tools: list[dict] = agent_tools + [TRANSFER_CALL_TOOL, END_CALL_TOOL]
        # Multi-agent handoff: advertise the built-in ``handoff_to`` tool when
        # the agent has handoff targets configured. Dispatched in
        # ``_forward_events`` (see ``_handle_handoff_function_call``).
        if getattr(self.agent, "handoffs", None):
            openai_tools.append(build_handoff_tool(self.agent.handoffs.keys()))

        # Forward optional engine-level Realtime knobs (carried on the Agent
        # by ``Patter._unpack_engine``) only when set, so the adapter's own
        # defaults remain authoritative for users that don't pass them.
        adapter_kwargs: dict = {
            "api_key": self._openai_key,
            "model": self.agent.model,
            "voice": self.agent.voice,
            "instructions": apply_tool_call_preambles(
                self.resolved_prompt,
                getattr(self.agent, "tool_call_preambles", False),
            ),
            "language": self.agent.language,
            "tools": openai_tools,
            "audio_format": self._audio_format,
        }
        reasoning_effort = getattr(self.agent, "openai_realtime_reasoning_effort", None)
        if reasoning_effort is not None:
            adapter_kwargs["reasoning_effort"] = reasoning_effort
        transcription_model = getattr(
            self.agent, "openai_realtime_input_audio_transcription_model", None
        )
        if transcription_model is not None:
            adapter_kwargs["input_audio_transcription_model"] = transcription_model
        # Forward the speakerphone noise-reduction + turn-detection tuning
        # knobs only when set, so the adapter's own defaults stay authoritative
        # for users who don't pass them. (POINT 1a / 1b.)
        noise_reduction = getattr(self.agent, "openai_realtime_noise_reduction", None)
        if noise_reduction is not None:
            adapter_kwargs["noise_reduction"] = noise_reduction
        turn_detection = getattr(self.agent, "realtime_turn_detection", None)
        if turn_detection is not None:
            adapter_kwargs["turn_detection"] = turn_detection
        # Response-decoupling flag (issue #154): when set, the stream-handler
        # reads it back off the adapter to decide whether to fire
        # ``response.create`` on speech-stop (default, decoupled) or wait for
        # the Whisper transcript (legacy). Forward only when set so the
        # adapter's own ``False`` default stays authoritative otherwise.
        gate_response = getattr(
            self.agent, "realtime_gate_response_on_transcript", None
        )
        if gate_response is not None:
            adapter_kwargs["gate_response_on_transcript"] = gate_response
        self._adapter = _adapter_cls(**adapter_kwargs)

        # Try to adopt a Realtime WebSocket parked during the ringing
        # window. When present we skip the cold ``connect()`` — the
        # parked socket has already paid the TCP + TLS + HTTP-101 +
        # ``session.update`` ack round-trip (~300-600 ms saved on first
        # audible word). Fall back transparently on cache miss / dead
        # socket / adapter missing ``adopt_websocket``.
        parked: dict | None = None
        pop_cb = self._pop_prewarmed_connections
        if pop_cb is None:
            logger.info(
                "[PREWARM] callId=%s provider=openai_realtime SKIPPED adoption: "
                "pop_prewarmed_connections callback not wired",
                self.call_id,
            )
        else:
            try:
                parked = pop_cb(self.call_id)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime FAILED pop: %s",
                    self.call_id,
                    exc,
                )
                parked = None
            if parked is None:
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime no slot present "
                    "(cache miss / parked task still in flight)",
                    self.call_id,
                )
        parked_realtime_ws = (parked or {}).get("openai_realtime")
        adopt_ok = False
        if parked_realtime_ws is not None:
            adopt = getattr(self._adapter, "adopt_websocket", None)
            # Liveness check robust across ``websockets`` versions. The
            # legacy client exposes a ``closed`` bool, the new asyncio
            # client exposes ``state`` (websockets.protocol.State enum)
            # and ``close_code`` (None while OPEN). Pre-2025-04 we used
            # ``getattr(ws, "closed", True)`` which defaulted to True
            # when the attribute didn't exist — causing the GA-shape
            # parked WS to be treated as dead and forcibly closed
            # right before adoption.
            ws_alive = _is_parked_ws_alive(parked_realtime_ws)
            ws_closed = not ws_alive
            if not callable(adopt):
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime adopter missing "
                    "adopt_websocket method",
                    self.call_id,
                )
            elif not ws_alive:
                logger.info(
                    "[PREWARM] callId=%s provider=openai_realtime parked WS died "
                    "between park and adopt (closed=%s)",
                    self.call_id,
                    ws_closed,
                )
            else:
                try:
                    adopt(parked_realtime_ws)
                    logger.info(
                        "[CONNECT] callId=%s provider=openai_realtime source=adopted ms=0",
                        self.call_id,
                    )
                    adopt_ok = True
                except Exception as exc:  # noqa: BLE001
                    logger.info(
                        "[PREWARM] callId=%s provider=openai_realtime adopt FAILED: %s",
                        self.call_id,
                        exc,
                    )
            if not adopt_ok:
                try:
                    await parked_realtime_ws.close()
                except Exception:
                    pass
        if not adopt_ok:
            await self._adapter.connect()
        logger.debug(
            "OpenAI Realtime connected (adapter=%s)",
            getattr(_adapter_cls, "__name__", repr(_adapter_cls)),
        )

        if self.agent.first_message:
            # Start measuring latency for the firstMessage turn (sendText →
            # first audio byte). Parity with TS handler.
            if self.metrics is not None:
                self.metrics.start_turn()
            # Use ``send_first_message`` (role=assistant) so the AI treats
            # ``first_message`` as its OWN opening line, not a user prompt
            # to respond to. Older adapters that don't expose the new method
            # fall back to ``send_text``.
            sender = getattr(
                self._adapter, "send_first_message", self._adapter.send_text
            )
            await sender(self.agent.first_message)

        self._background_task = asyncio.create_task(self._forward_events())

    async def _forward_events(self) -> None:
        from getpatter.tools.tool_executor import ToolExecutor  # type: ignore[import]

        tool_executor = ToolExecutor()
        # Arm first-byte capture so that the firstMessage turn (started in
        # start()) gets its tts_ms / total_ms recorded on the first audio
        # chunk. Parity with TS ``responseAudioStarted=false`` class field.
        waiting_first_audio = True
        current_agent_text = ""
        try:
            async for ev_type, ev_data in self._adapter.receive_events():
                if ev_type == "audio":
                    # Fallback: if audio arrives before speech_stopped (which
                    # can happen when JS/async event loop reorders WS frames
                    # under load, or with server VAD disabled) start the turn
                    # now so latency is still measured. Parity with TS.
                    if self.metrics is not None and not self.metrics.turn_active:
                        self.metrics.start_turn()
                    if waiting_first_audio:
                        if self.metrics is not None:
                            self.metrics.record_tts_first_byte()
                        # Speech-event: first wire-time chunk of this agent turn.
                        await self._emit_agent_speech_started(engine="openai_realtime")
                        # Speech-event: first TTS audio chunk of this turn.
                        # In Realtime mode the LLM and TTS are the same model
                        # (audio-out IS the model output), so the same edge
                        # marks both ``llm_first_token`` and ``tts_first_audio``
                        # for the SDK callback consumers. The dispatcher
                        # idempotency guards stop double-firing within a turn.
                        await self._emit_audio_out(tts_provider="openai_realtime")
                        waiting_first_audio = False
                    # Local-recording tap (agent side). The adapter emits the
                    # negotiated session codec: μ-law 8 kHz for ``g711_ulaw``
                    # sessions (all current carriers), PCM16 24 kHz otherwise
                    # — decode in the tap, never skip, so the recorder always
                    # receives PCM16 16 kHz.
                    if getattr(self, "local_recorder", None) is not None:
                        self._tap_agent_audio(
                            ev_data,
                            "mulaw_8k"
                            if self._audio_format == "g711_ulaw"
                            else "pcm16_24k",
                        )
                    await self.audio_sender.send_audio(ev_data)
                    await self.audio_sender.send_mark(f"audio_{id(ev_data)}")

                elif ev_type == "speech_stopped":
                    # OpenAI server-side VAD detected end-of-user-speech.
                    # This is the earliest reliable moment to start measuring
                    # turn latency in Realtime mode — transcript_input arrives
                    # noticeably later and understates end-to-end latency.
                    if self.metrics is not None and not self.metrics.turn_active:
                        self.metrics.start_turn()
                    # FIX-5: reserve a monotonic turn index the moment the turn
                    # opens so both the user line (on transcript_input) and the
                    # assistant line (on flush) carry the same index, letting
                    # the dashboard order them by (turn_index, role) even when
                    # the user line lands after the assistant line.
                    if self.metrics is not None:
                        self._current_turn_index = self.metrics.reserve_turn_index()
                    waiting_first_audio = True
                    current_agent_text = ""
                    # Mark a user transcript is expected so response_done
                    # waits for it before pushing the assistant turn.
                    self._user_transcript_pending = True
                    # Issue #154: decouple the model response from the Whisper
                    # transcript. By DEFAULT the GA session sets
                    # ``create_response: True`` (see _build_ga_session_config),
                    # so the SERVER auto-creates the response when it commits the
                    # user's audio buffer (``input_audio_buffer.committed``). The
                    # e2e model replies immediately, in parallel with the Whisper
                    # transcript — no ~500 ms transcript wait and no client-side
                    # race against the commit. Patter therefore does NOT drive
                    # ``response.create`` here. The Whisper transcript only
                    # populates the displayed transcript / history /
                    # ``on_transcript`` (pure observability); the hallucination
                    # filter applies to DISPLAY only and never gates or cancels
                    # the response. When ``gate_response_on_transcript`` is
                    # ``True`` (opt-out, legacy) the GA session sets
                    # ``create_response: False`` and the response is driven from
                    # the ``transcript_input`` branch below.
                    # Speech-event: raw VAD trailing edge. EOU is committed
                    # later on `transcript_input` (Realtime emits it after
                    # input_audio_buffer.committed).
                    await self._emit_user_speech_ended()

                elif ev_type == "transcript_input":
                    logger.debug("User: %s", sanitize_log_value(ev_data))
                    # Filter known Whisper-on-silence hallucinations. The
                    # Realtime API's input_audio_transcription is Whisper,
                    # and Whisper's training-set bias means PSTN echo /
                    # silence segments often transcribe as
                    # "Thank you for watching." / "Thanks for watching." /
                    # "[music]" etc. — feeding those back to the LLM
                    # produces phantom user turns the caller never spoke.
                    if _is_stt_hallucination(ev_data or ""):
                        logger.info(
                            "Realtime transcript_input dropped (likely "
                            "Whisper hallucination on silence/echo): %r",
                            sanitize_log_value((ev_data or "")[:60]),
                        )
                        self._user_transcript_pending = False
                        # FIX-1: flush any assistant turn that ``response_done``
                        # buffered while waiting for this (now-dropped) user
                        # transcript. Without this the buffered reply stalls
                        # until the ~3 s ``_assistant_buffer_timeout`` fallback
                        # fires, so the displayed reply lags and turns
                        # interleave. Capture + null + cancel the timer BEFORE
                        # flushing so a concurrent flush path can't double-emit.
                        if self._pending_assistant_turn is not None:
                            buffered = self._pending_assistant_turn
                            self._pending_assistant_turn = None
                            if self._pending_assistant_timer is not None:
                                self._pending_assistant_timer.cancel()
                                self._pending_assistant_timer = None
                            await self._flush_assistant_turn(buffered)
                        continue
                    if self.metrics is not None:
                        # Fallback: start turn here if speech_stopped was missed
                        # (server VAD disabled or custom config).
                        if not self.metrics.turn_active:
                            self.metrics.start_turn()
                        self.metrics.record_stt_complete(ev_data)
                    waiting_first_audio = True
                    current_agent_text = ""
                    # Speech-event: end-of-utterance committed (Realtime mode
                    # emits this on `input_audio_buffer.committed`, which is
                    # the canonical "user finished" signal).
                    await self._emit_user_speech_eos(
                        trigger="vad_silence", transcript_so_far=ev_data
                    )

                    self.conversation_history.append(
                        {"role": "user", "text": ev_data, "timestamp": time.time()}
                    )
                    self.transcript_entries.append({"role": "user", "text": ev_data})
                    if self.on_transcript:
                        # FIX-5: emit the live user line the moment it is known,
                        # stamped with the reserved turn index so the dashboard
                        # can place it ABOVE its assistant line even if that
                        # line was already surfaced.
                        await self._safe_on_transcript(
                            {
                                "role": "user",
                                "text": ev_data,
                                "call_id": self.call_id,
                                "turnIndex": self._current_turn_index,
                                "history": list(self.conversation_history),
                            }
                        )
                    # FIX-5: emit the live user transcript line to the dashboard
                    # store (via ``on_transcript_line`` → ``record_transcript_line``)
                    # keyed by the reserved turn index, so the live pane renders
                    # it the moment the filter accepts and can sort it above its
                    # agent line. Parity with TS ``recordTranscriptLine``.
                    await self._emit_transcript_line("user", ev_data)
                    # Legacy transcript-gated response (issue #154 opt-out).
                    # By default the response is already requested on
                    # ``speech_stopped`` above (decoupled from Whisper), so we
                    # do NOT request it here. Only when
                    # ``gate_response_on_transcript`` is ``True`` does Patter
                    # drive the response from this branch — AFTER the
                    # hallucination filter accepts the transcript — restoring
                    # the older behavior where the model waits for Whisper.
                    # The session config still sets
                    # ``turn_detection.create_response: false`` so OpenAI's
                    # server VAD never auto-creates a response on its own.
                    gate_response = getattr(
                        self._adapter, "gate_response_on_transcript", False
                    )
                    if gate_response:
                        request_response = getattr(
                            self._adapter, "request_response", None
                        )
                        if callable(request_response):
                            try:
                                await request_response()
                            except Exception as exc:  # noqa: BLE001
                                logger.debug(
                                    "Realtime request_response failed: %s", exc
                                )
                    # User transcript landed — flush any assistant turn
                    # that was buffered waiting for it.
                    self._user_transcript_pending = False
                    if self._pending_assistant_turn is not None:
                        buffered = self._pending_assistant_turn
                        self._pending_assistant_turn = None
                        if self._pending_assistant_timer is not None:
                            self._pending_assistant_timer.cancel()
                            self._pending_assistant_timer = None
                        await self._flush_assistant_turn(buffered)

                elif ev_type == "transcript_output":
                    if ev_data:
                        response_text: str = ev_data
                        # Speech-event: first LLM token (TTFT) for this turn.
                        # Idempotent — dispatcher guards on
                        # ``_first_token_for_turn``.
                        await self._emit_llm_first_token(
                            llm_provider="openai_realtime",
                            model=self.agent.model,
                        )
                        # Evaluate on the ACCUMULATED text: per-delta checks
                        # never matched a blocked term split across deltas.
                        blocked, guard_name = evaluate_guardrails(
                            self.agent, current_agent_text + response_text
                        )
                        if blocked:
                            await self._adapter.cancel_response()
                            # Drop the blocked sentence's audio already queued
                            # at the carrier — cancel_response alone let it
                            # play out in full.
                            try:
                                await self.audio_sender.send_clear()
                            except Exception as exc:  # noqa: BLE001
                                logger.debug(
                                    "send_clear on guardrail block failed: %s", exc
                                )
                            replacement = get_guardrail_replacement(
                                self.agent, guard_name
                            )
                            # Speak the replacement as the ASSISTANT's own
                            # response (instructed response.create) —
                            # ``send_text`` injected it as a phantom
                            # ``role:user`` turn, so the model REPLIED to
                            # "I can't respond to that" as if the caller had
                            # said it.
                            send_re = getattr(
                                self._adapter, "send_reassurance", None
                            )
                            if callable(send_re):
                                await send_re(replacement)
                            else:
                                await self._adapter.send_text(replacement)
                            current_agent_text = ""
                        else:
                            # Accumulate deltas — push single entry on response_done
                            current_agent_text += response_text

                elif ev_type == "speech_started":
                    # OpenAI server VAD detected the caller starting to speak.
                    # Behaviour splits on ``gate_response_on_transcript``:
                    #
                    #  - DEFAULT (gate False — SERVER-MANAGED): the GA session
                    #    sets ``interrupt_response: true``, so the OpenAI server
                    #    owns the barge-in cancel. Patter does the WebSocket-only
                    #    bookkeeping the server cannot do for us on this
                    #    transport: clear the carrier playout buffer
                    #    (``send_clear``) and truncate the in-flight item
                    #    (``truncate_playback`` — the server auto-truncates only
                    #    on WebRTC/SIP). It does NOT send ``response.cancel``
                    #    (redundant — the server already cancels), does NOT run
                    #    the MIN_AGENT_SPEAKING anti-flicker gate, and does NOT
                    #    call ``record_bargein_detected`` / ``anchor_user_speech_start``
                    #    (the engine turn stays anchored at ``speech_stopped`` —
                    #    re-anchoring to user-speech-start inflated total_ms).
                    #
                    #  - LEGACY (gate True — CLIENT-MANAGED opt-out): the GA
                    #    session sets ``interrupt_response: false`` and Patter
                    #    drives the full client-side barge-in: the
                    #    MIN_AGENT_SPEAKING anti-flicker gate (server VAD fires
                    #    ``speech_started`` on echo of the agent's own audio in
                    #    PSTN no-AEC scenarios), ``cancel_response`` (truncate +
                    #    response.cancel), and the FIX-3 barge-in metrics.
                    server_managed = not getattr(
                        self._adapter, "gate_response_on_transcript", False
                    )
                    if not server_managed:
                        # Anti-flicker gate (legacy only). ``OpenAIRealtimeStreamHandler``
                        # doesn't carry the full pipeline TTS-tracking state (no
                        # ``_is_speaking`` / ``_first_audio_sent_at``), so we use
                        # the adapter's own response-tracking attributes as a proxy.
                        response_started_at = getattr(
                            self._adapter,
                            "_current_response_first_audio_at",
                            None,
                        )
                        if response_started_at is not None:
                            elapsed = time.monotonic() - response_started_at
                            if elapsed < MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_NO_AEC:
                                logger.info(
                                    "Realtime barge-in suppressed "
                                    "(response < gate, %.2fs)",
                                    elapsed,
                                )
                                continue
                    await self.audio_sender.send_clear()
                    if server_managed:
                        # Server owns the cancel (interrupt_response=true). Only
                        # the WebSocket-transport truncate is needed client-side.
                        await self._adapter.truncate_playback()
                    else:
                        # FIX-3: stamp barge-in detection on the legacy interrupt
                        # path (mirrors the pipeline path). Arms the post-barge-in
                        # hygiene gate in _compute_turn_latency (``_last_bargein_at``
                        # within 100 ms of the next ``_turn_start`` → drop
                        # endpoint_ms / stt_ms). Stamp BEFORE cancel so the
                        # detection timestamp is the true interrupt edge.
                        if self.metrics is not None:
                            self.metrics.record_bargein_detected()
                        await self._adapter.cancel_response()
                    if self.metrics is not None:
                        self.metrics.record_turn_interrupted()
                    # Speech-event: user started speaking. If the agent was
                    # mid-turn this is a barge-in — close out the agent turn
                    # as interrupted before flagging the new user-speech edge,
                    # so consumers see ``agent_ended(interrupted=true)`` →
                    # ``user_started`` in causal order.
                    if not waiting_first_audio:
                        await self._emit_agent_speech_ended(interrupted=True)
                    await self._emit_user_speech_started()
                    # FIX-3: anchor the new turn at this VAD speech_start —
                    # LEGACY (client-managed) path only. On the server-managed
                    # path the engine turn deliberately stays anchored at
                    # ``speech_stopped``; re-anchoring here would double-count
                    # and inflate total_ms.
                    if not server_managed and self.metrics is not None:
                        self.metrics.anchor_user_speech_start()
                    waiting_first_audio = False
                    current_agent_text = ""
                    # Barge-in invalidates any buffered assistant turn —
                    # the user interrupted before the response was
                    # committed; do not surface it as if completed.
                    self._pending_assistant_turn = None
                    if self._pending_assistant_timer is not None:
                        self._pending_assistant_timer.cancel()
                        self._pending_assistant_timer = None
                    self._user_transcript_pending = False

                elif ev_type == "response_done":
                    if self.metrics is not None and isinstance(ev_data, dict):
                        usage = ev_data.get("usage", {})
                        if usage:
                            # ``response.done`` carries the model used for
                            # this turn (e.g. ``gpt-realtime-2``); pass it
                            # so the cost calc auto-resolves the per-model
                            # rate. Falls back to ``self.realtime_model`` set
                            # at call start when absent.
                            self.metrics.record_realtime_usage(
                                usage, model=ev_data.get("model")
                            )
                    response_was_cancelled = (
                        not current_agent_text
                        and self.metrics is not None
                        and self.metrics.turn_active
                    )
                    if current_agent_text:
                        text_to_flush = current_agent_text
                        current_agent_text = ""
                        if self._user_transcript_pending:
                            # Buffer until the user transcript arrives so
                            # the rendered order is [user, assistant, ...]
                            # rather than [assistant, user, ...].
                            self._pending_assistant_turn = text_to_flush
                            if self._pending_assistant_timer is not None:
                                self._pending_assistant_timer.cancel()
                            self._pending_assistant_timer = asyncio.create_task(
                                self._assistant_buffer_timeout()
                            )
                        else:
                            await self._flush_assistant_turn(text_to_flush)
                    elif self.metrics is not None and self.metrics.turn_active:
                        # response_done without agent text = cancelled / empty
                        # response. Close the active turn as interrupted so the
                        # next speech_stopped can start a fresh turn cleanly.
                        # Parity with TS handleAdapterEvent response_done path.
                        self.metrics.record_turn_interrupted()
                    # Speech-event: agent finished its turn. ``interrupted``
                    # tracks whether the response was cut by barge-in (no
                    # text emitted) versus a clean completion. We only fire
                    # when an agent turn was actually in flight (start_ms is
                    # set), to avoid spurious events on engine warmup.
                    if self._agent_turn_start_ms is not None:
                        await self._emit_agent_speech_ended(
                            interrupted=response_was_cancelled
                        )
                    waiting_first_audio = True

                elif ev_type == "function_call":
                    func_data = ev_data
                    if func_data["name"] == "transfer_call":
                        raw_args = func_data.get("arguments", "{}")
                        try:
                            args = (
                                json.loads(raw_args)
                                if isinstance(raw_args, str)
                                else raw_args
                            )
                        except (json.JSONDecodeError, ValueError):
                            logger.warning(
                                "function_call transfer_call: malformed JSON args, skipping"
                            )
                            continue
                        transfer_number = args.get("number", "")
                        transfer_mode = args.get("mode") or "cold"
                        transfer_summary = args.get("summary") or ""
                        if transfer_mode not in _TRANSFER_MODES:
                            rejection = json.dumps(
                                {
                                    "error": (
                                        f"Invalid transfer mode {transfer_mode!r}"
                                        " — use 'cold' or 'warm'"
                                    ),
                                    "status": "rejected",
                                }
                            )
                            await self._adapter.send_function_result(
                                func_data["call_id"], rejection
                            )
                            await self._emit_tool_event(
                                "transfer_call", args, rejection
                            )
                            continue
                        if not _validate_e164(transfer_number):
                            logger.warning(
                                "transfer_call rejected: invalid number %s",
                                mask_phone_number(transfer_number),
                            )
                            rejection = json.dumps(
                                {
                                    "error": "Invalid phone number format",
                                    "status": "rejected",
                                }
                            )
                            await self._adapter.send_function_result(
                                func_data["call_id"], rejection
                            )
                            await self._emit_tool_event(
                                "transfer_call", args, rejection
                            )
                            continue
                        if transfer_mode == "warm":
                            # Warm transfer: run the carrier sequence FIRST so
                            # an unsupported carrier / REST failure surfaces an
                            # error envelope and the AI keeps the call instead
                            # of going dark. Only a confirmed warm transfer
                            # ends this event loop.
                            outcome = await _invoke_transfer_fn(
                                self._transfer_fn,
                                transfer_number,
                                mode="warm",
                                summary=transfer_summary,
                            )
                            if isinstance(outcome, dict) and outcome.get("error"):
                                result = json.dumps(outcome)
                                await self._adapter.send_function_result(
                                    func_data["call_id"], result
                                )
                                await self._emit_tool_event(
                                    "transfer_call", args, result
                                )
                                continue
                            result = json.dumps(
                                outcome
                                if isinstance(outcome, dict)
                                else {
                                    "status": "transferring",
                                    "mode": "warm",
                                    "to": transfer_number,
                                }
                            )
                            await self._adapter.send_function_result(
                                func_data["call_id"], result
                            )
                            await self._emit_tool_event("transfer_call", args, result)
                            if self.on_transcript:
                                await self.on_transcript(
                                    {
                                        "role": "system",
                                        "text": (
                                            "Call transferred (warm) to "
                                            f"{transfer_number}"
                                        ),
                                        "call_id": self.call_id,
                                    }
                                )
                            return
                        logger.debug(
                            "Transferring call to %s",
                            mask_phone_number(transfer_number),
                        )
                        result = json.dumps(
                            {"status": "transferring", "to": transfer_number}
                        )
                        await self._adapter.send_function_result(
                            func_data["call_id"], result
                        )
                        await self._emit_tool_event("transfer_call", args, result)
                        if self._transfer_fn:
                            await self._transfer_fn(transfer_number)
                        if self.on_transcript:
                            await self._safe_on_transcript(
                                {
                                    "role": "system",
                                    "text": f"Call transferred to {transfer_number}",
                                    "call_id": self.call_id,
                                }
                            )
                        return

                    elif func_data["name"] == "end_call":
                        raw_args = func_data.get("arguments", "{}")
                        try:
                            args = (
                                json.loads(raw_args)
                                if isinstance(raw_args, str)
                                else raw_args
                            )
                        except (json.JSONDecodeError, ValueError):
                            logger.warning(
                                "function_call end_call: malformed JSON args, skipping"
                            )
                            continue
                        reason = args.get("reason", "conversation_complete")
                        logger.debug("Ending call: %s", reason)
                        result = json.dumps({"status": "ending", "reason": reason})
                        await self._adapter.send_function_result(
                            func_data["call_id"], result
                        )
                        await self._emit_tool_event("end_call", args, result)
                        if self._hangup_fn:
                            await self._hangup_fn()
                        if self.on_transcript:
                            await self._safe_on_transcript(
                                {
                                    "role": "system",
                                    "text": f"Call ended: {reason}",
                                    "call_id": self.call_id,
                                }
                            )
                        return

                    elif func_data["name"] == HANDOFF_TOOL_NAME and getattr(
                        self.agent, "handoffs", None
                    ):
                        await self._handle_handoff_function_call(func_data)

                    else:
                        tool_def = next(
                            (
                                t
                                for t in (self.agent.tools or [])
                                if t["name"] == func_data["name"]
                            ),
                            None,
                        )
                        if tool_def and (
                            tool_def.get("webhook_url") or tool_def.get("handler")
                        ):
                            args = func_data.get("arguments", "{}")
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except (json.JSONDecodeError, ValueError):
                                    logger.warning(
                                        "function_call %s: malformed JSON args",
                                        func_data["name"],
                                    )
                                    # A skipped call leaves a dangling
                                    # function_call item — the model waits
                                    # for an output that never comes (dead
                                    # air). Answer with an error envelope
                                    # instead. Mirrors TS.
                                    await self._adapter.send_function_result(
                                        func_data["call_id"],
                                        json.dumps(
                                            {
                                                "error": "Tool arguments were not "
                                                "valid JSON; the call was not "
                                                "executed.",
                                                "fallback": True,
                                            }
                                        ),
                                    )
                                    continue
                            # Surface the invocation BEFORE execution so the
                            # dashboard timeline shows it at the right point
                            # even if the handler throws or hangs.
                            await self._emit_tool_event(func_data["name"], args, None)
                            # Schedule reassurance filler if configured —
                            # bridges silence on slow tool calls. Cleared
                            # in finally below. Parity with TS handler.
                            reassurance_task = self._schedule_reassurance(
                                tool_def, func_data["name"]
                            )
                            # Progress sink: when the handler is an async
                            # generator that yields ``{"progress": "..."}``,
                            # forward each progress message via the Realtime
                            # adapter so the agent speaks the update inline.
                            adapter_for_progress = self._adapter

                            async def _on_progress(text: str) -> None:
                                if hasattr(adapter_for_progress, "send_text"):
                                    try:
                                        await adapter_for_progress.send_text(text)
                                    except Exception as exc:
                                        logger.warning(
                                            "Tool progress message failed: %s",
                                            exc,
                                        )

                            try:
                                result = await tool_executor.execute(
                                    tool_name=func_data["name"],
                                    arguments=args,
                                    call_context={
                                        "call_id": self.call_id,
                                        "caller": self.caller,
                                        "callee": self.callee,
                                    },
                                    webhook_url=tool_def.get("webhook_url", ""),
                                    handler=tool_def.get("handler"),
                                    on_progress=_on_progress,
                                    tool_timeout_s=tool_def.get("timeout_s"),
                                )
                            finally:
                                if reassurance_task is not None:
                                    reassurance_task.cancel()
                            await self._adapter.send_function_result(
                                func_data["call_id"], result
                            )
                            # Emit follow-up event with result so timeline
                            # shows full call/return semantics.
                            await self._emit_tool_event(func_data["name"], args, result)
                        else:
                            # Unknown (hallucinated) tool name, or a
                            # schema-only tool with neither webhook nor
                            # handler: ALWAYS answer the function_call —
                            # silence left a dangling item and the tool turn
                            # never completed (dead air). Mirrors TS.
                            logger.warning(
                                "function_call for unregistered tool '%s' — "
                                "returning error envelope",
                                func_data.get("name", ""),
                            )
                            await self._adapter.send_function_result(
                                func_data["call_id"],
                                json.dumps(
                                    {
                                        "error": f"Tool '{func_data.get('name', '')}' "
                                        "is not registered",
                                        "fallback": True,
                                    }
                                ),
                            )

                elif ev_type == "error":
                    # FIX-4: surface provider-side Realtime ``error`` events.
                    # The adapter yields these (e.g. session config rejected,
                    # rate-limited, transient server error) but the loop
                    # previously ignored them, so failures were invisible.
                    # Log type/code/message ONLY (never audio/transcript
                    # bodies) and continue — a single error frame must not
                    # terminate the call. Parity with TS ``onAdapterError``.
                    logger.warning(
                        "OpenAI Realtime error event: %s",
                        _summarize_realtime_error(ev_data),
                    )
        except Exception as exc:
            logger.exception("OpenAI Realtime forward error: %s", exc)

    async def on_audio_received(self, audio_bytes: bytes) -> None:
        """Forward decoded telephony audio to the OpenAI Realtime session (transcoding if needed)."""
        # Local-recording tap — BEFORE the adapter guard so the caller side
        # is captured even while the Realtime WS is still connecting. The
        # Realtime handler forwards carrier bytes untouched, so the inbound
        # encoding follows the negotiated session codec: ``g711_ulaw``
        # sessions (Twilio / Telnyx / Plivo) receive μ-law 8 kHz; the
        # ``input_transcode`` path receives PCM16 16 kHz from the carrier.
        # The recorder decodes to PCM16 16 kHz internally.
        if getattr(self, "local_recorder", None) is not None:
            if self._input_transcode == "pcm16_16k_to_g711_ulaw":
                _rec_enc = "pcm16_16k"
            elif self._audio_format == "g711_ulaw":
                _rec_enc = "mulaw_8k"
            else:
                _rec_enc = "pcm16_16k"
            self._tap_caller_audio(audio_bytes, _rec_enc)
        if self._adapter is None:
            return
        if self._input_transcode == "pcm16_16k_to_g711_ulaw":
            from getpatter.audio.transcoding import pcm16_to_mulaw

            # Use per-handler StatefulResampler to preserve ratecv filter state
            # across chunks and prevent boundary artefacts.
            if self._resampler_16k_to_8k is None:
                from getpatter.audio.transcoding import create_resampler_16k_to_8k

                self._resampler_16k_to_8k = create_resampler_16k_to_8k()
            audio_bytes = pcm16_to_mulaw(self._resampler_16k_to_8k.process(audio_bytes))
        await self._adapter.send_audio(audio_bytes)

    async def on_dtmf(self, digit: str) -> None:
        """Forward a DTMF keypress to the model as a synthetic user message."""
        if self._adapter is not None:
            await self._adapter.send_text(
                f"The user pressed key {digit} on their phone keypad."
            )

    async def cleanup(self) -> None:
        """Cancel the event-forward task and close the OpenAI Realtime adapter."""
        self._cancel_max_call_watchdog()
        if self._pending_assistant_timer is not None:
            self._pending_assistant_timer.cancel()
            self._pending_assistant_timer = None
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._adapter:
            await self._adapter.close()
        # Close MCP server connections. Best effort: a flaky MCP server
        # must not derail call-end teardown.
        await self._close_mcp()
        # Finalize the local recording WAV (guarded + idempotent).
        self._close_local_recorder()
        # Flush and discard the resampler tail on cleanup.
        if self._resampler_16k_to_8k is not None:
            self._resampler_16k_to_8k.flush()
            self._resampler_16k_to_8k = None


# ---------------------------------------------------------------------------
# ElevenLabs ConvAI StreamHandler
# ---------------------------------------------------------------------------


class ElevenLabsConvAIStreamHandler(StreamHandler):
    """Handles the elevenlabs_convai provider mode."""

    def __init__(
        self,
        agent,
        audio_sender: AudioSender,
        call_id: str,
        caller: str,
        callee: str,
        resolved_prompt: str,
        metrics,
        *,
        elevenlabs_key: str,
        for_twilio: bool = False,
        on_transcript=None,
        on_metrics=None,
        on_transcript_line=None,
        conversation_history: deque | None = None,
        transcript_entries: deque | None = None,
        output_audio_format: str | None = None,
        input_audio_format: str | None = None,
        speech_events=None,
    ) -> None:
        super().__init__(
            agent=agent,
            audio_sender=audio_sender,
            call_id=call_id,
            caller=caller,
            callee=callee,
            resolved_prompt=resolved_prompt,
            metrics=metrics,
            on_transcript=on_transcript,
            on_metrics=on_metrics,
            on_transcript_line=on_transcript_line,
            conversation_history=conversation_history,
            transcript_entries=transcript_entries,
            speech_events=speech_events,
        )
        self._elevenlabs_key = elevenlabs_key
        self._for_twilio = for_twilio
        # Caller-supplied codec overrides win over agent.elevenlabs_convai
        # config (resolved at start() time so the integration test that
        # mocks the adapter doesn't crash on a missing config dict).
        self._output_audio_format_override = output_audio_format
        self._input_audio_format_override = input_audio_format
        # When True (set in start() once we know the negotiated formats),
        # forward inbound caller audio as raw μ-law 8 kHz and skip the
        # outbound PCM16 → μ-law transcode in the audio sender. Mirrors
        # OpenAIRealtimeStreamHandler's ``audio_format='g711_ulaw'`` path.
        self._native_mulaw_8k = False
        self._adapter = None
        # Per-handler StatefulResampler for Twilio mulaw 8 kHz -> PCM16 16 kHz.
        # Only created when we actually need to resample (i.e. ConvAI
        # negotiated PCM16 16 kHz, not native μ-law).
        self._resampler_8k_to_16k = None

    async def start(self) -> None:
        """Connect to the ElevenLabs ConvAI agent and begin event forwarding."""
        self._arm_max_call_watchdog()
        from getpatter.providers.elevenlabs_convai import (
            ElevenLabsConvAIAdapter,  # type: ignore[import]
        )

        voice = (
            self.agent.voice if self.agent.voice != "alloy" else "EXAVITQu4vr4xnSDxMaL"
        )
        agent_id = ""
        el_config = getattr(self.agent, "elevenlabs_convai", None) or {}
        if isinstance(el_config, dict):
            agent_id = el_config.get("agent_id", "")

        if not agent_id:
            raise ValueError(
                "ElevenLabs ConvAI requires agent.elevenlabs_convai={'agent_id': '...'}. "
                "Create an agent in the ElevenLabs Conversational AI dashboard "
                "and pass its id."
            )

        # Resolve negotiated audio formats. Precedence (highest to lowest):
        #   1. Explicit handler kwargs (output_audio_format / input_audio_format)
        #   2. agent.elevenlabs_convai dict ("output_audio_format", "input_audio_format")
        #   3. None — let ConvAI pick its server default (PCM16 16 kHz)
        cfg_output = (
            el_config.get("output_audio_format")
            if isinstance(el_config, dict)
            else None
        )
        cfg_input = (
            el_config.get("input_audio_format") if isinstance(el_config, dict) else None
        )
        output_audio_format = self._output_audio_format_override or cfg_output
        input_audio_format = self._input_audio_format_override or cfg_input

        self._adapter = ElevenLabsConvAIAdapter(
            api_key=self._elevenlabs_key,
            agent_id=agent_id,
            voice_id=voice,
            language=self.agent.language,
            first_message=self.agent.first_message,
            output_audio_format=output_audio_format,
            input_audio_format=input_audio_format,
        )

        # Detect the μ-law 8 kHz fast-path. Both directions must be
        # ulaw_8000 — mixing PCM16 with μ-law would force one transcode
        # back, defeating the optimization.
        self._native_mulaw_8k = (
            output_audio_format == "ulaw_8000" and input_audio_format == "ulaw_8000"
        )
        if self._native_mulaw_8k:
            # Flip the audio sender into pass-through mode. Mirrors how
            # OpenAIRealtimeStreamHandler relies on the bridge constructing
            # the sender with ``input_is_mulaw_8k=True``. We can't change
            # that wiring from inside the handler, so we mutate the flag
            # in place — the AudioSender's ``send_audio`` checks it on
            # every chunk, so flipping it before the first agent audio
            # arrives is safe.
            if hasattr(self.audio_sender, "_input_is_mulaw_8k"):
                self.audio_sender._input_is_mulaw_8k = True  # type: ignore[attr-defined]
            logger.debug(
                "ElevenLabs ConvAI: native μ-law 8 kHz fast-path enabled "
                "(skipping inbound resample + outbound transcode)"
            )

        await self._adapter.connect()
        logger.debug("ElevenLabs ConvAI connected")

        self._background_task = asyncio.create_task(self._forward_events())

    async def _forward_events(self) -> None:
        # Arm first-byte capture so that the firstMessage turn (started in
        # start()) gets its tts_ms / total_ms recorded on the first audio
        # chunk. Parity with TS ``responseAudioStarted=false`` class field.
        waiting_first_audio = True
        current_agent_text = ""
        try:
            async for ev_type, ev_data in self._adapter.receive_events():
                if ev_type == "audio":
                    # Fallback: audio before speech_stopped. Parity with TS.
                    if self.metrics is not None and not self.metrics.turn_active:
                        self.metrics.start_turn()
                    if waiting_first_audio and self.metrics is not None:
                        self.metrics.record_tts_first_byte()
                        waiting_first_audio = False
                        # Speech-event: first TTS audio chunk for this turn.
                        # ConvAI is a fully-baked agent so the SDK doesn't see
                        # token-level LLM deltas; the audio edge is the only
                        # observable per-turn signal.
                        await self._emit_audio_out(tts_provider="elevenlabs_convai")
                    # Local-recording tap (agent side). ConvAI emits μ-law
                    # 8 kHz on the native fast-path, PCM16 16 kHz otherwise —
                    # decode in the tap so the recorder always receives
                    # PCM16 16 kHz.
                    if getattr(self, "local_recorder", None) is not None:
                        self._tap_agent_audio(
                            ev_data,
                            "mulaw_8k" if self._native_mulaw_8k else "pcm16_16k",
                        )
                    await self.audio_sender.send_audio(ev_data)

                elif ev_type == "speech_stopped":
                    # Start turn as soon as server VAD signals end-of-user-speech,
                    # not on transcript_input (which arrives later and understates latency).
                    if self.metrics is not None and not self.metrics.turn_active:
                        self.metrics.start_turn()
                    # FIX-5: reserve a monotonic turn index at turn-open so the
                    # user and assistant lines share a stable index for dashboard
                    # ordering (parity with the GA loop).
                    if self.metrics is not None:
                        self._current_turn_index = self.metrics.reserve_turn_index()
                    waiting_first_audio = True
                    current_agent_text = ""

                elif ev_type == "transcript_input":
                    logger.debug("User: %s", sanitize_log_value(ev_data))
                    if self.metrics is not None:
                        if not self.metrics.turn_active:
                            self.metrics.start_turn()
                        self.metrics.record_stt_complete(ev_data)
                    waiting_first_audio = True
                    current_agent_text = ""
                    self.conversation_history.append(
                        {"role": "user", "text": ev_data, "timestamp": time.time()}
                    )
                    self.transcript_entries.append({"role": "user", "text": ev_data})
                    if self.on_transcript:
                        await self._safe_on_transcript(
                            {
                                "role": "user",
                                "text": ev_data,
                                "call_id": self.call_id,
                                "turnIndex": self._current_turn_index,
                                "history": list(self.conversation_history),
                            }
                        )
                    # FIX-5: live user line to the dashboard store.
                    await self._emit_transcript_line("user", ev_data)

                elif ev_type == "transcript_output":
                    if ev_data:
                        response_text: str = ev_data
                        # Speech-event: per-turn TTFT (LLM first token).
                        # ConvAI's WS streams the assistant transcript text
                        # alongside audio; the first delta is the earliest
                        # observable proxy for an LLM token.
                        await self._emit_llm_first_token(
                            llm_provider="elevenlabs_convai",
                            model=self.agent.model,
                        )
                        blocked, _ = evaluate_guardrails(self.agent, response_text)
                        if blocked:
                            current_agent_text = ""
                        else:
                            current_agent_text += response_text

                elif ev_type == "response_done":
                    if current_agent_text:
                        self.conversation_history.append(
                            {
                                "role": "assistant",
                                "text": current_agent_text,
                                "timestamp": time.time(),
                            }
                        )
                        self.transcript_entries.append(
                            {"role": "assistant", "text": current_agent_text}
                        )
                        if self.on_transcript:
                            await self._safe_on_transcript(
                                {
                                    "role": "assistant",
                                    "text": current_agent_text,
                                    "call_id": self.call_id,
                                    "turnIndex": self._current_turn_index,
                                    "history": list(self.conversation_history),
                                }
                            )
                        # FIX-5: live assistant line + stable pre-reserved index.
                        await self._emit_transcript_line(
                            "assistant", current_agent_text
                        )
                        if self.metrics is not None:
                            turn = self.metrics.record_turn_complete(
                                current_agent_text,
                                pre_reserved_index=self._current_turn_index,
                            )
                            await self._emit_turn_metrics(turn)
                        current_agent_text = ""
                    elif self.metrics is not None and self.metrics.turn_active:
                        # response_done without agent text = cancelled / empty.
                        # Close the active turn as interrupted — parity with TS.
                        self.metrics.record_turn_interrupted()
                    waiting_first_audio = True

                elif ev_type == "function_call":
                    # ElevenLabs CLIENT tool invocation. Route through the
                    # same tool lookup/executor as the other engines — the
                    # ElevenLabs agent must ALWAYS get a client_tool_result
                    # (silence stalls it until the provider-side timeout).
                    await self._handle_convai_client_tool(ev_data or {})

                elif ev_type == "interruption":
                    await self.audio_sender.send_clear()
                    if self.metrics is not None:
                        self.metrics.record_turn_interrupted()
                    waiting_first_audio = False
                    current_agent_text = ""

                elif ev_type == "error":
                    # FIX-4: surface provider-side error events (parity with
                    # the GA loop). Log type/code/message ONLY (no PII) and
                    # continue — an error frame must not terminate the call.
                    logger.warning(
                        "ElevenLabs ConvAI error event: %s",
                        _summarize_realtime_error(ev_data),
                    )
        except Exception as exc:
            logger.exception("ElevenLabs ConvAI forward error: %s", exc)

    async def _handle_convai_client_tool(self, func_data: dict) -> None:
        """Execute an ElevenLabs ``client_tool_call`` and answer it.

        Tools are matched against ``agent.tools`` (plus the built-in
        ``transfer_call``/``end_call`` names so an ElevenLabs agent that
        declares them as client tools reaches the carrier helpers). Every
        path sends a ``client_tool_result`` — including unknown tools and
        execution errors — because a missing result stalls the ElevenLabs
        agent until its own tool timeout.
        """
        from getpatter.tools.tool_executor import ToolExecutor

        call_id = str(func_data.get("call_id", "") or "")
        name = str(func_data.get("name", "") or "")
        arguments = func_data.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, ValueError):
                arguments = {}

        async def _respond(result: str, *, is_error: bool = False) -> None:
            sender = getattr(self._adapter, "send_client_tool_result", None)
            if callable(sender):
                try:
                    await sender(call_id, result, is_error=is_error)
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    logger.warning("client_tool_result send failed: %s", exc)

        # Built-ins first: transfer_call / end_call declared as ElevenLabs
        # client tools route to the carrier helpers.
        if name == "transfer_call":
            number = str(arguments.get("number", "") or "")
            if not _validate_e164(number):
                await _respond(
                    json.dumps(
                        {"error": "Invalid phone number format", "status": "rejected"}
                    ),
                    is_error=True,
                )
                return
            if self._transfer_fn is not None:
                await self._transfer_fn(number)
                await _respond(f"Transferring to {number}")
            else:
                await _respond(
                    json.dumps({"error": "transfer not available"}), is_error=True
                )
            return
        if name == "end_call":
            await _respond("Call ended")
            if self._hangup_fn is not None:
                await self._hangup_fn()
            return

        tool_def = next(
            (t for t in (self.agent.tools or []) if t.get("name") == name),
            None,
        )
        if not tool_def or not (
            tool_def.get("webhook_url") or tool_def.get("handler")
        ):
            logger.warning(
                "ConvAI client_tool_call for unregistered tool '%s'", name
            )
            await _respond(
                json.dumps(
                    {"error": f"Tool '{name}' is not registered", "fallback": True}
                ),
                is_error=True,
            )
            return

        await self._emit_tool_event(name, arguments, None)
        executor = ToolExecutor()
        try:
            result = await executor.execute(
                tool_name=name,
                arguments=arguments,
                call_context={
                    "call_id": self.call_id,
                    "caller": self.caller,
                    "callee": self.callee,
                },
                webhook_url=tool_def.get("webhook_url", ""),
                handler=tool_def.get("handler"),
                tool_timeout_s=tool_def.get("timeout_s"),
            )
        except Exception as exc:  # noqa: BLE001 - always answer the agent
            logger.exception("ConvAI client tool '%s' failed: %s", name, exc)
            await _respond(
                json.dumps({"error": str(exc)[:200], "fallback": True}),
                is_error=True,
            )
            return
        finally:
            try:
                await executor.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        await _respond(result)
        await self._emit_tool_event(name, arguments, result)

    async def on_audio_received(self, audio_bytes: bytes) -> None:
        """Forward decoded telephony audio to ConvAI (μ-law fast-path or resampled PCM16)."""
        # Local-recording tap — BEFORE the adapter guard so caller audio is
        # captured even while the ConvAI WS is still connecting. Every
        # carrier wired to this handler today (Twilio / Plivo via
        # ``for_twilio=True``, Telnyx via PCMU bidirectional streaming)
        # delivers μ-law 8 kHz on the inbound leg; the recorder decodes to
        # PCM16 16 kHz internally.
        if getattr(self, "local_recorder", None) is not None:
            self._tap_caller_audio(audio_bytes, "mulaw_8k")
        if self._adapter is None:
            return
        # Native μ-law 8 kHz fast-path: ConvAI negotiated ulaw_8000 on the
        # input side too, so the caller's μ-law bytes go through untouched.
        # No mulaw → PCM16 decode, no 8 kHz → 16 kHz resample.
        if self._native_mulaw_8k:
            await self._adapter.send_audio(audio_bytes)
            return
        # Default path: ConvAI expects PCM16 16 kHz and Twilio sends μ-law
        # 8 kHz, so decode + resample before forwarding.
        if self._for_twilio:
            from getpatter.audio.transcoding import mulaw_to_pcm16

            # Use per-handler StatefulResampler to preserve ratecv state.
            if self._resampler_8k_to_16k is None:
                from getpatter.audio.transcoding import create_resampler_8k_to_16k

                self._resampler_8k_to_16k = create_resampler_8k_to_16k()
            pcm16k = self._resampler_8k_to_16k.process(mulaw_to_pcm16(audio_bytes))
            await self._adapter.send_audio(pcm16k)
        else:
            await self._adapter.send_audio(audio_bytes)

    async def cleanup(self) -> None:
        """Cancel the event-forward task and close the ConvAI adapter."""
        self._cancel_max_call_watchdog()
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._adapter:
            await self._adapter.close()
        # Finalize the local recording WAV (guarded + idempotent).
        self._close_local_recorder()
        # Flush and discard the resampler tail on cleanup.
        if self._resampler_8k_to_16k is not None:
            self._resampler_8k_to_16k.flush()
            self._resampler_8k_to_16k = None


# ---------------------------------------------------------------------------
# Pipeline StreamHandler (STT -> LLM -> TTS)
# ---------------------------------------------------------------------------


class PipelineStreamHandler(StreamHandler):
    """Handles the pipeline provider mode (configurable STT + LLM + TTS)."""

    def __init__(
        self,
        agent,
        audio_sender: AudioSender,
        call_id: str,
        caller: str,
        callee: str,
        resolved_prompt: str,
        metrics,
        *,
        openai_key: str = "",
        deepgram_key: str = "",
        elevenlabs_key: str = "",
        for_twilio: bool = False,
        input_is_mulaw_8k: bool | None = None,
        output_is_mulaw_8k: bool | None = None,
        transfer_fn=None,
        hangup_fn=None,
        send_dtmf_fn=None,
        on_transcript=None,
        on_message=None,
        on_metrics=None,
        conversation_history: deque | None = None,
        transcript_entries: deque | None = None,
        pop_prewarm_audio=None,
        pop_prewarmed_connections=None,
        speech_events=None,
    ) -> None:
        super().__init__(
            agent=agent,
            audio_sender=audio_sender,
            call_id=call_id,
            caller=caller,
            callee=callee,
            resolved_prompt=resolved_prompt,
            metrics=metrics,
            on_transcript=on_transcript,
            on_message=on_message,
            on_metrics=on_metrics,
            conversation_history=conversation_history,
            transcript_entries=transcript_entries,
            speech_events=speech_events,
        )
        # Optional accessor returning pre-rendered first-message audio for
        # ``call_id``. Wired by ``Patter.serve()`` when the parent client
        # has ``agent.prewarm_first_message=True``. ``None`` (default) means
        # "no prewarm — always run live TTS".
        self._pop_prewarm_audio = pop_prewarm_audio
        # Optional accessor returning pre-opened, fully-handshaked
        # provider WebSockets for ``call_id``. Wired by ``Patter.serve()``.
        # Returning ``None`` means "no parked sockets — fall back to
        # fresh ``connect()``".
        self._pop_prewarmed_connections = pop_prewarmed_connections
        self._openai_key = openai_key
        self._deepgram_key = deepgram_key
        self._elevenlabs_key = elevenlabs_key
        self._for_twilio = for_twilio
        # Explicit codec flags decouple "we run on Twilio" (for metrics /
        # telephony-specific knobs) from "the stream is PCMU 8 kHz and must
        # be transcoded before STT / from PCM16 for TTS". Twilio is always
        # mulaw 8 kHz; Telnyx is mulaw 8 kHz when ``streaming_start``
        # negotiates PCMU bidirectional (our default). Callers pass the
        # flags explicitly when they differ from `for_twilio`.
        self._input_is_mulaw_8k = (
            for_twilio if input_is_mulaw_8k is None else input_is_mulaw_8k
        )
        self._output_is_mulaw_8k = (
            for_twilio if output_is_mulaw_8k is None else output_is_mulaw_8k
        )
        self._transfer_fn = transfer_fn
        self._hangup_fn = hangup_fn
        self._send_dtmf_fn = send_dtmf_fn
        self._stt = None
        # Optional deferred STT connect task — set when prewarm-handoff
        # parallelises STT.connect with the firstMessage TTS synth.
        # Awaited BEFORE the STT receive loop starts so the message
        # pump never reads from a half-open WS.
        self._stt_connect_task: asyncio.Task | None = None
        # Background greeting playback (see _play_first_message).
        self._first_message_task: asyncio.Task | None = None
        self._tts = None
        # Auto-VAD: if ``agent.vad`` is None we attempt to load SileroVAD
        # with phone-friendly defaults during ``start()``. Stored separately
        # because ``agent`` is a frozen dataclass.
        self._auto_vad = None
        self._stt_task: asyncio.Task | None = None
        # The in-flight turn dispatch (LLM + TTS) runs as a SINGLE tracked task
        # so the STT receive loop keeps draining transcripts during a long
        # (30-90 s) agent-runtime turn and can fire transcript-based barge-in
        # against the LIVE turn. Exactly one is active at a time — the loop
        # awaits the previous one to settle before launching the next, so
        # conversation_history / metrics ordering is unchanged. None when idle.
        self._dispatch_task: asyncio.Task | None = None
        # Opt-in (default OFF): forward inbound audio to STT even while the
        # agent is speaking, so the transcript barge-in path can receive a
        # transcript on echo-masked PSTN links where the VAD never fires.
        # ECHO RISK without AEC — see ``on_audio_received`` self-hearing guard.
        self._forward_stt_while_speaking = os.environ.get(
            "PATTER_FORWARD_STT_WHILE_SPEAKING", ""
        ).strip().lower() in ("1", "true", "yes")
        if self._forward_stt_while_speaking:
            logger.warning(
                "PATTER_FORWARD_STT_WHILE_SPEAKING=on: inbound audio is sent to "
                "STT during TTS so transcript barge-in works on echo-masked "
                "links. Without AEC the agent's own voice may be transcribed as "
                "a phantom interruption — pair with agent.barge_in_strategies."
            )
        self._is_speaking = False
        # True only while the post-TTS tail-grace window is pending: the
        # agent has finished its turn but ``_is_speaking`` is still held for
        # ``PATTER_TTS_TAIL_GRACE_MS`` to swallow the fading echo tail. A VAD
        # ``speech_start`` (or a transcript) during this window is the user's
        # NEXT turn, not a barge-in — there is nothing left to interrupt. Set
        # by ``_end_speaking_with_grace``; cleared by ``_begin_speaking``, the
        # grace flip, barge-in cancels, and ``_end_tail_grace_for_new_turn``.
        self._tail_grace_active = False
        # Handle to the scheduled grace-flip task so it can be cancelled
        # (parity with TS ``clearGraceTimer``) — at most one pending at a
        # time. The ``_speaking_generation`` guard already makes a stale flip
        # a no-op; cancelling avoids leaving an idle ``asyncio.sleep`` task
        # per turn on long, fast-turn calls.
        self._grace_task: asyncio.Task | None = None
        # The agent's spoken text for the CURRENT turn, accumulated as tokens
        # stream. Used by the echo guard to reject the agent's own TTS bleeding
        # back into STT (when audio is forwarded during TTS without effective
        # AEC) so it never barges in or becomes a phantom user turn. Reset at
        # ``_begin_speaking``; only consulted while ``_forward_stt_while_speaking``.
        self._current_agent_spoken_text = ""
        # Whether the last completed turn was cut short by a confirmed barge-in
        # — set by ``_process_streaming_response`` so the spoken prefix is
        # appended to history with an ``[interrupted by caller]`` marker (keeps
        # a stateful agent runtime's context grounded in what was actually heard).
        self._last_response_interrupted = False
        # Per-turn LLM cancel event. Recreated on every new turn before LLM
        # consumption so a stale cancel from a previous turn cannot terminate
        # the next stream prematurely. Initialized here so the STT loop's
        # first turn (which references it via ``self._llm_cancel_event``
        # before any LLM consumption has run) does not AttributeError.
        self._llm_cancel_event: asyncio.Event = asyncio.Event()
        # Wall-clock timestamp (``time.time()`` units) of the last
        # ``_begin_speaking`` call. Cleared by the grace flip. Used by
        # ``_can_barge_in`` to suppress early self-cancellation while
        # the AEC filter is still converging (~500 ms warmup + safety
        # margin).
        self._speaking_started_at: float | None = None
        # Wall-clock timestamp (``time.time()`` units) when the FIRST TTS
        # audio chunk of the current turn actually reached the carrier wire
        # — set by ``_mark_first_audio_sent`` after ``audio_sender.send_audio``
        # succeeds, cleared by ``_begin_speaking`` / barge-in cancels. The
        # barge-in gate is anchored to this timestamp instead of
        # ``_speaking_started_at`` because cloud TTS providers (ElevenLabs,
        # Cartesia, ...) take 200-700 ms to emit the first byte. A gate
        # starting at ``_begin_speaking`` would expire on background noise
        # before any audio went out, exit the TTS loop on
        # ``_is_speaking=False``, and silently drop the agent's first turn.
        self._first_audio_sent_at: float | None = None
        # Estimated wall-clock (``time.time()`` units) when the LAST audio
        # byte pushed to the carrier finishes PLAYING on the phone. The
        # pipeline pushes TTS audio as fast as the provider synthesizes it
        # (no pacing) while the carrier buffers + plays at realtime, so "we
        # finished pushing" and "the caller finished hearing" can diverge by
        # tens of seconds — especially with agent-runtime LLMs (Hermes /
        # OpenClaw) that deliver a long reply all at once after a thinking
        # pause. ``_end_speaking_with_grace`` holds ``_is_speaking=True``
        # (with ``_tail_grace_active=False``) until this cursor passes, so a
        # barge-in during the audible backlog still takes the cancel path
        # (``send_clear`` drops the carrier buffer) instead of being treated
        # as a calm next turn. Advanced by ``_track_outbound_playback``;
        # reset by the barge-in cancel paths and
        # ``_end_tail_grace_for_new_turn``. Mirrors TS
        # ``playbackBufferedUntil``.
        self._playback_buffered_until: float = 0.0
        # Per-turn playback timeline used to estimate the response prefix the
        # caller actually HEARD when a barge-in lands. ``_turn_playback_total_s``
        # accumulates the playout duration of every chunk pushed this turn
        # (including filler audio, which keeps the timeline aligned);
        # ``_turn_spoken_segments`` records ``(sentence_text,
        # cumulative_start_s)`` for each RESPONSE sentence at its first audible
        # chunk (filler / error-fallback audio advances the clock but adds no
        # segment). ``heard = total - remaining_backlog`` then maps to a
        # sentence-granular prefix — see ``_heard_response_prefix``. Both reset
        # at ``_begin_speaking``. Mirrors TS ``turnPlaybackTotalMs`` /
        # ``turnSpokenSegments``.
        self._turn_playback_total_s: float = 0.0
        self._turn_spoken_segments: list[tuple[str, float]] = []
        # Optional barge-in confirmation strategies (see
        # ``getpatter.services.barge_in_strategies``). With an empty tuple
        # the SDK uses the legacy "cancel on first VAD speech_start"
        # behaviour. With one or more strategies, a VAD speech_start during
        # TTS marks the barge-in as *pending* — the agent's TTS keeps
        # streaming naturally — and the strategies are consulted on every
        # STT transcript. The first strategy that approves confirms the
        # barge-in and the cancel/flush sequence runs; if none confirm
        # within ``_barge_in_confirm_s`` the pending state is dropped and
        # the agent finishes its sentence.
        self._barge_in_strategies: tuple = tuple(
            getattr(agent, "barge_in_strategies", ()) or ()
        )
        _confirm_ms = getattr(agent, "barge_in_confirm_ms", 1500)
        try:
            self._barge_in_confirm_s: float = max(0.1, float(_confirm_ms) / 1000.0)
        except (TypeError, ValueError):
            self._barge_in_confirm_s = 1.5
        # Wall-clock timestamp of the most recent VAD-marked pending
        # barge-in. ``None`` means "not pending"; a numeric value means
        # the agent has already produced a turn worth of audio AND VAD
        # has seen user speech, but no strategy has confirmed yet.
        self._barge_in_pending_since: float | None = None
        # Background task that fires the pending-timeout. Cancelled on
        # confirmation, on agent stop, and on call shutdown so a stale
        # pending never bleeds into the next turn. In
        # ``barge_in_mode="pause_resume"`` this same handle holds the
        # false-interruption resume timer (``_pause_resume_timeout``).
        self._barge_in_pending_task = None
        # ---- Pause-and-resume false-interruption handling ----
        # ``barge_in_mode="pause_resume"`` (opt-in): on VAD speech_start
        # during the agent's turn, output is PAUSED (carrier cleared, sends
        # gated on ``_output_paused``) instead of cancelled. A committed
        # final transcript within ``_barge_in_confirm_s`` KILLS the turn
        # (full cancel path); otherwise the agent RESUMES from the first
        # sentence the caller had not fully heard. Mirrors TS
        # ``bargeInMode`` / ``outputPaused``.
        _mode = getattr(agent, "barge_in_mode", "cancel") or "cancel"
        if _mode not in ("cancel", "pause_resume"):
            logger.warning(
                "Unknown barge_in_mode %r — falling back to 'cancel'", _mode
            )
            _mode = "cancel"
        self._barge_in_mode: str = _mode
        # True while output is paused: ``_synthesize_sentence`` queues
        # chunks into the per-sentence retention entries instead of
        # sending, and the streaming loops buffer whole sentences as text.
        self._output_paused: bool = False
        # Per-pause decision event — set when the pause resolves (resume,
        # kill, or teardown) so loop-side waiters can proceed. Recreated at
        # every ``_start_pause_resume``.
        self._pause_decision_event: asyncio.Event | None = None
        # Sentences produced by the LLM while paused (text, pre-guardrail).
        # Spoken in order on resume; discarded on kill. Bounded by
        # ``_PAUSE_MAX_BUFFERED_SENTENCES`` — overflow degrades to a full
        # cancel so memory stays bounded even against a runaway stream.
        self._paused_sentences: list[str] = []
        # Per-turn retained sentence audio (pause_resume mode only): one
        # entry per response sentence holding every TTS chunk produced for
        # it ({"text", "chunks", "sent"}). ``sent`` is the count of chunks
        # actually delivered to the carrier — the resume path resets it to
        # 0 for the unheard tail and re-sends from memory (no TTS
        # re-billing). Index-aligned with ``_turn_spoken_segments`` for the
        # stamped prefix. Bounded by ``_PAUSE_RESUME_MAX_RETAINED_S``.
        self._turn_sentence_audio: list[dict] = []
        self._pause_retained_bytes: int = 0
        # Set when the retained-audio cap was exceeded while NOT paused
        # (very long carrier backlog): retention is released and
        # pause_resume falls back to legacy cancel for the rest of the
        # turn. Reset at ``_begin_speaking``.
        self._pause_resume_overflowed: bool = False
        # Sentence index (into ``_turn_spoken_segments`` /
        # ``_turn_sentence_audio``) of the first sentence the caller had
        # NOT fully heard at pause time — the resume offset. Sentence
        # granularity: the partially-played sentence is replayed from its
        # start (natural-sounding repair) rather than resumed mid-word.
        self._pause_resume_index: int = 0
        # Monotonic counter incremented at every TTS-start. ``_end_speaking_with_grace``
        # captures the value at scheduling time and only flips ``_is_speaking`` to
        # False if no new turn started in the meantime. Prevents an in-flight grace
        # task from clobbering the speaking flag of the *next* turn (mirrors TS).
        self._speaking_generation: int = 0
        # Ring buffer of inbound PCM16 16 kHz frames captured while the
        # agent is speaking and the self-hearing guard is dropping audio.
        # On barge-in we flush this buffer to STT so Deepgram (or any
        # other streaming STT) receives the user's first ~600 ms of
        # speech — which would otherwise be lost while the VAD's
        # ``min_speech_duration`` window accumulated and fired
        # ``speech_start``. Each frame is 20 ms × 32 bytes (16 kHz ×
        # 16-bit mono) ≈ 640 bytes; capped to 30 frames ≈ 600 ms ≈
        # ~19 KB per concurrent call.
        # ``deque(maxlen=...)`` evicts the oldest frame in O(1) on append —
        # a plain list with ``pop(0)`` is O(n) and this runs per media frame
        # (~50/s) while the agent speaks. Cap rationale at the append site.
        self._inbound_audio_ring: deque[bytes] = deque(maxlen=13)
        # True when VAD fired ``speech_start`` during the agent's turn but
        # the barge-in gate suppressed it. The grace-timer flip drains the
        # ring buffer to STT so the user's words are not silently discarded.
        # Mirrors TS ``suppressedSpeechPending``.
        self._suppressed_speech_pending: bool = False
        # Wall-clock timestamp of the most recent barge-in cancel, used by
        # ``_begin_speaking`` to enforce a short drain window so the remote
        # PSTN player finishes flushing the cancelled turn's tail before
        # the next TTS chunk lands on top of it. Without this, the first
        # sentence of a post-barge-in turn audibly overlaps with the tail
        # of the cancelled turn (~50-200 ms of doubled audio).
        self._last_cancel_at: float | None = None
        # Acoustic echo canceller, lazily instantiated in ``start()`` when
        # ``agent.echo_cancellation`` is set. ``None`` otherwise — the mic
        # path stays a pure pass-through for handset/headset deployments
        # that don't need it.
        self._aec = None
        # Task reference for the in-flight LLM-consumption loop.  Set by
        # ``_process_streaming_response`` and cancelled on barge-in so the
        # provider stops streaming tokens we will never speak — saves API
        # cost and frees the LLM connection slot earlier.
        self._llm_consume_task: asyncio.Task | None = None
        self._call_control = None
        self._llm_loop = None
        self._msg_accepts_call = False
        self._remote_handler = None
        # Throttle state for back-to-back STT finals — see ``_commit_transcript``.
        self._last_commit_text: str = ""
        self._last_commit_at: float = 0.0
        # Inbound audio processing chain: decode (mulaw->PCM16) -> stateful
        # 8k->16k resample -> AEC near-end -> ``agent.audio_filter`` -> VAD.
        # Lazily constructed on the first media frame (slice 1 of the
        # pipeline-stages decomposition — docs/architecture/pipeline-stages.md);
        # owns the per-handler StatefulResampler previously held in
        # ``_resampler_8k_to_16k``.
        self._input_chain: InputProcessingChain | None = None
        # FIFO of outstanding Twilio marks the SDK has sent but not yet seen
        # echoed back. Used by the firstMessage paced sender to bound the
        # carrier-side buffer depth — without this the loop pushed the entire
        # TTS stream into Twilio's WebSocket in one burst and a sendClear
        # racing the queued media frames was unable to interrupt the agent
        # for up to ~2 s (BUG #128). ``on_mark`` pops entries when Twilio
        # confirms playback; ``_drain_pending_marks`` resolves every entry on
        # cancel so any awaiter exits on the next tick. Telnyx never
        # populates this queue (no mark concept on Telnyx's wire protocol —
        # the loop falls back to time-based pacing).
        self._pending_marks: list[tuple[str, asyncio.Future[None]]] = []
        # Monotonic counter for first-message mark names. Distinct from the
        # generic ``audio_*`` marks the Realtime path sends so the two paths
        # can coexist without name collisions.
        self._first_message_mark_counter: int = 0
        # Cached result of ``_is_tts_output_format_native_for_carrier()``
        # — settled once at ``start()`` time after ``set_telephony_carrier``
        # has run on the TTS adapter. ``True`` means
        # ``_encode_pipeline_audio`` can take the bypass path (raw bytes
        # → base64, no resample/encode). Parity with TS
        # ``StreamHandler.ttsOutputFormatNativeForCarrier``.
        self._tts_output_format_native_for_carrier: bool = False
        # --- PREEMPTIVE GENERATION (opt-in, built-in LLM loop only) ---
        # When enabled, a confident INTERIM transcript starts a speculative
        # LLM+TTS dispatch whose audio is HELD in memory; the final
        # transcript's commit either releases it (matching text — the
        # already-generated audio flushes immediately) or discards it and
        # dispatches normally. See ``_note_interim_transcript`` /
        # ``_try_release_speculation``. Parity with TS ``preemptiveEnabled``.
        self._preemptive_enabled: bool = bool(
            getattr(agent, "preemptive_generation", False)
        )
        try:
            self._preemptive_min_stable_s: float = max(
                0.0, float(getattr(agent, "preemptive_min_stable_ms", 300)) / 1000.0
            )
        except (TypeError, ValueError):
            self._preemptive_min_stable_s = 0.3
        # The single in-flight speculation (at most one). ``None`` when idle,
        # when discarded, or once released (a released speculation becomes
        # the live turn tracked by ``_dispatch_task`` instead).
        self._speculation: _SpeculativeTurn | None = None
        # Interim-stability tracking: normalized text of the newest interim
        # plus the one-shot watcher that starts a speculation once the text
        # has been unchanged for ``_preemptive_min_stable_s``.
        self._interim_norm: str = ""
        self._interim_text: str = ""
        self._interim_stable_task: asyncio.Task | None = None
        # ---- Semantic turn detection (opt-in via ``agent.turn_detector``) ----
        # When a detector is configured, a VAD ``speech_end`` no longer
        # finalizes STT immediately: the detector scores the rolling window
        # below and the finalize is deferred (held) while it predicts
        # "incomplete", bounded by ``agent.max_semantic_hold_ms``. With the
        # default ``turn_detector=None`` every line below is dormant and the
        # speech_end path is byte-identical to previous releases.
        self._turn_detector = getattr(agent, "turn_detector", None)
        try:
            self._max_semantic_hold_ms: int = max(
                0, int(getattr(agent, "max_semantic_hold_ms", 1200))
            )
        except (TypeError, ValueError):
            self._max_semantic_hold_ms = 1200
        # Rolling buffer of post-decode PCM16 16 kHz frames — the last ~8 s
        # (256 000 bytes) the detector consumes per prediction. Only
        # appended when a detector is configured; cleared on turn commit.
        self._semantic_audio_ring: deque[bytes] = deque()
        self._semantic_audio_ring_bytes: int = 0
        # True while a sub-threshold prediction is holding the finalize
        # open. Resolved by: a follow-up prediction crossing the threshold,
        # a VAD ``speech_start`` (user resumed — hold moot), the hard cap,
        # or a committed transcript (STT endpointed on its own).
        self._semantic_hold_active: bool = False
        # ``time.monotonic()`` deadline for the hard cap, None when idle.
        self._semantic_hold_deadline: float | None = None
        # Generation counter — invalidates the wall-clock backstop task when
        # the hold it was scheduled for has already been resolved/cancelled.
        self._semantic_hold_generation: int = 0
        # Wall-clock backstop: finalizes at the cap even if inbound audio
        # stalls entirely (the frame-driven poll below then never runs).
        self._semantic_hold_task: asyncio.Task | None = None
        # Bytes of audio accumulated since the last prediction while
        # holding — re-polls every ``_SEMANTIC_POLL_MS`` of silence.
        self._semantic_poll_pending_bytes: int = 0
        # Set on the FIRST detector failure: semantic endpointing is then
        # disabled for the remainder of the call (one clear warning, plain
        # VAD-silence behavior) instead of warning per turn against a
        # permanently broken model. Mirrors TS ``turnDetectorFailed`` and
        # the existing ``vadDisabled`` fail-once pattern.
        self._semantic_detector_failed: bool = False
        # EOU trigger for the NEXT committed turn (``EouTrigger.VAD_SILENCE``
        # | ``EouTrigger.SEMANTIC_TURN_DETECTOR``). Stamped by the semantic
        # finalize paths, consumed (and reset) by ``_dispatch_turn``.
        # Mirrors TS ``lastEouTrigger``.
        self._last_eou_trigger: str = EouTrigger.VAD_SILENCE

    async def start(self) -> None:
        """Initialize STT/TTS providers, hooks, and start the STT receive loop."""
        self._arm_max_call_watchdog()
        from getpatter.models import CallControl

        # Create STT. Pipeline mode always transcodes Twilio mulaw 8 kHz →
        # PCM16 16 kHz in on_audio_received before forwarding to STT, so the
        # STT adapter must be configured for linear16 @ 16 kHz — even on
        # Twilio. Passing `for_twilio=True` would build a mulaw-expecting
        # adapter that misinterprets the already-decoded PCM as garbage.
        if self.agent.stt:
            self._stt = _create_stt_from_config(self.agent.stt, for_twilio=False)
        elif self._deepgram_key:
            from getpatter.providers.deepgram_stt import DeepgramSTT  # type: ignore[import]

            self._stt = DeepgramSTT(
                api_key=self._deepgram_key,
                language=self.agent.language,
                encoding="linear16",
                sample_rate=16000,
            )

        # Create TTS
        if self.agent.tts:
            self._tts = _create_tts_from_config(self.agent.tts)
        elif self._elevenlabs_key:
            from getpatter.providers.elevenlabs_tts import ElevenLabsTTS  # type: ignore[import]

            self._tts = ElevenLabsTTS(
                api_key=self._elevenlabs_key, voice_id=self.agent.voice
            )

        # Advise the TTS adapter of the telephony carrier so it can pick a
        # wire-native ``output_format`` (e.g. ``ulaw_8000`` on Twilio) and
        # skip a client-side transcode. The hook is opt-in per-adapter:
        # adapters that don't expose ``set_telephony_carrier`` keep their
        # constructed format. Adapters that do (e.g. ElevenLabsWebSocketTTS)
        # only auto-flip when the user did NOT explicitly pass output_format.
        if self._tts is not None and hasattr(self._tts, "set_telephony_carrier"):
            try:
                self._tts.set_telephony_carrier(
                    "twilio" if self._for_twilio else "telnyx"
                )
            except Exception:  # pragma: no cover - defensive; adapter bug
                logger.debug(
                    "TTS set_telephony_carrier failed; using construction-time format",
                    exc_info=True,
                )
        # Re-evaluate after set_telephony_carrier so the _encode_pipeline_audio
        # fast path is enabled for the current carrier when the adapter
        # auto-flipped (or the user constructed with a native format).
        # Parity with TS ``StreamHandler.ttsOutputFormatNativeForCarrier``.
        self._tts_output_format_native_for_carrier = (
            self._is_tts_output_format_native_for_carrier()
        )
        if self._tts_output_format_native_for_carrier:
            logger.debug(
                "TTS outputFormat matches %s wire codec — bypassing client-side transcode",
                "twilio" if self._for_twilio else "telnyx",
            )
            # Flip the audio sender into pass-through mode so it stops
            # transcoding (16 kHz PCM → mulaw) bytes that are already in
            # the carrier's wire format. Mirrors the ConvAI handler's
            # ``_native_mulaw_8k`` fast-path and TS ``encodePipelineAudio``
            # bypass. Parity with TS ``StreamHandler.ttsOutputFormatNativeForCarrier``.
            if hasattr(self.audio_sender, "_input_is_mulaw_8k"):
                self.audio_sender._input_is_mulaw_8k = True  # type: ignore[attr-defined]

        if self._stt is None:
            logger.warning("Pipeline mode: no STT configured")
        if self._tts is None:
            logger.warning("Pipeline mode: no TTS configured")

        # Auto-VAD: load SileroVAD with telephony-tuned defaults if the user
        # didn't pass one. Falls back silently to the STT-endpoint heuristic
        # when the ``silero`` extra is missing — same behaviour as before for
        # users who have not installed onnxruntime.
        if getattr(self.agent, "vad", None) is None:
            try:
                from getpatter.providers.silero_vad import SileroVAD

                self._auto_vad = await asyncio.to_thread(SileroVAD.for_phone_call)
                logger.info(
                    "auto-VAD enabled (SileroVAD, phone preset). Pass agent.vad=... to override."
                )
            except ImportError:
                logger.info(
                    "auto-VAD unavailable: onnxruntime/numpy not installed. "
                    "Install with `pip install getpatter[silero]` for fast barge-in."
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "auto-VAD load failed (%s); falling back to STT-endpoint heuristic",
                    exc,
                )

        # Acoustic echo cancellation: opt-in.
        #
        # Per the industry consensus on PSTN echo cancellation and
        # Twilio's own guidance, time-domain NLMS server-side
        # AEC is the right tool only when the SDK has near-direct access
        # to the mic and speaker (browser WebRTC, mobile native). PSTN
        # paths route through a 250–1500 ms Twilio jitter buffer + carrier
        # loop — far outside the 32 ms window of a 512-tap NLMS filter at
        # 16 kHz, so the filter cannot model the echo and silently
        # degenerates into pass-through. Emit a warning so the operator
        # knows to either rely on the self-hearing guard alone (handset /
        # earpiece — minimal bleed) or keep AEC off (default) and tune
        # the VAD ``min_speech_duration`` if bleed-driven false positives
        # appear during firstMessage.
        if getattr(self.agent, "echo_cancellation", False):
            carrier = "twilio" if self._for_twilio else "telnyx"
            logger.warning(
                "echo_cancellation=True on %s (PSTN). Server-side NLMS "
                "cannot model PSTN's ~250-1500 ms round-trip echo with a "
                "32 ms filter window — it will silently no-op. Best "
                "practice: keep echo_cancellation=False; rely on the "
                "carrier + caller device's built-in echo suppression and "
                "Patter's self-hearing guard. Enable AEC only for "
                "browser/native deployments where the SDK owns the audio "
                "path end-to-end.",
                carrier,
            )
            try:
                from getpatter.audio.aec import NlmsEchoCanceller

                self._aec = NlmsEchoCanceller(sample_rate=16000)
                logger.info(
                    "echo cancellation enabled (NLMS, 512 taps + 0.5 s "
                    "warmup μ=0.5); filter converges within ~250 ms of TTS "
                    "playback in low-latency loops."
                )
            except ImportError:
                logger.warning(
                    "echo_cancellation=True but numpy is not installed; "
                    "install with `pip install getpatter[silero]` (numpy is part of that extra)."
                )

        # Prewarm-handoff: try to adopt pre-opened provider WebSockets
        # that the prewarm pipeline (see
        # ``Patter._park_provider_connections``) parked during the
        # carrier ringing window. When a parked WS is still OPEN we
        # skip the cold ``connect()`` and the STT first-turn can flow
        # audio without paying the 150-400 ms TLS handshake. Failures
        # (cache miss, parked WS died) fall back transparently.
        parked: dict | None = None
        if self._pop_prewarmed_connections is not None:
            try:
                parked = self._pop_prewarmed_connections(self.call_id)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.debug("pop_prewarmed_connections raised: %s", exc)
                parked = None

        # Adopt the TTS WS first — synchronous handoff (the live
        # ``synthesize`` call below picks it up via the adapter's
        # single-slot adoption queue).
        parked_tts = (parked or {}).get("tts")
        if parked_tts is not None and self._tts is not None:
            adopt = getattr(self._tts, "adopt_websocket", None)
            ws_alive = parked_tts.ws is not None and _is_parked_ws_alive(parked_tts.ws)
            if callable(adopt) and ws_alive:
                try:
                    adopt(parked_tts)
                    logger.info(
                        "[CONNECT] callId=%s provider=tts source=adopted ms=0",
                        self.call_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("TTS adopt_websocket failed: %s; falling back", exc)
                    try:
                        await parked_tts.ws.close()
                    except Exception:
                        pass
            else:
                try:
                    await parked_tts.ws.close()
                except Exception:
                    pass

        # Kick off STT connect WITHOUT awaiting yet — we only need STT
        # ready to receive incoming user audio, not to send the first
        # agent message out. Parallelising STT.connect with the TTS
        # firstMessage synth shaves 200-400 ms off the perceived
        # first-turn latency.
        stt_connect_task: asyncio.Task | None = None
        if self._stt is not None:
            parked_stt = (parked or {}).get("stt")
            adopt_stt = getattr(self._stt, "adopt_websocket", None)
            stt_started_at = time.monotonic()
            stt_adopted = False
            if (
                parked_stt is not None
                and callable(adopt_stt)
                and isinstance(parked_stt, tuple)
                and len(parked_stt) == 2
            ):
                session, ws = parked_stt
                if _is_parked_ws_alive(ws):
                    try:
                        adopt_stt(session, ws)
                        logger.info(
                            "[CONNECT] callId=%s provider=stt source=adopted ms=%d",
                            self.call_id,
                            int((time.monotonic() - stt_started_at) * 1000),
                        )
                        stt_adopted = True
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "STT adopt_websocket failed: %s; falling back", exc
                        )
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        try:
                            await session.close()
                        except Exception:
                            pass
                else:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    try:
                        await session.close()
                    except Exception:
                        pass
            elif parked_stt is not None:
                # Unknown handle shape — discard cleanly.
                await _safe_close_parked_handle(parked_stt)

            if not stt_adopted:

                async def _connect_stt() -> None:
                    await self._stt.connect()
                    logger.info(
                        "[CONNECT] callId=%s provider=stt source=fresh ms=%d",
                        self.call_id,
                        int((time.monotonic() - stt_started_at) * 1000),
                    )

                stt_connect_task = asyncio.create_task(_connect_stt())

        # Stash the deferred connect task so the receive-loop launcher
        # below awaits it before starting the message pump.
        self._stt_connect_task = stt_connect_task

        logger.debug("Pipeline mode: STT connect kicked off")

        # Play first_message if configured and no on_message handler — as a
        # BACKGROUND task (see _play_first_message): the greeting must not
        # block the carrier read loop. _begin_speaking completes BEFORE
        # start() returns so the self-hearing guard engages immediately.
        if (
            self.agent.first_message
            and self.on_message is None
            and self._tts is not None
        ):
            await self._begin_speaking(is_first_message=True)
            self._first_message_task = asyncio.create_task(
                self._play_first_message()
            )

            def _log_first_message_result(task: asyncio.Task) -> None:
                if task.cancelled():
                    return
                exc = task.exception()
                if exc is not None:
                    logger.error("first_message playback failed: %s", exc)

            self._first_message_task.add_done_callback(_log_first_message_result)

        # CallControl for pipeline mode
        self._call_control = CallControl(
            call_id=self.call_id,
            caller=self.caller,
            callee=self.callee,
            telephony_provider="twilio" if self._for_twilio else "telnyx",
            _transfer_fn=self._transfer_fn,
            _hangup_fn=self._hangup_fn,
            _send_dtmf_fn=self._send_dtmf_fn,
        )

        # Check if on_message accepts CallControl
        if self.on_message is not None and callable(self.on_message):
            try:
                sig = inspect.signature(self.on_message)
                self._msg_accepts_call = len(sig.parameters) >= 2
            except (ValueError, TypeError):
                pass

        # Built-in LLM loop. Three paths:
        #   1. `agent.llm` set + `on_message` set → ValueError (caught early
        #      in serve(), but we re-assert here for belt-and-braces).
        #   2. `agent.llm` set → use the user-supplied LLMProvider; openai_key
        #      is not required.
        #   3. Otherwise fall back to the legacy OpenAI default (requires
        #      `openai_key`).
        agent_llm = getattr(self.agent, "llm", None)
        if agent_llm is not None and self.on_message is not None:
            raise ValueError(
                "Cannot pass both `llm=` on the agent and `on_message=` on serve(). "
                "Pick one — `llm=` for built-in LLMs, `on_message=` for custom logic."
            )

        if self.on_message is None and (agent_llm is not None or self._openai_key):
            from getpatter.services.llm_loop import LLMLoop
            from getpatter.tools.tool_executor import ToolExecutor

            # Inject the built-in transfer_call / end_call tools — parity with
            # the realtime path (see ``OpenAIRealtimeStreamHandler.start``
            # where ``openai_tools = agent_tools + [TRANSFER_CALL_TOOL,
            # END_CALL_TOOL]``). Without this, pipeline-mode LLMs never see
            # the built-ins and can't initiate a handoff or hangup no matter
            # what the system prompt says.
            # Discover MCP tools BEFORE building the tool list: pipeline
            # mode silently ignored ``agent.mcp_servers`` (only the realtime
            # handler called this), despite the documented mode-agnostic
            # contract. No-op without configured servers.
            await self._init_mcp_tools()
            # Merge the built-in consult tool (if configured) before the
            # handoff built-ins so the pipeline LLM sees it too.
            self.agent = _inject_consult_tool(self.agent)
            combined_tools = self._build_combined_pipeline_tools()
            tool_executor = ToolExecutor() if combined_tools else None
            llm_model = self.agent.model
            if "realtime" in llm_model:
                llm_model = "gpt-4o-mini"
            self._llm_loop = LLMLoop(
                openai_key=self._openai_key,
                model=llm_model,
                system_prompt=self.resolved_prompt,
                tools=combined_tools,
                tool_executor=tool_executor,
                llm_provider=agent_llm,
                metrics=self.metrics,
                event_bus=self._event_bus,
                disable_phone_preamble=getattr(
                    self.agent, "disable_phone_preamble", False
                ),
                on_tool_call=self._record_tool_call,
            )

        # Create remote message handler once if on_message is a remote URL
        from getpatter.services.remote_message import (
            RemoteMessageHandler,
            is_remote_url,
        )

        if is_remote_url(self.on_message):
            self._remote_handler = RemoteMessageHandler()

        # Start STT receive loop. If we kicked off the WS connect in
        # parallel with the firstMessage TTS, make sure that connect
        # has completed before the receive loop starts polling — a
        # half-open WS would surface "Not connected. Call connect()
        # first." on the first audio frame.
        if self._stt_connect_task is not None:
            try:
                await self._stt_connect_task
            except Exception as exc:  # noqa: BLE001
                logger.error("STT connect failed: %s", exc)
                # Tear down the call cleanly — we can't proceed with
                # transcription. The carrier-side pump will see the
                # closed WS and end the call.
                if self._hangup_fn is not None:
                    try:
                        await self._hangup_fn()
                    except Exception:
                        pass
                return
            finally:
                self._stt_connect_task = None
        if self._stt is not None:
            self._stt_task = asyncio.create_task(self._stt_loop())

    def _build_hook_context(self) -> HookContext:
        """Build a HookContext for the current call state."""
        return HookContext(
            call_id=self.call_id,
            caller=self.caller,
            callee=self.callee,
            history=tuple(self.conversation_history),
        )

    def _build_combined_pipeline_tools(self) -> list[dict]:
        """Build the full pipeline tool list for the CURRENT ``self.agent``:
        user tools + built-in ``transfer_call`` / ``end_call`` + the
        ``handoff_to`` tool when handoff targets are configured. Re-invoked
        after a handoff so the LLM loop advertises the target agent's tools
        (including its onward handoff map)."""
        combined = _augment_with_builtin_handoff_tools(
            self.agent.tools,
            transfer_fn=self._transfer_fn,
            hangup_fn=self._hangup_fn,
        )
        if getattr(self.agent, "handoffs", None):
            combined.append(
                {
                    **build_handoff_tool(self.agent.handoffs.keys()),
                    "handler": self._handoff_tool_handler,
                }
            )
        return combined

    async def _handoff_tool_handler(self, arguments: dict, call_context: dict) -> str:
        """Handler closure for the built-in ``handoff_to`` tool (pipeline)."""
        args = arguments or {}
        return await self._perform_handoff(
            args.get("name", ""), args.get("reason") or ""
        )

    async def _perform_handoff(self, name: str, reason: str) -> str:
        """Swap the live pipeline call to the named handoff target agent.

        Updates ``self.agent`` (frozen dataclass → ``dataclasses.replace``
        inside :func:`_apply_handoff_target`), swaps the LLM loop's system
        prompt + tool list so the NEXT turn runs as the target agent, and
        appends a system-style history entry recording the handoff. ALWAYS
        returns a tool-result string — an unknown name produces an error
        envelope, never silence.

        Live audio infrastructure (STT/TTS/VAD instances — and therefore the
        speaking voice) established at call start is intentionally retained:
        swapping a connected TTS provider mid-call is not supported in v1.
        An INFO log is emitted when the target requested a different voice.
        """
        handoffs: dict = getattr(self.agent, "handoffs", None) or {}
        target = handoffs.get(name)
        if target is None:
            return json.dumps(
                {
                    "error": f"Unknown handoff agent {name!r}",
                    "available": sorted(handoffs.keys()),
                }
            )
        if target.voice and target.voice != self.agent.voice:
            logger.info(
                "handoff_to %r: voice change is not supported mid-call in "
                "pipeline mode (the TTS adapter is already connected) — "
                "keeping the current voice.",
                name,
            )
        self.agent = _inject_consult_tool(_apply_handoff_target(self.agent, target))
        self.resolved_prompt = resolve_agent_prompt(self.agent)
        if self._llm_loop is not None:
            self._llm_loop.update_agent(
                system_prompt=self.resolved_prompt,
                tools=self._build_combined_pipeline_tools(),
                disable_phone_preamble=getattr(
                    self.agent, "disable_phone_preamble", False
                ),
            )
        handoff_text = _handoff_history_text(name, reason)
        self.conversation_history.append(
            {"role": "system", "text": handoff_text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "system", "text": handoff_text})
        if self.on_transcript is not None:
            await self.on_transcript(
                {
                    "role": "system",
                    "text": handoff_text,
                    "call_id": self.call_id,
                }
            )
        return json.dumps({"status": "handed_off", "to": name})

    async def _emit_assistant_transcript(self, text: str) -> None:
        """Push an assistant turn into history+transcript_entries and fire
        ``on_transcript`` so host applications observe pipeline-mode
        replies the same way they observe realtime-mode replies (mirrors
        :meth:`OpenAIRealtimeStreamHandler._flush_assistant_turn`).
        Caller is responsible for filtering empty strings.
        """
        self.conversation_history.append(
            {"role": "assistant", "text": text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "assistant", "text": text})
        if self.on_transcript is not None:
            await self._safe_on_transcript(
                {
                    "role": "assistant",
                    "text": text,
                    "call_id": self.call_id,
                    "history": list(self.conversation_history),
                }
            )

    async def _record_tool_call(self, name: str, arguments: dict, result: Any) -> None:
        """Surface a tool invocation into the transcript timeline. Emits
        TWO events: one ``role=tool`` entry for the call and a second one
        for the result (mirrors realtime-mode's two ``_emit_tool_event``
        calls in :meth:`OpenAIRealtimeStreamHandler._forward_events`).
        Wired as the :class:`LLMLoop` ``on_tool_call`` observer for
        pipeline mode.
        """
        try:
            args_text = json.dumps(arguments or {})
        except (TypeError, ValueError):
            args_text = "{}"
        # Coerce non-string results (e.g. providers that return a dict) to
        # JSON for the transcript display; the LLM has already received
        # the executor's raw return value via the messages array.
        if result is None:
            result_text: str | None = None
        elif isinstance(result, str):
            result_text = result
        else:
            try:
                result_text = json.dumps(result)
            except (TypeError, ValueError):
                result_text = str(result)

        # 1) Call event — transcript shows ``name(args_json)``
        call_text = f"{name}({args_text})"
        self.conversation_history.append(
            {"role": "tool", "text": call_text, "timestamp": time.time()}
        )
        self.transcript_entries.append({"role": "tool", "text": call_text})
        if self.on_transcript is not None:
            await self._safe_on_transcript(
                {
                    "role": "tool",
                    "text": call_text,
                    "call_id": self.call_id,
                    "tool_name": name,
                    "tool_args": arguments or {},
                    "tool_result": None,
                }
            )

        # 2) Result event — transcript shows ``name(...) → result`` (truncated)
        if result_text is not None:
            displayed = (
                result_text if len(result_text) <= 200 else result_text[:200] + "…"
            )
            res_text = f"{name}(...) → {displayed}"
            self.conversation_history.append(
                {"role": "tool", "text": res_text, "timestamp": time.time()}
            )
            self.transcript_entries.append({"role": "tool", "text": res_text})
            if self.on_transcript is not None:
                await self._safe_on_transcript(
                    {
                        "role": "tool",
                        "text": res_text,
                        "call_id": self.call_id,
                        "tool_name": name,
                        "tool_args": arguments or {},
                        "tool_result": result_text,
                    }
                )

    async def _synthesize_sentence(
        self,
        sentence: str,
        hook_executor: PipelineHookExecutor,
        hook_ctx: HookContext,
        first_tts_chunk: list,
        record_segment: bool = True,
    ) -> bool:
        """Synthesize a single sentence through TTS with hooks. Returns False if interrupted.

        ``record_segment=False`` (filler / error-fallback audio) advances the
        playback clock without adding a heard-prefix segment — that audio is
        not part of the LLM's reply. See ``_heard_response_prefix``.
        """
        if self._tts is None:
            return True

        # Apply text transforms before the beforeSynthesize hook
        transformed = sentence
        text_transforms = getattr(self.agent, "text_transforms", None)
        if text_transforms:
            for fn in text_transforms:
                transformed = fn(transformed)

        # beforeSynthesize hook (per-sentence)
        processed = await hook_executor.run_before_synthesize(transformed, hook_ctx)
        if processed is None:
            return True  # hook skipped this sentence, not an interruption

        _tts_span = start_span(
            SPAN_TTS,
            {
                "getpatter.tts.text_len": len(processed),
                "patter.call.id": self.call_id,
            },
        )
        _tts_span.__enter__()
        gen = self._tts.synthesize(processed)
        # Drop any stale PCM16 alignment carry byte between sentences — TTS
        # providers yield arbitrary-length chunks, so an odd byte from the
        # previous sentence would corrupt the first sample of this one.
        # Matches TS ``ttsByteCarry = null`` reset at each synth boundary.
        self.audio_sender.reset_pcm_carry()
        # Pause-and-resume retention: in ``barge_in_mode="pause_resume"``
        # every chunk of a RESPONSE sentence is kept in a per-sentence
        # entry so a paused turn can re-send the cleared-but-unheard tail
        # at resume time without re-billing TTS. ``None`` (legacy mode /
        # filler audio / post-overflow) keeps the direct send path
        # byte-identical to today.
        entry = self._begin_retained_sentence(processed) if record_segment else None
        try:
            async for audio_chunk in gen:
                if not self._is_speaking:
                    return False  # caller handles interrupted metrics

                # afterSynthesize hook (per-chunk). The await may yield
                # control to the event loop long enough for VAD to fire
                # ``speech_start during TTS → BARGE-IN``, which calls
                # ``_cancel_speaking()`` and flips ``_is_speaking`` to
                # False. Re-check below before pushing the resulting
                # audio to the carrier — without this re-check, exactly
                # one trailing chunk (~20–100 ms of audio) would race
                # past the cancel and prolong the perceived "agent
                # didn't stop" window.
                processed_audio = await hook_executor.run_after_synthesize(
                    audio_chunk, processed, hook_ctx
                )
                if processed_audio is None:
                    continue  # hook discarded this chunk
                if not self._is_speaking:
                    return False  # barge-in fired during the hook await

                if first_tts_chunk[0] and not getattr(self, "_output_paused", False):
                    # Flip the per-turn "first PCM chunk emitted" flag BEFORE
                    # the metrics branch so it is a reliable "audio reached the
                    # carrier" signal even when ``self.metrics is None`` — the
                    # llm_error_message fallback gate depends on it. While the
                    # pause gate holds the chunk in memory it has NOT reached
                    # the carrier — the flag (and the audio_out speech event)
                    # waits for the first post-resume chunk.
                    first_tts_chunk[0] = False
                    if self.metrics is not None:
                        self.metrics.record_tts_first_byte()
                    # Speech-event: per-turn first TTS audio chunk. Idempotent
                    # in the dispatcher; fires for the first sentence's first
                    # synthesized chunk per turn.
                    await self._emit_audio_out()
                if self._event_bus is not None:
                    self._event_bus.emit(
                        "tts_chunk",
                        {"bytes": len(processed_audio)},
                    )
                # Pause-and-resume retention path: the chunk is appended to
                # the sentence's entry; while paused it stays queued, while
                # speaking it is drained (sent) immediately. Segment
                # stamping / AEC tap / playback tracking live in
                # ``_drain_sentence_entry`` so they fire at SEND time, not
                # at synthesis time.
                if entry is not None and getattr(
                    self, "_pause_resume_overflowed", False
                ):
                    entry = None  # retention released mid-sentence
                if entry is not None:
                    if await self._retain_pause_chunk(entry, processed_audio):
                        if not getattr(self, "_output_paused", False):
                            await self._drain_sentence_entry(entry)
                            if not self._is_speaking:
                                return False  # cancel raced the drain
                        continue
                    if not self._is_speaking:
                        # Overflow while paused degraded to a full cancel.
                        return False
                    # Overflow while speaking: retention released — fall
                    # through to the direct send path for this chunk and
                    # the rest of the turn. The sentence keeps its already
                    # stamped segment (if any); the inline stamp below is
                    # skipped to avoid a duplicate.
                    entry = None
                    record_segment = False
                if getattr(self, "_output_paused", False):
                    # Paused with no retention entry (filler / error-
                    # fallback audio): drop the chunk — replaying
                    # moment-filling audio after a pause is pointless.
                    continue
                # Far-end tap for the echo canceller. On the default path
                # ``processed_audio`` is the exact PCM 16 kHz bytes that get
                # transcoded + sent to the carrier — the cleanest reference
                # of "what the speaker is about to play". Push BEFORE
                # ``send_audio`` so a very fast carrier echo is still seen by
                # the next mic frame. SKIPPED on the carrier-native fast path
                # — there these are mulaw 8 kHz wire bytes, which corrupted
                # the int16-PCM-16k reference (and odd-length chunks crashed
                # np.frombuffer mid-turn, misreported as an LLM error).
                if self._aec is not None and not getattr(
                    self, "_tts_output_format_native_for_carrier", False
                ):
                    self._aec.push_far_end(processed_audio)
                # Local-recording tap (agent side) — decodes on the
                # carrier-native μ-law fast path instead of skipping.
                self._tap_pipeline_agent_audio(processed_audio)
                if record_segment:
                    # First audible chunk of this sentence — stamp its start
                    # on the per-turn playback timeline so a barge-in can
                    # estimate the heard prefix at sentence granularity.
                    # ``getattr`` is defensive against test fixtures built
                    # via ``object.__new__`` (no ``__init__``).
                    segments = getattr(self, "_turn_spoken_segments", None)
                    if segments is not None:
                        segments.append(
                            (processed, getattr(self, "_turn_playback_total_s", 0.0))
                        )
                    record_segment = False
                await self.audio_sender.send_audio(processed_audio)
                self._track_outbound_playback(len(processed_audio))
                self._mark_first_audio_sent()
        finally:
            await gen.aclose()
            _tts_span.__exit__(None, None, None)
            # Drop any partial int16 byte so cross-sentence corruption never
            # leaks past an exception / early return.
            self.audio_sender.reset_pcm_carry()
        return True

    def _schedule_long_turn_filler(
        self,
        first_tts_chunk: list,
        hook_executor: PipelineHookExecutor,
        hook_ctx: HookContext,
    ) -> "asyncio.Task | None":
        """Spawn the opt-in long-turn filler task, or ``None`` when disabled.

        Returns ``None`` (no task) when ``agent.long_turn_message`` is unset /
        empty — the default, byte-identical to today's behaviour. Otherwise
        returns a task that waits ``agent.long_turn_message_after_s`` seconds and
        then, IFF no audio has reached the carrier this turn
        (``first_tts_chunk[0]`` still ``True``) AND we still own the floor
        (``self._is_speaking``), synthesizes the filler ONCE via
        ``_synthesize_sentence``. Guards strictly on "no audio emitted yet" so it
        cannot double-speak; self-synthesis failure degrades to silence.
        """
        message = getattr(self.agent, "long_turn_message", None)
        if not message:
            return None
        after_s = getattr(self.agent, "long_turn_message_after_s", 4.0)

        async def _filler() -> None:
            try:
                await asyncio.sleep(after_s)
            except asyncio.CancelledError:
                # Cancelled before firing (real audio started / turn ended).
                raise
            # Fire at most once, only if the caller still heard SILENCE this
            # turn and we still hold the floor (no concurrent barge-in).
            if first_tts_chunk[0] and self._is_speaking:
                try:
                    # Filler audio is not part of the LLM's reply — advance
                    # the playback clock without a heard-prefix segment.
                    await self._synthesize_sentence(
                        message,
                        hook_executor,
                        hook_ctx,
                        first_tts_chunk,
                        record_segment=False,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover - defensive
                    logger.exception("long_turn_message filler synthesis failed")

        return asyncio.create_task(_filler())

    async def _cancel_long_turn_filler(self, task: "asyncio.Task | None") -> None:
        """Cancel the long-turn filler task and await its teardown.

        Idempotent and race-safe: a ``None`` / already-finished task is a no-op,
        ``CancelledError`` from the cancel is suppressed, and any exception the
        task raised before cancellation is swallowed (already logged inside the
        task). Returns ``None`` so callers can reassign the handle in one line.
        """
        if task is None:
            return None
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - defensive
            logger.debug(
                "long_turn_message filler task ended with error", exc_info=True
            )
        return None

    async def _process_streaming_response(self, result, call_id: str) -> str:
        """Process a streaming (async generator) response through TTS with sentence chunking."""
        chunker = SentenceChunker(
            aggressive_first_flush=getattr(self.agent, "aggressive_first_flush", False),
            language=getattr(self.agent, "language", "en"),
        )
        full_response_parts: list[str] = []
        await self._begin_speaking()
        first_tts_chunk = [True]
        llm_first_token_sent = [True]  # Fix 5: track LLM TTFT

        hooks = getattr(self.agent, "hooks", None)
        hook_executor = PipelineHookExecutor(hooks)
        hook_ctx = self._build_hook_context()

        # Opt-in long-turn filler: when the turn is SLOW (agent runtime running
        # tools/memory) and NO audio has reached the carrier yet, speak a short
        # filler instead of dead silence. Distinct from ``llm_error_message``
        # (that fires on an LLM ERROR; this fires on SLOWNESS). The task waits
        # ``long_turn_message_after_s`` then, IFF still no audio this turn AND we
        # still own the floor, synthesizes the filler ONCE. Cancelled the moment
        # real audio is emitted, on the error branch, and in the finally.
        long_turn_task = self._schedule_long_turn_filler(
            first_tts_chunk, hook_executor, hook_ctx
        )

        # NOTE: the per-turn ``_llm_cancel_event`` is reset at the TOP of
        # ``_dispatch_turn`` (before ``LLMLoop.run`` is handed the event), not
        # here. Recreating it at this point — after ``run`` already captured
        # the previous reference — used to leave the generator bound to a
        # different event object than the consumption loop reads, and left a
        # barge-in's set event leaking into the next turn. The event is *set*
        # by ``_handle_barge_in`` to break out of the loop below and close the
        # generator (propagating cancellation into the provider connection).

        interrupted = False
        llm_error = False
        _llm_span = start_span(
            SPAN_LLM,
            {"patter.call.id": self.call_id},
        )
        _llm_span.__enter__()
        try:
            try:
                async for token in result:
                    if self._llm_cancel_event.is_set():
                        interrupted = True
                        break
                    full_response_parts.append(token)
                    # Keep the echo-guard reference current as the agent speaks,
                    # so a barge-in transcript arriving mid-turn can be compared
                    # against what the agent has said SO FAR (echo lags the
                    # tokens, so this is already ahead of the bleed).
                    self._current_agent_spoken_text = "".join(full_response_parts)
                    # Fix 5: record LLM first-token (TTFT).
                    if llm_first_token_sent[0] and self.metrics is not None:
                        self.metrics.record_llm_first_token()
                        llm_first_token_sent[0] = False
                        # Speech-event: fire per-turn TTFT marker for SDK
                        # callback consumers. Idempotent in the dispatcher.
                        await self._emit_llm_first_token(
                            llm_provider=self._infer_llm_provider(),
                            model=self.agent.model,
                        )

                    sentences = chunker.push(token)
                    # pause_resume: a resume may have fired between tokens —
                    # speak the sentences buffered during the pause FIRST so
                    # the reply stays in order.
                    if not getattr(self, "_output_paused", False):
                        released = self._release_paused_sentences()
                        if released:
                            sentences = released + sentences
                    # Fix 3: mark first-sentence boundary for accurate tts_ms.
                    if sentences and self.metrics is not None and first_tts_chunk[0]:
                        self.metrics.record_llm_first_sentence()
                    for sentence in sentences:
                        if not self._is_speaking:
                            interrupted = True
                            break
                        # pause_resume: while output is paused, buffer the
                        # sentence (bounded) — spoken on resume, discarded
                        # on kill. Keeps consuming LLM tokens either way.
                        if await self._buffer_sentence_if_paused(sentence):
                            continue

                        blocked, guard_name = evaluate_guardrails(self.agent, sentence)
                        if blocked:
                            sentence = get_guardrail_replacement(self.agent, guard_name)

                        # Tier 2 — per-sentence after_llm transform. Runs
                        # between the chunker and TTS so PII redaction /
                        # persona overlay / refusal swap can edit individual
                        # sentences without buffering the full LLM response.
                        # Returning None drops the sentence silently.
                        if hook_executor.has_after_llm_sentence():
                            transformed = await hook_executor.run_after_llm_sentence(
                                sentence, hook_ctx
                            )
                            if transformed is None:
                                continue  # hook dropped this sentence
                            sentence = transformed

                        # Real audio is about to be synthesized — cancel the
                        # long-turn filler so it can never fire (or double-speak)
                        # once the agent's own reply has started. Cancelling
                        # before the await is race-safe: asyncio is single-
                        # threaded, so the filler coroutine cannot interleave
                        # between this cancel and the synthesis call.
                        long_turn_task = await self._cancel_long_turn_filler(
                            long_turn_task
                        )
                        if not await self._synthesize_sentence(
                            sentence, hook_executor, hook_ctx, first_tts_chunk
                        ):
                            interrupted = True
                            break

                    if interrupted:
                        break
            except Exception as exc:
                llm_error = True
                chunker.reset()  # discard partial content on LLM error
                logger.exception("LLM streaming error: %s", exc)
                # The turn errored — stop the filler so it cannot speak over the
                # (distinct) error fallback below.
                long_turn_task = await self._cancel_long_turn_filler(long_turn_task)
                # Close the active turn as interrupted so the metrics accumulator
                # does not leak an open turn when LLM throws mid-stream.
                if self.metrics is not None and self.metrics.turn_active:
                    self.metrics.record_turn_interrupted()

                # Opt-in spoken fallback: when the LLM stream raised BEFORE any
                # assistant audio was emitted this turn and the agent configured
                # a non-empty ``llm_error_message``, speak that line through the
                # normal TTS turn lifecycle (subject to barge-in). Gated on
                # ``first_tts_chunk[0]`` — still ``True`` means no PCM chunk has
                # been sent to the carrier yet, i.e. the caller heard SILENCE —
                # rather than on token receipt, so a provider that streams
                # partial tokens ('Let me check…') and then times out before a
                # sentence boundary (the chunker never produced a complete
                # sentence, so TTS never ran) still triggers the fallback. Also
                # gated on ``_is_speaking`` so a concurrent barge-in that flipped
                # the floor does not get talked over. Wrapped in its own guard so
                # a TTS outage on top of an LLM outage degrades to today's
                # silence rather than raising out of the handler.
                fallback = getattr(self.agent, "llm_error_message", None)
                if fallback and first_tts_chunk[0] and self._is_speaking:
                    try:
                        # Error-fallback audio is not part of the LLM's reply
                        # — no heard-prefix segment.
                        await self._synthesize_sentence(
                            fallback,
                            hook_executor,
                            hook_ctx,
                            first_tts_chunk,
                            record_segment=False,
                        )
                    except Exception:  # pragma: no cover - defensive
                        logger.exception("llm_error_message fallback synthesis failed")

            if self.metrics is not None:
                self.metrics.record_llm_complete()

            # Flush remaining text from chunker (skip if LLM errored). The
            # outer loop exists for pause_resume: the turn must not end
            # while a pause decision is outstanding — buffered sentences
            # are spoken on resume; a kill marks the turn interrupted. Each
            # wait is bounded by the confirm window (the resume timer
            # guarantees a decision), and legacy mode never pauses so the
            # loop runs exactly once — byte-identical behaviour.
            if not llm_error and not interrupted:
                pending_sentences = chunker.flush()
                while True:
                    for sentence in pending_sentences:
                        if not self._is_speaking:
                            interrupted = True
                            break
                        if await self._buffer_sentence_if_paused(sentence):
                            continue

                        blocked, guard_name = evaluate_guardrails(self.agent, sentence)
                        if blocked:
                            sentence = get_guardrail_replacement(self.agent, guard_name)

                        if hook_executor.has_after_llm_sentence():
                            transformed = await hook_executor.run_after_llm_sentence(
                                sentence, hook_ctx
                            )
                            if transformed is None:
                                continue
                            sentence = transformed

                        # Real flushed audio about to play — cancel the filler.
                        long_turn_task = await self._cancel_long_turn_filler(
                            long_turn_task
                        )
                        if not await self._synthesize_sentence(
                            sentence, hook_executor, hook_ctx, first_tts_chunk
                        ):
                            interrupted = True
                            break
                    if interrupted:
                        break
                    if not (
                        getattr(self, "_output_paused", False)
                        or getattr(self, "_paused_sentences", None)
                    ):
                        break
                    if not await self._await_pause_decision():
                        interrupted = True
                        break
                    pending_sentences = self._release_paused_sentences()
        finally:
            # Ensure the long-turn filler task never outlives the turn (clean
            # cancellation, CancelledError suppressed inside the helper).
            await self._cancel_long_turn_filler(long_turn_task)
            # Schedule the flip to idle. Keeps the speaking flag set during
            # the audio tail still playing on the carrier so STT echo on
            # the trailing samples doesn't look like a fresh user turn.
            await self._end_speaking_with_grace()
            # If a barge-in cut us off mid-stream, close the LLM generator
            # so the underlying HTTP/WS connection releases any tokens we
            # would never speak. Best-effort — generators that already
            # exhausted normally are no-ops on aclose().
            if interrupted and hasattr(result, "aclose"):
                try:
                    await result.aclose()
                except Exception:  # pragma: no cover - defensive
                    pass
            try:
                _llm_span.__exit__(None, None, None)
            except Exception:  # pragma: no cover - defensive
                pass

        response_text = "".join(full_response_parts)

        if not interrupted and not llm_error and response_text:
            if self.metrics is not None:
                self.metrics.record_tts_complete(response_text)
                turn = self.metrics.record_turn_complete(response_text)
                await self._emit_turn_metrics(turn, call_id=call_id)
        # Tell the caller (``_dispatch_turn``) whether this turn was cut short so
        # the spoken prefix is recorded in history WITH a marker. A stateful
        # agent runtime (Hermes/OpenClaw) then sees, on the next turn, that it
        # was interrupted and what the caller actually heard — instead of an
        # ungrounded full reply that pollutes its context.
        self._last_response_interrupted = interrupted
        if interrupted and response_text:
            # Truncate to what the caller actually HEARD, not everything the
            # LLM generated. An agent-runtime LLM delivers the full reply at
            # once, so by barge-in time ``full_response_parts`` can hold tens
            # of seconds of text the caller never listened to — recording it
            # would make a stateful runtime believe it was all said. Falls
            # back to the legacy full-text marker when no playback segments
            # were tracked (e.g. no TTS configured).
            heard = self._heard_response_prefix()
            if heard is not None:
                heard_text, _heard_everything = heard
                response_text = (
                    f"{heard_text} [interrupted by caller]"
                    if heard_text
                    else "[interrupted by caller]"
                )
            else:
                response_text = f"{response_text} [interrupted by caller]"
        return response_text

    async def _process_regular_response(self, response_text: str, call_id: str) -> None:
        """Process a regular (non-streaming) response through TTS."""
        if self.metrics is not None:
            self.metrics.record_llm_complete()

        if not response_text:
            return

        # Guardrails check (pipeline mode — was previously missing)
        blocked, guard_name = evaluate_guardrails(self.agent, response_text)
        if blocked:
            response_text = get_guardrail_replacement(self.agent, guard_name)

        await self._emit_assistant_transcript(response_text)
        # Echo-guard reference: only the streaming path populated it, so
        # under forward-STT-while-speaking the echo of non-streaming
        # (on_message / webhook) replies compared against an empty string
        # and was committed as a phantom user turn.
        self._current_agent_spoken_text = response_text
        # Use sentence chunking + hooks for consistent behavior with streaming path
        hooks = getattr(self.agent, "hooks", None)
        hook_executor = PipelineHookExecutor(hooks)
        hook_ctx = self._build_hook_context()

        chunker = SentenceChunker()
        sentences = chunker.push(response_text) + chunker.flush()
        if not sentences:
            sentences = [response_text] if response_text else []

        await self._begin_speaking()
        first_tts_chunk = [True]
        interrupted = False
        try:
            # Outer loop mirrors ``_process_streaming_response``: in
            # pause_resume mode the turn waits out an in-flight pause
            # decision (buffered sentences speak on resume, a kill marks
            # interrupted); legacy mode never pauses → single pass.
            pending_sentences = sentences
            while True:
                for sentence in pending_sentences:
                    if not self._is_speaking:
                        interrupted = True
                        break
                    if await self._buffer_sentence_if_paused(sentence):
                        continue
                    if not await self._synthesize_sentence(
                        sentence, hook_executor, hook_ctx, first_tts_chunk
                    ):
                        interrupted = True
                        break
                if interrupted:
                    break
                if not (
                    getattr(self, "_output_paused", False)
                    or getattr(self, "_paused_sentences", None)
                ):
                    break
                if not await self._await_pause_decision():
                    interrupted = True
                    break
                pending_sentences = self._release_paused_sentences()
        finally:
            # Schedule the flip to idle (see ``_process_streaming_response``).
            await self._end_speaking_with_grace()

        if not interrupted:
            if self.metrics is not None:
                self.metrics.record_tts_complete(response_text)
                turn = self.metrics.record_turn_complete(response_text)
                await self._emit_turn_metrics(turn, call_id=call_id)

    async def _handle_barge_in(self, transcript) -> None:
        """Decide whether ``transcript`` confirms a barge-in and run the
        cancel/flush path if so. Mirrors TS ``handleBargeIn``.

        The legacy contract — "any transcript while speaking cancels the
        agent" — applies when ``agent.barge_in_strategies`` is empty.
        With one or more strategies configured, the transcript is fed
        to :func:`evaluate_strategies` and the cancel only runs when at
        least one strategy approves; otherwise the agent keeps talking.
        """
        if not (transcript.text and self._is_speaking):
            return
        # Echo guard FIRST — before the tail-grace rescue: the grace window
        # (the ~1.5 s after TTS ends) is exactly when the agent's
        # final-sentence echo arrives via STT. Running the rescue first
        # treated that echo as "the next turn", flipped speaking state off,
        # and the downstream isSpeaking-gated echo check could no longer
        # fire — the agent answered its own words as a phantom user turn.
        # Active under ``_forward_stt_while_speaking`` (the only path that
        # feeds TTS audio to STT) AND while output is paused (pause_resume
        # forwards mic audio to STT during the confirm window, and the
        # just-cleared audio's PSTN echo tail can lag into it), so the
        # default VAD path is unaffected. Mirrors TS ``handleBargeIn``.
        if (
            getattr(self, "_forward_stt_while_speaking", False)
            or getattr(self, "_output_paused", False)
        ) and _looks_like_echo(
            transcript.text, getattr(self, "_current_agent_spoken_text", "")
        ):
            logger.info(
                "Barge-in suppressed: transcript matches agent's own speech "
                "(echo) — %r",
                sanitize_log_value(transcript.text[:40]),
            )
            return
        # Near-duplicate / hallucination guard BEFORE cancelling: Deepgram
        # yields both the speech_final and a later is_final frame for the
        # same utterance — the twin arriving up to 2 s after dispatch found
        # the agent speaking, cancelled its brand-new turn, and was THEN
        # dropped as a duplicate by _commit_transcript (agent went silent
        # for the turn). Apply the same filters here, with the commit
        # window semantics (exact dup ≤2 s, near-dup ≤0.5 s).
        _normalised = transcript.text.strip().lower()
        _since_last = time.time() - getattr(self, "_last_commit_at", 0.0)
        if _is_stt_hallucination(_normalised):
            logger.debug(
                "Barge-in skipped: STT hallucination %r",
                sanitize_log_value(transcript.text[:40]),
            )
            return
        if _since_last < 2.0 and _normalised == getattr(
            self, "_last_commit_text", ""
        ):
            logger.debug(
                "Barge-in skipped: duplicate of just-committed transcript %r",
                sanitize_log_value(transcript.text[:40]),
            )
            return
        if _since_last < 0.5 and _is_near_duplicate(
            _normalised, getattr(self, "_last_commit_text", "")
        ):
            logger.debug(
                "Barge-in skipped: near-duplicate of just-committed transcript %r",
                sanitize_log_value(transcript.text[:40]),
            )
            return
        # Defensive ``getattr`` — test fixtures build the handler via
        # ``object.__new__`` and skip ``__init__`` (no tail-grace state).
        if getattr(self, "_tail_grace_active", False):
            # A transcript arriving during the post-TTS tail grace is the
            # next turn, not a barge-in (the agent already finished). End the
            # grace and return WITHOUT cancelling — the same transcript then
            # flows on to ``_commit_transcript``/``_dispatch_turn`` as a
            # normal new turn. Closes the race where a transcript lands
            # before the VAD speech_start rescue fires.
            await self._end_tail_grace_for_new_turn()
            return
        if not self._can_barge_in():
            aec_state = "on" if getattr(self, "_aec", None) is not None else "off"
            logger.info(
                "Barge-in transcript suppressed (agent speaking < gate, aec=%s)",
                aec_state,
            )
            return
        # Pause-and-resume: while output is paused, only a committed FINAL
        # transcript (non-hallucination, non-duplicate) may confirm the kill
        # — interims and noise wait for the resume timer instead. The
        # confirming transcript then continues through the strategy/legacy
        # decision below exactly as today.
        if getattr(self, "_output_paused", False) and not self._passes_paused_kill_filters(
            transcript
        ):
            logger.debug(
                "Paused turn: transcript %r cannot confirm the kill "
                "(interim/hallucination/duplicate) — awaiting resume timer",
                sanitize_log_value(transcript.text[:40]),
            )
            return
        strategies = getattr(self, "_barge_in_strategies", ()) or ()
        if strategies:
            from getpatter.services.barge_in_strategies import evaluate_strategies

            confirmed = await evaluate_strategies(
                strategies,
                transcript=transcript.text,
                is_interim=not getattr(transcript, "is_final", True),
                agent_speaking=self._is_speaking,
            )
            if not confirmed:
                logger.debug(
                    "Barge-in NOT confirmed by any strategy (transcript=%r); "
                    "agent continues talking",
                    sanitize_log_value(transcript.text[:40]),
                )
                return
            logger.info(
                "Barge-in confirmed by strategy on transcript %r",
                sanitize_log_value(transcript.text[:40]),
            )
        await self._do_cancel_for_barge_in(transcript.text)

    async def _do_cancel_for_barge_in(self, transcript_text: str) -> None:
        """Actually cancel the in-flight agent turn (TTS + LLM stream + ring).

        Split out of :meth:`_handle_barge_in` so the same cancel logic can
        run from the legacy "transcript = cancel" path AND the opt-in
        "strategy confirmed = cancel" path without duplication.
        """
        # Capture pending state BEFORE _clear_pending_barge_in() drops it —
        # if VAD already started the overlap window via
        # ``_start_pending_barge_in`` we MUST NOT call ``record_overlap_start``
        # again (that would overwrite T1 with T2 and produce a near-zero
        # ``InterruptionMetrics.detection_delay_ms`` on the strategy path).
        # ``getattr`` is defensive against test fixtures that build a
        # handler shell via ``object.__new__`` and don't initialise the
        # pending-barge-in state — the safe default is "no pending".
        had_pending = getattr(self, "_barge_in_pending_since", None) is not None
        self._clear_pending_barge_in()
        if self.metrics is not None:
            if not had_pending:
                # Legacy path or VAD never fired — start the overlap window now.
                self.metrics.record_overlap_start()
            self.metrics.record_bargein_detected()
        logger.debug(
            "Barge-in: caller spoke over agent (%s)",
            sanitize_log_value(transcript_text[:40]),
        )
        with start_span(
            SPAN_BARGEIN,
            {"patter.call.id": self.call_id},
        ):
            self._is_speaking = False
            self._tail_grace_active = False
            self._speaking_started_at = None
            self._first_audio_sent_at = None
            self._last_cancel_at = time.time()
            # A barge-in landing AFTER the turn completed (carrier still
            # draining the buffered tail) — rewrite the history to the heard
            # prefix FIRST, while the playback cursor still measures what
            # was left unheard (or, for a paused turn, while the frozen
            # pause cursor still does).
            self._maybe_truncate_completed_turn_history()
            # Pause-and-resume: a kill while paused discards the held
            # buffers (queued sentences + retained audio) and wakes any
            # pause-decision waiter, which then observes the interrupt.
            self._discard_pause_state()
            # The ``send_clear`` below drops whatever the carrier had
            # buffered ahead — snap the playback cursor back and kill any
            # pending grace task so its phase-1 wait (carrier backlog) /
            # tail-grace flag cannot fire against the cancelled turn.
            self._playback_buffered_until = 0.0
            self._clear_grace_task()
            # Unblock any firstMessage paced-send loop that's sitting in
            # ``_wait_for_mark_window`` — without this the loop keeps
            # awaiting echoes for up to ``_MARK_AWAIT_TIMEOUT_S`` per
            # outstanding mark before observing ``_is_speaking=False``,
            # which keeps the agent "speaking" from the user's perspective
            # for hundreds of extra ms after barge-in (BUG #128). Defensive
            # ``getattr`` is for test fixtures that build a handler shell
            # via ``object.__new__`` and skip ``__init__``.
            if getattr(self, "_pending_marks", None) is not None:
                self._drain_pending_marks()
            cancel_event = getattr(self, "_llm_cancel_event", None)
            if cancel_event is not None:
                cancel_event.set()
            # Force-close any in-flight TTS streaming socket. Without this,
            # the firstMessage live ``synthesize`` path (used when the prewarm
            # accumulator hadn't completed before pickup) would block on its
            # inner ``await ws.recv()`` for up to ``frame_timeout`` (30 s) —
            # ``_init_pipeline`` would never return, the STT ``on_transcript``
            # callback would never register, and every subsequent user turn
            # would be silently dropped. Provider-duck-typed: adapters that
            # don't expose ``cancel_active_stream`` are no-ops here.
            # Parity with TS ``StreamHandler.cancelSpeaking``.
            _tts = getattr(self, "_tts", None)
            _cancel_fn = getattr(_tts, "cancel_active_stream", None)
            if callable(_cancel_fn):
                try:
                    _cancel_fn()
                except Exception as _exc:
                    logger.debug("TTS cancel_active_stream raised: %s", _exc)
            try:
                await self.audio_sender.send_clear()
            except Exception as exc:
                logger.debug("send_clear during barge-in failed: %s", exc)
            # Speech-event: agent stop edge — interrupted by the caller.
            await self._emit_agent_speech_ended(interrupted=True)
            # Replay the self-hearing ring so the words the user spoke
            # BEFORE the confirming transcript reach STT (the models.py
            # contract for confirmed barge-ins promised this flush).
            try:
                await self._flush_inbound_audio_ring()
            except Exception as exc:  # noqa: BLE001 - best-effort replay
                logger.debug("barge-in ring flush failed: %s", exc)
            if self.metrics is not None:
                self.metrics.record_tts_stopped()
                self.metrics.record_turn_interrupted()
                # Re-anchor to legitimate VAD speech_start so post-barge-in
                # latency anchors don't carry from the interrupted turn.
                self.metrics.anchor_user_speech_start()
                self.metrics.record_overlap_end(was_interruption=True)

    async def _start_pending_barge_in(self) -> None:
        """Mark a VAD-detected barge-in as pending (no cancel yet).

        Only used when ``agent.barge_in_strategies`` is non-empty. The
        agent's TTS keeps streaming naturally; an
        :meth:`_pending_barge_in_timeout` task will drop the pending
        state if no strategy confirms within ``_barge_in_confirm_s``.
        """
        if self._barge_in_pending_since is not None:
            return
        self._barge_in_pending_since = time.time()
        if self.metrics is not None:
            self.metrics.record_overlap_start()
        logger.info(
            "Barge-in PENDING (VAD speech_start during TTS); awaiting strategy confirmation"
        )
        # Replay the ring NOW: the strategies confirm on transcripts, so STT
        # must see the user's leading words while the pending window runs
        # (on_audio_received also forwards live frames while pending).
        try:
            await self._flush_inbound_audio_ring()
        except Exception as exc:  # noqa: BLE001 - best-effort replay
            logger.debug("pending barge-in ring flush failed: %s", exc)
        try:
            self._barge_in_pending_task = asyncio.create_task(
                self._pending_barge_in_timeout()
            )
        except RuntimeError as exc:  # pragma: no cover - no running loop
            logger.debug("could not schedule pending barge-in timeout: %s", exc)
            self._barge_in_pending_task = None

    async def _pending_barge_in_timeout(self) -> None:
        try:
            await asyncio.sleep(self._barge_in_confirm_s)
        except asyncio.CancelledError:
            return
        if self._barge_in_pending_since is None:
            return
        logger.info(
            "Pending barge-in timed out after %.2fs; agent resumes (no strategy confirmed)",
            self._barge_in_confirm_s,
        )
        if self.metrics is not None:
            self.metrics.record_overlap_end(was_interruption=False)
            # Re-anchor to legitimate VAD speech_start so anchors that drifted
            # during the pending barge-in window don't pollute the next turn.
            self.metrics.anchor_user_speech_start()
        self._barge_in_pending_since = None
        self._barge_in_pending_task = None

    def _clear_pending_barge_in(self) -> None:
        """Drop pending state without cancelling — used on confirm and on
        agent stop. Idempotent and safe to call from test fixtures that
        construct the handler via ``object.__new__`` (no __init__)."""
        task = getattr(self, "_barge_in_pending_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._barge_in_pending_task = None
        self._barge_in_pending_since = None

    # ---------------------------------------------------------------
    # Pause-and-resume false-interruption handling (barge_in_mode =
    # "pause_resume"): PAUSE output on VAD speech_start,
    # KILL on a committed final transcript within the confirm window,
    # RESUME from the first not-fully-heard sentence otherwise.
    # ---------------------------------------------------------------

    # Cap on sentences buffered as text while output is paused. A pause
    # lasts at most ``_barge_in_confirm_s`` (1.5 s default) so this is
    # generous; an agent-runtime LLM that delivers its whole reply at once
    # can exceed it — overflow degrades to a full cancel so memory stays
    # bounded. Mirrors TS ``PAUSE_MAX_BUFFERED_SENTENCES``.
    _PAUSE_MAX_BUFFERED_SENTENCES: int = 32
    # Cap (seconds of playout) on retained per-sentence TTS audio — both
    # the already-sent tail kept for re-send and chunks queued while
    # paused. 15 s ≈ 480 KB of PCM16 @ 16 kHz per concurrent call.
    # Overflow while paused → degrade to full cancel; overflow while
    # speaking (very long carrier backlog) → release the retention and
    # fall back to legacy cancel behaviour for the rest of the turn.
    # Mirrors TS ``PAUSE_RESUME_MAX_RETAINED_S``.
    _PAUSE_RESUME_MAX_RETAINED_S: float = 15.0

    def _pause_retained_cap_bytes(self) -> int:
        """Retained-audio cap in bytes for the active TTS chunk format
        (mirrors the bytes-per-second logic of ``_track_outbound_playback``)."""
        bytes_per_s = (
            8_000.0
            if getattr(self.audio_sender, "_input_is_mulaw_8k", False)
            else 32_000.0
        )
        return int(self._PAUSE_RESUME_MAX_RETAINED_S * bytes_per_s)

    def _should_pause_for_barge_in(self) -> bool:
        """Whether a VAD ``speech_start`` during the agent's turn should take
        the pause-and-resume path instead of cancel/pending.

        Requires ``barge_in_mode="pause_resume"`` AND resumable state: a
        dispatch in flight (the sentence/TTS loops honour the pause gate) or
        retained sentence audio from a just-completed turn still playing out
        of the carrier buffer. The firstMessage paced sender keeps today's
        immediate-cancel behaviour (its prewarm-bytes path has no retained
        sentences to resume from — known limitation, see ``_start_pause_resume``).
        """
        if getattr(self, "_barge_in_mode", "cancel") != "pause_resume":
            return False
        if getattr(self, "_pause_resume_overflowed", False):
            return False
        if getattr(self, "_output_paused", False):
            return True  # already paused — stay on the pause path (idempotent)
        dispatch = getattr(self, "_dispatch_task", None)
        if dispatch is not None and not dispatch.done():
            return True
        return bool(getattr(self, "_turn_sentence_audio", None))

    def _compute_pause_resume_point(self) -> tuple[int, float]:
        """Resume offset at SENTENCE granularity.

        Returns ``(index, heard_s)`` where ``index`` is the first sentence
        (into ``_turn_spoken_segments`` / ``_turn_sentence_audio``) whose
        playback had NOT completed when the pause landed — computed from the
        #164 playback-cursor bookkeeping: ``heard = total_pushed -
        carrier_backlog``. Granularity choice: the partially-played sentence
        is replayed from its start (mark/clear bookkeeping is per-sentence
        and a clipped sentence restarted at its boundary sounds like a
        natural repair), rather than resumed mid-word at byte offset.
        """
        segments = getattr(self, "_turn_spoken_segments", None) or []
        total_s = getattr(self, "_turn_playback_total_s", 0.0)
        remaining_s = max(
            0.0, getattr(self, "_playback_buffered_until", 0.0) - time.time()
        )
        heard_s = max(0.0, total_s - remaining_s)
        idx = len(segments)
        for i in range(len(segments) - 1, -1, -1):
            end_s = segments[i + 1][1] if i + 1 < len(segments) else total_s
            if end_s > heard_s + 1e-6:
                idx = i
            else:
                break
        return idx, heard_s

    async def _start_pause_resume(self) -> None:
        """PAUSE the agent's output on a VAD ``speech_start`` (pause_resume
        mode): gate further sends on ``_output_paused``, ``send_clear`` the
        carrier so queued audio stops quickly, and schedule the
        false-interruption resume timer. The LLM stream and the TTS
        provider stream are deliberately NOT cancelled — tokens keep
        buffering as sentences and synthesized audio queues in memory (both
        bounded) so a resume can pick up seamlessly.
        """
        if getattr(self, "_output_paused", False):
            return
        # Anchor the overlap window exactly like ``_start_pending_barge_in``
        # so a kill records detection_delay from VAD-T1 (never restarted).
        if getattr(self, "_barge_in_pending_since", None) is None:
            self._barge_in_pending_since = time.time()
            if self.metrics is not None:
                self.metrics.record_overlap_start()
        # A stale strategy-pending timer is superseded by the pause timer.
        task = getattr(self, "_barge_in_pending_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._barge_in_pending_task = None
        self._output_paused = True
        self._pause_decision_event = asyncio.Event()
        # Freeze the playback bookkeeping at the heard offset: the clear
        # below drops the carrier backlog, so anything pushed beyond the
        # heard cursor is void. A kill that follows then computes the
        # heard prefix from this frozen state; a resume re-advances it as
        # the tail is re-sent.
        idx, heard_s = self._compute_pause_resume_point()
        self._pause_resume_index = idx
        self._turn_playback_total_s = heard_s
        self._playback_buffered_until = 0.0
        # The phase-1 grace wait (carrier backlog) is void after the clear;
        # resume re-arms it for the re-sent tail.
        self._clear_grace_task()
        if getattr(self, "_pending_marks", None):
            self._drain_pending_marks()
        logger.info(
            "Barge-in PAUSE (VAD speech_start during TTS); resuming from "
            "sentence %d unless a transcript confirms within %.2fs",
            idx,
            getattr(self, "_barge_in_confirm_s", 1.5),
        )
        try:
            await self.audio_sender.send_clear()
        except Exception as exc:
            logger.debug("send_clear during pause failed: %s", exc)
        # Output is silent from here — flush the self-hearing ring so STT
        # receives the user's leading words and can produce the confirming
        # transcript (or nothing, for a cough). ``on_audio_received``
        # forwards subsequent frames to STT while paused for the same reason.
        await self._flush_inbound_audio_ring()
        try:
            self._barge_in_pending_task = asyncio.create_task(
                self._pause_resume_timeout()
            )
        except RuntimeError as exc:  # pragma: no cover - no running loop
            logger.debug("could not schedule pause-resume timeout: %s", exc)
            self._barge_in_pending_task = None

    async def _pause_resume_timeout(self) -> None:
        """Fire the false-interruption resume when no transcript confirmed
        the pause within ``_barge_in_confirm_s``."""
        try:
            await asyncio.sleep(self._barge_in_confirm_s)
        except asyncio.CancelledError:
            return
        if not getattr(self, "_output_paused", False):
            return
        await self._resume_after_false_interruption()

    async def _resume_after_false_interruption(self) -> None:
        """RESUME output after a pause that no transcript confirmed.

        Re-sends the cleared-but-unheard tail from the retained sentence
        audio (sentence granularity, no TTS re-billing), unpauses the live
        send path, and records the event as a FALSE interruption: the
        overlap closes via ``record_overlap_end(was_interruption=False)``
        (the backchannel counter — the interruption count is NOT
        incremented) and the turn is never marked interrupted.
        """
        if not getattr(self, "_output_paused", False):
            return
        entries = getattr(self, "_turn_sentence_audio", None) or []
        idx = max(0, min(getattr(self, "_pause_resume_index", 0), len(entries)))
        tail = entries[idx:]
        # Drop the stale segment stamps of the sentences about to be
        # replayed — the replay re-stamps them at their new positions on
        # the (frozen-then-resumed) playback timeline, so a later barge-in
        # still maps to an accurate heard prefix without duplicates.
        segments = getattr(self, "_turn_spoken_segments", None)
        if segments is not None:
            del segments[idx:]
        for entry in tail:
            entry["sent"] = 0
        if self.metrics is not None:
            # False interruption — the backchannel path. Mirrors
            # ``_pending_barge_in_timeout``.
            self.metrics.record_overlap_end(was_interruption=False)
            self.metrics.anchor_user_speech_start()
        self._barge_in_pending_since = None
        self._barge_in_pending_task = None
        logger.info(
            "False interruption: no confirming transcript within %.2fs — "
            "resuming %d retained sentence(s)",
            getattr(self, "_barge_in_confirm_s", 1.5),
            len(tail),
        )
        if self._event_bus is not None:
            self._event_bus.emit(
                "false_interruption", {"resumed_sentences": len(tail)}
            )
        # Re-send the unheard tail BEFORE unpausing so the in-flight
        # synthesis (which queues while paused) cannot interleave a newer
        # chunk ahead of the replayed audio.
        for entry in tail:
            if not self._is_speaking:
                break
            # Sentence boundary — drop any stale PCM16 alignment carry, the
            # same contract ``_synthesize_sentence`` keeps per sentence.
            self.audio_sender.reset_pcm_carry()
            await self._drain_sentence_entry(entry, force=True)
        self._output_paused = False
        # Close the unpause race: a chunk queued between the last drain and
        # the flag flip would otherwise wait for the next live chunk.
        if tail and self._is_speaking:
            await self._drain_sentence_entry(tail[-1], force=True)
        evt = getattr(self, "_pause_decision_event", None)
        if evt is not None and not evt.is_set():
            evt.set()
        # Post-complete turn (carrier was draining the buffered tail when
        # the pause landed): the turn body already finished pushing — its
        # grace task was cancelled at pause time — so re-arm the grace
        # machinery for the re-sent backlog: phase-1 hold keeps barge-in
        # armed for the whole audible window, exactly as #164. A turn
        # still in flight arms it itself in its ``finally``.
        if getattr(self, "_turn_output_done", True) and self._is_speaking:
            await self._end_speaking_with_grace()

    def _discard_pause_state(self) -> None:
        """Drop all pause-and-resume state (flags + buffers) and wake any
        pause-decision waiter. Used by the kill path, fresh turns, and
        teardown. Idempotent; safe on ``object.__new__`` test fixtures."""
        self._output_paused = False
        self._pause_resume_index = 0
        paused = getattr(self, "_paused_sentences", None)
        if paused:
            paused.clear()
        entries = getattr(self, "_turn_sentence_audio", None)
        if entries:
            entries.clear()
        self._pause_retained_bytes = 0
        evt = getattr(self, "_pause_decision_event", None)
        if evt is not None and not evt.is_set():
            evt.set()

    async def _await_pause_decision(self) -> bool:
        """Block until the in-flight pause resolves. ``True`` → resumed
        (keep speaking); ``False`` → killed (turn interrupted). Bounded:
        fails open past the confirm window plus margin (the resume timer
        guarantees a decision; the margin covers teardown races)."""
        while getattr(self, "_output_paused", False) and self._is_speaking:
            evt = getattr(self, "_pause_decision_event", None)
            if evt is None:
                break
            try:
                await asyncio.wait_for(
                    evt.wait(),
                    timeout=getattr(self, "_barge_in_confirm_s", 1.5) + 5.0,
                )
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                logger.debug("pause decision wait timed out — failing open")
                break
        return self._is_speaking

    async def _buffer_sentence_if_paused(self, sentence: str) -> bool:
        """While paused, buffer ``sentence`` (pre-guardrail text) for the
        resume drain and return ``True``; return ``False`` when not paused
        (caller synthesizes normally). Overflow degrades to a full cancel —
        the bounded buffer is a memory-safety valve, not a speech queue."""
        if not getattr(self, "_output_paused", False):
            return False
        buf = getattr(self, "_paused_sentences", None)
        if buf is None:
            buf = self._paused_sentences = []
        if len(buf) >= self._PAUSE_MAX_BUFFERED_SENTENCES:
            logger.warning(
                "pause_resume sentence buffer overflow (%d) — degrading to full cancel",
                len(buf),
            )
            await self._do_cancel_for_barge_in("<pause_resume sentence-buffer overflow>")
            return True  # handled; the loop observes _is_speaking=False next
        buf.append(sentence)
        return True

    def _release_paused_sentences(self) -> list[str]:
        """Pop-and-return every sentence buffered during the pause."""
        buf = getattr(self, "_paused_sentences", None)
        if not buf:
            return []
        out = list(buf)
        buf.clear()
        return out

    def _begin_retained_sentence(self, text: str) -> dict | None:
        """Open a retention entry for a response sentence (pause_resume mode
        only — returns ``None`` otherwise, keeping the legacy send path
        byte-identical). Filler / error-fallback audio is never retained
        (``record_segment=False`` callers skip this)."""
        if getattr(self, "_barge_in_mode", "cancel") != "pause_resume":
            return None
        if getattr(self, "_pause_resume_overflowed", False):
            return None
        entries = getattr(self, "_turn_sentence_audio", None)
        if entries is None:
            entries = self._turn_sentence_audio = []
        entry = {"text": text, "chunks": [], "sent": 0}
        entries.append(entry)
        return entry

    async def _retain_pause_chunk(self, entry: dict, chunk: bytes) -> bool:
        """Append ``chunk`` to the sentence's retention entry, enforcing the
        retained-audio cap. Returns ``True`` when retained; ``False`` on
        overflow (paused → the turn was just killed; speaking → retention
        was released and the caller falls back to direct sends)."""
        entry["chunks"].append(chunk)
        self._pause_retained_bytes = (
            getattr(self, "_pause_retained_bytes", 0) + len(chunk)
        )
        if self._pause_retained_bytes <= self._pause_retained_cap_bytes():
            return True
        if getattr(self, "_output_paused", False):
            logger.warning(
                "pause_resume retained-audio cap (%.0fs) exceeded while paused — "
                "degrading to full cancel",
                self._PAUSE_RESUME_MAX_RETAINED_S,
            )
            await self._do_cancel_for_barge_in("<pause_resume audio-buffer overflow>")
        else:
            logger.info(
                "pause_resume retained-audio cap (%.0fs) exceeded — disabling "
                "pause-resume for this turn (legacy cancel applies)",
                self._PAUSE_RESUME_MAX_RETAINED_S,
            )
            self._pause_resume_overflowed = True
            self._pause_retained_bytes = 0
            entries = getattr(self, "_turn_sentence_audio", None) or []
            for e in entries:
                e["chunks"] = []
                e["sent"] = 0
        return False

    async def _drain_sentence_entry(self, entry: dict, force: bool = False) -> None:
        """Send every not-yet-sent chunk of a retention entry to the carrier
        (claim-then-send so concurrent drains can never double-send).
        Stamps the sentence's heard-prefix segment at its first sent chunk —
        a replay (``sent`` reset to 0) re-stamps at the new timeline
        position. ``force=True`` bypasses the pause gate (resume path only).
        """
        while entry["sent"] < len(entry["chunks"]):
            if not self._is_speaking:
                return
            if getattr(self, "_output_paused", False) and not force:
                return
            idx = entry["sent"]
            entry["sent"] = idx + 1
            chunk = entry["chunks"][idx]
            if idx == 0:
                segments = getattr(self, "_turn_spoken_segments", None)
                if segments is not None:
                    segments.append(
                        (entry["text"], getattr(self, "_turn_playback_total_s", 0.0))
                    )
            # Far-end tap mirrors the direct send path: SKIPPED on the
            # carrier-native fast path where these are mulaw 8 kHz wire
            # bytes that would corrupt the int16-PCM-16k AEC reference.
            if getattr(self, "_aec", None) is not None and not getattr(
                self, "_tts_output_format_native_for_carrier", False
            ):
                self._aec.push_far_end(chunk)
            # Local-recording tap (agent side) — decodes on the
            # carrier-native μ-law fast path instead of skipping.
            self._tap_pipeline_agent_audio(chunk)
            await self.audio_sender.send_audio(chunk)
            self._track_outbound_playback(len(chunk))
            self._mark_first_audio_sent()

    def _passes_paused_kill_filters(self, transcript) -> bool:
        """Whether a transcript may KILL a paused turn: it must be a
        committed FINAL (interims cannot confirm), not a known STT
        hallucination, and not a duplicate of the last committed utterance —
        the same filter family ``_commit_transcript`` applies, evaluated
        without consuming its dedup state (the transcript still flows on to
        ``_commit_transcript`` to become the user's next turn)."""
        if not (
            getattr(transcript, "is_final", False)
            or getattr(transcript, "speech_final", False)
        ):
            return False
        normalised = transcript.text.strip().lower()
        stripped = normalised.rstrip(".,!?;: ").strip()
        if stripped in _STT_HALLUCINATIONS or stripped == "":
            return False
        if (
            normalised == getattr(self, "_last_commit_text", "")
            and time.time() - getattr(self, "_last_commit_at", 0.0) < 2.0
        ):
            return False
        return True

    def _commit_transcript(self, text: str) -> bool:
        """Dedup + throttle + hallucination filter for final STT transcripts.

        Mirrors TS ``commitTranscript``. Returns ``True`` if the transcript
        should be committed to a turn, ``False`` if it must be dropped.
        Drop reasons: common hallucinations, the agent's own TTS echo (when
        forwarding audio to STT during TTS), exact duplicate within 2 s, or a
        near-duplicate within 500 ms (the same utterance double-emitted) — a
        genuinely different fast follow-up is NOT dropped.
        """
        now = time.time()
        normalised = text.strip().lower()
        stripped = normalised.rstrip(".,!?;: ").strip()
        since_last = now - self._last_commit_at

        if stripped in _STT_HALLUCINATIONS or stripped == "":
            logger.debug("Dropped likely STT hallucination: %r", normalised[:40])
            return False
        # Echo guard: while the agent is still speaking (the forward-STT echo
        # window — or a pause_resume confirm window, which forwards mic audio
        # to STT while the agent formally holds the floor), a transcript that
        # matches the agent's own speech is its TTS bleeding back into STT,
        # not a user turn. Gated on ``_is_speaking`` so a real post-turn
        # reply (committed when the agent is idle) is never dropped, and the
        # default VAD path — which withholds audio during TTS — is unaffected.
        if (
            (
                getattr(self, "_forward_stt_while_speaking", False)
                or getattr(self, "_output_paused", False)
            )
            and getattr(self, "_is_speaking", False)
            and _looks_like_echo(text, getattr(self, "_current_agent_spoken_text", ""))
        ):
            logger.debug(
                "Dropped agent-echo transcript (not a user turn): %r", normalised[:40]
            )
            return False
        if since_last < 2.0 and normalised == self._last_commit_text:
            logger.debug(
                "Dropped duplicate final transcript (%.1fs since last): %r",
                since_last,
                normalised[:40],
            )
            return False
        # Back-to-back: drop a NEAR-DUPLICATE within 0.5 s (Deepgram emitting
        # ``speech_final`` then ``is_final`` for the SAME utterance). A
        # genuinely DIFFERENT utterance arriving this fast (e.g. the real reply
        # right after a suppressed phantom) must NOT be swallowed — dropping it
        # unconditionally left an empty ``[interrupted]`` turn before this fix.
        if since_last < 0.5 and _is_near_duplicate(normalised, self._last_commit_text):
            logger.debug(
                "Dropped back-to-back near-duplicate final (%.2fs since last): %r",
                since_last,
                normalised[:40],
            )
            return False
        self._last_commit_text = normalised
        self._last_commit_at = now
        return True

    async def _stt_loop(self) -> None:
        # Throttle state lives on the instance so ``_commit_transcript`` can be
        # reused across iterations. See ``_commit_transcript`` for filter rules.
        try:
            async for transcript in self._stt.receive_transcripts():
                await self._handle_barge_in(transcript)
                # Fix 1: start STT latency timer on first partial transcript so
                # stt_ms measures from speech-start not final-transcript delivery.
                if transcript.text and self.metrics is not None:
                    self.metrics.start_turn_if_idle()
                # Emit fine-grained transcript events (additive — existing
                # ``on_transcript`` callback path is unchanged).
                if transcript.text and self._event_bus is not None:
                    self._event_bus.emit(
                        "transcript_partial"
                        if not transcript.is_final
                        else "transcript_final",
                        {
                            "text": transcript.text,
                            "is_final": bool(transcript.is_final),
                            "confidence": float(transcript.confidence or 0.0),
                        },
                    )
                # Gate LLM dispatch on either ``is_final`` or ``speech_final``.
                # Deepgram's ``speech_final`` is a faster end-of-utterance hint
                # that fires before ``is_final`` on each turn — accepting it
                # here removes ~300–700 ms of per-turn latency at parity with
                # the TS handler.
                if not (
                    (transcript.is_final or transcript.speech_final) and transcript.text
                ):
                    # PREEMPTIVE GENERATION: a confident interim may start a
                    # speculative LLM+TTS dispatch (audio held until the final
                    # commits). No-op unless ``agent.preemptive_generation``.
                    if transcript.text:
                        await self._note_interim_transcript(transcript.text)
                    continue
                if not self._commit_transcript(transcript.text):
                    # Final transcript dropped (dedup / hallucination /
                    # back-to-back). Any VAD ``speech_end`` that fired
                    # during this dropped utterance already stamped
                    # ``_endpoint_signal_at``; if we leave it there, the
                    # NEXT legitimate utterance inherits the stale anchor
                    # (its agent_response_ms then includes the silence
                    # gap between the dropped utterance and the real one).
                    if self.metrics is not None:
                        self.metrics.anchor_user_speech_start()
                    continue

                # A final transcript committed — interim-stability tracking
                # for this utterance is over (prevents a stale stability
                # watcher from speculating on the just-answered utterance).
                self._reset_interim_tracking()

                # Decouple dispatch from the receive loop: run the turn as a
                # SINGLE tracked task so the ``async for`` keeps draining
                # transcripts during a long (30-90 s) agent-runtime turn and
                # can fire transcript-based barge-in against the LIVE turn —
                # the head-of-line-blocking fix. Settle the previous turn
                # first so exactly one dispatch is in flight and the per-turn
                # conversation_history / metrics ordering is preserved.
                await self._await_dispatch_settle()
                # PREEMPTIVE GENERATION: when a speculative turn matching this
                # final is in flight, RELEASE it (its task becomes the live
                # dispatch) instead of starting a fresh one; a mismatch
                # discards the speculation and falls through to the normal
                # dispatch below.
                if await self._try_release_speculation(transcript.text):
                    continue
                self._dispatch_task = asyncio.create_task(
                    self._dispatch_turn(transcript.text)
                )

        except Exception as exc:
            logger.exception("Pipeline STT loop error: %s", exc)
        finally:
            # No more transcripts can arrive — a still-pending speculation
            # will never see its final, so tear it down (teardown, not a
            # miss) before settling the last dispatch.
            self._reset_interim_tracking()
            await self._abort_speculation(reason="stt_loop_end", count_miss=False)
            # Return only once the last dispatch fully settles, so callers and
            # tests that inspect state right after ``await _stt_loop()`` still
            # observe completed turn effects (the loop no longer blocks DURING
            # a turn, but it does block until the FINAL turn is done).
            await self._await_dispatch_settle()

    async def _await_dispatch_settle(self) -> None:
        """Await the in-flight turn dispatch to fully settle.

        Called before launching the next turn (single-in-flight) and once
        more when the STT loop exits. Two cases: the prior dispatch either
        completed naturally (await is a no-op) or was cancelled by a barge-in
        (await lets its ``finally`` — grace flip, LLM span close, ring reset,
        history flush — run BEFORE the next turn's ``_begin_speaking``). Always
        clears the handle so a backgrounded-task exception is retrieved (no
        ``Task exception was never retrieved`` leak).
        """
        task = self._dispatch_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover - teardown path
            # Re-raise when WE are the one being cancelled (STT-loop
            # teardown): swallowing it here defeated the cancelling task's
            # own cleanup and let a racing transcript respawn a dispatch
            # that survived teardown.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception as exc:
            # NOT debug: _dispatch_turn's on_message path has no internal
            # handler — webhook raise_for_status / 30 s read timeouts / user
            # exceptions all surface here, and at DEBUG the caller heard
            # silence while operators saw nothing.
            logger.exception("LLM dispatch turn failed: %s", exc)
        finally:
            # Only clear if it is still the task we awaited — a re-entrant
            # launch could have replaced it (it cannot today: the loop is the
            # sole launcher and awaits here first, but be defensive).
            if self._dispatch_task is task:
                self._dispatch_task = None

    # ------------------------------------------------------------------
    # PREEMPTIVE GENERATION (opt-in) — speculative dispatch on a confident
    # interim transcript; commit-or-discard at end of utterance. Mirrors TS
    # ``noteInterimTranscript`` / ``tryReleaseSpeculation``.
    # ------------------------------------------------------------------

    def _can_speculate(self) -> bool:
        """Whether a speculative dispatch may start right now.

        Built-in LLM loop only (an ``on_message`` handler may have external
        side effects per invocation, so it is never run speculatively), and
        only while the agent is idle: not speaking (an interim during agent
        speech is barge-in material, not a next turn) and no turn dispatch
        in flight (single-in-flight contract).
        """
        if not getattr(self, "_preemptive_enabled", False):
            return False
        if self.on_message is not None or self._llm_loop is None:
            return False
        if self._is_speaking:
            return False
        dispatch = self._dispatch_task
        return dispatch is None or dispatch.done()

    def _speculation_input_ok(self, text: str) -> bool:
        """Read-only mirror of the :meth:`_commit_transcript` filters.

        A candidate interim must pass the same hallucination / echo /
        duplicate checks a final would face at commit time — otherwise we
        would speculate on text whose final is guaranteed to be dropped.
        Unlike ``_commit_transcript`` this NEVER mutates the dedup state.
        """
        normalised = text.strip().lower()
        stripped = normalised.rstrip(".,!?;: ").strip()
        if stripped in _STT_HALLUCINATIONS or stripped == "":
            return False
        if (
            getattr(self, "_forward_stt_while_speaking", False)
            and getattr(self, "_is_speaking", False)
            and _looks_like_echo(text, getattr(self, "_current_agent_spoken_text", ""))
        ):
            return False
        # The matching final would be dropped as a duplicate at commit time.
        since_last = time.time() - self._last_commit_at
        if since_last < 2.0 and normalised == self._last_commit_text:
            return False
        return True

    async def _note_interim_transcript(self, text: str) -> None:
        """Track an interim transcript and start a speculation when it
        qualifies: (a) it ends with sentence-final punctuation (immediate),
        or (b) it has been unchanged for ``preemptive_min_stable_ms``
        (one-shot stability watcher). No-op when preemptive generation is
        disabled or the handler cannot speculate right now."""
        if not getattr(self, "_preemptive_enabled", False):
            return
        norm = _normalize_for_echo(text)
        if not norm:
            return
        spec = self._speculation
        if spec is not None and spec.norm_text == norm and not spec.failed:
            return  # already speculating on this exact utterance
        if not self._can_speculate():
            self._cancel_interim_stability_task()
            self._interim_norm = ""
            return
        if not self._speculation_input_ok(text):
            return
        if _ends_with_sentence_final_punct(text):
            # High-confidence interim — speculate immediately (replacing any
            # stale speculation on older text).
            self._cancel_interim_stability_task()
            self._interim_norm = norm
            self._interim_text = text
            await self._start_speculation(text)
            return
        if norm != self._interim_norm:
            # Text changed — restart the stability window.
            self._interim_norm = norm
            self._interim_text = text
            self._cancel_interim_stability_task()
            if self._preemptive_min_stable_s <= 0:
                await self._start_speculation(text)
                return
            self._interim_stable_task = asyncio.create_task(
                self._interim_stability_watch(norm)
            )

    async def _interim_stability_watch(self, norm: str) -> None:
        """One-shot watcher: after ``preemptive_min_stable_ms`` of the interim
        text being unchanged, start the speculation (re-validating every gate
        — the world may have moved on while we slept)."""
        try:
            await asyncio.sleep(self._preemptive_min_stable_s)
        except asyncio.CancelledError:
            return
        if self._interim_norm != norm:
            return  # a newer interim superseded this one
        spec = self._speculation
        if spec is not None and spec.norm_text == norm and not spec.failed:
            return
        if not self._can_speculate() or not self._speculation_input_ok(
            self._interim_text
        ):
            return
        try:
            await self._start_speculation(self._interim_text)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Preemptive: stability-triggered speculation failed")

    def _cancel_interim_stability_task(self) -> None:
        """Cancel the pending interim-stability watcher, if any. Idempotent;
        safe from fixtures built via ``object.__new__`` (no ``__init__``)."""
        task = getattr(self, "_interim_stable_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._interim_stable_task = None

    def _reset_interim_tracking(self) -> None:
        """Drop interim-stability state — called once a final commits (the
        utterance is decided) and on teardown."""
        self._cancel_interim_stability_task()
        self._interim_norm = ""
        self._interim_text = ""

    async def _start_speculation(self, interim_text: str) -> None:
        """Launch a speculative dispatch for ``interim_text``, replacing (and
        counting as a miss) any previous speculation on different text."""
        await self._abort_speculation(reason="replaced_by_newer_interim")
        if self._speculation is not None:
            # A concurrent path (the stability watcher vs. the STT loop)
            # registered a NEWER speculation while we awaited the old one's
            # unwind — keep it. Overwriting here would orphan its task parked
            # on the commit decision forever (held audio + an open LLM
            # stream, never aborted, never counted as a miss).
            return
        spec = _SpeculativeTurn(interim_text)
        self._speculation = spec
        spec.task = asyncio.create_task(self._run_speculative_dispatch(spec))
        logger.debug(
            "Preemptive: speculation started on interim %r",
            sanitize_log_value(interim_text[:60]),
        )

    async def _abort_speculation(
        self, *, reason: str, count_miss: bool = True
    ) -> None:
        """Discard the current speculation (if any): signal cancel, await the
        task's unwind (bounded), and count a miss unless this is teardown.
        The speculative task never touched history / carrier / per-turn
        metrics, so there is nothing to roll back. Idempotent."""
        spec = getattr(self, "_speculation", None)
        if spec is None:
            return
        # Deregister synchronously so a concurrent commit cannot release a
        # speculation that is already being torn down.
        self._speculation = None
        spec.failed = True
        spec.cancel_event.set()
        # Wake a task parked on the commit decision; ``released`` stays False
        # so it unwinds as a discard.
        spec.release_event.set()
        task = spec.task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
        if (
            count_miss
            and self.metrics is not None
            and hasattr(self.metrics, "record_preemptive_miss")
        ):
            self.metrics.record_preemptive_miss()
        logger.debug("Preemptive: speculation discarded (%s)", reason)

    def _fail_speculation_inline(self, spec: _SpeculativeTurn, reason: str) -> None:
        """Self-abort from WITHIN the speculative task (LLM error, buffer
        overflow, afterTranscribe veto). Marks the speculation unreleasable
        and deregisters it so the commit path dispatches normally. Never
        awaits (the caller IS the task)."""
        spec.failed = True
        spec.cancel_event.set()
        spec.release_event.set()
        if self._speculation is spec:
            self._speculation = None
        if self.metrics is not None and hasattr(
            self.metrics, "record_preemptive_miss"
        ):
            self.metrics.record_preemptive_miss()
        logger.debug("Preemptive: speculation failed (%s)", reason)

    async def _try_release_speculation(self, final_text: str) -> bool:
        """Commit-time decision for the in-flight speculation.

        Returns ``True`` when the speculation was RELEASED — the caller must
        NOT dispatch a normal turn (the speculative task is now the live
        turn, tracked via ``_dispatch_task``). Returns ``False`` when there
        was no usable speculation (none in flight, failed, or mismatched —
        the mismatch is discarded here) and the normal dispatch must run.

        On release, all commit-point bookkeeping the normal path performs in
        ``_dispatch_turn`` happens HERE — metrics anchors (so TTFT/latency
        reflect user-perceived timing from the REAL final-transcript commit),
        the conversation-history user push (final transcript text), and the
        ``on_transcript`` callback — exactly once per turn.
        """
        spec = getattr(self, "_speculation", None)
        if spec is None:
            return False
        if (
            spec.failed
            or spec.cancel_event.is_set()
            or not _speculation_matches(spec.interim_text, final_text)
        ):
            await self._abort_speculation(reason="final_mismatch")
            return False

        # ---- RELEASE ----
        self._speculation = None
        spec.final_text = final_text
        # Point the live cancel machinery at the speculative stream so the
        # existing barge-in paths (``_do_cancel_for_barge_in`` and the VAD
        # legacy cancel, which set ``self._llm_cancel_event``) tear it down
        # exactly like a normal turn's stream.
        self._llm_cancel_event = spec.cancel_event

        if self.metrics is not None:
            if hasattr(self.metrics, "record_preemptive_hit"):
                self.metrics.record_preemptive_hit()
            self.metrics.start_turn_if_idle()
            self.metrics.record_vad_stop()
            self.metrics.record_stt_complete(final_text)
            self.metrics.record_stt_final_timestamp()
        with start_span(
            SPAN_STT,
            {
                "getpatter.stt.text_len": len(final_text),
                "patter.call.id": self.call_id,
            },
        ):
            pass
        logger.debug("User: %s", sanitize_log_value(final_text))

        # History/transcript record the FINAL transcript text as the user
        # message (the LLM consumed the matching interim — normalized-equal
        # by definition of the release gate).
        self.transcript_entries.append({"role": "user", "text": final_text})
        self.conversation_history.append(
            {"role": "user", "text": final_text, "timestamp": self._last_commit_at}
        )
        if self.on_transcript:
            await self.on_transcript(
                {
                    "role": "user",
                    "text": final_text,
                    "call_id": self.call_id,
                    "history": list(self.conversation_history),
                }
            )
        if self.metrics is not None:
            self.metrics.record_on_user_turn_completed_delay(0.0)
            self.metrics.record_turn_committed()
        # Released turns bypass ``_dispatch_turn``: perform its semantic
        # turn-detection commit bookkeeping here too, EOS event included, so
        # combining ``preemptive_generation`` with ``turn_detector`` neither
        # leaks a stale stamped trigger into the next turn nor skips the
        # committed-EOS speech event.
        if getattr(self, "_turn_detector", None) is not None:
            self._cancel_semantic_hold()
            self._reset_semantic_window()
            _eou_trigger = self._last_eou_trigger
            self._last_eou_trigger = EouTrigger.VAD_SILENCE
        else:
            _eou_trigger = (
                "vad_silence"
                if (getattr(self.agent, "vad", None) or self._auto_vad) is not None
                else "manual_commit"
            )
        await self._emit_user_speech_eos(
            trigger=_eou_trigger, transcript_so_far=final_text
        )

        spec.released = True
        spec.release_event.set()
        # The speculative task is now the live turn — single-in-flight
        # semantics keep holding through ``_await_dispatch_settle``.
        self._dispatch_task = spec.task
        logger.info(
            "Preemptive: speculation RELEASED on matching final %r",
            sanitize_log_value(final_text[:60]),
        )
        return True

    def _spec_buffer_seconds(self, spec: _SpeculativeTurn) -> float:
        """Playout duration of the audio a speculation has buffered so far.
        Same bytes-per-second model as ``_track_outbound_playback``."""
        bytes_per_s = (
            8_000.0
            if getattr(self.audio_sender, "_input_is_mulaw_8k", False)
            else 32_000.0
        )
        return spec.buffered_bytes / bytes_per_s

    async def _spec_send_chunk(
        self, spec: _SpeculativeTurn, processed_audio: bytes
    ) -> None:
        """Push one (already hook-processed) audio chunk of a RELEASED
        speculation to the carrier — the same per-chunk bookkeeping
        ``_synthesize_sentence`` performs on the live path."""
        if spec.first_tts_chunk[0]:
            spec.first_tts_chunk[0] = False
            if self.metrics is not None:
                self.metrics.record_tts_first_byte()
            await self._emit_audio_out()
        if self._event_bus is not None:
            self._event_bus.emit("tts_chunk", {"bytes": len(processed_audio)})
        # Far-end tap mirrors the direct send path: SKIPPED on the
        # carrier-native fast path where these are mulaw 8 kHz wire bytes
        # that would corrupt the int16-PCM-16k AEC reference.
        if self._aec is not None and not getattr(
            self, "_tts_output_format_native_for_carrier", False
        ):
            self._aec.push_far_end(processed_audio)
        # Local-recording tap (agent side) — decodes on the carrier-native
        # μ-law fast path instead of skipping.
        self._tap_pipeline_agent_audio(processed_audio)
        await self.audio_sender.send_audio(processed_audio)
        self._track_outbound_playback(len(processed_audio))
        self._mark_first_audio_sent()

    async def _spec_ensure_flushed(self, spec: _SpeculativeTurn) -> None:
        """Idempotent release flush: take the floor (``_begin_speaking``),
        stamp the post-commit LLM markers, and stream every buffered sentence
        to the carrier in order. After this the speculative task continues as
        a plain live turn. No-op until the speculation is released."""
        if spec.flushed or not spec.released:
            return
        spec.flushed = True
        await self._begin_speaking()
        # Post-commit metric markers: the user-perceived TTFT for a released
        # speculation is "final commit → audio", so the first-token /
        # first-sentence stamps are recorded NOW (post ``record_turn_
        # committed``) rather than back when the speculative stream actually
        # produced them.
        if spec.response_parts:
            if self.metrics is not None and not spec.llm_first_token_recorded:
                self.metrics.record_llm_first_token()
            if not spec.llm_first_token_recorded:
                spec.llm_first_token_recorded = True
                await self._emit_llm_first_token(
                    llm_provider=self._infer_llm_provider(),
                    model=self.agent.model,
                )
            # Echo-guard reference for barge-in comparisons during the live
            # continuation (``_begin_speaking`` reset it).
            self._current_agent_spoken_text = "".join(spec.response_parts)
        if spec.buffered and self.metrics is not None:
            self.metrics.record_llm_first_sentence()
        for sentence_text, chunks in spec.buffered:
            if not chunks:
                continue
            # Per-sentence carry reset, mirroring ``_synthesize_sentence``.
            self.audio_sender.reset_pcm_carry()
            record_segment = True
            for audio in chunks:
                if not self._is_speaking:
                    # Barge-in landed mid-flush — stop exactly like the live
                    # sentence loop would.
                    spec.interrupted = True
                    spec.buffered = []
                    return
                if record_segment:
                    self._turn_spoken_segments.append(
                        (sentence_text, self._turn_playback_total_s)
                    )
                    record_segment = False
                await self._spec_send_chunk(spec, audio)
            self.audio_sender.reset_pcm_carry()
        spec.buffered = []  # release the held memory

    async def _spec_synthesize_buffered(
        self,
        spec: _SpeculativeTurn,
        sentence: str,
        hook_executor: PipelineHookExecutor,
        hook_ctx: HookContext,
    ) -> bool:
        """Synthesize one sentence of an UNRELEASED speculation, holding the
        audio in ``spec.buffered``. Transitions to live sending mid-sentence
        the moment the release lands. Returns ``False`` when the speculation
        must stop (cancelled, overflow, or barge-in after a mid-sentence
        release)."""
        if self._tts is None:
            # No TTS configured — nothing audible to hold; still track the
            # sentence so the released turn records it (parity with the live
            # path, which is also silent without TTS).
            spec.buffered.append((sentence, []))
            return True

        transformed = sentence
        text_transforms = getattr(self.agent, "text_transforms", None)
        if text_transforms:
            for fn in text_transforms:
                transformed = fn(transformed)
        processed = await hook_executor.run_before_synthesize(transformed, hook_ctx)
        if processed is None:
            return True  # hook skipped this sentence

        chunks: list[bytes] = []
        # Register BEFORE synthesis so a mid-sentence release flushes the
        # partial chunks collected so far in order.
        spec.buffered.append((processed, chunks))
        _tts_span = start_span(
            SPAN_TTS,
            {
                "getpatter.tts.text_len": len(processed),
                "patter.call.id": self.call_id,
            },
        )
        _tts_span.__enter__()
        gen = self._tts.synthesize(processed)
        try:
            async for audio_chunk in gen:
                if spec.cancel_event.is_set() and not spec.released:
                    return False
                processed_audio = await hook_executor.run_after_synthesize(
                    audio_chunk, processed, hook_ctx
                )
                if processed_audio is None:
                    continue
                if spec.released and not spec.flushed:
                    # The final committed while this sentence was mid-synth —
                    # flush everything buffered (including this sentence's
                    # earlier chunks) and continue live below.
                    await self._spec_ensure_flushed(spec)
                if spec.flushed:
                    if not self._is_speaking:
                        spec.interrupted = True
                        return False
                    await self._spec_send_chunk(spec, processed_audio)
                else:
                    chunks.append(processed_audio)
                    spec.buffered_bytes += len(processed_audio)
                    if self._spec_buffer_seconds(spec) > _PREEMPTIVE_MAX_BUFFER_S:
                        self._fail_speculation_inline(spec, "buffer_overflow")
                        return False
        finally:
            await gen.aclose()
            _tts_span.__exit__(None, None, None)
        return True

    async def _spec_speak_sentence(
        self,
        spec: _SpeculativeTurn,
        sentence: str,
        hook_executor: PipelineHookExecutor,
        hook_ctx: HookContext,
    ) -> bool:
        """Guardrails + tier-2 hook + synthesis for one speculative sentence
        — buffered pre-release, live post-release (same transforms either
        way). Returns ``False`` when the turn must stop."""
        blocked, guard_name = evaluate_guardrails(self.agent, sentence)
        if blocked:
            sentence = get_guardrail_replacement(self.agent, guard_name)
        if hook_executor.has_after_llm_sentence():
            transformed = await hook_executor.run_after_llm_sentence(
                sentence, hook_ctx
            )
            if transformed is None:
                return True  # hook dropped this sentence
            sentence = transformed
        if spec.released:
            await self._spec_ensure_flushed(spec)
            if not self._is_speaking:
                spec.interrupted = True
                return False
            if (
                self.metrics is not None
                and spec.first_tts_chunk[0]
                and not spec.buffered
            ):
                # First sentence of the turn is being synthesized live
                # (nothing was buffered pre-release) — stamp the boundary the
                # streaming path stamps via ``record_llm_first_sentence``.
                self.metrics.record_llm_first_sentence()
            ok = await self._synthesize_sentence(
                sentence, hook_executor, hook_ctx, spec.first_tts_chunk
            )
            if not ok:
                spec.interrupted = True
            return ok
        return await self._spec_synthesize_buffered(
            spec, sentence, hook_executor, hook_ctx
        )

    async def _finish_released_speculation(
        self, spec: _SpeculativeTurn, *, llm_error: bool
    ) -> None:
        """Turn-complete bookkeeping for a RELEASED speculation — mirrors the
        tail of ``_process_streaming_response`` + ``_dispatch_turn`` (metrics
        turn record, interrupted heard-prefix marker, assistant history
        entry). Runs exactly once, after all audio was sent/cancelled."""
        response_text = "".join(spec.response_parts)
        interrupted = spec.interrupted
        if not interrupted and not llm_error and response_text:
            if self.metrics is not None:
                self.metrics.record_tts_complete(response_text)
                turn = self.metrics.record_turn_complete(response_text)
                await self._emit_turn_metrics(turn, call_id=self.call_id)
        self._last_response_interrupted = interrupted
        if interrupted and response_text:
            heard = self._heard_response_prefix()
            if heard is not None:
                heard_text, _heard_everything = heard
                response_text = (
                    f"{heard_text} [interrupted by caller]"
                    if heard_text
                    else "[interrupted by caller]"
                )
            else:
                response_text = f"{response_text} [interrupted by caller]"
        if response_text:
            await self._emit_assistant_transcript(response_text)

    async def _run_speculative_dispatch(self, spec: _SpeculativeTurn) -> None:
        """Body of one speculative turn: LLM stream → sentence chunking →
        buffered TTS, then commit-or-discard.

        Until release this task is side-effect free outside ``spec`` itself
        — no conversation-history writes, no carrier audio, no per-turn
        metrics (LLM token usage/cost IS recorded by ``LLMLoop``: the tokens
        were genuinely consumed either way). After release it behaves
        exactly like a live ``_dispatch_turn`` body.
        """
        result = None
        llm_error = False
        stopped = False
        try:
            hooks = getattr(self.agent, "hooks", None)
            hook_executor = PipelineHookExecutor(hooks)
            hook_ctx = self._build_hook_context()

            # afterTranscribe gates/edits the text the LLM sees — same as a
            # normal dispatch. A veto means the matching final would be
            # vetoed too; fail the speculation and let the commit path run
            # the hook again on the real final.
            filtered_text = await hook_executor.run_after_transcribe(
                spec.interim_text, hook_ctx
            )
            if filtered_text is None:
                self._fail_speculation_inline(spec, "after_transcribe_veto")
                return

            # Prompt parity with ``_dispatch_turn``: snapshot history and
            # append the (filtered) user entry to the SNAPSHOT only — the
            # shared conversation_history is committed at release time.
            snapshot = list(self.conversation_history)
            snapshot.append(
                {"role": "user", "text": filtered_text, "timestamp": time.time()}
            )
            call_ctx = {
                "call_id": self.call_id,
                "caller": self.caller,
                "callee": self.callee,
            }
            chunker = SentenceChunker(
                aggressive_first_flush=getattr(
                    self.agent, "aggressive_first_flush", False
                ),
                language=getattr(self.agent, "language", "en"),
            )
            result = self._llm_loop.run(
                filtered_text,
                snapshot,
                call_ctx,
                hook_executor=hook_executor,
                hook_ctx=hook_ctx,
                cancel_event=spec.cancel_event,
            )
            try:
                token_iter = result.__aiter__()
                while True:
                    next_token = asyncio.ensure_future(token_iter.__anext__())
                    # Pre-release: race the next token against the commit
                    # decision so buffered audio flushes the MOMENT the final
                    # commits — even while the LLM is silent between tokens
                    # (agent-runtime LLMs can pause for seconds mid-stream).
                    while not next_token.done() and not spec.released:
                        if spec.cancel_event.is_set():
                            break
                        release_wait = asyncio.ensure_future(
                            spec.release_event.wait()
                        )
                        try:
                            await asyncio.wait(
                                {next_token, release_wait},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            release_wait.cancel()
                            try:
                                await release_wait
                            except (asyncio.CancelledError, Exception):
                                pass
                        if (
                            spec.released
                            and not spec.flushed
                            and not spec.cancel_event.is_set()
                        ):
                            await self._spec_ensure_flushed(spec)
                    if spec.cancel_event.is_set():
                        # Aborted (pre-release discard) or barge-in cancelled
                        # (post-release) — abandon the pending token fetch.
                        if not next_token.done():
                            next_token.cancel()
                        try:
                            await next_token
                        except (
                            StopAsyncIteration,
                            asyncio.CancelledError,
                            Exception,
                        ):
                            pass
                        if spec.released:
                            spec.interrupted = True
                        stopped = True
                        break
                    try:
                        token = await next_token
                    except StopAsyncIteration:
                        break
                    spec.response_parts.append(token)
                    if spec.released:
                        # Flush as soon as the release is observed — never
                        # hold already-synthesized audio while waiting for
                        # the next sentence boundary.
                        if not spec.flushed:
                            await self._spec_ensure_flushed(spec)
                        # Live continuation — keep the echo-guard reference
                        # and user-perceived TTFT current.
                        self._current_agent_spoken_text = "".join(
                            spec.response_parts
                        )
                        if not spec.llm_first_token_recorded:
                            spec.llm_first_token_recorded = True
                            if self.metrics is not None:
                                self.metrics.record_llm_first_token()
                            await self._emit_llm_first_token(
                                llm_provider=self._infer_llm_provider(),
                                model=self.agent.model,
                            )
                    for sentence in chunker.push(token):
                        if not await self._spec_speak_sentence(
                            spec, sentence, hook_executor, hook_ctx
                        ):
                            stopped = True
                            break
                    if stopped:
                        break
            except Exception as exc:
                if spec.cancel_event.is_set() and not spec.released:
                    return  # torn down mid-stream by an abort — silent
                llm_error = True
                chunker.reset()
                logger.exception(
                    "Preemptive: LLM streaming error during speculation: %s", exc
                )
                if not spec.released:
                    # Unreleased — fail silently; the final dispatches
                    # normally (and gets its own, live, error handling).
                    self._fail_speculation_inline(spec, "llm_error")
                    return
                # Released — the turn is live: mirror the live error path.
                if self.metrics is not None and self.metrics.turn_active:
                    self.metrics.record_turn_interrupted()
                fallback = getattr(self.agent, "llm_error_message", None)
                if fallback and spec.first_tts_chunk[0] and self._is_speaking:
                    try:
                        await self._synthesize_sentence(
                            fallback,
                            hook_executor,
                            hook_ctx,
                            spec.first_tts_chunk,
                            record_segment=False,
                        )
                    except Exception:  # pragma: no cover - defensive
                        logger.exception(
                            "llm_error_message fallback synthesis failed"
                        )

            if not llm_error and not stopped:
                for sentence in chunker.flush():
                    if not await self._spec_speak_sentence(
                        spec, sentence, hook_executor, hook_ctx
                    ):
                        stopped = True
                        break

            if not spec.released:
                if spec.cancel_event.is_set() or spec.failed:
                    return  # aborted pre-release — unwind silently
                # Generation finished before the final committed — park and
                # hold the audio until the commit decision lands.
                await spec.release_event.wait()
                if not spec.released:
                    return  # discarded

            # Released: flush anything still held (covers "LLM finished
            # before the final committed" — the common case), then run the
            # turn-complete bookkeeping. ``_end_speaking_with_grace`` pairs
            # with the ``_begin_speaking`` inside the flush.
            try:
                if not spec.interrupted and not llm_error:
                    await self._spec_ensure_flushed(spec)
            finally:
                if spec.flushed:
                    await self._end_speaking_with_grace()
            if self.metrics is not None:
                self.metrics.record_llm_complete()
            await self._finish_released_speculation(spec, llm_error=llm_error)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Preemptive: speculative dispatch failed")
            if not spec.released:
                self._fail_speculation_inline(spec, "exception")
            elif spec.flushed and self._is_speaking:
                # Never leave the floor held on an unexpected released-path
                # failure.
                await self._end_speaking_with_grace()
        finally:
            if result is not None and hasattr(result, "aclose"):
                try:
                    await result.aclose()
                except Exception:  # pragma: no cover - defensive
                    pass
            if self._speculation is spec:
                self._speculation = None

    async def _dispatch_turn(self, transcript_text: str) -> None:
        """Run the post-commit pipeline (record STT → afterTranscribe →
        LLM dispatch → TTS → turn-complete) as a tracked background task so
        the STT receive loop keeps draining transcripts during the turn.
        """
        # Reset the per-turn LLM cancel event BEFORE dispatch so a stale
        # cancel set by a previous turn's barge-in (``_do_cancel_for_barge_in``
        # calls ``cancel_event.set()``) cannot terminate this turn's LLM
        # stream the instant it starts. This must happen before
        # ``self._llm_loop.run(..., cancel_event=self._llm_cancel_event)`` is
        # handed the event — recreating it later (inside
        # ``_process_streaming_response``) was too late: ``run`` had already
        # captured the set event, so the next turn after any barge-in went
        # silent. Parity with TS, which allocates a fresh ``AbortController``
        # per turn in ``runPipelineLlm``.
        self._llm_cancel_event = asyncio.Event()

        # Record one STT span per final transcript turn. The span is
        # short-lived (just the attribute set) because STT is
        # streaming — we do not re-wrap the long-lived iterator.
        with start_span(
            SPAN_STT,
            {
                "getpatter.stt.text_len": len(transcript_text),
                "patter.call.id": self.call_id,
            },
        ):
            pass

        logger.debug("User: %s", sanitize_log_value(transcript_text))

        if self.metrics is not None:
            self.metrics.start_turn_if_idle()  # turn may already be open
            # Known limitation: per-turn audio_seconds is not tracked
            # here; metrics rely on total _stt_byte_count plus the
            # end_call() estimation pass.
            # first_wins: only a FALLBACK when no VAD speech_end stamped
            # this turn (no local VAD configured). Unconditional stamping
            # made EOU delay ≈0 on every emission (TS gates the stamp on the
            # provider's speechFinal for the same reason).
            self.metrics.record_vad_stop(first_wins=True)
            self.metrics.record_stt_complete(transcript_text)
            self.metrics.record_stt_final_timestamp()

        # Semantic turn detection (opt-in): a committed transcript
        # supersedes any in-flight hold (the STT endpointed on its own), and
        # the per-turn rolling window restarts so the next turn is scored on
        # its own audio.
        if self._turn_detector is not None:
            self._cancel_semantic_hold()
            self._reset_semantic_window()
        # Speech-event: end-of-utterance committed (pipeline analogue of
        # Realtime's input_audio_buffer.committed, which fires at the server
        # commit signal regardless of what the app does with the text).
        # Fires HERE — at transcript commit, before the hook veto and the
        # handler-availability checks — so both the on_message and built-in
        # LLM paths (and discarded orphan turns) advance the dispatcher's
        # turn index. With a semantic detector configured, consume the
        # trigger its finalize path stamped (``semantic_turn_detector`` when
        # the model approved the commit) — single consumption point so the
        # event fires exactly once per committed turn. Otherwise the trigger
        # reflects how this commit was driven: local VAD silence when a VAD
        # is active, else the STT provider's own endpointing.
        if self._turn_detector is not None:
            _eou_trigger = self._last_eou_trigger
            self._last_eou_trigger = EouTrigger.VAD_SILENCE
        else:
            _eou_trigger = (
                "vad_silence"
                if (getattr(self.agent, "vad", None) or self._auto_vad) is not None
                else "manual_commit"
            )
        await self._emit_user_speech_eos(
            trigger=_eou_trigger, transcript_so_far=transcript_text
        )

        # Endpoint span — silence-detected → LLM-dispatch window. Open
        # here (right after VAD stop / final transcript is recorded)
        # and close it just before ``record_turn_committed`` below.
        endpoint_span = start_span(
            SPAN_ENDPOINT,
            {"patter.call.id": self.call_id},
        )
        endpoint_span.__enter__()
        endpoint_closed = False

        def _close_endpoint_span() -> None:
            nonlocal endpoint_closed
            if endpoint_closed:
                return
            endpoint_closed = True
            try:
                endpoint_span.__exit__(None, None, None)
            except Exception:  # pragma: no cover - defensive
                pass

        # Raw transcript always goes to dashboard/transcript log
        self.transcript_entries.append({"role": "user", "text": transcript_text})

        # Reuse the timestamp already captured by _commit_transcript and stored
        # in self._last_commit_at. This avoids a second time.time() call per
        # transcript, which would exhaust the finite fake-clock iterators used
        # in unit tests (and is wasteful in production too).
        _turn_ts = self._last_commit_at

        # Append raw text to conversation_history NOW so that on_transcript
        # receives a history snapshot that includes the current user turn
        # (parity with OpenAIRealtimeStreamHandler which appends before firing
        # on_transcript). Replaced by filtered_text below, or popped on any
        # early-return path so a vetoed/orphaned turn never lingers.
        self.conversation_history.append(
            {"role": "user", "text": transcript_text, "timestamp": _turn_ts}
        )

        if self.on_transcript:
            await self._safe_on_transcript(
                {
                    "role": "user",
                    "text": transcript_text,
                    "call_id": self.call_id,
                    "history": list(self.conversation_history),
                }
            )

        # --- afterTranscribe hook ---
        hooks = getattr(self.agent, "hooks", None)
        hook_executor = PipelineHookExecutor(hooks)
        hook_ctx = self._build_hook_context()
        filtered_text = await hook_executor.run_after_transcribe(
            transcript_text, hook_ctx
        )
        if filtered_text is None:
            logger.debug("afterTranscribe hook vetoed turn")
            if self.metrics is not None:
                self.metrics.record_turn_interrupted()
            # Remove the speculatively-appended user turn before returning so a
            # vetoed turn does not linger in conversation_history.
            if (
                self.conversation_history
                and self.conversation_history[-1].get("text") == transcript_text
            ):
                self.conversation_history.pop()
            _close_endpoint_span()
            return

        if self.metrics is not None:
            self.metrics.record_on_user_turn_completed_delay(0.0)
        if self.on_message is None and self._llm_loop is None:
            # No message handler or LLM loop — discard orphaned turn.
            if self.metrics is not None:
                self.metrics.record_turn_interrupted()
            # Pop the speculatively-appended user turn so it does not
            # accumulate as an orphaned entry when there is nothing to consume
            # it (no handler and no built-in LLM loop).
            if (
                self.conversation_history
                and self.conversation_history[-1].get("text") == transcript_text
            ):
                self.conversation_history.pop()
            _close_endpoint_span()
            return

        # Replace the raw-text speculative entry with filtered_text (the text
        # actually sent to the LLM).
        if (
            self.conversation_history
            and self.conversation_history[-1].get("text") == transcript_text
        ):
            self.conversation_history.pop()
        # Snapshot history BEFORE appending the current turn:
        # ``LLMLoop._build_messages`` replays the given history and then
        # appends ``user_text`` itself, so including the current turn here
        # sent the user's utterance to the model twice on every turn.
        history_snapshot = list(self.conversation_history)
        self.conversation_history.append(
            {"role": "user", "text": filtered_text, "timestamp": _turn_ts}
        )

        # Built-in LLM loop path
        if self.on_message is None and self._llm_loop is not None:
            call_ctx = {
                "call_id": self.call_id,
                "caller": self.caller,
                "callee": self.callee,
            }
            if self.metrics is not None:
                self.metrics.record_turn_committed()
            _close_endpoint_span()
            result = self._llm_loop.run(
                filtered_text,
                history_snapshot,
                call_ctx,
                hook_executor=hook_executor,
                hook_ctx=hook_ctx,
                cancel_event=self._llm_cancel_event,
            )
            response_text = await self._process_streaming_response(result, self.call_id)
            if response_text:
                await self._emit_assistant_transcript(response_text)
            return

        # on_message handler path
        if self.metrics is not None:
            self.metrics.record_turn_committed()
        _close_endpoint_span()
        msg_data = {
            "text": filtered_text,
            "call_id": self.call_id,
            "caller": self.caller,
            "callee": self.callee,
            "history": list(self.conversation_history),
        }

        response_text = ""
        streaming = False

        from getpatter.services.remote_message import (
            is_remote_url,
            is_websocket_url,
        )

        if is_remote_url(self.on_message):
            remote = self._remote_handler
            if is_websocket_url(self.on_message):
                result = remote.call_websocket(self.on_message, msg_data)
                streaming = True
            else:
                response_text = await remote.call_webhook(self.on_message, msg_data)
                streaming = False
        elif self._msg_accepts_call:
            result = self.on_message(msg_data, self._call_control)
        else:
            result = self.on_message(msg_data)

        if not is_remote_url(self.on_message):
            if asyncio.iscoroutine(result):
                response_text = await result
                streaming = False
            elif inspect.isasyncgen(result):
                streaming = True
            else:
                response_text = result
                streaming = False

        # Check if handler ended the call
        if self._call_control is not None and self._call_control.ended:
            return

        if streaming:
            response_text = await self._process_streaming_response(result, self.call_id)
            if response_text:
                await self._emit_assistant_transcript(response_text)
        else:
            if not response_text:
                # Common misuse: on_message was provided as an observer
                # (returning None) but it actually replaces the built-in LLM
                # loop. Warn loudly — the caller hears no audio until the
                # handler returns a non-empty string.
                logger.warning(
                    "on_message returned empty/None — no TTS will play. "
                    "If you intended to observe transcripts, use on_transcript "
                    "instead; if you meant to answer via the built-in LLM, "
                    "remove on_message and pass openai_key."
                )
            await self._process_regular_response(response_text, self.call_id)

    async def on_audio_received(self, audio_bytes: bytes) -> None:
        """Forward caller audio to STT (transcoding to PCM16 16 kHz, running VAD/hooks)."""
        # Local-recording tap — ABOVE the STT/barge-in early-returns so the
        # caller channel has no gaps when STT is unset or inbound frames are
        # dropped during TTS (``barge_in_threshold_ms == 0``). The recorder
        # performs the same mulaw→PCM16 decode + 8→16 kHz resample as the
        # STT path below (own stateful resampler), so it always receives
        # PCM16 16 kHz regardless of the carrier wire codec. Tapped PRE-AEC:
        # the recording captures what the caller actually sent.
        if getattr(self, "local_recorder", None) is not None:
            self._tap_caller_audio(
                audio_bytes,
                "mulaw_8k" if self._input_is_mulaw_8k else "pcm16_16k",
            )
        if self._stt is None:
            return
        # Always forward caller audio to STT — even while the agent is
        # speaking — so barge-in detection can trigger. When
        # ``barge_in_threshold_ms == 0`` on the agent, skip STT during TTS
        # to avoid echo-loop costs (opt-out for noisy links).
        if self._is_speaking and getattr(self.agent, "barge_in_threshold_ms", 300) == 0:
            return
        # Inbound PCMU 8 kHz (Twilio always, Telnyx when streaming_start
        # negotiated PCMU bidirectional) must be decoded to PCM16 and
        # up-sampled to 16 kHz before hitting STT adapters configured for
        # linear16 @ 16 kHz. Decode -> stateful resample -> AEC near-end ->
        # ``agent.audio_filter`` -> VAD all live in the
        # ``InputProcessingChain`` (slice 1 of the pipeline-stages
        # decomposition — docs/architecture/pipeline-stages.md). Lazily
        # constructed (mirrors the old lazy resampler) with late-bound
        # getters so ``start()`` — and test fixtures — can install
        # ``_aec`` / ``_auto_vad`` after the chain already exists.
        chain = getattr(self, "_input_chain", None)
        if chain is None:
            chain = InputProcessingChain(
                input_is_mulaw_8k=self._input_is_mulaw_8k,
                get_aec=lambda: getattr(self, "_aec", None),
                get_audio_filter=lambda: getattr(self.agent, "audio_filter", None),
                get_vad=lambda: getattr(self.agent, "vad", None)
                or getattr(self, "_auto_vad", None),
            )
            self._input_chain = chain
        frame = await chain.process(audio_bytes)
        pcm = frame.pcm

        # ---- Semantic turn detection: rolling audio window ----
        # Keep the last ~8 s of post-chain (decoded, AEC'd, filtered) PCM16
        # 16 kHz so the detector can score the caller's current turn on the
        # VAD speech_end edge. Zero cost when no ``agent.turn_detector`` is
        # configured (or after the detector failed and semantic endpointing
        # was disabled).
        if self._turn_detector is not None and not self._semantic_detector_failed:
            self._semantic_buffer_append(pcm)

        # ---- VAD event handling (Fix 8) ----
        # The chain fed the (AEC'd, filtered) frame to ``agent.vad`` (or the
        # auto-loaded SileroVAD) *before* STT so we can react to speech_start
        # with immediate barge-in (clearing the carrier audio buffer) rather
        # than waiting for the STT engine's slower endpoint.
        if frame.vad_configured:
            vad_event = frame.vad_event
            if vad_event is not None:
                if vad_event.type == "speech_start":
                    # Speech-event: the seven-event public API never fired in
                    # pipeline mode (only realtime emitted) — wire the user
                    # start edge here. No-op without a dispatcher.
                    await self._emit_user_speech_started()
                    # The user resumed speaking — an active semantic hold
                    # (detector judged the previous pause mid-turn) is proven
                    # right; drop it so the utterance keeps accumulating and
                    # the next speech_end re-evaluates from scratch.
                    if self._turn_detector is not None:
                        self._cancel_semantic_hold()
                    # Tail-grace new-turn rescue: the agent already finished
                    # its turn and we are only in the post-TTS echo-guard
                    # window. A VAD speech_start here is the user's next turn,
                    # not a barge-in — end the grace synchronously so this
                    # utterance flows to STT as a clean new turn instead of
                    # being swallowed by the self-hearing guard or mislabelled
                    # as an empty ``[interrupted]`` turn (the multi-turn
                    # silence bug). After this ``_is_speaking`` is False, so
                    # the if/elif below is a no-op and the frame falls through
                    # to STT. Parity with TS ``endTailGraceForNewTurn``.
                    if self._is_speaking and getattr(self, "_tail_grace_active", False):
                        await self._end_tail_grace_for_new_turn()
                    phantom_suppressed = self._is_speaking and not self._can_barge_in()
                    if phantom_suppressed:
                        # Within the per-turn warmup gate. With AEC on
                        # this is the ~1 s filter convergence window;
                        # without AEC it is just a 0.25 s anti-flicker
                        # margin. INFO so unexpected suppressions are
                        # visible without enabling debug logs.
                        #
                        # CRITICAL: do NOT touch metrics state here.
                        # An earlier bug (pre-0.6.1) called
                        # ``start_turn_if_idle()`` for every
                        # ``speech_start`` including suppressed phantoms,
                        # which stamped ``_turn_start`` at echo/loopback
                        # time. ``start_turn_if_idle`` then no-op'd on
                        # the legitimate user-speech ``speech_start``
                        # that followed (turn_start was already set),
                        # so ``user_speech_duration_ms`` was reported as
                        # 5-7 s even on short ~1 s utterances.
                        aec_state = (
                            "on" if getattr(self, "_aec", None) is not None else "off"
                        )
                        logger.info(
                            "VAD speech_start suppressed (agent speaking < gate, aec=%s)",
                            aec_state,
                        )
                        # Real user speech detected but gated out. The
                        # grace-timer flip will drain the ring buffer to
                        # STT so the user's words are not silently lost.
                        self._suppressed_speech_pending = True
                    elif self._is_speaking and self._should_pause_for_barge_in():
                        # PAUSE-AND-RESUME (opt-in ``barge_in_mode=
                        # "pause_resume"``): output pauses immediately —
                        # the carrier buffer is cleared so the agent goes
                        # silent within one frame — but nothing is
                        # cancelled. A committed final transcript within
                        # ``_barge_in_confirm_s`` kills the turn via
                        # ``_handle_barge_in`` → ``_do_cancel_for_barge_in``;
                        # otherwise ``_pause_resume_timeout`` resumes from
                        # the first not-fully-heard sentence. Takes
                        # precedence over the defer_cancel paths below —
                        # it is strictly safer (output stops immediately
                        # AND a false positive is recoverable). The frame
                        # falls through to STT below (paused output makes
                        # the line echo-quiet) so the confirm window can
                        # actually hear the user.
                        await self._start_pause_resume()
                    elif self._is_speaking:
                        # Caller spoke over in-flight TTS. The cancel is
                        # DEFERRED to transcript confirmation — instead of
                        # firing on raw VAD energy — when EITHER:
                        #   (a) opt-in ``barge_in_strategies`` are configured
                        #       (a strategy must approve the transcript), OR
                        #   (b) we forward STT during TTS WITHOUT AEC. On a
                        #       no-AEC link a VAD ``speech_start`` here is very
                        #       often the agent's OWN TTS echo, not the caller;
                        #       cancelling on it self-interrupts almost every
                        #       turn (the "bene bene" → [interrupted] cascade
                        #       seen on live Hermes/OpenClaw calls). Deferring
                        #       lets ``_handle_barge_in`` run the echo guard on
                        #       the resulting transcript and cancel only on real
                        #       caller speech; if no transcript confirms within
                        #       ``_barge_in_confirm_s`` the pending state times
                        #       out and the agent resumes its sentence.
                        # Otherwise (default VAD path, or forward-STT WITH AEC
                        # where the canceller makes VAD trustworthy) the legacy
                        # immediate cancel runs — existing users see no change.
                        # Parity with TS speech_start ``deferCancel``.
                        defer_cancel = bool(self._barge_in_strategies) or (
                            getattr(self, "_forward_stt_while_speaking", False)
                            and getattr(self, "_aec", None) is None
                        )
                        if defer_cancel:
                            await self._start_pending_barge_in()
                        else:
                            if self.metrics is not None:
                                self.metrics.record_bargein_detected()
                            with start_span(
                                SPAN_BARGEIN,
                                {"patter.call.id": self.call_id},
                            ):
                                try:
                                    await self.audio_sender.send_clear()
                                except Exception as exc:
                                    logger.debug(
                                        "send_clear during VAD barge-in failed: %s",
                                        exc,
                                    )
                                await self._flush_inbound_audio_ring()
                                if self.metrics is not None:
                                    self.metrics.record_tts_stopped()
                                    self.metrics.record_turn_interrupted()
                                self._is_speaking = False
                                self._tail_grace_active = False
                                self._speaking_started_at = None
                                self._first_audio_sent_at = None
                                self._speaking_generation += 1
                                self._last_cancel_at = time.time()
                                self._suppressed_speech_pending = False
                                # Post-complete barge-in during the buffered
                                # tail — rewrite history to the heard prefix
                                # BEFORE resetting the playback cursor.
                                self._maybe_truncate_completed_turn_history()
                                # ``send_clear`` above dropped the carrier's
                                # buffered audio — reset the playback cursor.
                                self._playback_buffered_until = 0.0
                                self._clear_grace_task()
                                # Tear down the in-flight LLM stream too. The
                                # consumption loop polls ``_llm_cancel_event``
                                # per chunk, but a turn parked PRE-first-token
                                # on a hung agent request never sees a chunk —
                                # the provider cancel watchdog (see
                                # ``OpenAICompatibleLLMProvider.stream``) closes
                                # the request the instant this fires. Parity
                                # with TS ``cancelSpeaking`` → ``llmAbort.abort``.
                                cancel_event = getattr(self, "_llm_cancel_event", None)
                                if cancel_event is not None:
                                    cancel_event.set()
                    if not phantom_suppressed and self.metrics is not None:
                        # Industry-standard pattern: every legitimate VAD speech_start
                        # re-anchors the turn timestamp pre-commit. This
                        # repairs the case where a partial transcript /
                        # rejected barge-in already stamped stale anchors,
                        # plus the original "phantom during warmup gate"
                        # vulnerability. No-op once the turn is committed.
                        self.metrics.anchor_user_speech_start()
                    # PREEMPTIVE GENERATION: the user resumed speaking while a
                    # speculative turn was buffering — the interim it was
                    # generated from is stale, so abort silently (nothing was
                    # audible; the next confident interim re-speculates). A
                    # RELEASED speculation is no longer registered here — it
                    # is the live turn and the barge-in paths above own it.
                    if (
                        not self._is_speaking
                        and getattr(self, "_speculation", None) is not None
                    ):
                        await self._abort_speculation(reason="user_speech_resumed")
                elif vad_event.type == "speech_end":
                    # Speech-event: user stop edge (pipeline-mode parity with
                    # realtime). No-op without a dispatcher.
                    await self._emit_user_speech_ended()
                    if self.metrics is not None:
                        self.metrics.record_vad_stop()
                    if (
                        self._turn_detector is not None
                        and not self._semantic_detector_failed
                    ):
                        # Semantic turn detection (opt-in): defer the STT
                        # finalize until the end-of-utterance model agrees
                        # the caller is done — or hold for at most
                        # ``max_semantic_hold_ms`` while it predicts
                        # "incomplete" (mid-sentence pause). The default
                        # ``turn_detector=None`` path below is unchanged,
                        # and a failed detector permanently rejoins it.
                        await self._semantic_eou_check()
                    else:
                        # The SDK's VAD has detected end-of-speech earlier
                        # and more reliably than the provider's own
                        # endpointing on PSTN (Deepgram natural-pause
                        # endpointing can run 1-6 s before it emits a
                        # final). Ask the provider to finalise the
                        # in-flight utterance NOW so the next turn can
                        # dispatch immediately.
                        await self._finalize_stt_for_eou()

            # Semantic hold poll: while the detector is holding the turn
            # open, every additional silent frame advances the audio clock —
            # re-score after each ``_SEMANTIC_POLL_MS`` window of silence and
            # force the finalize once the hard cap is reached. Frames that
            # carried a VAD transition are skipped: a ``speech_start`` just
            # cancelled the hold, and on the ``speech_end`` frame itself the
            # detector already scored this audio (the silence window starts
            # AFTER the decision point).
            if (
                self._turn_detector is not None
                and self._semantic_hold_active
                and vad_event is None
            ):
                await self._poll_semantic_hold(len(pcm))

            # Self-hearing guard: while the agent is speaking, don't pass
            # caller audio to STT — VAD already gave us authoritative
            # barge-in detection above, so any STT audio sent here would
            # just be the agent's own TTS echoing back.
            #
            # Pre-barge-in buffer: instead of dropping the frame on the
            # floor, push it into a small ring (last ~600 ms). On a
            # future BARGE-IN this ring is flushed to STT so the user's
            # first words — captured BEFORE the VAD's
            # ``min_speech_duration`` window let it emit ``speech_start``
            # — actually reach Deepgram. Without this buffer, short
            # interruptions ("stop") never produced a transcript and the
            # agent kept talking; long ones produced truncated
            # transcripts and the agent answered to fragments.
            # Pause-and-resume: while output is PAUSED the line is
            # echo-quiet (no TTS is playing), so frames flow straight to
            # STT — the confirm window depends on STT hearing the user.
            # ``_start_pause_resume`` already flushed the ring's leading
            # edge when the pause began.
            if self._is_speaking and not getattr(self, "_output_paused", False):
                # The deque's ``maxlen=13`` (~260 ms at 20 ms/frame, matching
                # SileroVAD ``min_speech_duration``) evicts the oldest frame
                # on append, so the post-barge-in replay only recovers the
                # VAD-missed leading edge of the user's speech, not ~350 ms
                # of pre-speech silence/agent-bleed. On PSTN (where AEC is a
                # no-op) Deepgram trained on English transcribes that
                # bleed as English garbage and commits it to the LLM as
                # a phantom user transcript. See BUGS.md 2026-05-05
                # post-barge-in bleed-transcription entry.
                self._inbound_audio_ring.append(pcm)
                # Opt-in: also forward the frame to STT during TTS so the
                # transcript barge-in path can receive a transcript on
                # echo-masked links where the VAD never fires. The ring push
                # above stays unconditional (leading-edge recovery preserved);
                # only the early-return is gated. ECHO RISK without AEC — the
                # agent's own voice may be transcribed as a phantom
                # interruption; pair with agent.barge_in_strategies. Default
                # OFF → byte-identical push-and-return.
                #
                # ALSO forward while a strategy barge-in is PENDING: the
                # strategies are consulted on transcripts, but with audio
                # withheld from STT no transcript could ever arrive — the
                # pending state always timed out and barge-in was
                # structurally impossible with strategies configured.
                _pending = (
                    getattr(self, "_barge_in_pending_since", None) is not None
                )
                if not self._forward_stt_while_speaking and not _pending:
                    return

        # before_send_to_stt hook — gate/transform the audio chunk before it
        # reaches the STT provider. Returning None drops the chunk (useful
        # for custom VAD / echo-cancellation / PII redaction).
        hooks = getattr(self.agent, "hooks", None)
        if hooks is not None:
            hook_executor = PipelineHookExecutor(hooks)
            hook_ctx = self._build_hook_context()
            processed = await hook_executor.run_before_send_to_stt(pcm, hook_ctx)
            if processed is None:
                return
            pcm = processed

        try:
            await self._stt.send_audio(pcm)
        except Exception as _exc:  # noqa: BLE001 - degrade, don't kill the call
            # A dropped STT WebSocket used to propagate out of the carrier
            # read loop and tear the whole call down as "Stream error".
            # Degrade to dropped frames (rate-limited log) like TS; the STT
            # loop's own error path handles recovery/escalation.
            now = time.time()
            last = getattr(self, "_stt_send_error_logged_at", 0.0)
            if now - last > 5.0:
                self._stt_send_error_logged_at = now
                logger.warning("STT send_audio failed (dropping frames): %s", _exc)
            return
        if self.metrics is not None:
            # Count bytes that actually reach the STT adapter. When the
            # input is mulaw 8 kHz (Twilio / Telnyx PCMU), ``audio_bytes``
            # is 1B/sample @ 8 kHz — but the metrics layer is configured
            # for 16-bit @ 16 kHz, so counting the raw mulaw payload
            # under-reports STT seconds by 4x. Use ``pcm`` (post-decode,
            # post-resample) so the byte count matches the configured
            # STT format.
            self.metrics.add_stt_audio_bytes(len(pcm))

    # ---------------------------------------------------------------
    # Semantic turn detection (opt-in via ``agent.turn_detector``)
    # ---------------------------------------------------------------

    # Rolling-window byte budget: the last 8 s of PCM16 @ 16 kHz — exactly
    # the maximum context smart-turn v3 consumes per prediction (256 000 B
    # per concurrent call, only when a detector is configured).
    _SEMANTIC_WINDOW_MAX_BYTES: int = 16000 * 2 * 8
    # While a hold is active, re-score after each additional silence window
    # of this many milliseconds of inbound audio (~10 frames at 20 ms/frame).
    _SEMANTIC_POLL_MS: int = 200

    def _semantic_buffer_append(self, pcm: bytes) -> None:
        """Append a post-decode PCM16-16k frame to the rolling 8 s window."""
        if not pcm:
            return
        self._semantic_audio_ring.append(pcm)
        self._semantic_audio_ring_bytes += len(pcm)
        while (
            self._semantic_audio_ring_bytes > self._SEMANTIC_WINDOW_MAX_BYTES
            and self._semantic_audio_ring
        ):
            dropped = self._semantic_audio_ring.popleft()
            self._semantic_audio_ring_bytes -= len(dropped)

    def _semantic_window_bytes(self) -> bytes:
        """Concatenate the rolling window for one detector prediction."""
        return b"".join(self._semantic_audio_ring)

    def _reset_semantic_window(self) -> None:
        """Drop the rolling window — called when a turn commits so the next
        turn's window contains only its own audio (mirrors the reference
        smart-turn integrations, which score per-turn audio)."""
        self._semantic_audio_ring.clear()
        self._semantic_audio_ring_bytes = 0

    async def _semantic_eou_check(self) -> None:
        """Score the rolling window; finalize, or hold for more silence.

        Fail-open AND fail-once: the first detector error falls back to the
        legacy immediate finalize (``vad_silence`` trigger) and disables
        semantic endpointing for the remainder of the call — a broken model
        must never stall a live phone call, and a permanently broken one
        (onnxruntime missing/incompatible, model file gone) must produce a
        single clear warning, not one per turn.
        """
        detector = self._turn_detector
        if detector is None:
            return
        try:
            probability = float(
                await detector.predict(self._semantic_window_bytes())
            )
        except Exception as exc:
            self._semantic_detector_failed = True
            logger.warning(
                "Semantic turn detector failed — disabling it for this call "
                "and falling back to plain VAD-silence endpointing: %s",
                exc,
            )
            self._cancel_semantic_hold()
            # The rolling window is dead weight now that the detector is
            # disabled — release the up-to-8 s of buffered PCM immediately.
            self._reset_semantic_window()
            self._last_eou_trigger = EouTrigger.VAD_SILENCE
            await self._finalize_stt_for_eou()
            return

        threshold = float(getattr(detector, "threshold", 0.5))
        if probability >= threshold:
            logger.debug(
                "Semantic turn detector: end of turn (p=%.3f >= %.2f)",
                probability,
                threshold,
            )
            self._cancel_semantic_hold()
            self._last_eou_trigger = EouTrigger.SEMANTIC_TURN_DETECTOR
            await self._finalize_stt_for_eou()
        elif not self._semantic_hold_active:
            logger.debug(
                "Semantic turn detector: holding turn open (p=%.3f < %.2f)",
                probability,
                threshold,
            )
            self._begin_semantic_hold()
        # else: already holding — stay held; the frame-driven poll (or the
        # wall-clock backstop) schedules the next decision.

    def _begin_semantic_hold(self) -> None:
        """Arm the hold state + the wall-clock backstop for the hard cap."""
        self._semantic_hold_active = True
        self._semantic_hold_deadline = (
            time.monotonic() + self._max_semantic_hold_ms / 1000.0
        )
        self._semantic_poll_pending_bytes = 0
        self._semantic_hold_generation += 1
        generation = self._semantic_hold_generation
        delay_s = self._max_semantic_hold_ms / 1000.0
        try:
            self._semantic_hold_task = asyncio.create_task(
                self._semantic_hold_backstop(generation, delay_s)
            )
        except RuntimeError:  # pragma: no cover — no running loop
            self._semantic_hold_task = None

    def _cancel_semantic_hold(self) -> None:
        """Drop the hold (and its backstop task) without finalizing.

        Idempotent, and safe on partially-constructed handlers (teardown
        paths run against ``object.__new__`` instances in unit tests) where
        the semantic state attributes were never initialized.
        """
        if not getattr(self, "_semantic_hold_active", False):
            return
        self._semantic_hold_active = False
        self._semantic_hold_deadline = None
        self._semantic_poll_pending_bytes = 0
        self._semantic_hold_generation += 1
        task = self._semantic_hold_task
        self._semantic_hold_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _poll_semantic_hold(self, frame_bytes: int) -> None:
        """Advance the audio clock of an active hold by one inbound frame.

        Finalizes (``vad_silence``) once the hard cap is reached; otherwise
        re-runs the detector after each additional ``_SEMANTIC_POLL_MS`` of
        silence so a model that flips to "complete" with more trailing
        silence commits the turn as ``semantic_turn_detector``.
        """
        deadline = self._semantic_hold_deadline
        if deadline is not None and time.monotonic() >= deadline:
            await self._resolve_semantic_hold_cap()
            return
        self._semantic_poll_pending_bytes += frame_bytes
        poll_bytes = int(16000 * 2 * (self._SEMANTIC_POLL_MS / 1000.0))
        if self._semantic_poll_pending_bytes < poll_bytes:
            return
        self._semantic_poll_pending_bytes = 0
        await self._semantic_eou_check()

    async def _semantic_hold_backstop(self, generation: int, delay_s: float) -> None:
        """Wall-clock cap enforcement — runs even if inbound audio stalls.

        Generation-guarded (mirrors the grace-flip pattern): a hold resolved
        before the timer fires invalidates this task, so it can never
        finalize a later turn's utterance.
        """
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:  # pragma: no cover — cancelled hold
            return
        if (
            generation != self._semantic_hold_generation
            or not self._semantic_hold_active
        ):
            return
        # Detach the handle BEFORE resolving so _cancel_semantic_hold (called
        # inside the resolve) does not cancel the currently-running task.
        self._semantic_hold_task = None
        await self._resolve_semantic_hold_cap()

    async def _resolve_semantic_hold_cap(self) -> None:
        """Hard cap reached: finalize anyway so the turn can never hang.

        The semantic model never agreed, so the commit reason is the
        accumulated silence — the EOU trigger stays ``vad_silence``.
        """
        if not self._semantic_hold_active:
            return
        logger.debug(
            "Semantic hold cap reached (%d ms) — finalizing on VAD silence",
            self._max_semantic_hold_ms,
        )
        self._cancel_semantic_hold()
        self._last_eou_trigger = EouTrigger.VAD_SILENCE
        await self._finalize_stt_for_eou()

    async def _finalize_stt_for_eou(self) -> None:
        """Ask the STT provider to finalize the in-flight utterance NOW.

        ``getattr`` so STT adapters that don't implement it (Whisper-class
        one-shot transcribers) simply skip. Extracted verbatim from the VAD
        ``speech_end`` branch so the default path stays byte-identical and
        the semantic turn-detector paths reuse it.
        """
        finalize = getattr(self._stt, "finalize", None)
        if callable(finalize):
            try:
                ret = finalize()
                if asyncio.iscoroutine(ret):
                    await ret
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("STT finalize threw: %s", exc)

    # ---------------------------------------------------------------
    # TTS speaking state helpers (Fix 9)
    # ---------------------------------------------------------------

    # Minimum drain window (seconds) between a barge-in cancel and the
    # next ``_begin_speaking``. 0.15 s covers a typical PSTN jitter
    # buffer drain + Twilio Media Stream clear propagation. Lower values
    # risk audio overlap on the first chunk; higher values increase the
    # perceived "agent ack" latency after a barge-in. Mirrors TS
    # ``StreamHandler.POST_CANCEL_DRAIN_MS``.
    _POST_CANCEL_DRAIN_S: float = 0.15

    async def _begin_speaking(self, is_first_message: bool = False) -> None:
        # Speech-event: agent start edge (pipeline parity with realtime).
        await self._emit_agent_speech_started(engine="pipeline")
        """Mark TTS playback as in-progress and bump the generation counter.

        Awaits the post-cancel drain window before flipping state so the
        remote PSTN player has time to flush the cancelled turn's tail.

        The generation counter is consulted by ``_end_speaking_with_grace``
        so a delayed flip-to-idle from a previous turn cannot cancel the
        speaking flag of the *current* turn.

        Args:
            is_first_message: When ``True`` stamps ``_first_audio_sent_at``
                synchronously before the TTS loop starts so the
                ``_can_barge_in()`` 250 ms anti-flicker gate (no-AEC PSTN
                default) runs in PARALLEL with TTS TTFB rather than only
                starting after audio actually arrives. Without this, the
                firstMessage is effectively un-interruptible for the first
                300-800 ms while waiting on cloud TTS first-byte.
        """
        if self._last_cancel_at is not None:
            elapsed = time.time() - self._last_cancel_at
            remaining = self._POST_CANCEL_DRAIN_S - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._speaking_generation += 1
        self._is_speaking = True
        # A fresh turn is actively streaming — not in the post-TTS echo
        # window. Clear the tail-grace flag so a VAD speech_start during this
        # turn is treated as a real barge-in (not a new-turn rescue).
        self._tail_grace_active = False
        self._speaking_started_at = time.time()
        # Stamp ``_first_audio_sent_at`` synchronously for EVERY turn so the
        # ``_can_barge_in()`` gate (250 ms anti-flicker for PSTN no-AEC) runs
        # in PARALLEL with LLM TTFT + TTS TTFB rather than starting only
        # after the first audio chunk reaches the wire. Without this, a turn
        # with a slow LLM (gpt-4o cold cache ~2 s) is effectively
        # un-interruptible for the entire LLM window: ``_first_audio_sent_at``
        # stays None, ``_can_barge_in`` returns False, and every VAD
        # ``speech_start`` is suppressed silently. Promoted from
        # firstMessage-only to default on 2026-05-14 (TS parity).
        # ``is_first_message`` is kept for backward compat with callers but
        # no longer changes behaviour.
        _ = is_first_message
        self._first_audio_sent_at = time.time()
        # Fresh turn — drop any stale pre-barge-in buffer from a previous
        # turn so we never replay yesterday's audio to STT.
        self._inbound_audio_ring.clear()
        self._suppressed_speech_pending = False
        # Fresh turn — reset the echo-guard reference so this turn's barge-in
        # checks compare against THIS turn's spoken text, not the last turn's.
        self._current_agent_spoken_text = ""
        # Fresh turn — reset the heard-prefix playback timeline.
        self._turn_playback_total_s = 0.0
        self._turn_spoken_segments = []
        # Fresh turn — drop any pause-and-resume state and retained audio
        # from the previous turn (a paused turn can never reach here — the
        # pause-decision wait resolves before the turn ends — but be
        # defensive) and re-enable retention after an overflow.
        self._discard_pause_state()
        self._turn_sentence_audio = []
        self._pause_retained_bytes = 0
        self._pause_resume_overflowed = False
        self._pause_decision_event = None
        # False until the turn body finishes pushing audio (the
        # ``_end_speaking_with_grace`` call in its finally). The resume
        # path uses it to decide whether the #164 grace machinery must be
        # re-armed for the re-sent tail (post-complete pause) or whether
        # the still-running turn body will arm it itself.
        self._turn_output_done = False
        # Reset the VAD detector so the next user utterance triggers a clean
        # SILENCE→SPEECH transition. Without this, PSTN echo from the
        # previous turn can keep the smoothed probability above the
        # deactivation threshold (0.35) for the entire turn — the VAD never
        # returns to SILENCE, ``speech_start`` never fires, and barge-in
        # feels "one-shot". The user's previous utterance was already
        # committed by STT before ``_begin_speaking`` is called, so resetting
        # state here cannot lose data.
        self._reset_vad()

    def _mark_first_audio_sent(self) -> None:
        """Record that the first TTS chunk of the current turn hit the wire.

        Idempotent within a turn: only the first call sets the timestamp.
        Must be invoked AFTER the underlying ``audio_sender.send_audio`` so
        the gate is anchored to "audio actually went out", not "we asked
        the carrier to send it". Mirrors TS ``markFirstAudioSent``.
        """
        if self._first_audio_sent_at is None:
            self._first_audio_sent_at = time.time()

    def _track_outbound_playback(self, num_bytes: int) -> None:
        """Advance ``_playback_buffered_until`` by the playout duration of an
        outbound TTS chunk.

        ``num_bytes`` is the chunk size BEFORE carrier encoding (the exact
        buffer handed to ``audio_sender.send_audio``): PCM16 @ 16 kHz in the
        default path (32 bytes/ms), or the carrier's native μ-law @ 8 kHz
        (8 bytes/ms) when the TTS adapter emits wire format directly
        (``audio_sender._input_is_mulaw_8k`` — Twilio/Plivo ``ulaw_8000``;
        Telnyx native is PCM16 16 kHz so it stays at 32 bytes/ms). Mirrors TS
        ``trackOutboundPlayback``.
        """
        if num_bytes <= 0:
            return
        bytes_per_s = (
            8_000.0
            if getattr(self.audio_sender, "_input_is_mulaw_8k", False)
            else 32_000.0
        )
        now = time.time()
        chunk_s = num_bytes / bytes_per_s
        buffered_until = getattr(self, "_playback_buffered_until", 0.0)
        base = buffered_until if buffered_until > now else now
        self._playback_buffered_until = base + chunk_s
        # Per-turn playout total — the time axis for the heard-prefix
        # estimate (see ``_heard_response_prefix``). Reset at
        # ``_begin_speaking``.
        self._turn_playback_total_s = (
            getattr(self, "_turn_playback_total_s", 0.0) + chunk_s
        )

    def _heard_response_prefix(self) -> tuple[str, bool] | None:
        """Estimate the response prefix the caller actually HEARD this turn.

        The pipeline pushes audio faster than realtime, so at barge-in time
        ``heard = total_pushed - carrier_backlog`` seconds of audio have
        actually played. Mapped at sentence granularity against
        ``_turn_spoken_segments``: a sentence counts as heard once its
        playback has STARTED (``start <= heard``), so the sentence playing at
        the moment of interruption is included.

        Returns ``None`` when no segments were tracked this turn (nothing
        synthesized through the tracked path — callers fall back to the
        legacy full-text behaviour). Otherwise ``(heard_text,
        heard_everything)``. Mirrors TS ``heardResponsePrefix``.
        """
        segments = getattr(self, "_turn_spoken_segments", None)
        if not segments:
            return None
        total_s = getattr(self, "_turn_playback_total_s", 0.0)
        remaining_s = max(
            0.0, getattr(self, "_playback_buffered_until", 0.0) - time.time()
        )
        heard_s = max(0.0, total_s - remaining_s)
        heard = [text for text, start_s in segments if start_s <= heard_s]
        return " ".join(heard), len(heard) == len(segments)

    def _rewrite_last_assistant_entry(self, text: str) -> None:
        """Replace the text of the most recent assistant entry in the
        conversation history and the dashboard transcript. No-op when the
        last entry is not an assistant turn (e.g. the caller's next turn was
        already committed)."""
        for entries in (
            getattr(self, "conversation_history", None),
            getattr(self, "transcript_entries", None),
        ):
            if not entries:
                continue
            last = entries[-1]
            if isinstance(last, dict) and last.get("role") == "assistant":
                last["text"] = text

    def _maybe_truncate_completed_turn_history(self) -> None:
        """Heard-prefix semantics for a barge-in that lands
        AFTER the turn completed, while the carrier is still playing the
        buffered tail.

        The completed turn already recorded its FULL reply in history, but
        the caller only heard part of it before interrupting — a stateful
        agent runtime (Hermes / OpenClaw) would otherwise "remember saying"
        things the caller never heard. Rewrites the last assistant entry to
        the heard prefix + ``[interrupted by caller]``.

        MUST run BEFORE the cancel path resets ``_playback_buffered_until``
        (the backlog is the heard-prefix input). No-op when a turn is still
        in flight (the streaming path applies its own marker), when there is
        no backlog, or when everything was already heard.
        """
        dispatch = getattr(self, "_dispatch_task", None)
        if dispatch is not None and not dispatch.done():
            return
        remaining_s = getattr(self, "_playback_buffered_until", 0.0) - time.time()
        # Pause-and-resume froze the playback bookkeeping at pause time
        # (cursor snapped to 0, total rewound to the heard offset), so a
        # kill while paused has no live backlog — the frozen heard prefix
        # below is still the right input for the rewrite.
        if remaining_s <= 0 and not getattr(self, "_output_paused", False):
            return
        heard = self._heard_response_prefix()
        if heard is None:
            return
        heard_text, heard_everything = heard
        if heard_everything:
            return
        self._rewrite_last_assistant_entry(
            f"{heard_text} [interrupted by caller]"
            if heard_text
            else "[interrupted by caller]"
        )

    def _can_barge_in(self) -> bool:
        """Whether barge-in is allowed to fire right now.

        Gate length depends on whether AEC is active:
        ``MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_AEC`` with AEC (covers filter
        warmup), ``MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_NO_AEC`` (0.5 s) without
        — an anti-flicker margin that keeps PSTN barge-in responsive while
        rejecting the first burst of echo/noise before real speech.

        ``getattr`` is used so test fixtures that flip ``_is_speaking``
        directly (without going through ``_begin_speaking``) still
        permit barge-in to fire.
        """
        started_at = getattr(self, "_speaking_started_at", None)
        if started_at is None:
            return True
        # Anchor the gate on "first audio actually emitted", not on
        # ``_begin_speaking`` (which fires before the TTS provider's
        # first-byte latency has elapsed). Without this guard, background
        # noise picked up by VAD ~250 ms after ``_begin_speaking`` triggers
        # a self-cancel BEFORE any TTS chunk has reached the wire — the
        # agent's first turn becomes silence even though the SDK believes
        # it spoke. Mirrors TS ``canBargeIn``.
        first_audio_at = getattr(self, "_first_audio_sent_at", None)
        if first_audio_at is None:
            return False
        elapsed = time.time() - first_audio_at
        gate = (
            MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_AEC
            if getattr(self, "_aec", None) is not None
            else MIN_AGENT_SPEAKING_S_BEFORE_BARGE_IN_NO_AEC
        )
        return elapsed >= gate

    async def _end_speaking_with_grace(self) -> None:
        # Speech-event: agent stop edge (clean turn end).
        await self._emit_agent_speech_ended(interrupted=False)
        """Flip ``_is_speaking`` to False after a configurable grace period.

        TTS adapters typically signal "stream complete" while the carrier is
        still playing the tail of the last audio chunk. Resetting the flag
        immediately allows STT hallucinations on TTS echo to look like a
        fresh user turn. The grace window — controlled via
        ``PATTER_TTS_TAIL_GRACE_MS`` (default 1500 ms) — keeps the flag set
        while the trailing audio actually plays out. Setting the env var to
        ``0`` keeps the legacy synchronous behaviour for tests / soak runs.

        When the carrier still holds an audio backlog we pushed faster than
        realtime (``_playback_buffered_until`` in the future), the grace is
        preceded by a phase-1 wait that keeps the agent formally "speaking"
        — with barge-in armed — for the whole audible window. See the inline
        comments below.
        """
        # The turn body has finished pushing audio — from here on, a
        # pause-resume cycle owns re-arming the grace machinery (see
        # ``_resume_after_false_interruption``).
        self._turn_output_done = True
        try:
            grace_ms = int(os.environ.get("PATTER_TTS_TAIL_GRACE_MS", "1500"))
        except ValueError:
            grace_ms = 1500
        # NOTE: we do NOT flush ``_inbound_audio_ring`` here — the ring is
        # only drained on a real barge-in (where VAD confirmed user speech).
        # Flushing on every natural turn end was tried in an earlier
        # iteration and caused garbled out-of-order responses: the ring
        # captured during the agent's TTS contains audio with partially
        # cancelled echo and possibly over-cancelled user voice (Geigel
        # rho=0.6 misses quiet double-talk). Replaying that to STT on every
        # turn produced phantom transcripts that raced live STT input and
        # confused the LLM. Audio captured during the agent's turn that VAD
        # did NOT classify as speech is intentionally dropped at the next
        # ``_begin_speaking()``.
        if grace_ms <= 0:
            self._is_speaking = False
            self._tail_grace_active = False
            self._speaking_started_at = None
            self._first_audio_sent_at = None
            self._clear_pending_barge_in()
            # Hygiene: a turn that ended while paused (only reachable via
            # the LLM-error path — normal turns wait out the pause
            # decision) must not leak its pause buffers into idle time.
            self._discard_pause_state()
            await self._reset_barge_in_strategies()
            if self._suppressed_speech_pending:
                self._suppressed_speech_pending = False
                await self._flush_inbound_audio_ring()
            self._reset_vad()
            return

        gen = self._speaking_generation
        # Cancel any still-pending flip from a previous turn so at most one
        # grace task is ever in flight (parity with TS ``clearGraceTimer``).
        self._clear_grace_task()
        # Phase 1 — the carrier is still PLAYING audio we already pushed.
        # Agent-runtime LLMs (Hermes/OpenClaw) deliver the whole reply at
        # once, TTS outruns realtime, and the carrier buffers tens of
        # seconds of audio that keeps playing long after this method runs.
        # For that whole audible window the agent IS still speaking from the
        # caller's perspective: keep ``_is_speaking=True`` with
        # ``_tail_grace_active=False`` so VAD/transcript barge-in takes the
        # cancel path (``send_clear`` drops the carrier buffer) instead of
        # the next-turn rescue — without this, "the agent detects the
        # interruption but keeps talking". A barge-in meanwhile cancels this
        # task (``_clear_grace_task`` in the cancel paths). With no backlog
        # (token-paced LLMs) the tail grace starts immediately, exactly as
        # before. Phase 2 — the existing echo-tail grace.
        buffered_s = getattr(self, "_playback_buffered_until", 0.0) - time.time()
        if buffered_s <= 0:
            # The agent has finished pushing audio and the carrier played it
            # out; ``_is_speaking`` is now held only to suppress the fading
            # echo tail. Mark this as the tail-grace window so fast next-turn
            # speech is rescued as a new turn rather than mis-detected as a
            # barge-in.
            self._tail_grace_active = True

        async def _flip_after_grace() -> None:
            try:
                # Phase 1: wait out the estimated carrier-side backlog.
                while True:
                    remaining = (
                        getattr(self, "_playback_buffered_until", 0.0) - time.time()
                    )
                    if remaining <= 0:
                        break
                    await asyncio.sleep(remaining)
                    if self._speaking_generation != gen:
                        return
                if self._speaking_generation != gen:
                    return
                # Phase 2: the echo-tail grace window.
                self._tail_grace_active = True
                await asyncio.sleep(grace_ms / 1000)
                # Only reset if no newer turn started while we slept; a
                # newer turn would have bumped ``_speaking_generation``.
                if self._speaking_generation == gen:
                    self._is_speaking = False
                    self._tail_grace_active = False
                    self._speaking_started_at = None
                    self._first_audio_sent_at = None
                    self._clear_pending_barge_in()
                    # See the zero-grace branch — drop any pause state a
                    # turn that errored mid-pause left behind.
                    self._discard_pause_state()
                    await self._reset_barge_in_strategies()
                    if self._suppressed_speech_pending:
                        self._suppressed_speech_pending = False
                        await self._flush_inbound_audio_ring()
                    # Reset VAD so any stuck SPEECH state from echo /
                    # loopback during the agent's turn does not block the
                    # next user utterance from emitting ``speech_start``.
                    self._reset_vad()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("tts grace flip failed: %s", exc)

        self._grace_task = asyncio.create_task(_flip_after_grace())

    def _clear_grace_task(self) -> None:
        """Cancel the pending grace-flip task, if any. Idempotent; safe from
        test fixtures built via ``object.__new__`` (no ``__init__``)."""
        task = getattr(self, "_grace_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._grace_task = None

    async def _end_tail_grace_for_new_turn(self) -> None:
        """End the post-TTS tail-grace window because the user has begun
        their next turn.

        Unlike a barge-in, the agent's response already played out in full —
        there is nothing to cancel and no turn was interrupted. We flip the
        speaking flag off (bumping ``_speaking_generation`` so the scheduled
        grace-flip task no-ops), recover any leading audio the self-hearing
        guard captured into the ring (the user's first ~250 ms, which VAD
        needed before it could emit ``speech_start``), and let the live STT
        stream take over. Crucially we do NOT call ``send_clear``,
        ``record_bargein_detected`` or ``record_turn_interrupted`` — none of
        those apply to a turn that completed normally.

        Without this, fast next-turn speech (humans reply in 200-700 ms, well
        inside the 1500 ms default grace) is withheld from STT and recorded
        as an empty ``[interrupted]`` turn, after which the agent goes silent
        for the rest of the call.
        """
        self._is_speaking = False
        self._tail_grace_active = False
        self._speaking_started_at = None
        self._first_audio_sent_at = None
        # Tail grace only starts after the playback cursor drained (phase 1
        # of ``_end_speaking_with_grace``), so no carrier backlog is left.
        self._playback_buffered_until = 0.0
        # Invalidate the pending grace-flip task scheduled by
        # ``_end_speaking_with_grace`` so it cannot later flip state on a turn
        # that has already moved on (bump the generation AND cancel the task —
        # parity with TS ``clearGraceTimer``).
        self._speaking_generation += 1
        self._clear_grace_task()
        self._clear_pending_barge_in()
        # The next turn owns the floor — any stale pause state is void.
        self._discard_pause_state()
        await self._reset_barge_in_strategies()
        # Recover the user's leading words. Same rationale as the barge-in
        # flush — but here it is the only audio recovery, since the agent
        # already stopped and no new TTS will overwrite it.
        self._suppressed_speech_pending = False
        await self._flush_inbound_audio_ring()

    async def _reset_barge_in_strategies(self) -> None:
        if not self._barge_in_strategies:
            return
        from getpatter.services.barge_in_strategies import reset_strategies

        await reset_strategies(self._barge_in_strategies)

    def _reset_vad(self) -> None:
        """Reset the active VAD provider's per-utterance state.

        No-op when the provider does not implement the optional
        :py:meth:`getpatter.providers.base.VADProvider.reset` hook
        (default implementation in ``VADProvider`` is a no-op). Safe to
        call from any context — failures are swallowed; a flaky reset
        must never silently kill barge-in for every subsequent turn.

        Parity with TS ``resetVad``.
        """
        vad = getattr(self.agent, "vad", None) or self._auto_vad
        if vad is None:
            return
        try:
            vad.reset()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("VAD reset threw: %s", exc)

    async def _flush_inbound_audio_ring(self) -> None:
        """Replay the audio captured by the self-hearing guard right
        before a confirmed barge-in.

        VAD's ``min_speech_duration`` window (default 250 ms) means
        ``speech_start`` fires only AFTER the user has been talking
        for that long; without this replay STT sees only the tail of
        the user's interruption and produces "the line is breaking up"
        partial transcripts. We deliberately do NOT call this on
        natural turn end — see the comment in
        ``_end_speaking_with_grace`` for why.
        """
        if self._stt is None or not self._inbound_audio_ring:
            return
        replayed = len(self._inbound_audio_ring)
        # Snapshot before the awaits below — a concurrent media frame could
        # otherwise mutate the deque mid-iteration (RuntimeError).
        frames = list(self._inbound_audio_ring)
        self._inbound_audio_ring.clear()
        for buf in frames:
            try:
                await self._stt.send_audio(buf)
            except Exception as exc:
                logger.debug("send_audio replay failed: %s", exc)
        logger.debug(
            "Flushed %d pre-turn-end frame(s) (~%d ms) to STT",
            replayed,
            replayed * 20,
        )

    def _is_tts_output_format_native_for_carrier(self) -> bool:
        """Return True when the TTS adapter's output_format is already in the
        carrier's wire codec — meaning no client-side resample/transcode is
        needed in ``TwilioAudioSender.send_audio``.

        Every carrier wire here is μ-law 8 kHz — the SDK's own
        ``streaming_start`` pins Telnyx to PCMU (the old ``pcm_16000``
        expectation shipped raw PCM16 onto the μ-law wire: static).

        Parity with TS ``StreamHandler.isTtsOutputFormatNativeForCarrier``.
        """
        if self._tts is None:
            return False
        fmt = getattr(self._tts, "output_format", None)
        if not isinstance(fmt, str):
            return False
        return fmt == "ulaw_8000"

    def _tap_pipeline_agent_audio(self, chunk: bytes) -> None:
        """Local-recording tap for outbound pipeline TTS chunks.

        Sits next to the AEC far-end taps. Unlike AEC (which must skip
        non-PCM bytes), the recording tap DECODES on the carrier-native fast
        path: when the TTS adapter emits the wire codec directly
        (``_tts_output_format_native_for_carrier``) the chunk is μ-law 8 kHz
        on mulaw carriers (Twilio / Plivo) — decode + resample in the
        recorder so it always receives PCM16 16 kHz. Telnyx-native
        (``pcm_16000``) and the default transcode path are already
        PCM16 16 kHz.
        """
        # ``getattr`` is defensive against test fixtures built via
        # ``object.__new__`` (no ``__init__``) — same pattern as the
        # ``_turn_spoken_segments`` access in ``_synthesize_sentence``.
        if getattr(self, "local_recorder", None) is None:
            return
        encoding = (
            "mulaw_8k"
            if (
                getattr(self, "_tts_output_format_native_for_carrier", False)
                and getattr(self, "_for_twilio", False)
            )
            else "pcm16_16k"
        )
        self._tap_agent_audio(chunk, encoding)

    # 40 ms @ 16 kHz mono PCM16 = 1280 bytes. Sized to mirror the smallest
    # live-TTS chunk boundary so cancel granularity (mark/clear bookkeeping)
    # is identical regardless of whether the firstMessage came from the
    # prewarm cache or a live ``tts.synthesize`` stream.
    _PREWARM_CHUNK_BYTES: int = 1280
    # Maximum unconfirmed Twilio marks while streaming firstMessage. Each
    # chunk is 40 ms of audio at 16 kHz PCM16, so a window of 3 caps the
    # in-flight queue at ~120 ms. This means a barge-in's ``send_clear`` has
    # at most ~120 ms of buffered audio to flush — vs. ~2-5 s with the
    # previous burst-send code (BUG #128). 3 hit the smallest barge-in cap
    # without audible playback gaps under typical PSTN RTT in 2026-05
    # acceptance.
    _FIRST_MESSAGE_MARK_WINDOW: int = 3
    # Per-chunk soft timeout (s) for awaiting a mark echo. Caps the
    # deadlock window when a carrier (or a test double) never echoes —
    # playout may glitch by one chunk on timeout but the call stays alive.
    _MARK_AWAIT_TIMEOUT_S: float = 0.5
    # Bytes-per-millisecond for a 16 kHz PCM16 mono stream. Used by
    # ``_send_paced_first_message_bytes`` to translate chunk size into a
    # playout-duration sleep so we never deliver faster than the carrier
    # can decode + play out (which manifested as severe crackling on the
    # HTTP-TTS path with client-side resampling). 16000 samples/sec × 2
    # bytes/sample = 32 bytes/ms.
    _PCM16_16K_BYTES_PER_MS: int = 32

    def _drain_pending_marks(self) -> None:
        """Resolve every entry in ``_pending_marks`` and empty the FIFO.

        Idempotent — safe to call from the barge-in cancel path and again
        from the grace flip without leaking unresolved futures.
        """
        if not self._pending_marks:
            return
        for _name, fut in self._pending_marks:
            if not fut.done():
                try:
                    fut.set_result(None)
                except asyncio.InvalidStateError:
                    pass
        self._pending_marks.clear()

    async def _send_mark_awaitable(self) -> asyncio.Future | None:
        """Send a Twilio ``mark`` event and return a future that resolves
        when the carrier echoes it back (via :meth:`on_mark`), or when
        :meth:`_drain_pending_marks` runs. Returns ``None`` on non-Twilio
        carriers — the caller should fall back to time-based pacing.
        """
        if not self._for_twilio:
            return None
        self._first_message_mark_counter += 1
        mark_name = f"fm_{self._first_message_mark_counter}"
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending_marks.append((mark_name, fut))
        try:
            await self.audio_sender.send_mark(mark_name)
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.debug("send_mark failed (%s): %s", mark_name, exc)
            # Drop the waiter so the queue can't fill with orphans.
            for idx, (name, f) in enumerate(self._pending_marks):
                if name == mark_name:
                    self._pending_marks.pop(idx)
                    break
            if not fut.done():
                fut.set_result(None)
        return fut

    async def _wait_for_mark_window(self) -> None:
        """Block until the in-flight mark queue depth is below
        ``_FIRST_MESSAGE_MARK_WINDOW``. Returns immediately on cancel
        because :meth:`_drain_pending_marks` resolves every pending future.
        """
        while (
            self._is_speaking
            and len(self._pending_marks) >= self._FIRST_MESSAGE_MARK_WINDOW
        ):
            _name, oldest = self._pending_marks[0]
            try:
                await asyncio.wait_for(
                    asyncio.shield(oldest),
                    timeout=self._MARK_AWAIT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                # Drop the head so subsequent loops don't deadlock on the
                # same mark forever. Twilio mark echo may have been lost
                # in transit; carrier playback will continue regardless.
                pass
            # Pop the head if still present (a successful echo would have
            # done it via ``on_mark``; only a timeout leaves it in place).
            if self._pending_marks and self._pending_marks[0][0] == _name:
                self._pending_marks.pop(0)

    async def on_mark(self, mark_name: str) -> None:
        """Handle a Twilio ``mark`` echo and resolve the matching firstMessage
        waiter (if any). Marks are matched FIFO: an echo for ``fm_3`` also
        resolves ``fm_1`` and ``fm_2`` in case the carrier batches echoes.
        """
        if not mark_name:
            return
        idx = -1
        for i, (name, _fut) in enumerate(self._pending_marks):
            if name == mark_name:
                idx = i
                break
        if idx < 0:
            return
        resolved = self._pending_marks[: idx + 1]
        del self._pending_marks[: idx + 1]
        for _name, fut in resolved:
            if not fut.done():
                try:
                    fut.set_result(None)
                except asyncio.InvalidStateError:
                    pass

    async def _stream_prewarm_bytes(self, prewarm_bytes: bytes) -> bool:
        """Stream a cached firstMessage buffer in pacing-friendly chunks."""
        return await self._send_paced_first_message_bytes(prewarm_bytes)

    async def _play_first_message(self) -> None:
        """Stream the configured greeting — runs as a BACKGROUND task.

        ``start()`` used to execute this inline, which blocked the carrier
        bridge's single read loop for the whole greeting: no media frames
        were processed (VAD/barge-in structurally impossible on the first
        message), ``stop`` frames went unnoticed, and prewarmed mark-gated
        pacing starved because mark acks could never be read (0.5 s timeout
        per chunk → ~13x slower than realtime, guaranteed jitter underrun).
        ``start()`` awaits ``_begin_speaking(is_first_message=True)`` BEFORE
        spawning this task so the self-hearing guard engages from the very
        first inbound frame.
        """
        if self.metrics is not None:
            self.metrics.start_turn()
        # Mark the agent as speaking for the duration of the first
        # message — without this, the self-hearing guard never
        # engages, the user's audio (mixed with TTS bleed) is
        # forwarded to STT and produces garbage transcripts, and
        # the ring buffer for pre-barge-in audio is never
        # populated. Mirrors the per-turn behaviour in
        # `_process_streaming_response` / `_process_regular_response`.
        #
        # ``is_first_message=True`` pre-stamps ``_first_audio_sent_at``
        # synchronously so the barge-in gate runs in parallel with TTS
        # TTFB instead of only after audio arrives — without this, the
        # firstMessage is effectively un-interruptible for 300-800 ms.
        first_chunk_sent = False
        # Drop any stale PCM16 carry byte from a prior synth (none at call
        # start, but defensive for parity with TS ``ttsByteCarry = null``).
        self.audio_sender.reset_pcm_carry()
        # Check the prewarm cache first. When ``Patter.call`` was made
        # with ``agent.prewarm_first_message=True`` the firstMessage
        # has already been synthesised during the ringing window — we
        # stream the bytes directly through the carrier-side
        # AudioSender (which handles native-rate → carrier-rate
        # resampling) and skip the TTS round-trip entirely.
        prewarm_bytes: bytes | None = None
        if self._pop_prewarm_audio is not None:
            try:
                prewarm_bytes = self._pop_prewarm_audio(self.call_id)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.debug("pop_prewarm_audio raised: %s", exc)
                prewarm_bytes = None
        try:
            if prewarm_bytes:
                if self.metrics is not None:
                    self.metrics.record_tts_first_byte()
                first_chunk_sent = await self._stream_prewarm_bytes(prewarm_bytes)
            else:
                # Streaming TTS path (no prewarm cache). Uses the same
                # simple per-chunk send as _synthesize_sentence —
                # ElevenLabs HTTP streams at near-real-time speed so the
                # carrier-side buffer stays bounded without mark-gated
                # pacing.  Routing streaming chunks through
                # _send_paced_first_message_bytes caused crackling: its
                # drain+reset on every HTTP chunk destroyed mark
                # back-pressure continuity and the per-sub-chunk sleep
                # slowed delivery below Twilio's playout rate, producing
                # periodic buffer underruns.  The prewarm path (a single
                # pre-synthesised buffer) still uses
                # _send_paced_first_message_bytes because that buffer can
                # be several seconds long and needs pacing.
                async for audio_chunk in self._tts.synthesize(
                    self.agent.first_message
                ):
                    if not self._is_speaking:
                        break
                    if not first_chunk_sent:
                        first_chunk_sent = True
                        if self.metrics is not None:
                            self.metrics.record_tts_first_byte()
                    # AEC far-end tap gated on the
                    # carrier-native fast path: when the TTS adapter was
                    # auto-flipped to ulaw_8000, these bytes are mulaw
                    # wire bytes — pushing them into an AEC built for
                    # int16 PCM 16 kHz corrupted the reference (and an
                    # odd-length mulaw chunk crashed np.frombuffer).
                    if self._aec is not None and not getattr(
                        self, "_tts_output_format_native_for_carrier", False
                    ):
                        self._aec.push_far_end(audio_chunk)
                    # Local-recording tap (agent side) — decodes on the
                    # carrier-native μ-law fast path instead of skipping.
                    self._tap_pipeline_agent_audio(audio_chunk)
                    await self.audio_sender.send_audio(audio_chunk)
                    self._mark_first_audio_sent()
        finally:
            # Drop any partial int16 byte to prevent cross-turn corruption
            # if the stream threw before a complete sample was delivered.
            self.audio_sender.reset_pcm_carry()
            # Flip back to not-speaking with grace so the ring
            # buffer accumulated during the intro is flushed and
            # the next user utterance is recognised cleanly.
            await self._end_speaking_with_grace()
        if first_chunk_sent:
            # History append must NOT depend on metrics being enabled:
            # with ``metrics=None`` the greeting was absent from LLM
            # context (the model could re-greet) and from transcripts.
            self.conversation_history.append(
                {
                    "role": "assistant",
                    "text": self.agent.first_message,
                    "timestamp": time.time(),
                }
            )
            # Echo-guard reference: under forward-STT-while-speaking the
            # guard compared echoes against an EMPTY reference during the
            # greeting (the highest-echo window of the call) and treated
            # the agent's own first words as a user barge-in.
            self._current_agent_spoken_text = self.agent.first_message
        if first_chunk_sent and self.metrics is not None:
            # Bill the firstMessage TTS characters — they were synthesised
            # at ElevenLabs (or the configured TTS provider) and the
            # customer pays for them. The previous flow only called
            # ``record_turn_complete`` here, which finalises the turn
            # but does NOT increment ``_total_tts_characters`` — so a
            # 5-turn call with an 82-char greeting was under-billed
            # by ~22% on TTS cost. ``record_tts_complete`` is the
            # canonical accumulator entry point for TTS char billing.
            self.metrics.record_tts_complete(self.agent.first_message)
            turn = self.metrics.record_turn_complete(self.agent.first_message)
            await self._emit_turn_metrics(turn)

    async def _send_paced_first_message_bytes(self, bytes_: bytes) -> bool:
        """Iterate ``bytes_`` as ``_PREWARM_CHUNK_BYTES``-sized PCM16 slices
        and forward each via ``audio_sender.send_audio`` with mark-gated
        pacing (Twilio) or playout-time-based pacing (Telnyx).

        Caps the carrier-side buffer at ``_FIRST_MESSAGE_MARK_WINDOW``
        chunks so a barge-in's ``send_clear`` has at most ~120 ms (Twilio)
        or zero (Telnyx, immediately after the latest sleep) of audio to
        flush. The previous burst-send code let Twilio's buffer reach
        several seconds — a barge-in's ``send_clear`` race-lost against
        the queued media frames and the agent kept talking on the user's
        earpiece for up to ~2 s after the user spoke (BUG #128).

        Bails immediately when ``_is_speaking`` flips to ``False`` — both
        via the loop's pre-iter check and via :meth:`_drain_pending_marks`
        (called from the barge-in cancel path) which unblocks any
        in-flight :meth:`_wait_for_mark_window` await.

        Returns ``True`` when at least one chunk hit the wire — the caller
        uses that to decide whether to record the TTS-first-byte /
        turn-complete metrics.
        """
        # Reset the per-send mark counter so each invocation produces a
        # fresh ``fm_1, fm_2, ...`` sequence. Without this the counter
        # grows monotonically across turns on a re-used handler and a
        # stale ``fm_N`` echo from an earlier turn could match a mark
        # name issued later, corrupting the FIFO matching in
        # ``on_mark``. The ``_pending_marks`` queue is also expected
        # empty here by the caller's cancel / cleanup paths; if it is
        # not (defensive re-entry) we drain before resetting.
        if self._pending_marks:
            self._drain_pending_marks()
        self._first_message_mark_counter = 0
        first_chunk_sent = False
        # Once the mark window is first filled we switch to playout-time
        # pacing to prevent batch-ACK bursts from draining the carrier
        # jitter buffer. Before that we send in burst so the first
        # ``_FIRST_MESSAGE_MARK_WINDOW`` chunks pre-fill the PSTN jitter
        # buffer (250–1500 ms). The earlier experiment of pure-burst
        # delivery (no per-chunk sleep) produced severe carrier-side
        # crackling on the HTTP TTS path (pcm_16000 → mulaw_8000 client-
        # side resample) because the burst arrived at Twilio faster than
        # its media-stream decoder could process — even though the docs
        # say "of any size". The pace-by-playout path is the robust
        # default; mark back-pressure remains as an extra guard.
        initial_fill_complete = False
        for i in range(0, len(bytes_), self._PREWARM_CHUNK_BYTES):
            if not self._is_speaking:
                break  # barge-in mid-buffer — stop now
            await self._wait_for_mark_window()
            if not self._is_speaking:
                break
            chunk = bytes_[i : i + self._PREWARM_CHUNK_BYTES]
            if not first_chunk_sent:
                first_chunk_sent = True
            # Same carrier-native gate as the live-TTS far-end taps: prewarm
            # bytes are mulaw 8 kHz on Twilio/Plivo.
            if self._aec is not None and not getattr(
                self, "_tts_output_format_native_for_carrier", False
            ):
                self._aec.push_far_end(chunk)
            # Local-recording tap (agent side) — decodes on the
            # carrier-native μ-law fast path instead of skipping.
            self._tap_pipeline_agent_audio(chunk)
            await self.audio_sender.send_audio(chunk)
            self._mark_first_audio_sent()
            mark_awaitable = await self._send_mark_awaitable()
            if (
                not initial_fill_complete
                and len(self._pending_marks) >= self._FIRST_MESSAGE_MARK_WINDOW
            ):
                initial_fill_complete = True
            # Telnyx has no mark concept — always pace by playout time.
            # Twilio: the first ``_FIRST_MESSAGE_MARK_WINDOW`` chunks go
            # out in burst to pre-fill the PSTN jitter buffer, then
            # playout-time pacing kicks in (via the sticky
            # ``initial_fill_complete`` flag) to prevent batch-ACK bursts
            # from draining the buffer → crackling.
            if mark_awaitable is None or initial_fill_complete:
                # Derive the byte rate from the active output format: on the
                # carrier-native path the prewarm cache holds mulaw 8 kHz
                # (8 B/ms, not 32) — pacing those bytes at the PCM16-16k rate
                # delivered 4x faster than playout, re-opening the barge-in
                # flush window the pacing exists to bound. Mirrors the TS
                # ``bytesPerMs`` selection.
                bytes_per_ms = (
                    8
                    if getattr(self, "_tts_output_format_native_for_carrier", False)
                    else self._PCM16_16K_BYTES_PER_MS
                )
                playout_ms = max(1, len(chunk) // bytes_per_ms)
                await asyncio.sleep(playout_ms / 1000.0)
        return first_chunk_sent

    async def cleanup(self) -> None:
        """Cancel the STT loop and close STT/TTS/remote-message adapters."""
        self._cancel_max_call_watchdog()
        # Abort any in-flight LLM stream and close any in-flight TTS WS so
        # the run_pipeline_llm / synthesize awaits unblock immediately
        # instead of waiting up to 30 s for their own watchdog timers.
        # Without this, the carrier's stop event ends the call but a
        # pending TTS WS frame-wait fires a stale "LLM loop error" /
        # "TTS streaming error" log line tens of seconds later. Parity
        # with TS ``StreamHandler.handleStop`` / ``handleWsClose``.
        cancel_event = getattr(self, "_llm_cancel_event", None)
        if cancel_event is not None:
            cancel_event.set()
        _tts_cancel = getattr(getattr(self, "_tts", None), "cancel_active_stream", None)
        if callable(_tts_cancel):
            try:
                _tts_cancel()
            except Exception:
                pass
        # Stop a still-running greeting task before tearing anything down.
        _fm_task = getattr(self, "_first_message_task", None)
        if _fm_task is not None and not _fm_task.done():
            _fm_task.cancel()
            try:
                await _fm_task
            except (asyncio.CancelledError, Exception):
                pass
        self._first_message_task = None
        # Cancel the STT consumer FIRST: while cleanup awaited the cancelled
        # dispatch task, the still-alive STT loop (blocked in
        # ``_await_dispatch_settle`` on that same task) could wake first and
        # respawn a fresh dispatch with a fresh cancel_event — an orphan turn
        # running LLM + TTS against closed adapters after teardown.
        if self._stt_task:
            self._stt_task.cancel()
            try:
                await self._stt_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stt_task = None
        # PREEMPTIVE GENERATION: stop the interim-stability watcher and tear
        # down any in-flight speculation (teardown — not a miss) before
        # adapters close underneath it. Runs AFTER the STT loop cancel so no
        # spawn source (STT interim / stability watcher) can re-register a
        # speculation while the abort is awaited.
        self._cancel_interim_stability_task()
        try:
            await self._abort_speculation(reason="cleanup", count_miss=False)
        except Exception:  # pragma: no cover - teardown must never raise
            logger.debug("speculation cleanup failed", exc_info=True)
        # Hard-cancel the backgrounded turn dispatch (teardown backstop) so no
        # orphan task touches a finalized handler. The cancel_event.set() above
        # lets a post-first-token turn break gracefully; the cancel covers a
        # turn parked pre-first-token on a hung agent request.
        _dispatch_task = getattr(self, "_dispatch_task", None)
        if _dispatch_task is not None and not _dispatch_task.done():
            _dispatch_task.cancel()
            try:
                await _dispatch_task
            except (asyncio.CancelledError, Exception):
                pass
        self._dispatch_task = None
        # Drop any pending barge-in timeout BEFORE we tear down metrics /
        # adapters. Without this, a call that ends while a barge-in is
        # pending leaves an asyncio.Task scheduled to fire
        # ``_barge_in_confirm_s`` later and call
        # ``metrics.record_overlap_end`` on a finalised metrics object —
        # a slow leak in long-running servers and a race producing
        # spurious overlap_end events. Idempotent: safe to call when no
        # pending state exists.
        self._clear_pending_barge_in()
        # Drop pause-and-resume buffers and wake any pause-decision waiter
        # so a call ending mid-pause cannot strand a loop awaiting the
        # (now cancelled) resume timer.
        self._discard_pause_state()
        # Drop any active semantic-turn hold so its wall-clock backstop task
        # cannot fire after teardown and call ``stt.finalize`` on a closed
        # adapter. Idempotent; no-op when no ``turn_detector`` is configured.
        self._cancel_semantic_hold()
        # Cancel any pending tail-grace flip task so it does not sleep past
        # teardown and touch a finalised handler.
        self._clear_grace_task()
        # Resolve every pending firstMessage mark future before tearing
        # down adapters. Without this, a call that ends abnormally mid
        # firstMessage (carrier WS drop, hangup during the paced sender)
        # leaves orphan ``asyncio.Future`` instances awaited by the send
        # loop that nothing will ever resolve.
        if getattr(self, "_pending_marks", None) is not None:
            self._drain_pending_marks()
        # Reset the firstMessage mark counter so a re-used handler
        # instance starts ``fm_<n>`` numbering at 1 on the next call.
        # See ``_send_paced_first_message_bytes`` for the per-send reset
        # that protects the within-call path.
        self._first_message_mark_counter = 0
        # Per-resource guards: one raising close used to skip every later
        # close (leaked TTS/remote sockets on an STT close failure).
        if self._stt is not None:
            try:
                await self._stt.close()
            except Exception as _exc:  # noqa: BLE001 - teardown must continue
                logger.warning("STT close failed: %s", _exc)
        if self._tts is not None:
            try:
                await self._tts.close()
            except Exception as _exc:  # noqa: BLE001 - teardown must continue
                logger.warning("TTS close failed: %s", _exc)
        if self._remote_handler is not None:
            try:
                await self._remote_handler.close()
            except Exception as _exc:  # noqa: BLE001 - teardown must continue
                logger.warning("Remote-message close failed: %s", _exc)
        # Close MCP sessions opened by _init_mcp_tools (pipeline mode).
        try:
            await self._close_mcp()
        except Exception as _exc:  # noqa: BLE001 - teardown must continue
            logger.debug("MCP close failed: %s", _exc)
        # Finalize the local recording WAV (guarded + idempotent) — covers
        # abnormal teardown too: the bridge ``finally`` block always runs
        # ``cleanup()``, so a truncated call still gets its header patched.
        self._close_local_recorder()
        # Flush and discard the inbound resampler tail on cleanup (owned by
        # the input processing chain since slice 1 of the pipeline-stages
        # decomposition). ``getattr`` so test fixtures built via
        # ``object.__new__`` (no ``__init__``) stay safe.
        chain = getattr(self, "_input_chain", None)
        if chain is not None:
            chain.flush()
            self._input_chain = None

    @property
    def stt(self):
        """Expose STT adapter for post-call metrics queries."""
        return self._stt


# ---------------------------------------------------------------------------
# Shared post-call metrics helpers
# ---------------------------------------------------------------------------


async def fetch_deepgram_cost(metrics, stt, deepgram_key: str) -> None:
    """Query Deepgram API for actual STT cost after a call ends."""
    if (
        metrics is None
        or stt is None
        or not deepgram_key
        or not hasattr(stt, "request_id")
        or not stt.request_id
    ):
        return
    try:
        import httpx as _httpx

        async with _httpx.AsyncClient() as http:
            proj_resp = await http.get(
                "https://api.deepgram.com/v1/projects",
                headers={"Authorization": f"Token {deepgram_key}"},
                timeout=5.0,
            )
            if proj_resp.status_code == 200:
                projects = proj_resp.json().get("projects", [])
                if projects:
                    project_id = projects[0].get("project_id", "")
                    if project_id:
                        req_resp = await http.get(
                            f"https://api.deepgram.com/v1/projects/{project_id}/requests/{stt.request_id}",
                            headers={"Authorization": f"Token {deepgram_key}"},
                            timeout=5.0,
                        )
                        if req_resp.status_code == 200:
                            usd = (
                                req_resp.json()
                                .get("response", {})
                                .get("details", {})
                                .get("usd", None)
                            )
                            if usd is not None:
                                metrics.set_actual_stt_cost(float(usd))
                                logger.debug("Deepgram actual cost: $%s", usd)
    except Exception as exc:
        logger.debug("Could not fetch Deepgram request cost: %s", exc)
