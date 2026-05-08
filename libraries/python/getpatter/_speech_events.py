"""Speech-edge events for turn-taking instrumentation.

Defines :class:`SpeechEvents`, the per-call dispatcher that fires user-facing
async callbacks and (when available) records OpenTelemetry span events on the
current call span. The 7 events mirror the public APIs of LiveKit Agents,
Pipecat and OpenAI Realtime so downstream metrics map onto the canonical
Hamming AI / Coval / Cekura voice-agent metric set without translation.

This module is private (leading underscore). The public surface is the 7
``on_*`` attributes plus :meth:`conversation_state` exposed on the
:class:`getpatter.Patter` instance, and the :class:`SpeechEvents` class itself
is re-exported at the package root for advanced users (custom adapters,
test harnesses).

Industry alignment table::

    User VAD start  : LiveKit ``user_state_changed -> speaking`` /
                      Pipecat ``VADUserStartedSpeakingFrame`` /
                      OpenAI Realtime ``input_audio_buffer.speech_started``
    User VAD end    : ``..._stopped`` (raw VAD edge — *not* end-of-utterance)
    User EOU        : LiveKit ``user_turn_completed`` / Pipecat
                      ``UserStoppedSpeakingFrame`` / OpenAI Realtime
                      ``input_audio_buffer.committed``
    Agent first wire: Pipecat ``BotStartedSpeakingFrame``
    Agent done      : Pipecat ``BotStoppedSpeakingFrame``
    LLM first token : Pipecat ``LLMFullResponseStartFrame`` (per-turn TTFT)
    TTS first audio : Pipecat ``OutputAudioRawFrame`` (first per turn)

Both VAD edge and end-of-utterance are surfaced separately because they are
two different signals (`silence_gap_ms_max` wants the EOU; `cross_talk_pct`
wants the raw VAD edge).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("getpatter.events")


# Type aliases — keep the signature short at call sites.
SpeechEventCallback = Callable[[dict], Awaitable[None] | None]


class UserState(StrEnum):
    """Per-side user speech state — mirror of the TypeScript ``UserState``
    string-literal union.

    Values match LiveKit Agents' ``user_state_changed`` vocabulary so
    downstream observability dashboards (Hamming AI / Coval / Cekura) can
    map Patter events onto the canonical voice-agent metric set without
    translation.
    """

    LISTENING = "listening"
    SPEAKING = "speaking"
    THINKING = "thinking"
    AWAY = "away"


class AgentState(StrEnum):
    """Per-side agent speech state — mirror of the TypeScript ``AgentState``
    string-literal union.

    Values match LiveKit Agents' ``agent_state_changed`` vocabulary; see
    :class:`UserState` for the rationale.
    """

    INITIALIZING = "initializing"
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class EouTrigger(StrEnum):
    """Reason the dispatcher fired :meth:`SpeechEvents.fire_user_speech_eos`.

    Mirror of the TypeScript ``EouTrigger`` string-literal union. The
    runner inspects this value to decide whether the EOU was committed by
    a raw VAD silence interval, a semantic turn-detector model, or an
    explicit caller-driven commit (e.g. dialler "user pressed #").
    """

    VAD_SILENCE = "vad_silence"
    SEMANTIC_TURN_DETECTOR = "semantic_turn_detector"
    MANUAL_COMMIT = "manual_commit"


@dataclass(frozen=True)
class ConversationStateSnapshot:
    """Read-only snapshot of the per-side conversation state.

    Mirror of the TypeScript ``ConversationStateSnapshot`` interface.
    Returned by :meth:`SpeechEvents.conversation_state_snapshot` for
    callers that prefer a typed value over the legacy ``dict[str, str]``
    that :attr:`SpeechEvents.conversation_state` continues to return for
    backwards compatibility.
    """

    user: UserState
    agent: AgentState


# State-machine values mirror LiveKit's user/agent state vocabulary.
# Kept as plain tuples for backwards compatibility with callers that
# imported the constants directly. New code should prefer :class:`UserState`
# / :class:`AgentState`.
USER_STATES = tuple(s.value for s in UserState)
AGENT_STATES = tuple(s.value for s in AgentState)


class SpeechEvents:
    """Per-call dispatcher for the seven turn-taking events.

    A single instance is shared by every :class:`Patter` instance and
    survives across calls — the per-turn state (``turn_idx``,
    ``first_token_for_turn``, ``first_audio_for_turn``) lives here too so
    the runner sees a monotonically-increasing turn index across a session.

    Backwards compatibility
    -----------------------
    Every callback defaults to ``None`` and the OTel emission is optional
    (no-op if ``opentelemetry`` is not installed). Existing users who never
    set a callback see exactly the previous behaviour and zero overhead.
    """

    __slots__ = (
        "on_user_speech_started",
        "on_user_speech_ended",
        "on_user_speech_eos",
        "on_agent_speech_started",
        "on_agent_speech_ended",
        "on_llm_token",
        "on_audio_out",
        "_user_state",
        "_agent_state",
        "_turn_idx",
        "_first_token_for_turn",
        "_first_audio_for_turn",
        "_call_start_ms",
    )

    def __init__(self) -> None:
        # Public callback slots — any of them may be set by the user.
        self.on_user_speech_started: Optional[SpeechEventCallback] = None
        self.on_user_speech_ended: Optional[SpeechEventCallback] = None
        self.on_user_speech_eos: Optional[SpeechEventCallback] = None
        self.on_agent_speech_started: Optional[SpeechEventCallback] = None
        self.on_agent_speech_ended: Optional[SpeechEventCallback] = None
        self.on_llm_token: Optional[SpeechEventCallback] = None
        self.on_audio_out: Optional[SpeechEventCallback] = None

        # State machine — read via ``conversation_state``.
        self._user_state: str = "listening"
        self._agent_state: str = "initializing"

        # Per-turn cursors. ``turn_idx`` increments on every committed EOU.
        self._turn_idx: int = 0
        self._first_token_for_turn: bool = True
        self._first_audio_for_turn: bool = True

        # Optional call start (ms since epoch) — used to compute
        # ``audio_offset_ms`` payloads when the caller does not provide one.
        self._call_start_ms: Optional[int] = None

    # ------------------------------------------------------------------
    # Lifecycle / state queries
    # ------------------------------------------------------------------

    @property
    def conversation_state(self) -> dict[str, str]:
        """Snapshot of the current per-side state.

        Returns ``{"user": <user_state>, "agent": <agent_state>}``. Mirrors
        LiveKit's ``user_state_changed`` / ``agent_state_changed`` payloads
        and is safe to call at any time (read-only, no I/O).
        """
        return {"user": self._user_state, "agent": self._agent_state}

    @property
    def conversation_state_snapshot(self) -> ConversationStateSnapshot:
        """Typed, immutable parity-mirror of the TypeScript
        ``conversationState`` getter (returns ``ConversationStateSnapshot``).

        Prefer this over :attr:`conversation_state` in new code — the
        typed snapshot lets type-checkers catch state-name typos and gives
        IDEs autocomplete on ``snapshot.user`` / ``snapshot.agent``. The
        legacy ``dict[str, str]`` accessor stays for backwards
        compatibility.
        """
        return ConversationStateSnapshot(
            user=UserState(self._user_state),
            agent=AgentState(self._agent_state),
        )

    @property
    def turn_idx(self) -> int:
        """Current 0-based turn index. Increments on every EOU commit."""
        return self._turn_idx

    def mark_call_started(self, ts_ms: int | None = None) -> None:
        """Record the call-start wall-clock for ``audio_offset_ms`` math.

        Optional — if the caller never sets it, ``audio_offset_ms`` is
        omitted from event payloads.
        """
        self._call_start_ms = ts_ms if ts_ms is not None else _now_ms()
        self._user_state = "listening"
        self._agent_state = "idle"

    def reset_turn_state(self) -> None:
        """Reset per-turn cursors. Called automatically on EOU commit."""
        self._first_token_for_turn = True
        self._first_audio_for_turn = True

    # ------------------------------------------------------------------
    # User-side events
    # ------------------------------------------------------------------

    async def fire_user_speech_started(
        self,
        *,
        vad_confidence: float | None = None,
        audio_offset_ms: int | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Fire on the VAD positive edge of the inbound stream.

        Do not coalesce: the runner consumes positive→negative→positive
        transitions in order. For server-VAD engines (OpenAI Realtime,
        Telnyx Voice AI), forward the upstream signal directly — do not
        re-run a VAD layer on top.
        """
        payload: dict[str, Any] = {
            "timestamp_ms": timestamp_ms if timestamp_ms is not None else _now_ms(),
        }
        if vad_confidence is not None:
            payload["vad_confidence"] = vad_confidence
        offset = self._resolve_offset(audio_offset_ms, payload["timestamp_ms"])
        if offset is not None:
            payload["audio_offset_ms"] = offset
        self._user_state = "speaking"
        await self._dispatch(
            self.on_user_speech_started,
            payload,
            span_event="patter.event.user_speech_started",
            span_attrs={
                k: v
                for k, v in {
                    "patter.audio.offset_ms": payload.get("audio_offset_ms"),
                    "patter.vad.confidence": payload.get("vad_confidence"),
                }.items()
                if v is not None
            },
        )

    async def fire_user_speech_ended(
        self,
        *,
        speech_duration_ms: int,
        vad_confidence: float | None = None,
        audio_offset_ms: int | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Fire on the VAD trailing edge (raw — *not* EOU).

        ``speech_duration_ms`` is the length of the segment that just
        ended; the runner uses it to compute talk-ratio.
        """
        payload: dict[str, Any] = {
            "timestamp_ms": timestamp_ms if timestamp_ms is not None else _now_ms(),
            "speech_duration_ms": speech_duration_ms,
        }
        if vad_confidence is not None:
            payload["vad_confidence"] = vad_confidence
        offset = self._resolve_offset(audio_offset_ms, payload["timestamp_ms"])
        if offset is not None:
            payload["audio_offset_ms"] = offset
        # Transition back to listening — EOU may still be pending.
        self._user_state = "listening"
        await self._dispatch(
            self.on_user_speech_ended,
            payload,
            span_event="patter.event.user_speech_ended",
            span_attrs={"patter.speech.duration_ms": speech_duration_ms},
        )

    async def fire_user_speech_eos(
        self,
        *,
        trigger: str,
        trailing_silence_ms: int | None = None,
        transcript_so_far: str | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Fire on the committed end-of-utterance.

        This is the canonical "user finished" signal — VAD edge + trailing
        silence + (optionally) a semantic turn-detector model agreement.
        ``trigger`` MUST be one of ``"vad_silence"``,
        ``"semantic_turn_detector"``, ``"manual_commit"``.

        The runner uses the timestamp of this event to compute
        ``eos_to_first_token_ms`` (Hamming AI threshold: <800 ms good,
        >1500 ms critical).
        """
        if trigger not in {"vad_silence", "semantic_turn_detector", "manual_commit"}:
            logger.warning("on_user_speech_eos called with unknown trigger=%s", trigger)
        payload: dict[str, Any] = {
            "timestamp_ms": timestamp_ms if timestamp_ms is not None else _now_ms(),
            "trigger": trigger,
        }
        if trailing_silence_ms is not None:
            payload["trailing_silence_ms"] = trailing_silence_ms
        if transcript_so_far is not None:
            payload["transcript_so_far"] = transcript_so_far

        # EOU commit advances turn_idx and arms first-token / first-audio.
        self._turn_idx += 1
        self.reset_turn_state()
        self._user_state = "listening"
        self._agent_state = "thinking"

        await self._dispatch(
            self.on_user_speech_eos,
            payload,
            span_event="patter.event.user_speech_eos",
            span_attrs={
                "patter.eos.trigger": trigger,
                **(
                    {"patter.eos.trailing_silence_ms": trailing_silence_ms}
                    if trailing_silence_ms is not None
                    else {}
                ),
            },
        )

    # ------------------------------------------------------------------
    # Agent-side events
    # ------------------------------------------------------------------

    async def fire_agent_speech_started(
        self,
        *,
        tts_provider: str | None = None,
        engine: str | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Fire on the FIRST audio chunk of the current agent turn that
        crosses to the wire (not the first chunk produced by TTS).

        The user hears the wire chunk, so this is the timestamp the
        runner anchors barge-in latency on.
        """
        payload: dict[str, Any] = {
            "timestamp_ms": timestamp_ms if timestamp_ms is not None else _now_ms(),
            "turn_idx": self._turn_idx,
        }
        if tts_provider is not None:
            payload["tts_provider"] = tts_provider
        if engine is not None:
            payload["engine"] = engine
        self._agent_state = "speaking"
        await self._dispatch(
            self.on_agent_speech_started,
            payload,
            span_event="patter.event.agent_speech_started",
            span_attrs={
                "patter.turn.idx": self._turn_idx,
                **({"patter.tts.provider": tts_provider} if tts_provider else {}),
                **({"patter.engine": engine} if engine else {}),
            },
        )

    async def fire_agent_speech_ended(
        self,
        *,
        speech_duration_ms: int,
        interrupted: bool = False,
        timestamp_ms: int | None = None,
    ) -> None:
        """Fire on the LAST audio chunk of the current agent turn.

        ``interrupted=True`` marks the turn as cancelled by barge-in;
        the runner treats it as the ``agent_speech_stopped`` half of a
        barge-in pair.
        """
        payload: dict[str, Any] = {
            "timestamp_ms": timestamp_ms if timestamp_ms is not None else _now_ms(),
            "turn_idx": self._turn_idx,
            "speech_duration_ms": speech_duration_ms,
            "interrupted": interrupted,
        }
        self._agent_state = "idle"
        await self._dispatch(
            self.on_agent_speech_ended,
            payload,
            span_event="patter.event.agent_speech_ended",
            span_attrs={
                "patter.turn.idx": self._turn_idx,
                "patter.speech.duration_ms": speech_duration_ms,
                "patter.turn.interrupted": interrupted,
            },
        )

    # ------------------------------------------------------------------
    # LLM / TTS events
    # ------------------------------------------------------------------

    async def fire_llm_first_token(
        self,
        *,
        llm_provider: str,
        model: str,
        timestamp_ms: int | None = None,
    ) -> None:
        """Fire on the FIRST LLM token of the current turn (TTFT marker).

        Idempotent within a turn — guarded by ``_first_token_for_turn``.
        Combined with ``on_user_speech_eos.timestamp_ms`` the runner
        computes ``eos_to_first_token_ms``.
        """
        if not self._first_token_for_turn:
            return
        self._first_token_for_turn = False
        payload: dict[str, Any] = {
            "timestamp_ms": timestamp_ms if timestamp_ms is not None else _now_ms(),
            "turn_idx": self._turn_idx,
            "llm_provider": llm_provider,
            "model": model,
        }
        await self._dispatch(
            self.on_llm_token,
            payload,
            span_event="patter.event.llm_first_token",
            span_attrs={
                "gen_ai.request.model": model,
                "gen_ai.provider.name": llm_provider,
                "patter.turn.idx": self._turn_idx,
            },
        )

    async def fire_audio_out(
        self,
        *,
        tts_provider: str,
        timestamp_ms: int | None = None,
    ) -> None:
        """Fire on the FIRST TTS audio chunk for the current turn.

        Distinct from :meth:`fire_agent_speech_started`: this is the
        agent-side buffer arrival (TTS warmup), not the wire-time chunk.
        Idempotent within a turn — guarded by ``_first_audio_for_turn``.
        """
        if not self._first_audio_for_turn:
            return
        self._first_audio_for_turn = False
        payload: dict[str, Any] = {
            "timestamp_ms": timestamp_ms if timestamp_ms is not None else _now_ms(),
            "turn_idx": self._turn_idx,
            "tts_provider": tts_provider,
        }
        await self._dispatch(
            self.on_audio_out,
            payload,
            span_event="patter.event.tts_first_audio",
            span_attrs={
                "patter.turn.idx": self._turn_idx,
                "patter.tts.provider": tts_provider,
            },
        )

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _resolve_offset(self, given: int | None, ts_ms: int) -> int | None:
        if given is not None:
            return given
        if self._call_start_ms is not None:
            return max(0, ts_ms - self._call_start_ms)
        return None

    async def _dispatch(
        self,
        cb: Optional[SpeechEventCallback],
        payload: dict[str, Any],
        *,
        span_event: str | None,
        span_attrs: dict[str, Any] | None = None,
    ) -> None:
        """Fire the user callback and optionally record an OTel span event.

        Callback exceptions are logged but never propagate (a misbehaving
        observer must not crash a live phone call).
        """
        if span_event is not None:
            _record_span_event(span_event, span_attrs or {})
        if cb is None:
            return
        try:
            result = cb(payload)
            if hasattr(result, "__await__"):
                await result  # type: ignore[func-returns-value]
        except Exception:  # noqa: BLE001 — never propagate observer errors.
            logger.exception("Speech-event callback %s raised", span_event)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _record_span_event(name: str, attrs: dict[str, Any]) -> None:
    """Attach an event to the current OTel span. No-op if OTel is missing
    or no span is active. Never raises — observability must never crash
    the call.
    """
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        span.add_event(name, attributes=attrs)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to record OTel span event %s", name, exc_info=True)
