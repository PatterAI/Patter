# Decomposing `PipelineStreamHandler` into composable stages

Status: **accepted** — slice 1 implemented, slices 2-4 design-only.
Audience: SDK maintainers. This is an internal architecture document, not a
user-facing docs page (it is intentionally outside `docs/docs.json` nav).

Scope: `libraries/python/getpatter/stream_handler.py` (`PipelineStreamHandler`)
and `libraries/typescript/src/stream-handler.ts` (`StreamHandler`, pipeline
branch). Realtime/ConvAI handlers are out of scope — they delegate turn-taking
to the provider.

---

## 1. Problem

`PipelineStreamHandler` is a god-class: one object owns audio decode, echo
cancellation, VAD, the self-hearing gate, the pre-barge-in ring buffer, STT
feeding, transcript dedup/commit, turn dispatch, LLM streaming, sentence
chunking, TTS, transcoding/pacing/marks, playback-cursor estimation,
interruption history rewriting, and the metrics turn lifecycle.

That logic is held together by ~20 mutable flags, mutated from four concurrent
contexts:

1. **Carrier media frames** — `on_audio_received` / `handleAudio`, ~50 Hz.
2. **The STT receive loop** — `_stt_loop` / `processTranscript`, a long-lived
   task draining transcripts.
3. **The turn dispatch task** — `_dispatch_turn` / `dispatchTurn` (LLM + TTS),
   a tracked background task so barge-in can fire against the live turn.
4. **Timers** — the grace flip (`_grace_task` / `graceTimer`), the pending
   barge-in timeout, the long-turn filler, first-message mark futures, and
   `cleanup()`/`handleStop` teardown racing all of the above.

The flags belong to three interleaved state machines (inventoried in §2), and
a deep review attributed roughly half of the recently-fixed bugs (§5) to
unguarded interleavings between them. LiveKit Agents (node overrides) and
Pipecat (frame processors) both structure the same problem as a graph of
small, replaceable stages; this document maps `PipelineStreamHandler` onto an
equivalent decomposition that we can land incrementally without breaking the
public API.

A concrete symptom of the monolith: `Agent.audio_filter` / `audioFilter`
(Krisp / DeepFilterNet noise suppression) has been accepted by the public API,
documented as "integrated before VAD and STT" (`providers/base.py`
`AudioFilter` docstring), fully implemented and unit-tested — and **never
invoked by any pipeline**. There was no input stage to own it, so the wiring
was simply forgotten. Slice 1 (§6) fixes this.

---

## 2. Today's state, by machine (field inventory)

Python names given; TypeScript mirrors are camelCase (`_is_speaking` ↔
`isSpeaking`) unless noted.

### 2.1 Machine A — speaking / grace / barge-in

| Field | Type | Meaning |
| --- | --- | --- |
| `_is_speaking` | bool | TTS turn formally in flight (incl. carrier backlog + tail grace). |
| `_tail_grace_active` | bool | `_is_speaking` is held only for the post-TTS echo window — nothing left to interrupt. |
| `_speaking_generation` | int | Monotonic turn counter; stale grace flips no-op when it moved. |
| `_speaking_started_at` | float? | `_begin_speaking` wall-clock stamp. |
| `_first_audio_sent_at` | float? | Anchor for the `_can_barge_in()` warmup gate (stamped synchronously at `_begin_speaking` since 2026-05-14). |
| `_playback_buffered_until` | float | Estimated wall-clock when the carrier finishes *playing* what we pushed (TTS outruns realtime). |
| `_turn_playback_total_s` / `_turn_spoken_segments` | float / list | Per-turn playout timeline → heard-prefix estimation at barge-in. |
| `_grace_task` | Task? | The scheduled two-phase grace flip. |
| `_barge_in_pending_since` / `_barge_in_pending_task` | float? / Task? | VAD fired during TTS but cancel is deferred to transcript confirmation (strategies, or forward-STT without AEC). |
| `_suppressed_speech_pending` | bool | A real `speech_start` was gated out by the warmup gate; flush the ring at grace flip. |
| `_inbound_audio_ring` | deque(maxlen=13) | ~260 ms of mic frames captured while speaking; replayed to STT on barge-in (leading-edge recovery). |
| `_last_cancel_at` | float? | Enforces the 150 ms post-cancel drain before the next `_begin_speaking`. |
| `_current_agent_spoken_text` | str | Echo-guard reference (forward-STT mode): agent's in-flight spoken text. |
| `_last_response_interrupted` | bool | Marks history entry `[interrupted by caller]`. |
| `_llm_cancel_event` (TS: `llmAbort`) | Event / AbortController | Per-turn cancellation token, recreated at the top of `_dispatch_turn`. |
| `_pending_marks` / `_first_message_mark_counter` | list / int | Outstanding Twilio mark FIFO bounding carrier buffer depth (BUG #128). |
| `_aec` | NlmsEchoCanceller? | Echo canceller; its presence also selects the barge-in gate length. |

Composite phases (derived, never reified today):

```
IDLE ──_begin_speaking()──▶ SPEAKING(gate-closed) ──gate elapses──▶ SPEAKING(armed)
                                                       │
              VAD speech_start + defer policy          ▼
SPEAKING(armed) ────────────────────────▶ SPEAKING + BARGE_IN_PENDING
                                                       │ timeout → SPEAKING(armed)
                                                       │ strategy/echo-guard confirm
dispatch done                                          ▼
SPEAKING ──_end_speaking_with_grace()──▶ DRAINING_BACKLOG (phase 1:
            _is_speaking=True, _tail_grace_active=False, cursor in future)
   ──cursor drains──▶ TAIL_GRACE (phase 2: _tail_grace_active=True)
   ──grace_ms elapses + generation matches──▶ IDLE
```

Transitions with side effects:

- `_begin_speaking()` — awaits post-cancel drain; gen++; clears tail grace,
  ring, `_suppressed_speech_pending`, spoken-text, playback timeline; stamps
  both timestamps; resets VAD (one-shot barge-in bug).
- `_mark_first_audio_sent()` — idempotent first-wire-byte stamp.
- `_track_outbound_playback(n)` — advances the playback cursor at the chunk's
  real byte rate (PCM16@16k or carrier μ-law@8k).
- `_end_speaking_with_grace()` — schedules the two-phase flip; flip clears
  pending barge-in, resets strategies, flushes the ring iff
  `_suppressed_speech_pending`, resets VAD; generation-guarded.
- VAD `speech_start` (in `on_audio_received`):
  - during TAIL_GRACE → `_end_tail_grace_for_new_turn()` (new turn, **no**
    interruption metrics, ring flushed to STT);
  - during SPEAKING with gate closed → phantom-suppressed
    (`_suppressed_speech_pending = True`; metrics deliberately untouched);
  - during SPEAKING with gate open → `_start_pending_barge_in()` (defer
    policy) or the legacy immediate cancel (send_clear, ring flush, metrics,
    LLM cancel);
  - non-phantom events re-anchor `metrics.anchor_user_speech_start()`.
- VAD `speech_end` → `metrics.record_vad_stop()` + best-effort STT
  `finalize()`.
- Transcript while speaking → `_handle_barge_in`: tail-grace rescue → echo
  guard → `_can_barge_in` gate → strategies → `_do_cancel_for_barge_in`
  (heard-prefix history rewrite, cursor reset, grace-task kill, mark drain,
  LLM cancel, TTS `cancel_active_stream`, `send_clear`, metrics).
- `_pending_barge_in_timeout` — drops pending state,
  `record_overlap_end(false)`, re-anchors.
- `cleanup()` — cancels dispatch/grace/pending tasks, drains marks, closes
  adapters, flushes the inbound resampler.

### 2.2 Machine B — transcript drain / dispatch

| Field | Type | Meaning |
| --- | --- | --- |
| `_stt_task` | Task | The `_stt_loop` receive pump. |
| `_dispatch_task` | Task? | The single in-flight turn (LLM+TTS); the loop settles it before launching the next (head-of-line-blocking fix). |
| `_last_commit_text` / `_last_commit_at` | str / float | Dedup + near-duplicate throttle + hallucination filter state (`_commit_transcript`). |
| `_llm_consume_task` | Task? | In-flight LLM consumption; cancelled on barge-in. |
| `_stt_connect_task` | Task? | Deferred STT connect (prewarm handoff). |

Flow: `receive_transcripts()` → `_handle_barge_in` → event-bus emits →
final/speech_final gate → `_commit_transcript` → `_await_dispatch_settle` →
`_dispatch_turn` (hooks → LLM → chunker → TTS).

### 2.3 Machine C — metrics turn lifecycle

Owned by `CallMetricsAccumulator` but **driven** from both machines above:
`start_turn_if_idle`, `anchor_user_speech_start`, `record_vad_stop`,
`record_stt_complete/final_timestamp`, `record_overlap_start/end`,
`record_bargein_detected`, `record_tts_stopped`, `record_turn_interrupted`,
`record_tts_first_byte`, `add_stt_audio_bytes`. The accumulator's anchors are
a hidden state machine of their own (the pre-0.6.1 phantom-`speech_start` bug
stamped `turn_start` at echo time and inflated `user_speech_duration_ms` to
5-7 s).

---

## 3. Target decomposition

```
                       ┌──────────────────────────────────────────────────────────┐
 carrier media frame ─▶│ InputChain                                               │
                       │  decode (μ-law→PCM16) → resample 8k→16k (stateful)       │
                       │  → AEC near-end → audio_filter → VAD → self-hearing gate │
                       │  → inbound ring buffer → before_send_to_stt → STT feed   │
                       └──────────────┬──────────────────────────┬────────────────┘
                                      │ VAD events               │ audio to STT
                                      ▼                          ▼
                       ┌──────────────────────────┐   ┌─────────────────────┐
                       │ TurnManager              │◀──│ TranscriptGate      │◀─ STT transcripts
                       │  IDLE/SPEAKING/DRAINING/ │   │  dedup, hallucination│
                       │  TAIL_GRACE phases,      │   │  filter, echo guard, │
                       │  barge-in policy,        │   │  commit throttle     │
                       │  playback cursor,        │   └─────────────────────┘
                       │  heard-prefix, metrics   │
                       │  transition actions      │
                       └──────────┬───────────────┘
                                  │ begin/cancel/end turn
                                  ▼
                       ┌──────────────────────────────────────────────────────────┐
                       │ OutputChain                                              │
                       │  LLM stream → after_llm tiers → sentence chunker         │
                       │  → text transforms → before/after_synthesize → TTS       │
                       │  → transcode/pacing → marks → AEC far-end tap            │
                       │  → playback tracking (feeds TurnManager cursor)          │
                       └──────────────────────────────────────────────────────────┘
```

`PipelineStreamHandler` remains the composition root: it owns providers'
lifecycles (`start`/`cleanup`), the carrier `AudioSender`, history/transcript
deques, and wires the three stages together. Every stage is independently
constructible and unit-testable with fakes.

### 3.1 InputChain (Python `getpatter/services/input_chain.py`, TS `src/services/input-chain.ts`)

Final-state interface (slice 1 ships the subset marked ✅; the gate/ring/STT
feed move in slice 4):

```python
@dataclass(frozen=True)
class InputFrame:                      # ✅ slice 1
    pcm: bytes                         # post decode/AEC/filter PCM16 @ 16 kHz
    vad_event: VADEvent | None         # at most one event per frame
    vad_configured: bool               # a VAD ran → self-hearing gate applies

class InputProcessingChain:
    async def process(self, audio_bytes: bytes) -> InputFrame: ...   # ✅
    def flush(self) -> None: ...       # ✅ resampler tail discard on cleanup
    # slice 4:
    def buffer_while_speaking(self, frame: InputFrame) -> None: ...  # ring push
    async def flush_ring_to_stt(self) -> int: ...
    async def feed_stt(self, frame: InputFrame) -> None: ...         # hook + send + metrics
    def reset_vad(self) -> None: ...
```

```ts
export interface ProcessedInputFrame {            // ✅ slice 1
  readonly pcm16k: Buffer;
  readonly vadEvent: VADEvent | null;
  readonly vadConfigured: boolean;
}
export class InputProcessingChain {
  process(audioBuffer: Buffer): Promise<ProcessedInputFrame>;        // ✅
  disableVad(): void;                                                // ✅ (call-scoped kill switch)
  isVadDisabled(): boolean;                                          // ✅
}
```

Design rules:

- **Stage order is fixed**: decode → AEC → `audio_filter` → VAD. The
  `AudioFilter` ABC documents "before VAD and STT"; AEC must run first so the
  noise suppressor never eats the echo-cancellation reference alignment, and
  the VAD must see *filtered* audio so noise suppression actually improves
  barge-in robustness.
- AEC / filter / VAD are resolved through **late-bound getters** (`get_aec`,
  `get_audio_filter`, `get_vad`), not captured at construction: the handler
  populates `_aec` / `_auto_vad` in `start()` and the unit suites assign them
  directly on handler instances.
- The filter wrapper is **fail-open with warn-once**: any `process()` raise
  (or non-bytes return) degrades to passthrough of the pre-filter PCM, logs
  one WARNING (then DEBUG), and never stops attempting — a transient provider
  hiccup must not permanently strip noise suppression, and a permanent one
  must not kill the call.
- The chain is **turn-state-free**: it never reads `_is_speaking`. The
  self-hearing gate (which *is* turn state) stays with the handler until
  slice 4, where it will consume `TurnManager.phase` instead.

### 3.2 TurnManager (slice 2 — design only)

Owns every Machine-A field from §2.1 and reifies the phases:

```python
class TurnPhase(enum.Enum):
    IDLE = "idle"
    SPEAKING = "speaking"            # gate-closed vs armed is derived
    DRAINING_BACKLOG = "draining"    # _is_speaking & not tail_grace & cursor>now
    TAIL_GRACE = "tail_grace"

class VadDecision(enum.Enum):
    NONE, NEW_TURN_RESCUE, PHANTOM_SUPPRESSED, BARGE_IN_PENDING, CANCEL_NOW

class TranscriptDecision(enum.Enum):
    PASS, NEW_TURN_RESCUE, ECHO_DROPPED, GATE_SUPPRESSED, NOT_CONFIRMED, CANCELLED

class TurnManager:
    phase: TurnPhase
    generation: int
    async def begin_turn(self) -> None                      # _begin_speaking
    def mark_first_audio(self) -> None
    def track_outbound_playback(self, num_bytes: int) -> None
    async def end_turn_with_grace(self) -> None             # two-phase flip
    async def on_vad_event(self, event: VADEvent) -> VadDecision
    async def on_transcript(self, transcript) -> TranscriptDecision
    async def cancel_turn(self, reason: str) -> None        # _do_cancel_for_barge_in core
    def can_barge_in(self) -> bool
    def heard_response_prefix(self) -> tuple[str, bool] | None
    def cancel_token(self) -> CancelToken                   # per-turn; recreated at begin
    async def close(self) -> None                           # kill timers
```

Key invariants the class enforces (instead of comments enforcing them):

- All mutations funnel through transitions; the generation counter is bumped
  *only* inside `begin_turn` / `cancel_turn` / `end_tail_grace_for_new_turn`.
- Effects on cancel (send_clear, mark drain, ring flush, LLM cancel, TTS
  cancel, metrics, history rewrite) run via injected callbacks so the manager
  stays I/O-free and deterministic under test.
- Metrics calls become **transition actions** (declared per edge), making the
  "never touch metrics on a phantom" rule structural.
- The handler keeps thin `_is_speaking`-style properties delegating to the
  manager during the transition window so existing tests that poke privates
  stay green.

### 3.3 OutputChain (slice 3 — design only)

```python
class OutputChain:
    async def speak_response(
        self,
        token_stream: AsyncIterator[str],
        *,
        turn: TurnManager,
        hook_executor: PipelineHookExecutor,
        record_segments: bool = True,
    ) -> SpeakResult   # spoken_text, interrupted, first_audio_at, tts_chars
    async def speak_text(self, text: str, *, record_segment: bool) -> bool
    # internals: SentenceChunker → after_llm.on_sentence → text_transforms
    #            → before_synthesize → TTS.synthesize → after_synthesize
    #            → AEC far-end tap → AudioSender (transcode/pacing/marks)
    #            → turn.track_outbound_playback + segment recording
```

Owns: sentence chunking, the per-sentence/per-chunk hook tiers, TTS provider
calls + `cancel_active_stream`, transcode/native-format bypass, the
first-message paced sender + mark FIFO, the long-turn filler and
`llm_error_message` fallback audio (which advance the playback clock without
recording heard-prefix segments).

### 3.4 TranscriptGate (slice 4, small)

`_commit_transcript`'s dedup/near-duplicate/hallucination/echo filters plus
the `_last_commit_*` state, as a pure object: `commit(text, now) -> bool`.

---

## 4. Hook & extension-point ownership map

| Public surface | Today (call site) | Owning stage after decomposition |
| --- | --- | --- |
| `PipelineHooks.before_send_to_stt` | `on_audio_received` tail | **InputChain** (STT-feed step; slice 4) |
| `PipelineHooks.after_transcribe` | `_dispatch_turn` | TurnManager dispatch edge (turn commit) |
| `PipelineHooks.before_llm` | `llm_loop` | **OutputChain** (LLM stage) |
| `PipelineHooks.after_llm.on_chunk` | `_process_streaming_response` | **OutputChain** |
| `PipelineHooks.after_llm.on_sentence` | sentence loop | **OutputChain** |
| `PipelineHooks.after_llm.on_response` | post-stream buffer | **OutputChain** |
| `PipelineHooks.before_synthesize` | `_synthesize_sentence` | **OutputChain** (TTS stage) |
| `PipelineHooks.after_synthesize` | `_synthesize_sentence` | **OutputChain** (TTS stage) |
| `Agent.vad` (+ auto-VAD) | `on_audio_received` | **InputChain** (✅ slice 1 feeds it) |
| `Agent.audio_filter` | **nowhere — dead parameter** | **InputChain** (✅ slice 1 wires it) |
| `Agent.echo_cancellation` (AEC) | near-end in `on_audio_received`, far-end in `_synthesize_sentence` | near-end **InputChain** (✅), far-end tap **OutputChain** |
| `Agent.barge_in_strategies` / `barge_in_confirm_ms` | `_handle_barge_in` / pending timer | **TurnManager** |
| `Agent.text_transforms` | `_synthesize_sentence` | **OutputChain** |
| `Agent.background_audio` | mixer in AudioSender path | **OutputChain** |
| `on_transcript` / `on_message` / `on_metrics` callbacks | handler | composition root (handler) |
| Event bus (`transcript_*`, `tts_chunk`, speech events) | scattered | emitted from owning stage, bus stays on handler |
| `on_mark` / pending-mark FIFO | handler | **OutputChain** |

## 5. Recently-fixed bugs → owning stage

(Refs: `CHANGELOG.md` Unreleased + 0.6.x, BUGS.md entries cited in code.)

| Bug (as fixed) | Root cause locus | Stage that owns it after decomposition |
| --- | --- | --- |
| Multi-turn silence: tail grace misclassified the user's next turn as barge-in (`_tail_grace_active`) | Machine A phase ambiguity | **TurnManager** (TAIL_GRACE is a real phase; `NEW_TURN_RESCUE` decision) |
| (Py) `_llm_cancel_event` from a barge-in leaked into the next turn | per-turn token lifecycle | **TurnManager** (`cancel_token()` recreated in `begin_turn`) |
| Barge-in dead during long Hermes/OpenClaw turns (STT loop head-of-line blocking) | dispatch coupling | TurnManager + dispatch edge (single-in-flight `_dispatch_task` stays at the composition root) |
| Pre-first-token LLM abort (hung agent request) | LLM cancel propagation | **OutputChain** (LLM stage) reacting to TurnManager's token |
| Forward-STT-without-AEC self-interruption (VAD-energy cancel deferred to transcript confirmation) | barge-in policy | **TurnManager** (defer policy on the `speech_start` edge) |
| Echo guard (`_looks_like_echo`) + back-to-back near-duplicate dedup swallowing a real fast follow-up | transcript filtering | **TranscriptGate** (+ TurnManager for the speaking-scoped echo check) |
| "Detects the interruption but keeps talking" — speaking state ended at last *push*, not last *playback* (`_playback_buffered_until`, two-phase grace) | playback model | **TurnManager** (cursor) fed by **OutputChain** (`track_outbound_playback`) |
| Interrupted history recorded text the caller never heard (heard-prefix / `[interrupted by caller]`) | playback timeline | **OutputChain** records segments; **TurnManager** computes prefix + rewrites |
| (Py) Twilio/Plivo `send_mark` ignored the caller's mark name → first-message pacing burned timeouts (also BUG #128 mark-window) | marks/pacing | **OutputChain** |
| (TS) unawaited `handler.handleAudio(...)` rejections killed the server | input boundary | **InputChain** boundary (server awaits one entrypoint) |
| (Py) inbound ring `list.pop(0)` O(n) per frame → `deque(maxlen=13)`; ring resized 600→260 ms after bleed-transcription (BUGS.md 2026-05-05) | ring buffer | **InputChain** (ring sizing + flush snapshotting) |
| One-shot barge-in: PSTN echo kept Silero stuck in SPEECH; `VADProvider.reset()` at turn boundaries | VAD state | **InputChain** owns `reset_vad()`; **TurnManager** invokes it on transitions |
| Phantom VAD `speech_start` stamped `turn_start` at echo time → `user_speech_duration_ms` 5-7 s (pre-0.6.1) | metrics-on-transition discipline | **TurnManager** (metrics as transition actions; phantoms structurally metric-free) |
| Phantom first-frame `speech_start` cancelled the prewarmed firstMessage (gate raised 100→500 ms) | barge-in gate anchoring | **TurnManager** (`can_barge_in`, anchored to `mark_first_audio`) |
| BUG #15: `before_send_to_stt` returning `None` still hit STT | STT feed hook | **InputChain** (STT-feed step) |
| BUG #20: interim transcripts skipped barge-in entirely | transcript edge | **TurnManager** (`on_transcript` runs before the final/speech_final gate) |
| `audio_filter` accepted, documented, tested — never invoked | missing input stage | **InputChain** — **fixed in slice 1** |

## 6. Migration plan — reviewable slices

Every slice keeps the public API (`Agent` fields, `PipelineHooks`, handler
entry points) and the full existing unit suites green, in **both** SDKs, in
the same PR (parity rule).

1. **Slice 1 — InputChain core + `audio_filter` wiring (this change).**
   Extract `InputProcessingChain` owning decode → stateful 8k→16k resample →
   AEC near-end → **`audio_filter` (new — fixes the dead parameter)** → VAD
   feed. `on_audio_received` / `handleAudio` delegate to
   `chain.process(frame)` and keep everything downstream (VAD-event handling,
   self-hearing gate, ring buffer, `before_send_to_stt`, STT feed) so the
   diff stays surgical. With no AEC/filter/VAD configured the byte path is
   identical to before. Contained provider fix: `KrispVivaFilter` gains an
   internal re-framing buffer so the pipeline's 20 ms frames satisfy its
   10 ms frame contract instead of raising per-frame.
2. **Slice 2 — TurnManager.** Move the Machine-A fields + transitions behind
   the §3.2 interface; handler keeps delegating properties for the private
   fields the test suites poke (`_is_speaking`, `_tail_grace_active`,
   `_playback_buffered_until`, …). Metrics calls become transition actions.
   No wire-visible behaviour change; the two-phase grace and defer policies
   move verbatim.
3. **Slice 3 — OutputChain.** Extract `_process_streaming_response` /
   `_synthesize_sentence` / paced first-message / marks / filler+error audio
   into `OutputChain.speak_response` / `speak_text`. The chain reports
   playback into TurnManager instead of poking handler fields.
4. **Slice 4 — finish InputChain + TranscriptGate.** Move the self-hearing
   gate, ring buffer, `before_send_to_stt` hook and STT feed into the
   InputChain (consuming `TurnManager.phase`); extract `_commit_transcript`
   into TranscriptGate. `PipelineStreamHandler` is then a composition root of
   ~4 collaborators, and the Realtime/ConvAI handlers can adopt the
   InputChain for their decode paths where applicable.

Out of scope for all slices: changing defaults, changing the wire behaviour,
or exposing the stages as public API (they stay internal until the shape has
survived a few releases).

## 7. Testing strategy per slice

- Slice 1 (shipped): chain-level unit tests with recording fakes assert the
  AEC → filter → VAD order, warn-once passthrough on filter failure, μ-law
  and PCM inputs, and stateful resampling continuity; handler-level tests
  prove `agent(audio_filter=...)` transforms the bytes reaching a fake STT
  and that failures fail open. The pre-existing suites
  (`test_before_send_to_stt_hook`, `test_pipeline_bargein*`,
  `test_pipeline_multiturn_tail_grace`, `stream-handler`/`barge-in-two-stage`
  TS suites) act as the byte-identical regression guard.
- Slice 2: table-driven transition tests for TurnManager (phase × event →
  decision + actions), plus property-style checks that no metrics action
  fires on phantom edges.
- Slice 3: OutputChain tests with a scripted TTS/LLM and a virtual clock
  asserting cursor advancement, heard-prefix segmentation, and mark-window
  pacing.
- Slice 4: gate/ring tests move from handler-level to chain-level; the
  handler suite shrinks to wiring assertions.
