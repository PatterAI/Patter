"""Eval session — drives the REAL pipeline call loop, no telephony required.

Unlike the legacy ``reply()``-callable path in :mod:`getpatter.evals.runner`
(which never touches the SDK), :class:`EvalSession` constructs a real
:class:`~getpatter.stream_handler.PipelineStreamHandler` and injects user
turns through the exact same path a live phone call uses:

``FakeSTT → _stt_loop → _handle_barge_in → _commit_transcript →
_dispatch_turn → LLMLoop (real ToolExecutor, hooks, guardrails) →
SentenceChunker → FakeTTS → FakeAudioSender``

so tool calling, pipeline hooks, guardrail replacement, dedup/hallucination
filtering, history handling, metrics, and the turn-taking state machine are
all exercised for real. Only the paid/external boundary is faked: the
telephony audio sender, the STT WebSocket, the TTS WebSocket, and —
optionally — the LLM (a deterministic :class:`ScriptedLLMProvider` for CI,
or any real :class:`~getpatter.services.llm_loop.LLMProvider` for live
evals).

Programmatic usage (the ``patter eval`` CLI keeps its legacy
``reply()``-factory contract; real-pipeline evals are programmatic-only —
see :class:`~getpatter.evals.case.EvalCase` ``agent=`` /
``llm_provider=`` for the :class:`~getpatter.evals.runner.EvalRunner`
integration)::

    from getpatter import Agent
    from getpatter.evals.session import (
        EvalSession, ScriptedLLMProvider, text_turn, tool_call_turn,
    )
    from getpatter.evals.assertions import expect

    provider = ScriptedLLMProvider([
        tool_call_turn("lookup_order", {"order_id": "A1"}),
        text_turn("Your order ships tomorrow."),
    ])
    agent = Agent(
        system_prompt="You are a support agent.",
        provider="pipeline",
        tools=({"name": "lookup_order", "parameters": {...},
                "handler": my_handler},),
    )

    async with EvalSession(agent=agent, llm_provider=provider) as s:
        result = await s.user_says("where is my order?")
        expect(result).tool_called(
            "lookup_order", args_subset={"order_id": "A1"}
        ).agent_text_contains("tomorrow")

For live evals, pass a real provider (e.g. ``getpatter.llm.openai.LLM``)
as ``llm_provider`` — everything else is identical.

Notes
-----
* ``agent.stt`` / ``agent.tts`` configs are ignored (replaced by the fakes);
  ``agent.provider`` is forced to ``"pipeline"``.
* ``on_message``-style agents are not supported — the session targets the
  built-in :class:`~getpatter.services.llm_loop.LLMLoop` path.
* ``TurnResult.agent_text`` is what the caller HEARD (post-guardrail,
  post-hook, post-text-transform sentences handed to TTS), which is the
  caller-side observable. ``history_snapshot`` mirrors the dashboard
  conversation history, where the streaming path records the raw LLM text.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import uuid
from collections import deque
from typing import Any, AsyncIterator

from getpatter.exceptions import ErrorCode, PatterConfigError, PatterError
from getpatter.stream_handler import AudioSender

logger = logging.getLogger("getpatter.evals")

__all__ = [
    "EvalSession",
    "TurnResult",
    "ToolCallRecord",
    "FakeAudioSender",
    "FakeSTT",
    "FakeTTS",
    "ScriptedLLMProvider",
    "text_turn",
    "tool_call_turn",
]


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ToolCallRecord:
    """One tool invocation observed through the handler's tool-event path.

    ``arguments`` is the parsed-JSON dict the :class:`LLMLoop` handed to the
    real ``ToolExecutor``; ``result`` is the executor's string return value
    (tool handlers' non-string returns are JSON-encoded by the executor).
    The dict is caller-immutable by convention (shallow immutability — same
    limitation as ``STTConfig.options``).
    """

    name: str
    arguments: dict
    result: str | None = None


@dataclasses.dataclass(frozen=True)
class TurnResult:
    """The observable outcome of one :meth:`EvalSession.user_says` turn.

    Attributes:
        user_text: The injected user utterance.
        agent_text: What the caller heard this turn — the sentences handed
            to TTS after guardrails / hooks / text transforms, joined by a
            single space. Falls back to the assistant history entry when no
            TTS sentence was produced (e.g. the turn was interrupted before
            synthesis).
        tool_calls: Tool invocations recorded via the handler's tool-event
            path (``LLMLoop`` ``on_tool_call`` → ``_record_tool_call``),
            in execution order.
        history_snapshot: The handler's full conversation history right
            after the turn settled (``role`` / ``text`` / ``timestamp``
            dicts, including ``role="tool"`` timeline entries).
        interrupted: True when the turn's response was cut short by the
            barge-in path.
        metrics_turn: The :class:`~getpatter.models.TurnMetrics` emitted for
            this turn via the handler's ``on_metrics`` callback, or ``None``
            when the turn did not complete normally (vetoed / interrupted).
    """

    user_text: str
    agent_text: str
    tool_calls: tuple[ToolCallRecord, ...]
    history_snapshot: tuple[dict, ...]
    interrupted: bool
    metrics_turn: Any | None = None

    def transcript(self) -> list[dict[str, str]]:
        """Render ``history_snapshot`` in the legacy judge transcript shape
        (``[{"role": "user"|"agent"|"tool", "text": ...}]``)."""
        out: list[dict[str, str]] = []
        for entry in self.history_snapshot:
            role = str(entry.get("role", "user"))
            out.append(
                {
                    "role": "agent" if role == "assistant" else role,
                    "text": str(entry.get("text", "")),
                }
            )
        return out


# ---------------------------------------------------------------------------
# Fakes — the only mocked boundary (telephony / STT / TTS / optionally LLM)
# ---------------------------------------------------------------------------


class FakeAudioSender(AudioSender):
    """Records every carrier-bound audio operation; auto-acks Twilio marks.

    ``sent_audio`` collects the raw chunks handed to the carrier,
    ``clears`` counts ``send_clear`` calls (barge-in flushes), ``marks``
    records mark names. When attached to a handler, each ``send_mark`` is
    immediately echoed back through ``handler.on_mark`` so mark-gated
    pacing never deadlocks.
    """

    def __init__(self) -> None:
        self.sent_audio: list[bytes] = []
        self.clears: int = 0
        self.marks: list[str] = []
        self._handler: Any = None

    def attach_handler(self, handler: Any) -> None:
        """Wire the auto-ack loopback for ``send_mark`` → ``on_mark``."""
        self._handler = handler

    async def send_audio(self, pcm_audio: bytes) -> None:
        self.sent_audio.append(pcm_audio)

    async def send_clear(self) -> None:
        self.clears += 1

    async def send_mark(self, mark_name: str) -> None:
        self.marks.append(mark_name)
        if self._handler is not None:
            # Synchronous ack — resolves the pending-mark future the handler
            # registered just before this call; no background task to leak.
            await self._handler.on_mark(mark_name)


class FakeSTT:
    """Queue-backed STT double feeding the handler's REAL ``_stt_loop``.

    The harness pushes finalised :class:`~getpatter.providers.base.Transcript`
    objects via :meth:`push_final`; the handler consumes them through the
    same ``receive_transcripts`` async-iterator a live Deepgram adapter
    exposes, so ``_handle_barge_in`` → ``_commit_transcript`` →
    ``_dispatch_turn`` run exactly as on a real call.

    Each queue item carries a *consumed* event that is set when the receive
    loop has fully processed the transcript's loop-body (i.e. the dispatch
    task for it — if any — has been created), which is what
    :meth:`EvalSession.user_says` awaits before grabbing the dispatch task.
    """

    _CLOSE = object()

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self.sent_audio: list[bytes] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def send_audio(self, audio_chunk: bytes) -> None:
        self.sent_audio.append(audio_chunk)

    def push_final(self, text: str) -> asyncio.Event:
        """Enqueue a final transcript; returns its *consumed* event."""
        if self.closed:
            raise PatterError(
                "FakeSTT is closed — the EvalSession was already cleaned up",
                code=ErrorCode.CONFIG,
            )
        from getpatter.providers.base import Transcript

        consumed = asyncio.Event()
        self._queue.put_nowait(
            (Transcript(text=text, is_final=True, confidence=1.0), consumed)
        )
        return consumed

    async def receive_transcripts(self) -> AsyncIterator[Any]:
        while True:
            item = await self._queue.get()
            if item is self._CLOSE:
                return
            transcript, consumed = item
            try:
                yield transcript
            finally:
                # Runs when the consumer advances to the next item (the loop
                # body for *transcript* — including dispatch-task creation —
                # has finished), OR when the generator is closed/cancelled,
                # so a waiting ``user_says`` can never deadlock.
                consumed.set()

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(self._CLOSE)


class FakeTTS:
    """TTS double: records the text it is asked to speak, yields silence.

    The default chunk is 320 bytes of PCM16 @ 16 kHz (10 ms of silence) so
    the handler's playback-backlog accounting stays in the sub-frame range
    and a following turn is never misclassified as a barge-in against a
    phantom carrier backlog.
    """

    def __init__(self, chunk: bytes = b"\x00" * 320) -> None:
        self.spoken: list[str] = []
        self._chunk = chunk
        self.closed = False

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self.spoken.append(text)
        yield self._chunk

    async def close(self) -> None:
        self.closed = True


class _NoopVAD:
    """No-op VAD injected when the agent has none — prevents ``start()``
    from auto-loading SileroVAD (ONNX model) in deterministic eval runs.
    Never consulted in practice: the session injects transcripts directly
    and feeds no audio frames."""

    async def process_frame(self, pcm: bytes, sample_rate: int) -> None:
        return None

    def reset(self) -> None:
        return None

    async def close(self) -> None:  # pragma: no cover - never exercised
        return None


# ---------------------------------------------------------------------------
# Scripted LLM provider (deterministic, for CI)
# ---------------------------------------------------------------------------


def text_turn(
    text: str, *, input_tokens: int = 8, output_tokens: int = 8
) -> list[dict]:
    """Build one scripted assistant turn that streams ``text`` then usage."""
    return [
        {"type": "text", "content": text},
        {
            "type": "usage",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    ]


def tool_call_turn(
    name: str, arguments: dict | None = None, *, call_id: str = "call_1"
) -> list[dict]:
    """Build one scripted turn that emits a single complete tool call.

    The :class:`~getpatter.services.llm_loop.LLMLoop` executes the tool via
    the real ``ToolExecutor`` and re-submits — so a tool scenario needs a
    follow-up scripted turn (usually :func:`text_turn`) for the
    post-tool-result response.
    """
    return [
        {
            "type": "tool_call",
            "index": 0,
            "id": call_id,
            "name": name,
            "arguments": json.dumps(arguments or {}),
        },
        {"type": "usage", "input_tokens": 8, "output_tokens": 4},
    ]


class ScriptedLLMProvider:
    """Deterministic :class:`~getpatter.services.llm_loop.LLMProvider`.

    Pops one scripted chunk-list per ``stream()`` call (i.e. per LLM-loop
    iteration — a tool-call turn consumes one script for the call and one
    for the post-result response). Records every request's ``messages`` and
    ``tools`` in :attr:`calls` so tests can assert exactly what the real
    pipeline sent to the model. Honours ``cancel_event`` between chunks
    like a well-behaved provider.
    """

    #: Stable pricing/dashboard key (no real pricing entry — cost is 0).
    provider_key = "scripted"

    def __init__(self, turns: list[list[dict]] | None = None) -> None:
        self._scripts: deque[list[dict]] = deque(turns or [])
        self.calls: list[dict[str, Any]] = []

    def add_turn(self, chunks: list[dict]) -> None:
        """Append another scripted turn (chunk list) to the script queue."""
        self._scripts.append(chunks)

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        cancel_event: asyncio.Event | None = None,
        call_id: str | None = None,
    ) -> AsyncIterator[dict]:
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": [dict(t) for t in tools] if tools else tools,
                "call_id": call_id,
            }
        )
        if not self._scripts:
            yield {"type": "done"}
            return
        for chunk in self._scripts.popleft():
            if cancel_event is not None and cancel_event.is_set():
                return
            yield dict(chunk)


# ---------------------------------------------------------------------------
# EvalSession
# ---------------------------------------------------------------------------


class EvalSession:
    """Async-context-managed harness around a real ``PipelineStreamHandler``.

    See the module docstring for usage. Construction is cheap; the handler
    is built and started in :meth:`start` (``async with`` calls it).

    Args:
        agent: The :class:`~getpatter.models.Agent` under test. Its
            ``stt`` / ``tts`` configs are replaced by fakes and ``provider``
            is forced to ``"pipeline"``; everything else (tools, guardrails,
            hooks, text transforms, first_message, variables, ...) is live.
        llm_provider: Optional LLM provider override. Defaults to
            ``agent.llm``. Pass a :class:`ScriptedLLMProvider` for
            deterministic CI evals or a real provider for live evals.
        openai_key: Legacy fallback — when neither ``llm_provider`` nor
            ``agent.llm`` is set, a real ``OpenAILLMProvider`` is built from
            this key (live evals only; never use in CI).
        call_id / caller / callee: Call identity threaded through the
            handler, tools' ``call_context``, and metrics.
        custom_params: Optional per-call variables resolved into the system
            prompt exactly like ``Patter.call(custom_params=...)``.
        turn_timeout_s: Per-turn ceiling for :meth:`user_says` (LLM + tools
            + TTS). Generous default for live providers; scripted providers
            finish in milliseconds.
    """

    def __init__(
        self,
        agent: Any,
        llm_provider: Any | None = None,
        *,
        openai_key: str = "",
        call_id: str | None = None,
        caller: str = "+15550000001",
        callee: str = "+15550000002",
        custom_params: dict | None = None,
        turn_timeout_s: float = 60.0,
    ) -> None:
        if agent is None:
            raise PatterConfigError("EvalSession requires an agent")
        self._source_agent = agent
        self._llm_provider = llm_provider
        self._openai_key = openai_key
        self.call_id = call_id or f"eval_{uuid.uuid4().hex[:12]}"
        self.caller = caller
        self.callee = callee
        self._custom_params = custom_params
        self._turn_timeout_s = turn_timeout_s

        # Built in start():
        self._handler: Any = None
        self.audio_sender = FakeAudioSender()
        self.stt = FakeSTT()
        self.tts = FakeTTS()
        self._tool_calls: list[ToolCallRecord] = []
        self._metrics_turns: list[Any] = []
        #: Every payload the handler fired through ``on_transcript`` —
        #: user / assistant / tool events, in emission order.
        self.transcript_events: list[dict] = []
        self._started = False
        self._closed = False

    # -- context manager ----------------------------------------------------

    async def __aenter__(self) -> "EvalSession":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.close()
        return False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Build and start the real handler (idempotent)."""
        if self._started:
            return
        from getpatter.stream_handler import (
            PipelineStreamHandler,
            create_metrics_accumulator,
            resolve_agent_prompt,
        )

        eval_agent = self._build_eval_agent()
        if (
            getattr(eval_agent, "llm", None) is None
            and not self._openai_key
        ):
            raise PatterConfigError(
                "EvalSession needs an LLM: pass llm_provider= (e.g. a "
                "ScriptedLLMProvider for CI), set agent.llm, or supply "
                "openai_key= for the built-in OpenAI provider."
            )

        resolved_prompt = resolve_agent_prompt(eval_agent, self._custom_params)
        metrics = create_metrics_accumulator(
            call_id=self.call_id,
            provider="pipeline",
            telephony_provider="eval",
            agent=eval_agent,
            deepgram_key="",
            elevenlabs_key="",
            pricing=None,
        )
        handler = PipelineStreamHandler(
            agent=eval_agent,
            audio_sender=self.audio_sender,
            call_id=self.call_id,
            caller=self.caller,
            callee=self.callee,
            resolved_prompt=resolved_prompt,
            metrics=metrics,
            openai_key=self._openai_key,
            for_twilio=False,
            input_is_mulaw_8k=False,
            output_is_mulaw_8k=False,
            on_transcript=self._on_transcript,
            on_metrics=self._on_metrics,
        )
        self.audio_sender.attach_handler(handler)
        # Inject the fakes BEFORE start(): the eval agent carries no
        # stt/tts config and no provider keys are set, so ``start()``
        # keeps these instances, connects the FakeSTT, plays
        # ``first_message`` through the FakeTTS, builds the real LLMLoop +
        # ToolExecutor, and launches the real ``_stt_loop`` against the
        # fake's transcript queue.
        handler._stt = self.stt
        handler._tts = self.tts
        self._handler = handler
        try:
            await handler.start()
            if handler._llm_loop is None:
                raise PatterConfigError(
                    "EvalSession could not build the LLM loop — the agent "
                    "has on_message-style wiring or no usable LLM provider."
                )
            # Observe tool executions through the handler's tool-event path:
            # keep the handler's own transcript-timeline behaviour
            # (role="tool" history entries + on_transcript events) and
            # additionally record structured ToolCallRecords for assertions.
            handler._llm_loop.set_on_tool_call(self._observe_tool_call)
        except BaseException:
            # __aexit__ never runs when __aenter__ raises — tear down here.
            with contextlib.suppress(Exception):
                await handler.cleanup()
            self._handler = None
            raise
        self._started = True

    async def close(self) -> None:
        """Run the handler's real ``cleanup()`` and settle cancelled tasks."""
        if self._closed:
            return
        self._closed = True
        handler = self._handler
        if handler is None:
            return
        # ``cleanup`` cancels the grace-flip task without awaiting it — hold a
        # reference so the session can await its teardown and leave the loop
        # with zero pending tasks.
        grace_task = getattr(handler, "_grace_task", None)
        await handler.cleanup()
        if grace_task is not None:
            grace_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await grace_task
        # One tick so any just-cancelled callbacks fully unwind.
        await asyncio.sleep(0)

    # -- turn driving --------------------------------------------------------

    async def user_says(
        self, text: str, *, timeout_s: float | None = None
    ) -> TurnResult:
        """Inject one final user transcript and await the full agent turn.

        The transcript flows through the handler's real receive loop —
        barge-in handling, dedup/hallucination filtering, hooks, the LLM
        loop with real tool execution, guardrails, sentence chunking, and
        TTS — exactly as on a live call. Returns once the turn's dispatch
        task settles.

        Raises:
            PatterError(INPUT_VALIDATION): the REAL pipeline dropped the
                transcript (duplicate within the 2 s throttle window, known
                STT hallucination, or empty text) — same behaviour as a
                live call.
            PatterError(TIMEOUT): the turn did not settle within
                ``timeout_s`` (default: the session's ``turn_timeout_s``).
        """
        if not self._started or self._handler is None:
            raise PatterError(
                "EvalSession is not started — use `async with EvalSession(...)`",
                code=ErrorCode.CONFIG,
            )
        if self._closed:
            raise PatterError(
                "EvalSession is closed", code=ErrorCode.CONFIG
            )
        timeout = timeout_s if timeout_s is not None else self._turn_timeout_s
        handler = self._handler

        # Per-turn snapshots — everything new after the turn belongs to it.
        history_len = len(handler.conversation_history)
        tools_len = len(self._tool_calls)
        spoken_len = len(self.tts.spoken)
        turns_len = len(self._metrics_turns)
        previous_dispatch = handler._dispatch_task
        # The flag is per-turn (rewritten by every streaming response); reset
        # so a previous turn's barge-in cannot bleed into this TurnResult.
        handler._last_response_interrupted = False

        consumed = self.stt.push_final(text)
        try:
            await asyncio.wait_for(consumed.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise PatterError(
                f"user turn {text!r} was not consumed by the STT loop within "
                f"{timeout}s — the receive loop may have died (check logs)",
                code=ErrorCode.TIMEOUT,
            ) from None

        dispatch = handler._dispatch_task
        if dispatch is None or dispatch is previous_dispatch:
            raise PatterError(
                f"user turn {text!r} was dropped by the pipeline's commit "
                "filter (duplicate within 2s, known STT hallucination, or "
                "empty) — exactly as it would be on a live call. Vary the "
                "text between turns or split the case.",
                code=ErrorCode.INPUT_VALIDATION,
            )
        try:
            await asyncio.wait_for(asyncio.shield(dispatch), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            dispatch.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await dispatch
            raise PatterError(
                f"agent turn for {text!r} did not settle within {timeout}s",
                code=ErrorCode.TIMEOUT,
            ) from None

        return self._build_turn_result(
            text,
            history_len=history_len,
            tools_len=tools_len,
            spoken_len=spoken_len,
            turns_len=turns_len,
        )

    # -- convenience accessors ------------------------------------------------

    @property
    def handler(self) -> Any:
        """The live :class:`PipelineStreamHandler` (None before ``start``)."""
        return self._handler

    @property
    def history(self) -> list[dict]:
        """Current conversation history (shallow copies of the entries)."""
        if self._handler is None:
            return []
        return [dict(e) for e in self._handler.conversation_history]

    def transcript(self) -> list[dict[str, str]]:
        """Full-session transcript in the judge shape (assistant → agent)."""
        out: list[dict[str, str]] = []
        for entry in self.history:
            role = str(entry.get("role", "user"))
            out.append(
                {
                    "role": "agent" if role == "assistant" else role,
                    "text": str(entry.get("text", "")),
                }
            )
        return out

    # -- internals -------------------------------------------------------------

    def _build_eval_agent(self) -> Any:
        """Return the agent reshaped for harness execution."""
        agent = self._source_agent
        overrides: dict[str, Any] = {}
        if getattr(agent, "provider", "pipeline") != "pipeline":
            overrides["provider"] = "pipeline"
        if getattr(agent, "stt", None) is not None:
            logger.info(
                "EvalSession: agent.stt config ignored — transcripts are "
                "injected directly"
            )
            overrides["stt"] = None
        if getattr(agent, "tts", None) is not None:
            logger.info("EvalSession: agent.tts config replaced by FakeTTS")
            overrides["tts"] = None
        if self._llm_provider is not None:
            overrides["llm"] = self._llm_provider
        # Without this, ``start()`` would try to auto-load SileroVAD (ONNX
        # model) — pointless here since the session feeds no audio frames.
        if getattr(agent, "vad", None) is None:
            overrides["vad"] = _NoopVAD()
        if overrides:
            agent = dataclasses.replace(agent, **overrides)
        return agent

    async def _observe_tool_call(
        self, name: str, arguments: dict, result: Any
    ) -> None:
        # First preserve the handler's own behaviour: role="tool" history
        # entries + on_transcript tool events (the handler's tool-event path).
        await self._handler._record_tool_call(name, arguments, result)
        if result is None or isinstance(result, str):
            result_text = result
        else:
            try:
                result_text = json.dumps(result)
            except (TypeError, ValueError):
                result_text = str(result)
        self._tool_calls.append(
            ToolCallRecord(
                name=name,
                arguments=dict(arguments or {}),
                result=result_text,
            )
        )

    async def _on_transcript(self, payload: dict) -> None:
        self.transcript_events.append(payload)

    async def _on_metrics(self, payload: dict) -> None:
        turn = payload.get("turn")
        if turn is not None:
            self._metrics_turns.append(turn)

    def _build_turn_result(
        self,
        user_text: str,
        *,
        history_len: int,
        tools_len: int,
        spoken_len: int,
        turns_len: int,
    ) -> TurnResult:
        handler = self._handler
        spoken = self.tts.spoken[spoken_len:]
        full_history = [dict(e) for e in handler.conversation_history]
        new_history = full_history[history_len:]
        if spoken:
            agent_text = " ".join(spoken)
        else:
            # No TTS output this turn (interrupted pre-synthesis / empty
            # reply) — fall back to the assistant history entry, if any.
            agent_text = " ".join(
                e.get("text", "")
                for e in new_history
                if e.get("role") == "assistant"
            )
        new_turns = self._metrics_turns[turns_len:]
        return TurnResult(
            user_text=user_text,
            agent_text=agent_text,
            tool_calls=tuple(self._tool_calls[tools_len:]),
            history_snapshot=tuple(full_history),
            interrupted=bool(
                getattr(handler, "_last_response_interrupted", False)
            ),
            metrics_turn=new_turns[-1] if new_turns else None,
        )
