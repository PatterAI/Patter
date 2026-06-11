/**
 * Shared stream handling logic for Twilio and Telnyx WebSocket connections.
 *
 * Encapsulates provider initialization, audio routing, transcript management,
 * metrics, guardrails, tool calling, call control, and on_message dispatching.
 * The provider-specific handlers in server.ts parse their respective WebSocket
 * message formats and delegate to this shared layer.
 */

import { WebSocket as WSWebSocket } from 'ws';
import { OpenAIRealtimeAdapter } from './providers/openai-realtime';
import { ElevenLabsConvAIAdapter } from './providers/elevenlabs-convai';
import { DeepgramSTT } from './providers/deepgram-stt';
import { createTTS } from './provider-factory';
import type { STTAdapter, TTSAdapter, STTTranscript } from './provider-factory';
import { CallMetricsAccumulator } from './metrics';
import { mulawToPcm16, pcm16ToMulaw, StatefulResampler, createResampler8kTo16k, createResampler16kTo8k } from './audio/transcoding';
import { LLMLoop } from './llm-loop';
import { RemoteMessageHandler, isRemoteUrl, isWebSocketUrl } from './remote-message';
import { createHistoryManager } from './handler-utils';
import { DefaultToolExecutor } from './llm-loop';
import { MCPManager } from './tools/mcp-client';
import type { AgentOptions, Guardrail, HookContext, PipelineMessageHandler, ToolDefinition, TransferCallOptions, TransferCallResult, VADProvider, CarrierKind } from './types';
import type { MetricsStore } from './dashboard/store';
import { getLogger } from './logger';
import { validateTwilioSid, TRANSFER_CALL_TOOL, END_CALL_TOOL } from './server';
import { buildConsultTool } from './consult';
import type { ProviderPricing } from './pricing';
import { SentenceChunker } from './sentence-chunker';
import { PipelineHookExecutor } from './pipeline-hooks';
import { InputProcessingChain } from './services/input-chain';
import { EventBus } from './observability/event-bus';
import type { PatterEventType } from './observability/event-bus';
import {
  SPAN_BARGEIN,
  SPAN_ENDPOINT,
  SPAN_LLM,
  startSpan,
} from './observability/tracing';

type AIAdapter = OpenAIRealtimeAdapter | ElevenLabsConvAIAdapter;

// ---------------------------------------------------------------------------
// Tool-call preambles (OpenAI Realtime)
// ---------------------------------------------------------------------------

/**
 * Default "# Preambles" guidance block prepended to the Realtime session
 * `instructions` when `AgentOptions.toolCallPreambles` is `true`.
 *
 * Steers the model (most effectively `gpt-realtime-2`, where preambles are
 * first-class) to speak ONE short, action-describing sentence immediately
 * before a tool call that may take a moment — in its own voice — so the
 * caller hears that work is happening during a slow (30-60 s) tool. The
 * "Prefer" phrasings are OpenAI-approved action openers; the "Avoid" list
 * blocks fillers that imply a result before the tool returns.
 *
 * MUST stay byte-identical to the Python `DEFAULT_TOOL_CALL_PREAMBLE_BLOCK`
 * in `stream_handler.py` so the two SDKs steer the model the same way.
 */
export const DEFAULT_TOOL_CALL_PREAMBLE_BLOCK = `# Preambles

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
- "Hmm..." / "One moment while I process that..."`;

/**
 * Prepend the "# Preambles" guidance block to a Realtime system prompt.
 *
 * - `knob` falsy (`undefined` / `false`) — returns `prompt` byte-identical
 *   (today's behavior exactly).
 * - `knob === true` — prepends {@link DEFAULT_TOOL_CALL_PREAMBLE_BLOCK}.
 * - `knob` is a string — prepends that string verbatim as the full block
 *   (override).
 *
 * Pure function: no mutation of the agent or any shared config. Mirrors
 * Python `apply_tool_call_preambles()` in `stream_handler.py`.
 */
export function applyToolCallPreambles(
  prompt: string,
  knob: boolean | string | undefined,
): string {
  if (!knob) return prompt;
  const block = typeof knob === 'string' ? knob : DEFAULT_TOOL_CALL_PREAMBLE_BLOCK;
  return prompt ? `${block}\n\n${prompt}` : block;
}

// ---------------------------------------------------------------------------
// Telephony bridge — abstracts Twilio vs Telnyx wire differences
// ---------------------------------------------------------------------------

/** Provider-specific operations that differ between Twilio, Telnyx and Plivo. */
export interface TelephonyBridge {
  /** Human-readable label for log messages. */
  readonly label: string;
  /** Telephony provider name for metrics. */
  readonly telephonyProvider: CarrierKind;
  /** Wire format of the inbound media stream after the carrier has accepted
   *  the call. Lets the StreamHandler decide whether to decode + resample
   *  inbound audio without needing carrier-name knowledge — mulaw 8 kHz
   *  carriers (Twilio, Plivo) say ``ulaw_8000``, PCM 16 kHz carriers
   *  (Telnyx with PCMU bidirectional negotiation off) say ``pcm_16000``. */
  readonly inputWireFormat: 'ulaw_8000' | 'pcm_16000';

  /** Send an audio chunk (base64-encoded) to the telephony WebSocket. */
  sendAudio(ws: WSWebSocket, audioBase64: string, streamSid: string): void;
  /** Send a mark event to track audio playback progress (no-op for Telnyx). */
  sendMark(ws: WSWebSocket, markName: string, streamSid: string): void;
  /** Send a clear/interrupt event to stop audio playback. */
  sendClear(ws: WSWebSocket, streamSid: string): void;

  /** Transfer the call to a different number or SIP URI via provider API.
   *  ``options.mode === 'warm'`` requests a hold-announce-bridge warm
   *  transfer (Twilio only for now); the default / omitted options run the
   *  historical cold (blind) redirect byte-identically. Returns a
   *  {@link TransferCallResult} envelope for warm mode (``{ error }`` when
   *  unsupported / failed — the call keeps running); cold mode may resolve
   *  ``void`` (legacy contract). */
  transferCall(callId: string, toNumber: string, options?: TransferCallOptions): Promise<TransferCallResult | void>;
  /** Hang up the call via provider API. */
  endCall(callId: string, ws: WSWebSocket): Promise<void>;
  /** Send DTMF digits to the caller. Carriers using REST (Telnyx) ignore
   *  ``ws``; carriers that send DTMF as a media-stream message (Plivo) use it. */
  sendDtmf?(ws: WSWebSocket, callId: string, digits: string, delayMs: number): Promise<void>;
  /** Start call recording via provider API (optional). */
  startRecording?(callId: string): Promise<void>;
  /** Stop call recording via provider API (optional). */
  stopRecording?(callId: string): Promise<void>;

  /** Create an STT instance appropriate for this provider's audio format.
   *  Returns any of the supported STT adapters (DeepgramSTT, WhisperSTT,
   *  CartesiaSTT, SonioxSTT, AssemblyAISTT) or null when no STT is configured. */
  createStt(agent: AgentOptions): Promise<STTAdapter | null>;
  /** Query actual telephony costs after call ends. */
  queryTelephonyCost(metricsAcc: CallMetricsAccumulator, callId: string): Promise<void>;
}

// ---------------------------------------------------------------------------
// Shared utility: guardrails
// ---------------------------------------------------------------------------

function checkGuardrails(text: string, guardrails: readonly Guardrail[] | undefined): Guardrail | null {
  if (!guardrails) return null;
  for (const guard of guardrails) {
    let blocked = false;
    if (guard.blockedTerms) {
      blocked = guard.blockedTerms.some((term) => text.toLowerCase().includes(term.toLowerCase()));
    }
    if (!blocked && guard.check) {
      blocked = guard.check(text);
    }
    if (blocked) return guard;
  }
  return null;
}

/** Strip control characters and truncate a string before writing it to logs. */
export function sanitizeLogValue(v: string, maxLen = 200): string {
  // eslint-disable-next-line no-control-regex
  const cleaned = v.replace(/[\x00-\x1f\x7f]/g, '');
  return cleaned.length > maxLen ? cleaned.slice(0, maxLen) + '...' : cleaned;
}

/**
 * Mask an E.164 phone number for logging. Keeps only the last 4 characters
 * to preserve enough context for correlation while avoiding PII leakage.
 * Mirrors ``getpatter.utils.log_sanitize.mask_phone_number``.
 */
export function maskPhoneNumber(number: unknown): string {
  if (!number) return '***';
  const text = String(number);
  if (text.length <= 4) return '***';
  return `***${text.slice(-4)}`;
}

function isValidE164(number: string): boolean {
  return /^\+[1-9]\d{6,14}$/.test(number);
}

/**
 * Augment a tool list with the built-in `transfer_call` / `end_call` tools,
 * wired to the telephony-level transfer / hangup callbacks. Used by pipeline
 * mode to match the Realtime path's tool surface (Realtime injects the same
 * two built-ins at `server.ts` and dispatches them via the bridge in this
 * file's tool dispatcher around line 3100). Without this the pipeline LLM
 * never sees the built-ins and cannot initiate a transfer or hangup
 * regardless of system-prompt instructions. Parity with Python helper
 * `_augment_with_builtin_handoff_tools` in `stream_handler.py`.
 *
 * Built-ins are skipped when the corresponding callback is missing (keeps
 * non-telephony test harnesses clean). User-provided tools keep their
 * original order; the built-ins are appended.
 */
export function augmentWithBuiltinHandoffTools(
  userTools: ToolDefinition[] | null | undefined,
  callbacks: {
    transferCall?: (number: string, options?: TransferCallOptions) => Promise<TransferCallResult | void>;
    endCall?: (reason: string) => Promise<void>;
  },
): ToolDefinition[] {
  const out: ToolDefinition[] = [...(userTools ?? [])];
  if (callbacks.transferCall) {
    const transferCall = callbacks.transferCall;
    out.push({
      ...TRANSFER_CALL_TOOL,
      handler: async (args: Record<string, unknown>): Promise<string> => {
        const number = typeof args.number === 'string' ? args.number : '';
        const mode = typeof args.mode === 'string' && args.mode ? args.mode : 'cold';
        const summary = typeof args.summary === 'string' ? args.summary : '';
        if (mode !== 'cold' && mode !== 'warm') {
          return JSON.stringify({
            error: `Invalid transfer mode '${mode}' — use 'cold' or 'warm'`,
            status: 'rejected',
          });
        }
        if (!isValidE164(number)) {
          return JSON.stringify({ error: 'Invalid phone number format', status: 'rejected' });
        }
        if (mode === 'warm') {
          const outcome = await transferCall(number, { mode: 'warm', summary });
          if (outcome && typeof outcome === 'object') {
            return JSON.stringify(outcome);
          }
          return JSON.stringify({ status: 'transferring', mode: 'warm', to: number });
        }
        // Cold mode: byte-identical to the historical behaviour.
        await transferCall(number);
        return JSON.stringify({ status: 'transferring', to: number });
      },
    });
  }
  if (callbacks.endCall) {
    const endCall = callbacks.endCall;
    out.push({
      ...END_CALL_TOOL,
      handler: async (args: Record<string, unknown>): Promise<string> => {
        const reason = typeof args.reason === 'string' ? args.reason : 'conversation_complete';
        await endCall(reason);
        return JSON.stringify({ status: 'ending', reason });
      },
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// Multi-agent handoff — built-in handoff_to tool
// ---------------------------------------------------------------------------

/** Name of the built-in multi-agent handoff tool injected when
 *  `AgentOptions.handoffs` is configured. */
export const HANDOFF_TOOL_NAME = 'handoff_to';

/**
 * Build the `handoff_to` tool schema for the given target-agent names.
 *
 * The names are surfaced both as a JSON-schema `enum` (so the model can only
 * pick a configured target) and in the description. Sorted for a
 * deterministic schema. Parity with Python `build_handoff_tool`.
 */
export function buildHandoffTool(handoffNames: readonly string[]): {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
} {
  const names = [...handoffNames].map(String).sort();
  return {
    name: HANDOFF_TOOL_NAME,
    description:
      'Hand the conversation off to another specialized agent. The call ' +
      "continues seamlessly with the new agent's instructions and tools. " +
      'Available agents: ' + names.join(', '),
    parameters: {
      type: 'object',
      properties: {
        name: {
          type: 'string',
          enum: names,
          description: 'Name of the agent to hand the conversation to',
        },
        reason: {
          type: 'string',
          description: 'Brief reason for the handoff',
        },
      },
      required: ['name'],
    },
  };
}

/**
 * Return a copy of `current` with the LLM-visible configuration of the
 * handoff `target` applied.
 *
 * Only conversational config swaps: `systemPrompt`, `tools`, `variables`,
 * `guardrails`, `textTransforms`, `consult`, `handoffs` (so chained handoffs
 * follow the target's own map), `disablePhonePreamble` and
 * `toolCallPreambles`. Live audio infrastructure established at call start —
 * STT/TTS/VAD instances, engine connection, carrier codec settings, and
 * therefore the voice on engines that cannot switch voice mid-session — is
 * intentionally retained from `current`. Parity with Python
 * `_apply_handoff_target`.
 */
export function applyHandoffTarget(current: AgentOptions, target: AgentOptions): AgentOptions {
  return {
    ...current,
    systemPrompt: target.systemPrompt,
    tools: target.tools,
    variables: target.variables,
    guardrails: target.guardrails,
    textTransforms: target.textTransforms,
    consult: target.consult,
    handoffs: target.handoffs,
    disablePhonePreamble: target.disablePhonePreamble,
    toolCallPreambles: target.toolCallPreambles,
  };
}

/** Render the system-style transcript line recording a handoff. */
export function handoffHistoryText(name: string, reason: string): string {
  let text = `[handoff] Conversation handed to agent '${name}'`;
  if (reason) text += ` — ${reason}`;
  return text;
}

/**
 * Short words / phrases that Whisper (and, less often, Deepgram) routinely
 * emit when fed silence or TTS echo on mulaw 8 kHz. Dropping them as turns
 * prevents the caller from entering a feedback loop where every silent frame
 * triggers a new LLM+TTS turn. Parity with Python `_STT_HALLUCINATIONS`.
 *
 * Whisper-specific full-phrase hallucinations: the model's training set was
 * dominated by YouTube captions — on silence / echo it falls back to the most
 * common training-set closers. These fire hard on PSTN echo loopback when the
 * agent's outbound audio bleeds into the input buffer and the upstream VAD
 * commits a "non-empty" segment to transcription.
 * Comparison happens against the lower-cased + stripped form.
 */
const HALLUCINATIONS = new Set([
  // Issue #154: the hallucination filter is now DISPLAY-ONLY — it no longer
  // gates response creation (the server drives the response on
  // ``input_audio_buffer.committed`` by default). Dropping a phrase here
  // therefore deletes the user's transcript line (recordSttComplete never
  // fires → empty user_text → dashboard skips the user line). So this set is
  // restricted to genuine NON-SPEECH artefacts that Whisper emits on
  // silence / TTS echo, NOT real conversational words. Standalone words like
  // 'yes', 'no', 'okay', 'right', 'you', 'thanks' were REMOVED — they are
  // legitimate user replies and must reach the transcript. Parity with
  // Python ``_STT_HALLUCINATIONS``.
  //
  // Whisper caption / training-set hallucinations. Whisper was trained heavily
  // on captioned video, so on silence / PSTN echo it falls back to the most
  // common caption credits + sign-offs. Curated from widely-reported
  // Whisper-on-silence outputs across the open-source ASR community.
  'thank you for watching',
  'thanks for watching',
  'thank you for watching!',
  'thanks for watching!',
  'thank you so much for watching',
  'thank you for watching please subscribe',
  'thanks for watching please subscribe',
  'thanks for listening',
  "we'll see you next time",
  'see you next time',
  'bye bye',
  'please subscribe',
  'please subscribe to my channel',
  "don't forget to subscribe",
  'like and subscribe',
  'subscribe',
  'subtitles by the amara.org community',
  'subtitles by the amara org community',
  'subtitles by',
  'transcribed by',
  'transcription by castingwords',
  'the end',
  // Music / sound markers.
  'music',
  '[music]',
  'piano music',
  'applause',
  '[applause]',
  '♪',
  // Silence markers.
  '[no audio]',
  '[silence]',
  '[blank_audio]',
  '(silence)',
]);

/**
 * True when `text` is — or is composed entirely of — known STT hallucinations.
 * Beyond an exact set lookup it (a) strips trailing punctuation, since Whisper
 * appends it ("Thank you.", "Bye bye.") and that alone defeats an exact match,
 * and (b) splits multi-closer segments ("We'll see you next time. Bye bye.")
 * on sentence boundaries, dropping the turn only when EVERY piece is a known
 * hallucination — so a real sentence that merely contains a filler word is
 * never falsely dropped. Parity with Python ``_is_stt_hallucination``.
 *
 * Exported for unit tests (issue #154 narrowed the blocklist to display-only
 * non-speech artefacts); not part of the public package surface.
 */
export function isSttHallucination(text: string): boolean {
  const stripped = text.trim().toLowerCase().replace(/[.,!?;:…。！？\s]+$/u, '').trim();
  if (stripped === '') return true;
  if (HALLUCINATIONS.has(stripped)) return true;
  const pieces = stripped
    .split(/[.!?…。！？]+/u)
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
  return pieces.length > 1 && pieces.every((p) => HALLUCINATIONS.has(p));
}

/** Fraction of a candidate's words that must appear in the agent's spoken text
 * for it to count as the agent's own TTS echoing back. Mirrors Python
 * ``_ECHO_WORD_OVERLAP_THRESHOLD``. */
const ECHO_WORD_OVERLAP_THRESHOLD = 0.6;

/** Minimum word count before a candidate can be classified as echo — short
 * caller replies that repeat the agent's offered words ("lunedì", "yes",
 * "Monday at two") are legitimate answers, never echo. Mirrors Python
 * ``_ECHO_MIN_CANDIDATE_WORDS``. */
const ECHO_MIN_CANDIDATE_WORDS = 4;

/** Lowercase, drop punctuation, collapse whitespace — for echo comparison. */
export function normalizeForEcho(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/u, ' ')
    .trim()
    .replace(/\s+/gu, ' ');
}

/** True when ``candidate`` looks like a fragment of ``agentText`` — i.e. the
 * agent's own TTS bleeding into STT (forwarded during TTS without effective
 * AEC) rather than real caller speech. Substring OR high word-overlap.
 * Mirrors Python ``_looks_like_echo``. */
export function looksLikeEcho(candidate: string, agentText: string): boolean {
  const a = normalizeForEcho(agentText);
  const c = normalizeForEcho(candidate);
  if (!a || !c) return false;
  const words = c.split(' ').filter(Boolean);
  // Never classify a short reply as echo — exempts single-word / few-word
  // caller answers that legitimately repeat the agent's offered words.
  if (words.length < ECHO_MIN_CANDIDATE_WORDS) return false;
  if (a.includes(c)) return true;
  const agentWords = new Set(a.split(' '));
  const overlap = words.filter((w) => agentWords.has(w)).length / words.length;
  return overlap >= ECHO_WORD_OVERLAP_THRESHOLD;
}

/** True when two normalised finals are the same utterance double-emitted
 * (identical, or one a substring of the other). Mirrors Python
 * ``_is_near_duplicate``. */
export function isNearDuplicate(a: string, b: string): boolean {
  if (!a || !b) return false;
  if (a === b) return true;
  const [shorter, longer] = a.length <= b.length ? [a, b] : [b, a];
  // Word-boundary aware: a character infix ("no" in "nothing else") is NOT a
  // duplicate; only a true word-prefix double-emit (speech_final+is_final) is.
  return longer.startsWith(shorter + ' ');
}

/** Sentence-ending characters shared with the hallucination splitter — also
 * the fast-path confidence signal for preemptive generation. Mirrors Python
 * ``_SENTENCE_ENDERS``. */
const SENTENCE_ENDERS = '.!?…。！？';

/** True when `text` (whitespace-trimmed) ends with sentence-final punctuation.
 * Mirrors Python ``_ends_with_sentence_final_punct``. */
export function endsWithSentenceFinalPunct(text: string): boolean {
  const stripped = (text ?? '').trimEnd();
  return stripped.length > 0 && SENTENCE_ENDERS.includes(stripped[stripped.length - 1]);
}

/**
 * Whether a committed FINAL transcript matches the INTERIM a speculative turn
 * was generated from, i.e. the speculation can be released. Both sides are
 * normalized via {@link normalizeForEcho} (lowercase, punctuation stripped,
 * whitespace collapsed), so a final that merely adds trailing punctuation /
 * capitalization to the interim still matches. Mirrors Python
 * ``_speculation_matches``.
 */
export function speculationMatches(interim: string, final: string): boolean {
  const a = normalizeForEcho(interim);
  const b = normalizeForEcho(final);
  return a.length > 0 && a === b;
}

/**
 * In-flight PREEMPTIVE GENERATION state for one speculated user turn.
 *
 * Created by `StreamHandler.startSpeculation` on a confident interim
 * transcript. The owning task runs the LLM + sentence-chunked TTS but HOLDS
 * all audio in `buffered` until the final transcript commits:
 *
 * - release (final matches): `released=true` + `signalDecision()` — the task
 *   flushes `buffered` to the carrier and continues live; it IS the real
 *   turn from then on (history/metrics recorded by the releaser + task).
 * - discard (mismatch / barge-in / replaced / overflow / teardown): the
 *   abort signal fires — the task unwinds without ever touching the carrier,
 *   conversation history, or per-turn metrics.
 *
 * Mirrors Python ``_SpeculativeTurn``.
 */
class SpeculativeTurn {
  readonly interimText: string;
  readonly normText: string;
  /** Per-speculation LLM cancel signal (same machinery the live path hands
   * to `llmLoop.run`). On release this becomes the handler's `llmAbort` so
   * the existing barge-in cancel paths reach the speculative stream. */
  readonly abort = new AbortController();
  released = false;
  /** True once buffered audio has been flushed to the carrier (release). */
  flushed = false;
  /** True when the speculation can no longer be released (LLM error, buffer
   * overflow, internal failure) — the commit path must dispatch normally. */
  failed = false;
  /** Barge-in after release cut the live continuation short. */
  interrupted = false;
  /** Stamped at release with the committed final transcript. */
  finalText = '';
  /** Per-sentence audio held until release. The chunks array is registered
   * BEFORE synthesis so a mid-sentence release flushes the partial too. */
  buffered: Array<{ text: string; chunks: Buffer[] }> = [];
  bufferedBytes = 0;
  responseParts: string[] = [];
  /** Same flag shape `synthesizeSentence` uses, shared across the buffered
   * flush and the live continuation so the per-turn first-byte metric stays
   * idempotent. */
  readonly ttsFirstByteSent = { value: false };
  llmFirstTokenRecorded = false;
  task: Promise<void> | null = null;
  /** Resolves once the commit-time decision is known (either way); the task
   * parks on it when generation finishes before the final commits. */
  readonly decision: Promise<void>;
  private decisionResolve!: () => void;

  constructor(interimText: string) {
    this.interimText = interimText;
    this.normText = normalizeForEcho(interimText);
    this.decision = new Promise<void>((resolve) => {
      this.decisionResolve = resolve;
    });
  }

  signalDecision(): void {
    this.decisionResolve();
  }
}

// ---------------------------------------------------------------------------
// StreamHandler context (immutable per-call configuration)
// ---------------------------------------------------------------------------

/** Per-call dependencies injected into `StreamHandler` (immutable for the call's lifetime). */
export interface StreamHandlerDeps {
  readonly config: {
    readonly openaiKey?: string;
    readonly twilioSid?: string;
    readonly twilioToken?: string;
  };
  readonly agent: AgentOptions;
  readonly bridge: TelephonyBridge;
  readonly metricsStore: MetricsStore;
  readonly pricing: Record<string, Partial<ProviderPricing>> | null;
  readonly remoteHandler: RemoteMessageHandler;
  /**
   * Per-call start callback. A returned object is treated as PER-CALL AGENT
   * OVERRIDES (snake_case keys: system_prompt, voice, model, language,
   * first_message, provider, tools, variables) — parity with Python's
   * ``apply_call_overrides``. Return nothing for the legacy observe-only
   * behaviour.
   */
  readonly onCallStart?: (
    data: Record<string, unknown>,
  ) => Promise<void | Record<string, unknown> | undefined> | void | Record<string, unknown>;
  readonly onCallEnd?: (data: Record<string, unknown>) => Promise<void>;
  readonly onTranscript?: (data: Record<string, unknown>) => Promise<void>;
  readonly onMessage?: PipelineMessageHandler | string;
  readonly onMetrics?: (data: Record<string, unknown>) => Promise<void>;
  readonly recording: boolean;
  /**
   * Optional factory returning a carrier-neutral local call recorder for
   * ``callId`` (wired by ``EmbeddedServer.makeLocalRecorder`` when
   * ``serve({ localRecording })`` is on). Returning ``null`` / leaving the
   * field unset keeps every recording tap a no-op. The handler owns the
   * recorder lifetime: created in ``handleCallStart``, finalized in
   * ``fireCallEnd`` (every teardown path funnels there).
   */
  readonly makeLocalRecorder?: (
    callId: string,
  ) => import('./audio/call-recorder').LocalCallRecorder | null;
  /** When true, only the first TTFB per call is forwarded to the event bus. Default false. */
  readonly reportOnlyInitialTtfb?: boolean;
  /**
   * Optional speech-edge events dispatcher. When provided, the handler emits
   * turn-taking edges (VAD start/stop, EOU commit, agent first/last wire
   * chunk) as the call progresses. ``undefined`` means no events are fired
   * — exact prior behaviour. See ``src/_speech-events.ts``.
   */
  readonly speechEvents?: import("./_speech-events").SpeechEvents;
  /** Build an AI adapter (OpenAI Realtime or ElevenLabs ConvAI). Injected to avoid circular imports. */
  readonly buildAIAdapter: (resolvedPrompt: string, tools?: readonly ToolDefinition[]) => AIAdapter;
  /** Sanitize untrusted key-value variables map. */
  readonly sanitizeVariables: (raw: Record<string, unknown>) => Record<string, string>;
  /** Replace {key} placeholders in a template string. */
  readonly resolveVariables: (template: string, variables: Record<string, string>) => string;
  /**
   * Optional accessor returning pre-rendered first-message audio for
   * ``callId``. Wired by ``Patter.serve()`` when the parent client has
   * ``agent.prewarmFirstMessage: true``. Returning ``undefined`` means
   * "no prewarm — always run live TTS".
   */
  readonly popPrewarmAudio?: (callId: string) => Buffer | undefined;
  /**
   * Optional accessor returning pre-opened, fully-handshaked provider
   * WebSockets for ``callId`` so the per-call StreamHandler can
   * adopt them at ``start`` instead of paying the cold handshake on
   * the first turn. Wired by ``Patter.serve()``. Returning
   * ``undefined`` (or any sub-field unset) means "no parked socket
   * for this provider — fall back to fresh ``connect()``".
   */
  readonly popPrewarmedConnections?: (
    callId: string,
  ) => import('./client').ParkedProviderConnections | undefined;
}

// ---------------------------------------------------------------------------
// StreamHandler — manages a single call session
// ---------------------------------------------------------------------------

/** Per-call session controller — owns the AI adapter, STT/TTS pipeline, and metrics. */
export class StreamHandler {
  private readonly deps: StreamHandlerDeps;
  private readonly ws: WSWebSocket;
  private caller: string;
  private callee: string;

  // Mutable call state
  private streamSid = '';
  private callId = '';
  private adapter: AIAdapter | null = null;
  private stt: STTAdapter | null = null;
  private tts: TTSAdapter | null = null;
  private isSpeaking = false;
  /**
   * True only while the post-TTS tail-grace window is pending: the agent has
   * finished its turn but ``isSpeaking`` is still held for
   * ``PATTER_TTS_TAIL_GRACE_MS`` to swallow the fading echo tail. A VAD
   * ``speech_start`` (or a transcript) during this window is the user's NEXT
   * turn, not a barge-in — there is nothing left to interrupt. Set by
   * ``endSpeakingWithGrace``; cleared by ``beginSpeaking``, the grace flip,
   * ``cancelSpeaking``, and ``endTailGraceForNewTurn``. Parity with Python
   * ``_tail_grace_active``.
   */
  private tailGraceActive = false;
  /**
   * Ring buffer of inbound PCM16 16 kHz frames captured while the agent
   * is speaking and the self-hearing guard is dropping audio. On
   * barge-in we flush this buffer to STT so Deepgram (or any other
   * streaming STT) receives the user's first ~500 ms of speech — which
   * would otherwise be lost while the VAD's `minSpeechDuration` window
   * accumulated and fired `speech_start`. Each frame is 20 ms × 32 bytes
   * (16 kHz × 16-bit mono) ≈ 640 bytes.
   *
   * Capped to ``INBOUND_AUDIO_RING_FRAMES`` to recover only the
   * VAD-missed leading edge of the user's speech (default 250 ms,
   * matching SileroVAD ``minSpeechDuration``). Earlier values up to
   * 600 ms were including ~350 ms of pre-speech silence/agent-bleed in
   * the replay; on PSTN (where AEC is a no-op) Deepgram trained on
   * English happily transcribes that bleed as English garbage
   * (``"The same as Edgar,"``, ``"Permadees."``) and commits it to
   * the LLM as a phantom user transcript. See BUGS.md 2026-05-05
   * post-barge-in bleed-transcription entry.
   */
  private inboundAudioRing: Buffer[] = [];
  private static readonly INBOUND_AUDIO_RING_FRAMES = 13;
  /**
   * Cached LLM provider tag used by speech-event payloads. Mirrors the
   * value passed to the metrics accumulator at construction time so the
   * speech-edge events report the same provider classification as
   * dashboard / pricing rows.
   */
  private llmProviderTag: string = "openai";
  /**
   * Auto-loaded SileroVAD when ``agent.vad`` is undefined. Populated by
   * ``initPipeline`` and queried alongside ``agent.vad`` on every audio frame.
   * Stays null when ``onnxruntime-node`` is not installed — the pipeline
   * then falls back to the STT-endpoint heuristic (legacy behaviour).
   */
  private autoVad: VADProvider | null = null;
  /**
   * Acoustic echo canceller (NLMS adaptive filter). Lazily instantiated in
   * ``initPipeline`` when ``agent.echoCancellation`` is true. ``null``
   * otherwise — the mic path stays a pure pass-through for handset /
   * headset deployments that don't have TTS bleed.
   */
  private aec: import('./audio/aec').NlmsEchoCanceller | null = null;
  /**
   * Carrier-neutral local call recorder (stereo WAV; left=caller,
   * right=agent). Created in ``handleCallStart`` via
   * ``deps.makeLocalRecorder`` when ``serve({ localRecording })`` is on;
   * ``null`` keeps every tap a no-op. Finalized (header patched, file
   * closed) in ``fireCallEnd`` — both ``handleStop`` and ``handleWsClose``
   * funnel there, so abnormal teardown still yields a parseable file.
   * Parity with Python ``StreamHandler.local_recorder``.
   */
  private localRecorder: import('./audio/call-recorder').LocalCallRecorder | null = null;
  /**
   * Monotonic counter incremented on every TTS-start. The grace timer
   * scheduled by ``endSpeakingWithGrace`` only flips ``isSpeaking=false``
   * if the counter still matches its capture — a new turn that started in
   * the meantime invalidates the obsolete timer instead of clobbering its
   * own ``isSpeaking=true``.
   */
  private speakingGeneration = 0;
  /**
   * Wall-clock timestamp (ms since epoch) when the current TTS turn
   * started — captured by ``beginSpeaking`` and cleared by
   * ``cancelSpeaking`` / the grace flip. Used to gate barge-in: we
   * suppress the cancel for the first
   * ``MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_AEC`` of every turn (when AEC
   * is on) so the AEC filter has time to converge — otherwise residual
   * TTS bleed in the mic stream looks like user speech to VAD and
   * triggers an immediate self-cancellation of the agent's first
   * sentence.
   */
  private speakingStartedAt: number | null = null;
  /**
   * Wall-clock (ms) when the FIRST TTS audio chunk actually reached the
   * carrier wire — set in ``markFirstAudioSent`` after ``bridge.sendAudio``
   * succeeds, cleared by ``beginSpeaking`` / ``cancelSpeaking``. The barge-in
   * gate measures elapsed from this instant, NOT from ``speakingStartedAt``,
   * because ElevenLabs (and other cloud TTS) take 200-700 ms to emit the
   * first byte. A gate anchored to ``beginSpeaking`` would expire on
   * background noise before any audio went out, exit the TTS loop on
   * ``isSpeaking=false``, and silently cut the agent's first turn.
   */
  private firstAudioSentAt: number | null = null;
  /**
   * Estimated wall-clock (ms) when the LAST audio byte pushed to the carrier
   * finishes PLAYING on the phone. The pipeline pushes TTS audio as fast as
   * the provider synthesizes it (no pacing) and the carrier buffers + plays
   * at realtime, so "we finished pushing" and "the caller finished hearing"
   * can diverge by tens of seconds — especially with agent-runtime LLMs
   * (Hermes/OpenClaw) that deliver a long reply all at once after a thinking
   * pause. ``endSpeakingWithGrace`` holds ``isSpeaking=true`` (with
   * ``tailGraceActive=false``) until this cursor passes, so a barge-in during
   * the audible backlog still takes the cancel path (``sendClear`` drops the
   * carrier buffer) instead of being treated as a calm next turn. Advanced by
   * ``trackOutboundPlayback``; reset by ``cancelSpeaking`` (the buffer is
   * cleared) and ``endTailGraceForNewTurn``.
   */
  private playbackBufferedUntil = 0;
  /**
   * Per-turn playback timeline used to estimate the response prefix the
   * caller actually HEARD when a barge-in lands. ``turnPlaybackTotalMs``
   * accumulates the playout duration of every chunk pushed this turn
   * (including filler audio, which keeps the timeline aligned);
   * ``turnSpokenSegments`` records ``{text, startMs}`` for each RESPONSE
   * sentence at its first audible chunk (filler / error-fallback audio
   * advances the clock but adds no segment). ``heard = total - backlog``
   * then maps to a sentence-granular prefix — see ``heardResponsePrefix``.
   * Both reset at ``beginSpeaking``. Mirrors Python
   * ``_turn_playback_total_s`` / ``_turn_spoken_segments``.
   */
  private turnPlaybackTotalMs = 0;
  private turnSpokenSegments: Array<{ readonly text: string; readonly startMs: number }> = [];
  /**
   * Optional barge-in confirmation strategies. With an empty array the
   * SDK falls back to the legacy "cancel on first VAD speech_start"
   * behaviour. With one or more strategies, a VAD speech_start during
   * TTS marks the barge-in as *pending* — TTS keeps streaming naturally
   * — and the strategies are consulted on every STT transcript via
   * ``handleBargeIn``. The first strategy that returns ``true`` cancels
   * the agent; if none confirm within ``bargeInConfirmMs`` the pending
   * state is dropped and the agent finishes its sentence.
   */
  private readonly bargeInStrategies: readonly import('./services/barge-in-strategies').BargeInStrategy[];
  /** Pending-barge-in confirmation timeout in milliseconds. */
  private readonly bargeInConfirmMs: number;
  /** Wall-clock (ms) when the current pending barge-in started, or
   * ``null`` if no barge-in is pending. */
  private bargeInPendingSince: number | null = null;
  /** Timer that fires the pending-barge-in timeout. In
   * ``bargeInMode: 'pause_resume'`` this same handle holds the
   * false-interruption resume timer. */
  private bargeInPendingTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * Pause-and-resume false-interruption handling (opt-in
   * ``agent.bargeInMode: 'pause_resume'``; default ``'cancel'`` keeps
   * today's behaviour byte-identical): PAUSE output on
   * VAD speech_start (carrier cleared, sends gated on ``outputPaused``),
   * KILL on a committed final transcript within ``bargeInConfirmMs``,
   * RESUME from the first not-fully-heard sentence otherwise. Mirrors
   * Python ``_barge_in_mode`` / ``_output_paused``.
   */
  private readonly bargeInMode: 'cancel' | 'pause_resume';
  /** True while output is paused: ``synthesizeSentence`` queues chunks
   * into per-sentence retention entries instead of sending, and the LLM
   * loops buffer whole sentences as text. */
  private outputPaused = false;
  /** Per-pause decision latch — resolved when the pause resolves
   * (resume, kill, or teardown) so loop-side waiters can proceed. */
  private pauseDecision: { promise: Promise<void>; resolve: () => void } | null =
    null;
  /** Sentences produced by the LLM while paused (text, pre-guardrail).
   * Spoken in order on resume; discarded on kill. Bounded by
   * ``PAUSE_MAX_BUFFERED_SENTENCES`` — overflow degrades to a full
   * cancel so memory stays bounded against a runaway stream. */
  private pausedSentences: string[] = [];
  /**
   * Per-turn retained sentence audio (pause_resume mode only): one entry
   * per response sentence holding every TTS chunk produced for it.
   * ``sent`` counts chunks actually delivered to the carrier — the
   * resume path resets it to 0 for the unheard tail and re-sends from
   * memory (no TTS re-billing). Index-aligned with
   * ``turnSpokenSegments`` for the stamped prefix. Bounded by
   * ``PAUSE_RESUME_MAX_RETAINED_S``.
   */
  private turnSentenceAudio: Array<{
    text: string;
    chunks: Buffer[];
    sent: number;
  }> = [];
  private pauseRetainedBytes = 0;
  /** Set when the retained-audio cap was exceeded while NOT paused (very
   * long carrier backlog): retention is released and pause_resume falls
   * back to legacy cancel for the rest of the turn. Reset at
   * ``beginSpeaking``. */
  private pauseResumeOverflowed = false;
  /** Sentence index (into ``turnSpokenSegments`` / ``turnSentenceAudio``)
   * of the first sentence the caller had NOT fully heard at pause time —
   * the resume offset. Sentence granularity: the partially-played
   * sentence is replayed from its start (natural-sounding repair) rather
   * than resumed mid-word. */
  private pauseResumeIndex = 0;
  /** False until the turn body finishes pushing audio (the
   * ``endSpeakingWithGrace`` call in its finally). The resume path uses
   * it to decide whether the #164 grace machinery must be re-armed for
   * the re-sent tail (post-complete pause) or whether the still-running
   * turn body will arm it itself. */
  private turnOutputDone = false;
  /** Cap on sentences buffered as text while output is paused. A pause
   * lasts at most ``bargeInConfirmMs`` (1.5 s default) so this is
   * generous; overflow degrades to a full cancel. Mirrors Python
   * ``_PAUSE_MAX_BUFFERED_SENTENCES``. */
  private static readonly PAUSE_MAX_BUFFERED_SENTENCES = 32;
  /** Cap (seconds of playout) on retained per-sentence TTS audio — both
   * the already-sent tail kept for re-send and chunks queued while
   * paused. 15 s ≈ 480 KB of PCM16 @ 16 kHz per concurrent call.
   * Overflow while paused → degrade to full cancel; overflow while
   * speaking → release retention and fall back to legacy cancel for the
   * rest of the turn. Mirrors Python ``_PAUSE_RESUME_MAX_RETAINED_S``. */
  private static readonly PAUSE_RESUME_MAX_RETAINED_S = 15;
  /**
   * Set to true when a VAD ``speech_start`` was suppressed by the
   * anti-echo gate during the current agent turn.  Cleared on
   * ``beginSpeaking`` and ``cancelSpeaking``.  When the turn ends
   * naturally (grace timer), the inbound audio ring is flushed to STT
   * so the user's speech is not silently discarded.
   */
  private suppressedSpeechPending = false;
  // ---- Semantic turn detection (opt-in via ``agent.turnDetector``) ----
  // When a detector is configured, a VAD ``speech_end`` no longer
  // finalizes STT immediately: the detector scores the rolling window
  // below and the finalize is deferred (held) while it predicts
  // "incomplete", bounded by ``agent.maxSemanticHoldMs``. With the
  // default ``turnDetector`` unset every field below is dormant and the
  // speech_end path is byte-identical to previous releases. Parity with
  // Python ``_semantic_*`` state on ``PipelineStreamHandler``.
  /** Rolling window byte budget: the last 8 s of PCM16 @ 16 kHz. */
  private static readonly SEMANTIC_WINDOW_MAX_BYTES = 16000 * 2 * 8;
  /** Re-score cadence while holding: one prediction per this much silence. */
  private static readonly SEMANTIC_POLL_MS = 200;
  /** Rolling buffer of post-decode PCM16-16k frames (bounded to 8 s). */
  private semanticAudioRing: Buffer[] = [];
  private semanticAudioRingBytes = 0;
  /** True while a sub-threshold prediction is holding the finalize open. */
  private semanticHoldActive = false;
  /** Wall-clock (ms) deadline for the hard cap, null when idle. */
  private semanticHoldDeadlineMs: number | null = null;
  /** Invalidates the backstop timer once its hold has been resolved. */
  private semanticHoldGeneration = 0;
  /** Wall-clock backstop — finalizes at the cap even if audio stalls. */
  private semanticHoldTimer: ReturnType<typeof setTimeout> | null = null;
  /** Bytes accumulated since the last prediction while holding. */
  private semanticPollPendingBytes = 0;
  /**
   * Set on the FIRST detector failure: semantic endpointing is then
   * disabled for the remainder of the call (one clear warning, plain
   * VAD-silence behavior) instead of warning per turn against a
   * permanently broken model. Mirrors Python ``_semantic_detector_failed``
   * and the existing ``vadDisabled`` fail-once pattern.
   */
  private turnDetectorFailed = false;
  /**
   * EOU trigger for the NEXT committed turn. Stamped by the semantic
   * finalize paths, consumed (and reset) on transcript commit. Parity
   * with Python ``_last_eou_trigger``.
   */
  private lastEouTrigger: import('./_speech-events').EouTrigger = 'vad_silence';
  /** Hard cap (ms) a semantic hold may defer the finalize. */
  private readonly maxSemanticHoldMs: number = 1200;
  /**
   * Minimum wall-clock duration (ms) the agent must have been speaking
   * before barge-in is allowed to fire when AEC is active. Covers the
   * AEC warmup window (~500 ms) plus a safety margin so residual bleed
   * during the convergence period does not self-trigger barge-in.
   */
  private static readonly MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_AEC = 1000;
  /**
   * Same as the AEC variant but for deployments where AEC is OFF
   * (default on PSTN — Twilio/Telnyx). Without an adaptive filter to
   * converge, the only justification for a gate is anti-flicker on
   * micro-events (cough, click). Raised 100 → 500 ms on 2026-05-19
   * after the 0.6.2 acceptance run showed a phantom VAD speech_start
   * firing on the very first inbound frame (~500 ms into the call,
   * which is past a 100 ms gate). The phantom barge-in cancelled the
   * prewarmed firstMessage, the user heard a clipped (graffiante)
   * audio fragment, and the SDK left ``_turnAlreadyClosed=true`` so
   * subsequent ``recordTurnComplete`` calls were no-ops. 500 ms
   * filters those phantoms while still letting a real interruption
   * land within half a second of agent onset.
   */
  private static readonly MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_NO_AEC = 500;
  /** Handle for the pending grace-period timer, so it can be cleared on cleanup. */
  private graceTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * AbortController for the current LLM streaming consumption.  Aborted by
   * ``cancelSpeaking`` so the in-flight LLM stream stops generating tokens
   * we will never speak — saves provider cost and frees the connection
   * earlier.  Mirrors Python ``_llm_cancel_event``.
   */
  private llmAbort: AbortController | null = null;

  /**
   * Wall-clock timestamp of the most recent ``cancelSpeaking`` call, or
   * ``null`` if no cancel has fired since the call started. Used by
   * ``beginSpeaking`` to enforce a short post-cancel drain window so the
   * remote PSTN player finishes flushing the previous turn's in-flight
   * audio before the next TTS chunk lands on top of it. Without this,
   * the first sentence of a post-barge-in turn audibly overlaps with
   * the tail of the cancelled turn (~50-200 ms of doubled audio).
   */
  private lastCancelAt: number | null = null;
  /**
   * Promise queue tracking outstanding Twilio marks the SDK has sent but
   * not yet seen echoed back. Used by the firstMessage send loop to bound
   * the depth of audio queued at the carrier — without this the loop
   * pushes the entire TTS stream into Twilio's WebSocket in one burst,
   * and a sendClear issued mid-buffer races against several seconds of
   * already-queued media frames (BUG #128). The window depth is
   * ``FIRST_MESSAGE_MARK_WINDOW``; ``onMark`` drains entries as Twilio
   * confirms playback, ``cancelSpeaking`` resolves every pending entry so
   * any awaiter exits immediately. Telnyx never populates this queue
   * (Telnyx's media-stream protocol has no mark concept — the loop
   * falls back to time-based pacing on that carrier).
   */
  private pendingMarks: Array<{
    name: string;
    resolve: () => void;
    promise: Promise<void>;
  }> = [];
  /**
   * Monotonic counter for first-message mark names. Distinct from
   * ``chunkCount`` (which the Realtime path uses) so the two paths can
   * coexist without name collisions even when firstMessage finishes while
   * a Realtime turn is still streaming.
   */
  // firstMessageMarkCounter / FIRST_MESSAGE_MARK_WINDOW /
  // MARK_AWAIT_TIMEOUT_MS were retired with the move to the Twilio-FIFO-
  // trusts model (sendPacedFirstMessageBytes no longer emits marks).
  // Marks are still consumed via ``onMark`` for any adapter that wants
  // to round-trip one, but the firstMessage path no longer back-pressures
  // on them.
  /**
   * Minimum drain window (ms) between a ``cancelSpeaking`` and the next
   * ``beginSpeaking``. 150 ms covers a typical PSTN jitter buffer drain
   * + Twilio Media Stream clear propagation. Lower values risk audio
   * overlap on the first chunk; higher values increase the perceived
   * "agent ack" latency after a barge-in. 150 ms is the smallest value
   * that consistently eliminated the overlap during 0.6.0 acceptance.
   */
  private static readonly POST_CANCEL_DRAIN_MS = 150;

  /**
   * Mark the start of a TTS span. Use instead of setting isSpeaking
   * directly. Awaits the post-cancel drain window before flipping state
   * so the remote player has time to flush the cancelled turn's tail.
   */
  private async beginSpeaking(isFirstMessage = false): Promise<void> {
    if (this.lastCancelAt !== null) {
      const elapsed = Date.now() - this.lastCancelAt;
      const remaining = StreamHandler.POST_CANCEL_DRAIN_MS - elapsed;
      if (remaining > 0) {
        await new Promise<void>((r) => setTimeout(r, remaining));
      }
    }
    this.speakingGeneration++;
    // Speech-event: agent start edge (pipeline parity with realtime).
    await this.emitAgentSpeechStarted();
    this.isSpeaking = true;
    // A fresh turn is actively streaming — not in the post-TTS echo window.
    // Clear the tail-grace flag so a VAD speech_start during this turn is
    // treated as a real barge-in (not a new-turn rescue).
    this.tailGraceActive = false;
    this.speakingStartedAt = Date.now();
    this.suppressedSpeechPending = false;
    // Stamp ``firstAudioSentAt`` synchronously for EVERY turn so the
    // ``canBargeIn()`` gate (250ms anti-flicker for PSTN no-AEC) runs in
    // PARALLEL with LLM TTFT + TTS TTFB rather than starting only after
    // the first audio chunk reaches the wire. Without this, a turn with
    // a slow LLM (gpt-4o cold cache ~2 s) is effectively un-interruptible
    // for the entire LLM window: ``firstAudioSentAt`` stays null, so
    // ``canBargeIn`` returns false and every VAD ``speech_start`` is
    // suppressed silently. Previously this fix was firstMessage-only;
    // promoted to default on 2026-05-11 after the user reported
    // "barge-in non funziona più" with gpt-4o.
    //
    // Note: the ``isFirstMessage`` parameter is kept for backward
    // compatibility with the call site, but no longer changes behaviour.
    void isFirstMessage;
    this.firstAudioSentAt = Date.now();
    // Fresh turn — drop any stale pre-barge-in buffer from a previous turn
    // so we never replay yesterday's audio to STT.
    this.inboundAudioRing = [];
    // Fresh turn — reset the echo-guard reference so barge-in checks compare
    // against THIS turn's spoken text, not the last turn's.
    this.currentAgentSpokenText = '';
    // Fresh turn — reset the heard-prefix playback timeline.
    this.turnPlaybackTotalMs = 0;
    this.turnSpokenSegments = [];
    // Fresh turn — drop any pause-and-resume state and retained audio from
    // the previous turn (a paused turn can never reach here — the
    // pause-decision wait resolves before the turn ends — but be
    // defensive) and re-enable retention after an overflow.
    this.discardPauseState();
    this.pauseResumeOverflowed = false;
    // False until the turn body finishes pushing audio — see
    // ``resumeAfterFalseInterruption``.
    this.turnOutputDone = false;
    // Reset the VAD detector so the next user utterance triggers a clean
    // SILENCE→SPEECH transition. Without this, PSTN echo from the previous
    // turn can keep the detector's smoothed probability above the
    // deactivation threshold (0.35) for the entire turn — the VAD never
    // returns to SILENCE, ``speech_start`` never fires for the user's next
    // utterance, and barge-in feels "one-shot" (works once, then never
    // again). The user's previous utterance was already committed by STT
    // before ``beginSpeaking`` is called, so resetting state here cannot
    // lose data.
    this.resetVad();
  }

  /**
   * Record that the first TTS audio chunk of the current turn has hit the
   * carrier wire. Idempotent within a turn — only the first call sets the
   * timestamp; later chunks are no-ops. Must be invoked AFTER the underlying
   * ``bridge.sendAudio`` resolves so the gate is anchored to "audio actually
   * went out", not "we asked the carrier to send it".
   */
  private markFirstAudioSent(): void {
    if (this.firstAudioSentAt === null) {
      this.firstAudioSentAt = Date.now();
    }
  }

  /**
   * Advance ``playbackBufferedUntil`` by the playout duration of an outbound
   * TTS chunk. ``numBytes`` is the size of the chunk BEFORE carrier encoding
   * (the same buffer handed to ``encodePipelineAudio``): PCM16 @ 16 kHz in
   * the default path (32 bytes/ms), or the carrier's native μ-law @ 8 kHz
   * (8 bytes/ms) when the TTS adapter emits wire format directly
   * (``ttsOutputFormatNativeForCarrier`` — Twilio/Plivo ``ulaw_8000``;
   * Telnyx native is ``pcm_16000`` so it stays at 32 bytes/ms).
   */
  private trackOutboundPlayback(numBytes: number): void {
    if (numBytes <= 0) return;
    const bytesPerMs =
      this.ttsOutputFormatNativeForCarrier &&
      this.deps.bridge.telephonyProvider !== 'telnyx'
        ? 8
        : 32;
    const now = Date.now();
    const chunkMs = numBytes / bytesPerMs;
    const base =
      this.playbackBufferedUntil > now ? this.playbackBufferedUntil : now;
    this.playbackBufferedUntil = base + chunkMs;
    // Per-turn playout total — the time axis for the heard-prefix estimate
    // (see ``heardResponsePrefix``). Reset at ``beginSpeaking``.
    this.turnPlaybackTotalMs += chunkMs;
  }

  /**
   * Estimate the response prefix the caller actually HEARD this turn.
   *
   * The pipeline pushes audio faster than realtime, so at barge-in time
   * ``heard = totalPushed - carrierBacklog`` ms of audio have actually
   * played. Mapped at sentence granularity against ``turnSpokenSegments``:
   * a sentence counts as heard once its playback has STARTED
   * (``startMs <= heardMs``), so the sentence playing at the moment of
   * interruption is included.
   *
   * Returns ``null`` when no segments were tracked this turn (nothing
   * synthesized through the tracked path — callers fall back to the legacy
   * full-text behaviour). Mirrors Python ``_heard_response_prefix``.
   */
  private heardResponsePrefix(): { text: string; heardEverything: boolean } | null {
    if (this.turnSpokenSegments.length === 0) return null;
    const remainingMs = Math.max(0, this.playbackBufferedUntil - Date.now());
    const heardMs = Math.max(0, this.turnPlaybackTotalMs - remainingMs);
    const heard = this.turnSpokenSegments.filter((s) => s.startMs <= heardMs);
    return {
      text: heard.map((s) => s.text).join(' '),
      heardEverything: heard.length === this.turnSpokenSegments.length,
    };
  }

  /**
   * Replace the text of the most recent assistant entry in the conversation
   * history. No-op when the last entry is not an assistant turn (e.g. the
   * caller's next turn was already committed).
   */
  private rewriteLastAssistantEntry(text: string): void {
    const entries = this.history.entries;
    const last = entries[entries.length - 1];
    if (last && last.role === 'assistant') {
      entries[entries.length - 1] = { ...last, text };
    }
  }

  /**
   * Heard-prefix semantics for a barge-in that lands AFTER
   * the turn completed, while the carrier is still playing the buffered
   * tail.
   *
   * The completed turn already recorded its FULL reply in history, but the
   * caller only heard part of it before interrupting — a stateful agent
   * runtime (Hermes / OpenClaw) would otherwise "remember saying" things
   * the caller never heard. Rewrites the last assistant entry to the heard
   * prefix + ``[interrupted by caller]``.
   *
   * MUST run BEFORE ``cancelSpeaking`` resets ``playbackBufferedUntil``
   * (the backlog is the heard-prefix input). No-op when a turn is still in
   * flight (the streaming path applies its own marker), when there is no
   * backlog, or when everything was already heard. Mirrors Python
   * ``_maybe_truncate_completed_turn_history``.
   */
  private maybeTruncateCompletedTurnHistory(): void {
    if (this.dispatchTask !== null) return; // turn still in flight
    const remainingMs = this.playbackBufferedUntil - Date.now();
    // Pause-and-resume froze the playback bookkeeping at pause time
    // (cursor snapped to 0, total rewound to the heard offset), so a kill
    // while paused has no live backlog — the frozen heard prefix below is
    // still the right input for the rewrite.
    if (remainingMs <= 0 && !this.outputPaused) return;
    const heard = this.heardResponsePrefix();
    if (heard === null || heard.heardEverything) return;
    this.rewriteLastAssistantEntry(
      heard.text ? `${heard.text} [interrupted by caller]` : '[interrupted by caller]',
    );
  }

  /**
   * Atomically end speaking AND invalidate any pending grace timer.
   * Use instead of ``this.isSpeaking = false`` at barge-in sites.
   *
   * Also aborts the in-flight LLM stream (if any) so the provider stops
   * billing tokens we will never speak.
   */
  private cancelSpeaking(): void {
    this.speakingGeneration++; // invalidates pending grace timers
    this.isSpeaking = false;
    this.tailGraceActive = false;
    this.speakingStartedAt = null;
    this.firstAudioSentAt = null;
    this.lastCancelAt = Date.now();
    this.suppressedSpeechPending = false;
    // The barge-in paths that call this also ``sendClear`` the carrier —
    // whatever audio was buffered ahead is dropped, so the playback cursor
    // snaps back to "nothing pending".
    this.playbackBufferedUntil = 0;
    // Drain any firstMessage mark waiters so a loop blocked on
    // ``waitForMarkWindow`` exits on the next tick and observes
    // ``!isSpeaking``. Without this the loop would stay blocked until
    // each mark either echoes (carrier still draining its queue) or
    // hits ``MARK_AWAIT_TIMEOUT_MS`` — keeping the agent "speaking"
    // from the user's perspective for hundreds of extra ms after
    // barge-in.
    this.drainPendingMarks();
    if (this.llmAbort !== null) {
      try {
        this.llmAbort.abort();
      } catch {
        // No-op — abort() throws nothing in modern runtimes, but be defensive.
      }
    }
    // Force-close any in-flight TTS streaming socket. Without this, the
    // firstMessage live ``synthesizeStream`` path (used when the prewarm
    // accumulator hadn't completed before pickup) would block on its
    // inner ``await Promise<frame>`` for 30 s — ``initPipeline`` would
    // never return, the STT ``onTranscript`` callback would never
    // register, and every subsequent user turn would be silently
    // dropped. Provider-duck-typed: adapters that don't expose
    // ``cancelActiveStream`` are no-ops here.
    const ttsCancelable = this.tts as
      | { cancelActiveStream?: () => void }
      | undefined;
    if (typeof ttsCancelable?.cancelActiveStream === 'function') {
      try {
        ttsCancelable.cancelActiveStream();
      } catch (err) {
        getLogger().debug(`TTS cancelActiveStream raised: ${String(err)}`);
      }
    }
  }

  /**
   * Resolve every entry in ``pendingMarks`` and empty the queue. Idempotent
   * — safe to call from ``cancelSpeaking`` and again from the grace path
   * without leaking pending promises.
   */
  private drainPendingMarks(): void {
    if (this.pendingMarks.length === 0) return;
    for (const entry of this.pendingMarks) {
      try {
        entry.resolve();
      } catch {
        // No-op — pending entries always own a fresh resolve fn.
      }
    }
    this.pendingMarks.length = 0;
  }

  // Mark-based back-pressure (sendMarkAwaitable / waitForMarkWindow)
  // was removed when sendPacedFirstMessageBytes switched to the
  // Twilio-FIFO-trusts model — see that method's doc comment for
  // rationale. ``pendingMarks`` and ``onMark`` are still kept so an
  // adapter that wants to round-trip a mark for some other purpose can
  // still do so without breaking the firstMessage path.

  /**
   * Bytes-per-millisecond for a 16 kHz PCM16 mono stream. Used by
   * ``sendPacedFirstMessageBytes`` to translate chunk size into a
   * playout-duration sleep so we never deliver faster than the carrier
   * can decode + play out (which manifested as severe crackling on the
   * HTTP-TTS path with client-side resampling). 16000 samples/sec × 2
   * bytes/sample = 32 bytes/ms.
   */
  private static readonly PCM16_16K_BYTES_PER_MS = 32;

  /** Cancel and clear the pending grace timer, if any. */
  private clearGraceTimer(): void {
    if (this.graceTimer !== null) {
      clearTimeout(this.graceTimer);
      this.graceTimer = null;
    }
  }

  /**
   * Mark the agent as no longer producing TTS, honoring a grace period that
   * approximates the carrier's playback buffer. The user may still hear the
   * agent for ~1 s after we finish pushing audio (Twilio buffers ~1500 ms);
   * keeping isSpeaking=true through that window keeps the VAD-driven
   * barge-in armed during the audible tail. Tunable via env.
   */
  private endSpeakingWithGrace(): void {
    // Speech-event: agent stop edge (clean turn end). Fire-and-forget —
    // this method is synchronous by design.
    void this.emitAgentSpeechEnded(false).catch(() => {});
    // The turn body has finished pushing audio — from here on, a
    // pause-resume cycle owns re-arming the grace machinery (see
    // ``resumeAfterFalseInterruption``).
    this.turnOutputDone = true;
    const rawGrace = process.env.PATTER_TTS_TAIL_GRACE_MS;
    const parsedGrace = rawGrace !== undefined ? Number(rawGrace) : NaN;
    const grace = (rawGrace !== undefined && Number.isFinite(parsedGrace))
      ? parsedGrace
      : 1500;
    if (rawGrace !== undefined && !Number.isFinite(parsedGrace)) {
      getLogger().warn(
        `PATTER_TTS_TAIL_GRACE_MS="${rawGrace}" is not a valid number — using default 1500ms`,
      );
    }
    // NOTE: we DO NOT flush ``inboundAudioRing`` here — the ring is only
    // drained on a real barge-in (where VAD confirmed user speech). Flushing
    // on every natural turn end was tried in an earlier iteration and
    // caused garbled out-of-order responses: the ring captured during the
    // agent's TTS contains audio with partially-cancelled echo and possibly
    // over-cancelled user voice (Geigel rho=0.6 misses quiet double-talk).
    // Replaying that to STT on every turn produced phantom transcripts that
    // raced live STT input and confused the LLM. Audio captured during the
    // agent's turn that VAD did NOT classify as speech is intentionally
    // dropped at the next ``beginSpeaking()``.
    if (grace > 0) {
      const gen = this.speakingGeneration;
      this.clearGraceTimer();
      const startTailGrace = (): void => {
        // The carrier has (estimatedly) finished playing everything we
        // pushed; ``isSpeaking`` is now held only to suppress the fading
        // echo tail. Mark the tail-grace window so fast next-turn speech is
        // rescued as a new turn rather than mis-detected as a barge-in.
        this.tailGraceActive = true;
        this.graceTimer = setTimeout(() => {
          this.graceTimer = null;
          if (this.speakingGeneration === gen) {
            this.isSpeaking = false;
            this.tailGraceActive = false;
            this.speakingStartedAt = null;
            this.firstAudioSentAt = null;
            this.clearPendingBargeIn();
            // Hygiene: a turn that ended while paused (only reachable via
            // the LLM-error path — normal turns wait out the pause
            // decision) must not leak its pause buffers into idle time.
            this.discardPauseState();
            void this.resetBargeInStrategies();
            // If VAD detected speech during the agent's turn but it was
            // gate-suppressed (agent hadn't been speaking long enough for
            // barge-in to fire), flush the ring buffer to STT now so the
            // user's words aren't silently lost.
            if (this.suppressedSpeechPending) {
              this.suppressedSpeechPending = false;
              this.flushInboundAudioRing();
            }
            // Reset VAD so any stuck SPEECH state from echo / loopback during
            // the agent's turn does not block the next user utterance from
            // emitting ``speech_start``.
            this.resetVad();
          }
        }, grace);
      };
      // Phase 1 — the carrier is still PLAYING audio we already pushed.
      // Agent-runtime LLMs (Hermes/OpenClaw) deliver the whole reply at
      // once, TTS outruns realtime, and the carrier buffers tens of seconds
      // of audio that keeps playing long after this method runs. For that
      // whole audible window the agent IS still speaking from the caller's
      // perspective: keep ``isSpeaking=true`` with ``tailGraceActive=false``
      // so VAD/transcript barge-in takes the cancel path (``sendClear``
      // drops the carrier buffer) instead of the next-turn rescue — without
      // this, "the agent detects the interruption but keeps talking".
      // A barge-in meanwhile bumps ``speakingGeneration`` (cancelSpeaking),
      // which no-ops this timer. Phase 2 — the existing echo-tail grace.
      const bufferedMs = Math.max(0, this.playbackBufferedUntil - Date.now());
      if (bufferedMs <= 0) {
        startTailGrace();
      } else {
        this.graceTimer = setTimeout(() => {
          this.graceTimer = null;
          if (this.speakingGeneration === gen) startTailGrace();
        }, bufferedMs);
      }
    } else {
      this.isSpeaking = false;
      this.tailGraceActive = false;
      this.speakingStartedAt = null;
      this.firstAudioSentAt = null;
      this.clearPendingBargeIn();
      // See the grace-flip branch — drop any pause state a turn that
      // errored mid-pause left behind.
      this.discardPauseState();
      void this.resetBargeInStrategies();
      if (this.suppressedSpeechPending) {
        this.suppressedSpeechPending = false;
        this.flushInboundAudioRing();
      }
      this.resetVad();
    }
  }

  /**
   * End the post-TTS tail-grace window because the user has begun their next
   * turn. Unlike a barge-in, the agent's response already played out in full
   * — there is nothing to cancel and no turn was interrupted. We flip the
   * speaking flag off (bumping ``speakingGeneration`` so the scheduled grace
   * timer no-ops), recover any leading audio the self-hearing guard captured
   * into the ring (the user's first ~250 ms, which VAD needed before it could
   * emit ``speech_start``), and let the live STT stream take over. We do NOT
   * call ``sendClear``, ``recordBargeinDetected`` or ``recordTurnInterrupted``
   * — none apply to a turn that completed normally.
   *
   * Without this, fast next-turn speech (humans reply in 200-700 ms, well
   * inside the 1500 ms default grace) is withheld from STT and recorded as an
   * empty ``[interrupted]`` turn, after which the agent goes silent for the
   * rest of the call. Parity with Python ``_end_tail_grace_for_new_turn``.
   */
  private endTailGraceForNewTurn(): void {
    this.isSpeaking = false;
    this.tailGraceActive = false;
    this.speakingStartedAt = null;
    this.firstAudioSentAt = null;
    // Tail grace only starts after the playback cursor drained (phase 1 of
    // ``endSpeakingWithGrace``), so there is no carrier backlog left here.
    this.playbackBufferedUntil = 0;
    this.speakingGeneration++; // invalidates the pending grace timer
    this.clearGraceTimer();
    this.clearPendingBargeIn();
    // The next turn owns the floor — any stale pause state is void.
    this.discardPauseState();
    void this.resetBargeInStrategies();
    // Recover the user's leading words. Same rationale as the barge-in flush
    // — but here it is the only audio recovery, since the agent already
    // stopped and no new TTS will overwrite it.
    this.suppressedSpeechPending = false;
    this.flushInboundAudioRing();
  }

  private async resetBargeInStrategies(): Promise<void> {
    if (this.bargeInStrategies.length === 0) return;
    const { resetStrategies } = await import('./services/barge-in-strategies.js');
    await resetStrategies(this.bargeInStrategies);
  }

  /**
   * Reset the active VAD provider's per-utterance state. No-op when the
   * provider does not implement the optional ``reset()`` hook. Safe to call
   * from any context — failures are swallowed and the VAD is disabled for
   * the rest of the call so a flaky reset can never silently kill barge-in
   * for every subsequent turn.
   */
  private resetVad(): void {
    const activeVad = this.deps.agent.vad ?? this.autoVad;
    if (!activeVad || this.inputChain.isVadDisabled()) return;
    try {
      const ret = activeVad.reset?.();
      if (ret instanceof Promise) {
        ret.catch((err) => {
          getLogger().debug(`VAD reset threw: ${String(err)}`);
        });
      }
    } catch (err) {
      getLogger().debug(`VAD reset threw: ${String(err)}`);
    }
  }

  /**
   * Whether barge-in is allowed to fire right now. Gate length depends
   * on whether AEC is active: 1 s with AEC (covers filter warmup),
   * 250 ms without (anti-flicker only — keeps PSTN barge-in responsive).
   */
  private canBargeIn(): boolean {
    if (this.speakingStartedAt === null) return true;
    // Anchor the gate on "first audio actually emitted", not on
    // ``beginSpeaking`` (which fires before the TTS provider's first-byte
    // latency has elapsed). Without this guard, background noise picked up
    // by VAD ~250 ms after ``beginSpeaking`` triggers a self-cancel BEFORE
    // any TTS chunk has reached the wire — the agent's first turn becomes
    // silence even though the SDK believes it spoke.
    if (this.firstAudioSentAt === null) return false;
    const elapsed = Date.now() - this.firstAudioSentAt;
    const gate = this.aec
      ? StreamHandler.MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_AEC
      : StreamHandler.MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_NO_AEC;
    return elapsed >= gate;
  }

  /**
   * Replay the audio captured by the self-hearing guard right before a
   * confirmed barge-in. VAD's ``minSpeechDuration`` window (default
   * 250 ms) means ``speech_start`` fires only AFTER the user has been
   * talking for that long; without this replay STT sees only the tail
   * of the user's interruption and produces "the line is breaking up"
   * partial transcripts. We deliberately do NOT call this on natural
   * turn end — see the comment in ``endSpeakingWithGrace`` for why.
   */
  private flushInboundAudioRing(): void {
    if (!this.stt || this.inboundAudioRing.length === 0) return;
    const replayed = this.inboundAudioRing.length;
    for (const buf of this.inboundAudioRing) {
      try {
        this.stt.sendAudio(buf);
      } catch (err) {
        getLogger().debug(`sendAudio replay failed: ${String(err)}`);
      }
    }
    this.inboundAudioRing = [];
    getLogger().debug(
      `Flushed ${replayed} pre-barge-in frame(s) (~${replayed * 20} ms) to STT`,
    );
  }
  /**
   * Per-call resolved tool list. Starts as ``null`` (falls back to
   * ``deps.agent.tools``). Populated by ``initMcpTools`` when MCP servers
   * are configured so discovered tools are merged in without mutating the
   * shared ``AgentOptions`` object. Code that needs the effective tool list
   * should read ``this.resolvedTools ?? this.deps.agent.tools``.
   */
  private resolvedTools: ToolDefinition[] | null = null;
  /**
   * Per-call effective agent configuration. Starts as ``deps.agent`` and is
   * REPLACED (never mutated — ``AgentOptions`` is shared and readonly) by a
   * multi-agent ``handoff_to`` so the rest of the call runs with the target
   * agent's LLM-visible config (system prompt, tools, variables, guardrails,
   * text transforms, onward handoffs). Parity with the Python handler's
   * ``self.agent`` swap.
   */
  private currentAgent: AgentOptions;
  private llmLoop: LLMLoop | null = null;
  /**
   * Per-call tool executor — provides retry-with-exponential-backoff and a
   * per-tool circuit breaker for Realtime function calls. Pipeline mode
   * uses its own executor inside ``LLMLoop``; this one is dedicated to
   * the Realtime path so a flaky downstream (DB outage, vendor rate
   * limit) returns a structured ``{ error, fallback: true }`` instead of
   * hanging the model on retries that will keep failing.
   */
  private readonly toolExecutor = new DefaultToolExecutor();
  /**
   * MCP server connection manager — populated lazily in
   * ``initMcpTools()`` when the agent declares ``mcpServers``. Holds
   * the open MCP client connections for the lifetime of the call so
   * we can dispatch ``tools/call`` without re-handshaking on every
   * function invocation. Cleared in ``fireCallEnd``.
   */
  private mcpManager: MCPManager | null = null;
  private chunkCount = 0;
  private callEndFired = false;
  private sttClosed = false;
  private currentAgentText = '';
  private responseAudioStarted = false;
  /**
   * Realtime turn ordering buffer. OpenAI Realtime emits
   * `input_audio_transcription.completed` (user transcript) AFTER
   * `response.done` (assistant complete) because Whisper transcription
   * runs in parallel with — and slower than — model response. Without
   * this buffer the pushed `history` order is [assistant, user, ...]
   * which renders out-of-order in the dashboard.
   *
   * Behaviour:
   *  - `onAdapterSpeechStopped` flips `userTranscriptPending = true`
   *  - `onAdapterResponseDone` checks the flag; if set, stashes the
   *    assistant text + a fallback timer
   *  - `onAdapterTranscriptInput` clears the flag, pushes user, then
   *    flushes any pending assistant turn
   *  - The fallback timer flushes the assistant alone if the user
   *    transcript never arrives (silence misclassified as speech, etc.)
   */
  private userTranscriptPending = false;
  private pendingAssistantTurn: string | null = null;
  private pendingAssistantTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * Reserved monotonic turn index for the in-flight Realtime turn (issue
   * #154, fix 5/6). Reserved in ``onAdapterSpeechStopped`` via
   * ``metricsAcc.reserveTurnIndex()`` the moment the turn OPENS, then threaded
   * through to the live per-line transcript events (``recordTranscriptLine``)
   * and into ``recordTurnComplete`` / ``recordTurnInterrupted`` so the
   * dashboard can sort a late-arriving user line ABOVE its agent line by
   * ``(turnIndex, role)``. ``null`` until the first turn opens. Parity with
   * Python ``_current_turn_index``.
   */
  private currentTurnIndex: number | null = null;
  /**
   * Hard cap on how long we wait for the user transcript before flushing
   * the buffered assistant turn alone. 3 s covers OpenAI Whisper's typical
   * 200-800 ms post-response delay with substantial headroom for slow
   * cellular audio uploads. Beyond this we accept the order will look
   * "assistant-only" rather than block the call's transcript display.
   */
  private static readonly REALTIME_USER_TRANSCRIPT_WAIT_MS = 3000;
  private maxDurationTimer: ReturnType<typeof setTimeout> | null = null;
  private transcriptProcessing = false;
  private transcriptQueue: STTTranscript[] = [];
  /**
   * The in-flight turn dispatch (LLM + TTS) runs as a SINGLE tracked promise
   * so the transcript drain loop keeps running ``handleBargeIn`` against the
   * LIVE turn during a long (30-90 s) agent-runtime response, instead of
   * head-of-line-blocking on it. Exactly one is in flight: the launcher awaits
   * the previous one to settle (fast — a barge-in already aborted it) before
   * starting the next, preserving history/metrics ordering. Parity with
   * Python ``_dispatch_task``.
   */
  private dispatchTask: Promise<void> | null = null;
  /** Background greeting playback (see playFirstMessage). */
  private firstMessageTask: Promise<void> | null = null;
  /**
   * Cap (ms) on how long teardown waits for the backgrounded dispatch to
   * settle. JS promises are not cancellable, so a user-supplied ``onMessage``
   * (which receives no AbortSignal) parked on a hung external call could block
   * call cleanup indefinitely — `llmAbort.abort()` only unblocks the built-in
   * LLM/TTS paths. We bound the WAIT (Python hard-cancels the task instead).
   * 30 s matches the webhook ceiling.
   */
  private static readonly DISPATCH_SETTLE_TIMEOUT_MS = 30_000;
  /**
   * Opt-in (default OFF): forward inbound audio to STT even while the agent is
   * speaking, so the transcript barge-in path can receive a transcript on
   * echo-masked PSTN links where the VAD never fires. ECHO RISK without AEC.
   * Parity with Python ``_forward_stt_while_speaking``.
   */
  private readonly forwardSttWhileSpeaking = ['1', 'true', 'yes'].includes(
    (process.env.PATTER_FORWARD_STT_WHILE_SPEAKING ?? '').trim().toLowerCase(),
  );
  // Throttle state for back-to-back STT finals — see ``commitTranscript``.
  private lastCommitText = '';
  private lastCommitAt = 0;
  // --- PREEMPTIVE GENERATION (opt-in, built-in LLM loop only) ---
  // When enabled, a confident INTERIM transcript starts a speculative
  // LLM+TTS dispatch whose audio is HELD in memory; the final transcript's
  // commit either releases it (matching text — the already-generated audio
  // flushes immediately) or discards it and dispatches normally. See
  // ``noteInterimTranscript`` / ``tryReleaseSpeculation``. Parity with
  // Python ``_preemptive_enabled``.
  private readonly preemptiveEnabled: boolean;
  private readonly preemptiveMinStableMs: number;
  /** The single in-flight speculation (at most one). ``null`` when idle,
   * when discarded, or once released (a released speculation becomes the
   * live turn tracked by ``dispatchTask`` instead). */
  private speculation: SpeculativeTurn | null = null;
  // Interim-stability tracking: normalized text of the newest interim plus
  // the one-shot timer that starts a speculation once the text has been
  // unchanged for ``preemptiveMinStableMs``.
  private interimNorm = '';
  private interimText = '';
  private interimStableTimer: ReturnType<typeof setTimeout> | null = null;
  /** Hard cap (ms of playout) on TTS audio buffered by a speculative turn.
   * Overflow aborts the speculation. Parity with Python
   * ``_PREEMPTIVE_MAX_BUFFER_S``. */
  private static readonly PREEMPTIVE_MAX_BUFFER_MS = 15_000;
  /** The agent's spoken text for the CURRENT turn, accumulated as tokens stream.
   * The echo guard rejects transcripts matching it (the agent's own TTS bleeding
   * back into STT when audio is forwarded during TTS without effective AEC).
   * Reset in ``beginSpeaking``; only consulted while ``forwardSttWhileSpeaking``.
   * Parity with Python ``_current_agent_spoken_text``. */
  private currentAgentSpokenText = '';
  // PCM16 byte-alignment carry for TTS streaming (pipeline mode).
  // HTTP streams from ElevenLabs / OpenAI / Cartesia can yield chunks of any
  // size, including odd byte counts. Silently dropping the trailing odd byte
  // misaligns every subsequent int16 sample in the stream (hi/lo bytes get
  // swapped), producing a voice drowned in loud hiss. We buffer the odd byte
  // across chunks so resample/mulaw encoding always sees aligned int16 frames.
  private ttsByteCarry: Buffer | null = null;
  // Per-session stateful resamplers eliminate chunk-boundary discontinuities.
  // Created lazily on first use; reset() on call end.
  private readonly inboundResampler: StatefulResampler = createResampler8kTo16k();
  private readonly outboundResampler: StatefulResampler = createResampler16kTo8k();
  /**
   * Inbound audio processing chain: decode (mulaw→PCM16) → stateful 8k→16k
   * resample → AEC near-end → ``agent.audioFilter`` → VAD (slice 1 of the
   * pipeline-stages decomposition — docs/architecture/pipeline-stages.md).
   * Shares ``inboundResampler`` so ``flushResamplers`` keeps draining the
   * tail on call close; AEC / filter / VAD are late-bound getters because
   * ``initPipeline`` (and the unit suites) install ``aec`` / ``autoVad``
   * after construction. Owns the per-call VAD error kill switch that
   * previously lived here as ``vadDisabled``.
   */
  private readonly inputChain: InputProcessingChain = new InputProcessingChain({
    resampler: this.inboundResampler,
    getAec: () => this.aec,
    getAudioFilter: () => this.deps.agent.audioFilter,
    getVad: () => this.deps.agent.vad ?? this.autoVad,
  });

  private readonly history: ReturnType<typeof createHistoryManager>;
  private readonly metricsAcc: CallMetricsAccumulator;
  private readonly _eventBus: EventBus;

  constructor(deps: StreamHandlerDeps, ws: WSWebSocket, caller: string, callee: string) {
    this.deps = deps;
    this.ws = ws;
    this.caller = caller;
    this.callee = callee;
    this.currentAgent = deps.agent;

    if (this.forwardSttWhileSpeaking) {
      getLogger().warn(
        'PATTER_FORWARD_STT_WHILE_SPEAKING=on: inbound audio is sent to STT ' +
          'during TTS so transcript barge-in works on echo-masked links. ' +
          "Without AEC the agent's own voice may be transcribed as a phantom " +
          'interruption — pair with agent.bargeInStrategies.',
      );
    }

    this.bargeInStrategies = (deps.agent.bargeInStrategies ?? []).slice();
    const confirmMs = deps.agent.bargeInConfirmMs;
    this.bargeInConfirmMs =
      typeof confirmMs === 'number' && Number.isFinite(confirmMs) && confirmMs > 0
        ? confirmMs
        : 1500;
    const mode = deps.agent.bargeInMode ?? 'cancel';
    if (mode !== 'cancel' && mode !== 'pause_resume') {
      getLogger().warn(`Unknown bargeInMode ${String(mode)} — falling back to 'cancel'`);
    }
    this.bargeInMode = mode === 'pause_resume' ? 'pause_resume' : 'cancel';
    this.preemptiveEnabled = deps.agent.preemptiveGeneration ?? false;
    const stableMs = deps.agent.preemptiveMinStableMs;
    this.preemptiveMinStableMs =
      typeof stableMs === 'number' && Number.isFinite(stableMs) && stableMs >= 0
        ? stableMs
        : 300;

    // Semantic turn detection hard cap (only consulted when
    // ``agent.turnDetector`` is configured). Parity with Python
    // ``max_semantic_hold_ms`` (default 1200 ms).
    const holdMs = deps.agent.maxSemanticHoldMs;
    this.maxSemanticHoldMs =
      typeof holdMs === 'number' && Number.isFinite(holdMs) && holdMs >= 0
        ? holdMs
        : 1200;

    this.history = createHistoryManager(200);

    // v0.5.0+: ``agent.stt`` / ``agent.tts`` are always STTAdapter / TTSAdapter
    // instances (or undefined). Provider classes expose a static
    // ``providerKey`` so we get a stable pricing/dashboard key (e.g. "deepgram")
    // instead of the alias class name "STT". Falls back to constructor.name
    // for any custom adapter that doesn't declare providerKey.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sttKey = (deps.agent.stt?.constructor as any)?.providerKey;
    const sttProviderName = deps.agent.stt
      ? (sttKey ?? deps.agent.stt.constructor?.name ?? 'custom')
      : undefined;
    // Adapter ``model`` field powers per-model rate resolution in
    // pricing.calculateSttCost. Empty string → provider default.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sttModelName = String(((deps.agent.stt as any)?.model ?? '') || '');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const ttsKey = (deps.agent.tts?.constructor as any)?.providerKey;
    const ttsProviderName = deps.agent.tts
      ? (ttsKey ?? deps.agent.tts.constructor?.name ?? 'custom')
      : undefined;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const ttsModelName = String(((deps.agent.tts as any)?.model ?? '') || '');
    const providerMode = deps.agent.provider ?? 'openai_realtime';
    // Realtime collapses STT+LLM+TTS into one model — capture it so the
    // token-based cost calc picks the right per-model rate (e.g. gpt-
    // realtime-2 vs gpt-realtime-mini). Use the agent's declared model
    // when set; fall back to the adapter default.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const realtimeModelName =
      providerMode === 'openai_realtime' || (providerMode as string) === 'openai_realtime_2'
        ? String(((deps.agent as any).model ?? '') || '') || 'gpt-realtime-mini'
        : '';
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const llmKey = (deps.agent.llm?.constructor as any)?.providerKey;
    let llmProviderName: string;
    if (deps.agent.llm) {
      if (llmKey) {
        llmProviderName = llmKey;
      } else {
        const stripped = (deps.agent.llm.constructor?.name ?? 'custom')
          .replace(/LLMProvider$/i, '')
          .replace(/LLM$/i, '')
          .replace(/Provider$/i, '')
          .toLowerCase();
        llmProviderName = stripped || 'custom';
      }
    } else {
      llmProviderName =
        providerMode === 'openai_realtime' || (providerMode as string) === 'openai_realtime_2'
          ? 'openai_realtime'
          : 'openai';
    }
    this.llmProviderTag = llmProviderName;

    this._eventBus = new EventBus();
    this.metricsAcc = new CallMetricsAccumulator({
      callId: '',
      providerMode,
      telephonyProvider: deps.bridge.telephonyProvider,
      sttProvider: sttProviderName,
      ttsProvider: ttsProviderName,
      llmProvider: llmProviderName,
      sttModel: sttModelName,
      ttsModel: ttsModelName,
      realtimeModel: realtimeModelName,
      pricing: deps.pricing,
      eventBus: this._eventBus,
      reportOnlyInitialTtfb: deps.reportOnlyInitialTtfb ?? false,
    });

    getLogger().debug(`WebSocket connection opened (${deps.bridge.label})`);
  }

  /**
   * Record a completed turn in the dashboard store and fire the user-supplied
   * ``onMetrics`` callback. Centralises the 4 emit sites (firstMessage, pipeline
   * streaming/regular LLM, WebSocket remote, Realtime response_done) so the
   * payload shape lives in one place.
   */
  /**
   * Emit a live per-line transcript event to the dashboard store (issue #154,
   * fix 5). Routed through a single helper so the call shape lives in one
   * place. ``recordTranscriptLine`` appends the line to the active call's
   * transcript and publishes a ``transcript_line`` SSE event; the dashboard
   * sorts by (turnIndex, user<assistant) so a late user line lands above its
   * agent line. No-op when no turn index has been reserved yet.
   */
  private emitTranscriptLine(role: 'user' | 'assistant', text: string): void {
    if (this.currentTurnIndex === null) return;
    // ``recordTranscriptLine`` is the canonical dashboard-store API for live
    // per-line transcript events (added to ``MetricsStore`` for issue #154).
    // Narrow to the exact contract here so the call site documents the shape
    // the store must satisfy; the runtime call dispatches to the real method.
    (
      this.deps.metricsStore as unknown as {
        recordTranscriptLine: (data: {
          call_id: string;
          turnIndex: number;
          role: 'user' | 'assistant';
          text: string;
        }) => void;
      }
    ).recordTranscriptLine({
      call_id: this.callId,
      turnIndex: this.currentTurnIndex,
      role,
      text,
    });
  }

  private async emitTurnMetrics(turn: unknown): Promise<void> {
    if (turn == null) return;
    this.deps.metricsStore.recordTurn({ call_id: this.callId, turn });
    if (!this.deps.onMetrics) return;
    // Fix 7 (Python parity, stream_handler.py:312): expose llm_ttft_ms at the
    // top level of the metrics payload so consumers can read it without
    // diving into turn.latency. The nested turn.latency.llm_ttft_ms is kept
    // for backwards compatibility.
    const turnMetrics = turn as { latency?: { llm_ttft_ms?: number } } | null;
    const llm_ttft_ms = turnMetrics?.latency?.llm_ttft_ms;
    await this.deps.onMetrics({
      call_id: this.callId,
      turn,
      ...(llm_ttft_ms !== undefined ? { llm_ttft_ms } : {}),
      cost_so_far: this.metricsAcc.getCostSoFar(),
    });
  }

  /** Reset the TTS odd-byte carry — call at every TTS stream entry/exit. */
  private resetTtsCarry(): void {
    this.ttsByteCarry = null;
  }

  /**
   * Flush both stateful resamplers and any TTS byte carry on call close.
   * Emits tail bytes through the telephony bridge so the last ~20 ms of audio
   * is not silently clipped on hangup. No-op if the WebSocket is already gone.
   */
  private flushResamplers(): void {
    // Flush inbound resampler (caller audio → STT)
    try {
      const inTail = this.inboundResampler.flush();
      if (inTail.length > 0 && this.stt) {
        this.stt.sendAudio(inTail);
      }
    } catch { /* best effort */ }

    // Flush outbound resampler (TTS → telephony, pipeline mode only)
    try {
      const outTail = this.outboundResampler.flush();
      if (outTail.length > 0 && this.ws.readyState === this.ws.OPEN) {
        const mulaw = pcm16ToMulaw(outTail);
        this.deps.bridge.sendAudio(this.ws, mulaw.toString('base64'), this.streamSid);
      }
    } catch { /* best effort */ }

    // Flush any leftover TTS carry byte (rare: only when last chunk was odd-length)
    this.ttsByteCarry = null;
  }

  /**
   * Start call recording when configured. Bridges expose
   * ``startRecording`` for carrier parity (Twilio and Telnyx supported).
   */
  private async startRecordingIfRequested(callId: string): Promise<void> {
    const { recording, config } = this.deps;
    if (!recording || !config.twilioSid || !config.twilioToken || !callId) return;
    if (!validateTwilioSid(callId)) {
      getLogger().warn(`Recording skipped: invalid Twilio CallSid format ${JSON.stringify(callId)}`);
      return;
    }
    try {
      const recUrl = `https://api.twilio.com/2010-04-01/Accounts/${config.twilioSid}/Calls/${callId}/Recordings.json`;
      const recResp = await fetch(recUrl, {
        method: 'POST',
        headers: {
          'Authorization': `Basic ${Buffer.from(`${config.twilioSid}:${config.twilioToken}`).toString('base64')}`,
        },
      });
      if (recResp.ok) {
        getLogger().debug(`Recording started for ${callId}`);
      } else {
        getLogger().warn(`could not start recording: ${await recResp.text()}`);
      }
    } catch (e) {
      getLogger().warn(`could not start recording: ${String(e)}`);
    }
  }

  // ---------------------------------------------------------------------------
  // Public: observer API
  // ---------------------------------------------------------------------------

  /**
   * Subscribe to a Patter event on the per-call EventBus.
   *
   * The most common use-case is 'metrics_collected' — fired after every
   * completed turn with the TurnMetrics payload.
   *
   * Returns an unsubscribe function; call it to stop receiving events.
   *
   * @example
   * const off = handler.addObserver((payload) => {
   *   console.log('turn metrics:', payload);
   * });
   * // later:
   * off();
   */
  addObserver<T = unknown>(
    cb: (payload: T) => void | Promise<void>,
    event: PatterEventType = 'metrics_collected',
  ): () => void {
    return this._eventBus.on<T>(event, cb);
  }

  // ---------------------------------------------------------------------------
  // Public: called by the provider-specific parsers in server.ts
  // ---------------------------------------------------------------------------

  /**
   * Handle the call-start event.
   *
   * @param callId       Call SID (Twilio) or call_control_id (Telnyx)
   * @param customParams TwiML custom parameters (Twilio only, empty for Telnyx)
   */
  /** Initialize per-call state, build the AI adapter, and dispatch the `onCallStart` callback. */
  async handleCallStart(callId: string, customParams: Record<string, string> = {}): Promise<void> {
    this.callId = callId;
    // metricsAcc.callId is readonly at the public type level but is INTERNAL
    // per-call state — the accumulator is always owned by this handler
    // instance and callId is not known at construction time (it arrives with
    // the first telephony event). Cast to mutable to stamp it here.
    (this.metricsAcc as unknown as { callId: string }).callId = callId;

    // Prefer TwiML <Parameter> values over WebSocket query params (Twilio
    // strips query params from the Stream URL, so customParams is the only
    // reliable source for caller/callee).
    if (customParams.caller && !this.caller) this.caller = customParams.caller;
    if (customParams.callee && !this.callee) this.callee = customParams.callee;

    // Single INFO line per call-start — full context in one place.
    const mode =
      this.deps.agent.engine
        ? `engine=${(this.deps.agent.engine as { kind?: string }).kind ?? 'unknown'}`
        : 'pipeline';
    getLogger().info(
      `Call started: ${callId} (${this.deps.bridge.label}, ${mode}, ${maskPhoneNumber(this.caller || '?')} → ${maskPhoneNumber(this.callee || '?')})`,
    );

    if (Object.keys(customParams).length > 0) {
      getLogger().debug(`Custom params: ${sanitizeLogValue(JSON.stringify(customParams))}`);
    }

    // Don't force direction='inbound' here. If the call was placed via
    // phone.call() the store already has direction='outbound' from
    // recordCallInitiated(); the store falls back to 'inbound' when no
    // existing record is present (i.e. true inbound webhook).
    this.deps.metricsStore.recordCallStart({
      call_id: callId,
      caller: this.caller,
      callee: this.callee,
    });

    // Safety: auto-hangup after 1 hour to prevent runaway billing
    const MAX_CALL_DURATION_MS = 60 * 60 * 1000;
    this.maxDurationTimer = setTimeout(async () => {
      getLogger().warn(`Call ${callId} hit max duration (${MAX_CALL_DURATION_MS / 60000}min), terminating`);
      try { await this.deps.bridge.endCall(callId, this.ws); } catch { /* best effort */ }
    }, MAX_CALL_DURATION_MS);

    // Notify standalone dashboard so active calls appear immediately
    try {
      const { notifyDashboard } = await import('./dashboard/persistence');
      notifyDashboard({
        call_id: callId,
        caller: this.caller,
        callee: this.callee,
      });
    } catch { /* ignore */ }

    if (this.deps.onCallStart) {
      // Resolve direction from the store: if the call was placed via
      // phone.call() the store has direction='outbound', otherwise inbound.
      const direction =
        this.deps.metricsStore.getActive(callId)?.direction ?? 'inbound';
      const overrides = await this.deps.onCallStart({
        call_id: callId,
        caller: this.caller,
        callee: this.callee,
        direction,
        telephony_provider: this.deps.bridge.telephonyProvider,
        ...(Object.keys(customParams).length > 0 ? { custom_params: customParams } : {}),
      });
      // Dynamic per-call configuration: Python applied a returned dict via
      // apply_call_overrides since 0.5.x; TS typed the callback void and
      // silently ignored the result.
      if (overrides && typeof overrides === 'object') {
        this.applyCallOverrides(overrides as Record<string, unknown>);
      }
    }

    await this.startRecordingIfRequested(callId);

    // Carrier-neutral local recording — created BEFORE the adapter /
    // pipeline init so the firstMessage TTS is captured. Independent of the
    // carrier-side `recording` flag above (both can be on). The factory
    // returns null when `localRecording` is off or setup failed.
    if (this.deps.makeLocalRecorder) {
      try {
        this.localRecorder = this.deps.makeLocalRecorder(callId);
      } catch (e) {
        getLogger().warn(`Local recorder setup failed: ${String(e)}`);
      }
    }

    // Resolve dynamic variables in system prompt
    const agentVars = this.deps.sanitizeVariables(this.deps.agent.variables ?? {});
    const safeCustomParams = this.deps.sanitizeVariables(customParams);
    const allVars = { ...agentVars, ...safeCustomParams };
    const resolvedPrompt = Object.keys(allVars).length > 0
      ? this.deps.resolveVariables(this.deps.agent.systemPrompt, allVars)
      : this.deps.agent.systemPrompt;

    const provider = this.deps.agent.provider ?? 'openai_realtime';

    // Resolve MCP servers BEFORE the adapter is built so the discovered
    // tools are visible to the model in its first session.update (Realtime)
    // or first LLM call (pipeline). One handshake + ``tools/list`` per
    // server, ~50-200 ms total. Failures are logged but not fatal — a
    // dead MCP server should not kill the entire call.
    await this.initMcpTools();
    // Merge the built-in consult tool (if configured) into the per-call tool
    // list so it reaches both the Realtime adapter and the pipeline LLM loop.
    this.injectConsultTool();

    if (provider === 'pipeline') {
      await this.initPipeline(resolvedPrompt);
    } else {
      // Realtime modes: optionally prepend the "# Preambles" guidance block so
      // the model speaks a short action sentence before a slow tool call. A
      // falsy ``toolCallPreambles`` leaves the instructions byte-identical.
      // Pipeline mode has its own phone preamble and is intentionally skipped.
      await this.initRealtimeAdapter(
        applyToolCallPreambles(resolvedPrompt, this.deps.agent.toolCallPreambles),
      );
    }
  }

  /**
   * Connect to every configured MCP server, discover their tools via
   * ``tools/list``, and merge them into ``agent.tools`` before the
   * adapter is built. The synthetic handlers dispatch back through the
   * MCP client so ``DefaultToolExecutor`` can invoke them like any
   * other handler-tool. No-op when ``agent.mcpServers`` is empty or the
   * optional ``@modelcontextprotocol/sdk`` is not installed.
   */
  private async initMcpTools(): Promise<void> {
    const servers = this.deps.agent.mcpServers;
    if (!servers || servers.length === 0) return;
    this.mcpManager = new MCPManager(servers);
    let discovered: ToolDefinition[];
    try {
      discovered = await this.mcpManager.connect();
    } catch (e) {
      getLogger().error(`MCP connect failed (continuing without MCP tools): ${String(e)}`);
      this.mcpManager = null;
      return;
    }
    if (discovered.length === 0) return;
    MCPManager.assertNoConflicts(this.deps.agent.tools as ToolDefinition[] | undefined, discovered);
    // Merge into a per-call tool list. The shared ``deps.agent`` is
    // intentionally NOT mutated (readonly; shared across concurrent calls on
    // the same ``serve()`` instance — mutating it would race with other
    // calls' ``initMcpTools``). Store the merged list on the handler
    // instance so ``buildAIAdapter`` and ``LLMLoop`` constructors below see
    // the discovered tools via ``this.resolvedTools``.
    this.resolvedTools = [...(this.deps.agent.tools as ToolDefinition[] | undefined ?? []), ...discovered];
    getLogger().info(`MCP: merged ${discovered.length} tool(s) into agent`);
  }

  /**
   * Merge the built-in ``consult`` tool into the per-call tool list when
   * ``agent.consult`` is set, mirroring {@link initMcpTools}: the shared
   * ``deps.agent`` is NOT mutated; the merged list is stored on
   * ``this.resolvedTools`` so ``buildAIAdapter`` (Realtime) and the pipeline
   * ``LLMLoop`` both see it. Idempotent — a no-op if a tool with the same name
   * is already present.
   */
  private injectConsultTool(): void {
    const consult = this.deps.agent.consult;
    if (!consult) return;
    const consultTool = buildConsultTool(consult);
    const base = this.resolvedTools ?? ((this.deps.agent.tools as ToolDefinition[] | undefined) ?? []);
    if (base.some((t) => t.name === consultTool.name)) return;
    this.resolvedTools = [...base, consultTool];
  }

  /** Set the stream SID (Twilio only, called after parsing 'start' event). */
  /** Set the carrier-side stream id (Twilio `streamSid` / Telnyx stream identifier). */
  setStreamSid(sid: string): void {
    this.streamSid = sid;
  }

  /**
   * Record a terminal/processing error as a coarse, anonymous code on the call
   * metrics (code only, never the message). Surfaced via `call_completed`
   * telemetry. Safe to call with any value; last write wins.
   */
  recordError(err: unknown): void {
    try {
      this.metricsAcc.recordError(err);
    } catch {
      /* never let error-recording throw */
    }
  }

  /** Handle an incoming audio chunk (already decoded from base64). */
  /** Forward inbound audio bytes to the AI adapter and (in pipeline mode) the STT provider. */
  async handleAudio(audioBuffer: Buffer): Promise<void> {
    // Local-recording tap (caller side) — BEFORE every engine-mode branch
    // and guard below, so the caller channel has no gaps while STT / the
    // realtime adapter are still connecting or frames are dropped during
    // TTS. The wire codec comes from the bridge: μ-law 8 kHz carriers
    // (Twilio, Plivo, Telnyx-PCMU) say ``ulaw_8000``; the recorder decodes
    // to PCM16 16 kHz internally. Parity with the Python handlers'
    // ``on_audio_received`` taps.
    if (this.localRecorder) {
      this.localRecorder.addCallerAudio(
        audioBuffer,
        this.deps.bridge.inputWireFormat === 'pcm_16000' ? 'pcm16_16k' : 'mulaw_8k',
      );
    }
    const provider = this.deps.agent.provider ?? 'openai_realtime';
    if (provider === 'pipeline' && this.stt) {
      // Decode (mulaw 8 kHz → PCM16) → stateful 8k→16k resample → AEC
      // near-end → ``agent.audioFilter`` → VAD all live in the
      // ``InputProcessingChain`` (slice 1 of the pipeline-stages
      // decomposition — docs/architecture/pipeline-stages.md). The chain
      // returns the processed frame plus at most one VAD event; everything
      // downstream (VAD-event handling, self-hearing gate, ring buffer,
      // ``beforeSendToStt`` hook, STT feed) stays here for this slice.
      const frame = await this.inputChain.process(audioBuffer);
      const pcm16k = frame.pcm16k;

      // Semantic turn detection: keep the last ~8 s of post-decode PCM16
      // 16 kHz so the detector can score the caller's current turn on the
      // VAD speech_end edge. Zero cost when no ``agent.turnDetector`` is
      // configured (or after the detector failed and semantic endpointing
      // was disabled). Parity with Python ``_semantic_buffer_append``.
      if (this.deps.agent.turnDetector && !this.turnDetectorFailed) {
        this.semanticBufferAppend(pcm16k);
      }

      // External VAD (e.g. Silero) when configured. Drives:
      //  - Self-hearing avoidance: while the agent is speaking we DO NOT pipe
      //    audio to STT, so STT can't transcribe the agent's own TTS feeding
      //    back through the caller microphone.
      //  - Fast barge-in: VAD speech_start during TTS triggers an immediate
      //    interruption (no waiting for STT to emit a transcript).
      //  - Endpointing-free STT: no need to wait for Deepgram's silence
      //    timeout — we already know when the user is talking.
      if (frame.vadConfigured) {
        try {
          const evt = frame.vadEvent;
          if (evt) {
            // INFO-level log so the user can see VAD activity in the standard
            // server output without flipping debug logging.
            getLogger().info(
              `[VAD] ${evt.type}  agentSpeaking=${this.isSpeaking}`,
            );
          }
          if (evt?.type === 'speech_start') {
            // Speech-event: the seven-event public API never fired in
            // pipeline mode (only realtime emitted) — wire the user start
            // edge here. No-op without a dispatcher.
            await this.emitUserSpeechStarted();
            // The user resumed speaking — an active semantic hold (the
            // turn detector judged the previous pause mid-turn) is proven
            // right; drop it so the utterance keeps accumulating and the
            // next speech_end re-evaluates from scratch.
            if (this.deps.agent.turnDetector) {
              this.cancelSemanticHold();
            }
            // Tail-grace new-turn rescue: the agent already finished its turn
            // and we are only in the post-TTS echo-guard window. A VAD
            // speech_start here is the user's next turn, not a barge-in — end
            // the grace so this utterance flows to STT as a clean new turn
            // instead of being swallowed by the self-hearing guard or
            // mislabelled as an empty ``[interrupted]`` turn (the multi-turn
            // silence bug). After this ``isSpeaking`` is false, so the
            // if/else below is a no-op and the frame falls through to STT.
            // Parity with Python ``_end_tail_grace_for_new_turn``.
            if (this.isSpeaking && this.tailGraceActive) {
              this.endTailGraceForNewTurn();
            }
            const phantomSuppressed = this.isSpeaking && !this.canBargeIn();
            if (phantomSuppressed) {
              // Within the per-turn warmup gate. With AEC on this is the
              // ~1 s filter convergence window; without AEC it is just a
              // 100 ms anti-flicker margin. INFO so unexpected
              // suppressions are visible without enabling debug logs.
              //
              // CRITICAL: do NOT touch metrics state here. An earlier
              // bug (pre-0.6.1) called ``startTurnIfIdle()`` for every
              // ``speech_start`` including suppressed phantoms, which
              // stamped ``turnStart`` at echo/loopback time. The
              // legitimate user-speech ``speech_start`` that followed
              // then no-op'd (turn_start was already set), so the
              // dashboard reported ``user_speech_duration_ms`` of 5-7 s
              // even on short ~1 s utterances.
              getLogger().info(
                `[VAD] speech_start suppressed (agent speaking < gate, aec=${this.aec ? 'on' : 'off'})`,
              );
              // Mark that real user speech was detected but gated out.
              // The grace-timer callback will replay the ring buffer to
              // STT so the speech isn't silently discarded when the
              // agent finishes naturally without a barge-in.
              this.suppressedSpeechPending = true;
            } else if (this.isSpeaking && this.shouldPauseForBargeIn()) {
              // PAUSE-AND-RESUME (opt-in ``bargeInMode: 'pause_resume'``):
              // output pauses immediately — the carrier buffer is cleared
              // so the agent goes silent within one frame — but nothing is
              // cancelled. A committed final transcript within
              // ``bargeInConfirmMs`` kills the turn via ``handleBargeIn`` →
              // ``runBargeInCancel``; otherwise the resume timer replays
              // from the first not-fully-heard sentence. Takes precedence
              // over the deferCancel paths below — it is strictly safer
              // (output stops immediately AND a false positive is
              // recoverable). The frame falls through to STT below (paused
              // output makes the line echo-quiet) so the confirm window
              // can actually hear the user. Parity with Python
              // ``_start_pause_resume``.
              this.startPauseResume();
            } else if (this.isSpeaking) {
              // Defer the cancel to transcript confirmation — instead of
              // firing on raw VAD energy — when EITHER opt-in
              // ``bargeInStrategies`` are configured OR we forward STT during
              // TTS WITHOUT AEC. On a no-AEC link a VAD ``speech_start`` here
              // is very often the agent's OWN echo, and cancelling on it
              // self-interrupts almost every turn (the "bene bene" →
              // [interrupted] cascade). Deferring lets ``handleBargeIn`` run
              // the echo guard on the resulting transcript and cancel only on
              // real caller speech; the pending state times out after
              // ``bargeInConfirmS`` so the agent resumes if nothing confirms.
              // Parity with Python on_audio_received ``defer_cancel``.
              const deferCancel =
                this.bargeInStrategies.length > 0 ||
                (this.forwardSttWhileSpeaking && !this.aec);
              if (deferCancel) {
                this.startPendingBargeIn();
                this.metricsAcc.anchorUserSpeechStart();
                return;
              }
              getLogger().info('[VAD] speech_start during TTS → BARGE-IN');
              this.metricsAcc.recordOverlapStart();
              this.metricsAcc.recordBargeinDetected();
              const bargeinSpan = startSpan(SPAN_BARGEIN, { 'patter.call.id': this.callId });
              try {
                // Post-complete barge-in during the buffered tail — rewrite
                // history to the heard prefix BEFORE cancelSpeaking resets
                // the playback cursor.
                this.maybeTruncateCompletedTurnHistory();
                this.cancelSpeaking();
                try {
                  this.deps.bridge.sendClear(this.ws, this.streamSid);
                } catch (err) {
                  getLogger().debug(`sendClear during VAD barge-in failed: ${String(err)}`);
                }
                // Replay the ring buffer of inbound frames captured while
                // the agent was speaking — those carry the user's first
                // ~500 ms of speech that the self-hearing guard had been
                // dropping on the floor. Without this flush, Deepgram
                // only sees audio AFTER `speech_start` fires (i.e. the
                // tail of the user's utterance), which is why short
                // interruptions like "stop" produced no transcript and
                // the agent kept talking.
                this.flushInboundAudioRing();
                this.metricsAcc.recordTtsStopped();
                this.metricsAcc.recordTurnInterrupted();
                this.metricsAcc.recordOverlapEnd(true);
              } finally {
                try {
                  bargeinSpan.end();
                } catch {
                  // Swallow.
                }
              }
            }
            if (!phantomSuppressed) {
              // Industry-standard pattern: every legitimate VAD speech_start re-anchors
              // the turn timestamp pre-commit. Repairs stale anchors from
              // rejected barge-ins / dropped final transcripts, plus the
              // original phantom-during-warmup-gate vulnerability.
              this.metricsAcc.anchorUserSpeechStart();
            }
            // PREEMPTIVE GENERATION: the user resumed speaking while a
            // speculative turn was buffering — the interim it was generated
            // from is stale, so abort silently (nothing was audible; the
            // next confident interim re-speculates). A RELEASED speculation
            // is no longer registered here — it is the live turn and the
            // barge-in paths above own it.
            if (!this.isSpeaking && this.speculation !== null) {
              await this.abortSpeculation('user_speech_resumed');
            }
          } else if (evt?.type === 'speech_end') {
            // Speech-event: user stop edge (pipeline parity with realtime).
            await this.emitUserSpeechEnded();
            this.metricsAcc.recordVadStop();
            if (this.deps.agent.turnDetector && !this.turnDetectorFailed) {
              // Semantic turn detection (opt-in): defer the STT finalize
              // until the end-of-utterance model agrees the caller is done
              // — or hold for at most ``maxSemanticHoldMs`` while it
              // predicts "incomplete" (mid-sentence pause). The default
              // ``turnDetector``-unset path below is unchanged, and a
              // failed detector permanently rejoins it.
              await this.semanticEouCheck();
            } else {
              // The SDK's VAD has detected end-of-speech earlier and more
              // reliably than the provider's own endpointing on PSTN
              // (Deepgram's natural-pause endpointing can run 1-6 s before
              // it emits a final). Ask the provider to finalise the
              // in-flight utterance NOW so the next turn can dispatch
              // immediately.
              this.finalizeSttForEou();
            }
          }

          // Semantic hold poll: while the detector is holding the turn
          // open, every additional silent frame advances the audio clock —
          // re-score after each ``SEMANTIC_POLL_MS`` window of silence and
          // force the finalize once the hard cap is reached. Frames that
          // carried a VAD transition are skipped: a ``speech_start`` just
          // cancelled the hold, and on the ``speech_end`` frame itself the
          // detector already scored this audio (the silence window starts
          // AFTER the decision point). Parity with Python
          // ``_poll_semantic_hold``.
          if (this.deps.agent.turnDetector && this.semanticHoldActive && !evt) {
            await this.pollSemanticHold(pcm16k.length);
          }
        } catch (err) {
          // Disable VAD for the rest of the call to avoid log spam on
          // repeated failures. Inference failures are already handled inside
          // the chain; this preserves the pre-extraction semantics where a
          // throw from the EVENT-HANDLING path above also disabled VAD.
          this.inputChain.disableVad();
          getLogger().warn(`VAD processFrame failed — disabling VAD for this call: ${String(err)}`);
        }
      }

      // Self-hearing guard: when the agent is speaking, do NOT forward audio
      // to STT. The agent's own TTS audio bleeds back through the caller mic
      // and Deepgram would happily transcribe it. With external VAD we still
      // detected barge-in above; without VAD we fall back to the legacy
      // "always forward + bargeInThresholdMs" path so users without a VAD
      // adapter aren't regressed.
      //
      // Pre-barge-in buffer: instead of dropping the frame on the floor,
      // we push it into a small ring (last ~600 ms). On a future
      // BARGE-IN this ring is flushed to STT so the user's first words
      // — captured BEFORE the VAD's `minSpeechDuration` window let it
      // emit `speech_start` — actually reach Deepgram. Without this
      // buffer, short interruptions ("stop") never produced a
      // transcript and the agent kept talking; long ones produced
      // truncated transcripts and the agent answered to fragments.
      // Pause-and-resume: while output is PAUSED the line is echo-quiet
      // (no TTS is playing), so frames flow straight to STT — the confirm
      // window depends on STT hearing the user. ``startPauseResume``
      // already flushed the ring's leading edge when the pause began.
      if (this.isSpeaking && !this.outputPaused) {
        if (frame.vadConfigured) {
          this.inboundAudioRing.push(pcm16k);
          if (
            this.inboundAudioRing.length > StreamHandler.INBOUND_AUDIO_RING_FRAMES
          ) {
            this.inboundAudioRing.shift();
          }
          // Opt-in: also forward the frame to STT during TTS so the transcript
          // barge-in path can receive a transcript on echo-masked links where
          // the VAD never fires. The ring push above stays unconditional
          // (leading-edge recovery preserved); only the early-return is gated.
          // ECHO RISK without AEC. Default OFF → byte-identical push-and-return.
          if (!this.forwardSttWhileSpeaking) return;
        } else if ((this.deps.agent.bargeInThresholdMs ?? 300) === 0) {
          return;
        }
      }

      // beforeSendToStt hook — gate/transform the audio chunk before it
      // reaches STT (custom VAD, echo cancellation, PII redaction, ...).
      // Guard: only allocate the executor + history spread when the hook is
      // actually registered — this path runs ~50/s so per-frame allocations
      // (PipelineHookExecutor + [...history.entries]) accumulate GC pressure
      // quickly on long calls.
      const hooks = this.deps.agent.hooks;
      if (hooks?.beforeSendToStt) {
        const hookExecutor = new PipelineHookExecutor(hooks);
        const hookCtx = this.buildHookContext();
        const processed = await hookExecutor.runBeforeSendToStt(pcm16k, hookCtx);
        if (processed === null) return;
        this.stt.sendAudio(processed);
        this.metricsAcc.addSttAudioBytes(processed.length);
      } else {
        this.stt.sendAudio(pcm16k);
        this.metricsAcc.addSttAudioBytes(pcm16k.length);
      }
    } else if (this.adapter) {
      // OpenAI Realtime (the GA adapter — used for both OpenAIRealtime and
      // OpenAIRealtime2) overrides sendAudio to transcode Twilio's mulaw 8 kHz
      // up to PCM-16 24 kHz internally, so the caller's raw mulaw bytes are
      // forwarded untouched here.
      // ElevenLabs ConvAI defaults to PCM 16kHz — transcode Twilio mulaw
      // first. When ConvAI was constructed via ``ElevenLabsConvAIAdapter
      // .forTwilio(...)`` (or any path that sets ``inputAudioFormat
      // === 'ulaw_8000'``) we negotiated μ-law on both directions, so we
      // forward the caller's μ-law bytes untouched — saves a decode +
      // resample on every inbound frame.
      if (
        this.adapter instanceof ElevenLabsConvAIAdapter &&
        this.deps.bridge.inputWireFormat === 'ulaw_8000' &&
        this.adapter.inputAudioFormat !== 'ulaw_8000'
      ) {
        const pcm8k = mulawToPcm16(audioBuffer);
        const pcm16k = this.inboundResampler.process(pcm8k);
        this.adapter.sendAudio(pcm16k);
      } else {
        this.adapter.sendAudio(audioBuffer);
      }
    }
  }

  /** Handle a DTMF keypress event (Twilio only). */
  /** Handle an inbound DTMF tone from the caller. */
  async handleDtmf(digit: string): Promise<void> {
    getLogger().debug(`DTMF: ${digit}`);
    if (this.adapter instanceof OpenAIRealtimeAdapter) {
      await this.adapter.sendText(`The user pressed key ${digit} on their phone keypad.`);
    }
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({ role: 'user', text: `[DTMF: ${digit}]`, call_id: this.callId });
    }
  }

  /**
   * Last mark name Twilio has confirmed playback of. Mirrors the Python
   * ``TwilioAudioSender.last_confirmed_mark`` field — barge-in heuristics
   * compare this against the latest sent mark to decide whether the agent's
   * audio has actually reached the caller yet.
   */
  lastConfirmedMark = '';

  /**
   * Handle a Twilio ``mark`` event acknowledging that a previously sent
   * audio chunk has been played out. Mirrors Python's
   * ``twilio_handler.py``: ``audio_sender.on_mark_confirmed(mark_name)`` +
   * ``handler.on_mark(mark_name)``.
   */
  /** Handle a Twilio Media Streams `mark` event acknowledging audio playback boundaries. */
  async onMark(markName: string): Promise<void> {
    if (!markName) return;
    // Resolve the firstMessage mark waiter (if any) so the send loop
    // can advance its sliding window. We resolve the matched entry AND
    // every entry before it in the queue — Twilio sometimes batches
    // mark echoes, and dropping earlier entries first keeps FIFO order
    // even when the higher-numbered echo arrives before a lower-
    // numbered one (rare but observed on degraded edges).
    const idx = this.pendingMarks.findIndex((m) => m.name === markName);
    if (idx < 0) return;
    // Only record the echo after we have confirmed it matches a known
    // queued mark. Before this gate ``onMark`` clobbered
    // ``lastConfirmedMark`` with any mark name — including stale
    // echoes that no longer correspond to anything we sent, or marks
    // emitted by adapters outside the firstMessage queue — which
    // would contaminate any downstream barge-in heuristic gated on
    // ``lastConfirmedMark``. The Python parity here is structural:
    // ``stream_handler.py``'s ``on_mark`` never touches a handler-
    // level field at all (the equivalent state lives on
    // ``TwilioAudioSender.last_confirmed_mark``, updated only via
    // the carrier's own echo handler).
    this.lastConfirmedMark = markName;
    const resolved = this.pendingMarks.splice(0, idx + 1);
    for (const entry of resolved) {
      try {
        entry.resolve();
      } catch {
        // No-op.
      }
    }
  }

  /**
   * Await the backgrounded turn dispatch during teardown, but never block
   * longer than ``DISPATCH_SETTLE_TIMEOUT_MS``. The earlier ``llmAbort.abort()``
   * settles the built-in LLM/TTS paths immediately; the cap only bites a
   * misbehaving user ``onMessage`` parked on a hung external call (JS promises
   * can't be cancelled). No-op when nothing is in flight.
   */
  private async settleDispatchForTeardown(): Promise<void> {
    // Settle the backgrounded greeting too: cancelSpeaking/llmAbort flips
    // isSpeaking so playFirstMessage's send loop exits on its next check —
    // this await just ensures its finally (resetTtsCarry / grace flip) ran
    // before adapters are torn down.
    if (this.firstMessageTask) {
      await this.firstMessageTask.catch(() => {});
      this.firstMessageTask = null;
    }
    if (!this.dispatchTask) return;
    const settle = this.dispatchTask.catch(() => {});
    let timer: ReturnType<typeof setTimeout> | undefined;
    const cap = new Promise<void>((resolve) => {
      timer = setTimeout(resolve, StreamHandler.DISPATCH_SETTLE_TIMEOUT_MS);
    });
    try {
      await Promise.race([settle, cap]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }


  /**
   * Apply per-call agent overrides returned by ``onCallStart``. snake_case
   * keys mirror the Python payload contract (``apply_call_overrides``);
   * ``stt_config``/``tts_config`` dicts are Python-only (TS agents carry
   * adapter instances, not configs) and are ignored here with a warning.
   * The deps object is per-handler, so swapping its ``agent`` is call-local.
   */
  private applyCallOverrides(overrides: Record<string, unknown>): void {
    const next: Record<string, unknown> = { ...this.deps.agent };
    const applied: string[] = [];
    const map: Record<string, string> = {
      system_prompt: 'systemPrompt',
      voice: 'voice',
      model: 'model',
      language: 'language',
      first_message: 'firstMessage',
      provider: 'provider',
      tools: 'tools',
      variables: 'variables',
    };
    for (const [key, field] of Object.entries(map)) {
      if (key in overrides) {
        next[field] = overrides[key];
        applied.push(key);
      }
    }
    if ('stt_config' in overrides || 'tts_config' in overrides) {
      getLogger().warn(
        'onCallStart overrides: stt_config/tts_config are Python-only (TS agents ' +
          'carry adapter instances) — ignored.',
      );
    }
    if (applied.length > 0) {
      (this.deps as { agent: AgentOptions }).agent = next as unknown as AgentOptions;
      getLogger().debug(`Per-call config overrides applied: ${applied.join(', ')}`);
    }
  }

  /** Handle call stop / stream end. */
  /** Handle a carrier-emitted `stop` event signalling the call has ended. */
  // ---------------------------------------------------------------------------
  // Semantic turn detection (opt-in via ``agent.turnDetector``)
  // Parity with Python ``PipelineStreamHandler._semantic_*`` helpers.
  // ---------------------------------------------------------------------------

  /** Append a post-decode PCM16-16k frame to the rolling 8 s window. */
  private semanticBufferAppend(pcm16k: Buffer): void {
    if (pcm16k.length === 0) return;
    this.semanticAudioRing.push(pcm16k);
    this.semanticAudioRingBytes += pcm16k.length;
    while (
      this.semanticAudioRingBytes > StreamHandler.SEMANTIC_WINDOW_MAX_BYTES &&
      this.semanticAudioRing.length > 0
    ) {
      const dropped = this.semanticAudioRing.shift();
      if (dropped) this.semanticAudioRingBytes -= dropped.length;
    }
  }

  /** Concatenate the rolling window for one detector prediction. */
  private semanticWindowBytes(): Buffer {
    return Buffer.concat(this.semanticAudioRing);
  }

  /**
   * Drop the rolling window — called when a turn commits so the next
   * turn's window contains only its own audio (mirrors the reference
   * smart-turn integrations, which score per-turn audio).
   */
  private resetSemanticWindow(): void {
    this.semanticAudioRing = [];
    this.semanticAudioRingBytes = 0;
  }

  /**
   * Score the rolling window; finalize, or hold for more silence.
   *
   * Fail-open AND fail-once: the first detector error falls back to the
   * legacy immediate finalize (``vad_silence`` trigger) and disables
   * semantic endpointing for the remainder of the call — a broken model
   * must never stall a live phone call, and a permanently broken one
   * (onnxruntime-node missing/incompatible, model file gone) must produce
   * a single clear warning, not one per turn.
   */
  private async semanticEouCheck(): Promise<void> {
    const detector = this.deps.agent.turnDetector;
    if (!detector) return;
    let probability: number;
    try {
      probability = await detector.predict(this.semanticWindowBytes());
    } catch (err) {
      this.turnDetectorFailed = true;
      getLogger().warn(
        'Semantic turn detector failed — disabling it for this call and ' +
          `falling back to plain VAD-silence endpointing: ${String(err)}`,
      );
      this.cancelSemanticHold();
      // The rolling window is dead weight now that the detector is
      // disabled — release the up-to-8 s of buffered PCM immediately.
      this.resetSemanticWindow();
      this.lastEouTrigger = 'vad_silence';
      this.finalizeSttForEou();
      return;
    }

    const threshold = detector.threshold ?? 0.5;
    if (probability >= threshold) {
      getLogger().debug(
        `Semantic turn detector: end of turn (p=${probability.toFixed(3)} >= ${threshold})`,
      );
      this.cancelSemanticHold();
      this.lastEouTrigger = 'semantic_turn_detector';
      this.finalizeSttForEou();
    } else if (!this.semanticHoldActive) {
      getLogger().debug(
        `Semantic turn detector: holding turn open (p=${probability.toFixed(3)} < ${threshold})`,
      );
      this.beginSemanticHold();
    }
    // else: already holding — stay held; the frame-driven poll (or the
    // wall-clock backstop) schedules the next decision.
  }

  /** Arm the hold state + the wall-clock backstop for the hard cap. */
  private beginSemanticHold(): void {
    this.semanticHoldActive = true;
    this.semanticHoldDeadlineMs = Date.now() + this.maxSemanticHoldMs;
    this.semanticPollPendingBytes = 0;
    this.semanticHoldGeneration += 1;
    const generation = this.semanticHoldGeneration;
    // Wall-clock cap enforcement — runs even if inbound audio stalls
    // entirely (the frame-driven poll then never runs). Generation-guarded
    // (mirrors the grace-timer pattern): a hold resolved before the timer
    // fires invalidates it, so it can never finalize a later utterance.
    this.semanticHoldTimer = setTimeout(() => {
      this.semanticHoldTimer = null;
      if (generation !== this.semanticHoldGeneration || !this.semanticHoldActive) {
        return;
      }
      this.resolveSemanticHoldCap();
    }, this.maxSemanticHoldMs);
  }

  /** Drop the hold (and its backstop timer) without finalizing. Idempotent. */
  private cancelSemanticHold(): void {
    if (!this.semanticHoldActive) {
      // Includes teardown on a handler that never held — keep the timer
      // clear anyway (defensive; it is always null here in practice).
      if (this.semanticHoldTimer !== null) {
        clearTimeout(this.semanticHoldTimer);
        this.semanticHoldTimer = null;
      }
      return;
    }
    this.semanticHoldActive = false;
    this.semanticHoldDeadlineMs = null;
    this.semanticPollPendingBytes = 0;
    this.semanticHoldGeneration += 1;
    if (this.semanticHoldTimer !== null) {
      clearTimeout(this.semanticHoldTimer);
      this.semanticHoldTimer = null;
    }
  }

  /**
   * Advance the audio clock of an active hold by one inbound frame.
   *
   * Finalizes (``vad_silence``) once the hard cap is reached; otherwise
   * re-runs the detector after each additional ``SEMANTIC_POLL_MS`` of
   * silence so a model that flips to "complete" with more trailing
   * silence commits the turn as ``semantic_turn_detector``.
   */
  private async pollSemanticHold(frameBytes: number): Promise<void> {
    if (this.semanticHoldDeadlineMs !== null && Date.now() >= this.semanticHoldDeadlineMs) {
      this.resolveSemanticHoldCap();
      return;
    }
    this.semanticPollPendingBytes += frameBytes;
    const pollBytes = Math.floor(16000 * 2 * (StreamHandler.SEMANTIC_POLL_MS / 1000));
    if (this.semanticPollPendingBytes < pollBytes) return;
    this.semanticPollPendingBytes = 0;
    await this.semanticEouCheck();
  }

  /**
   * Hard cap reached: finalize anyway so the turn can never hang. The
   * semantic model never agreed, so the commit reason is the accumulated
   * silence — the EOU trigger stays ``vad_silence``.
   */
  private resolveSemanticHoldCap(): void {
    if (!this.semanticHoldActive) return;
    getLogger().debug(
      `Semantic hold cap reached (${this.maxSemanticHoldMs} ms) — finalizing on VAD silence`,
    );
    this.cancelSemanticHold();
    this.lastEouTrigger = 'vad_silence';
    this.finalizeSttForEou();
  }

  /**
   * Ask the STT provider to finalize the in-flight utterance NOW.
   * Optional chained — Whisper-class adapters that don't support
   * per-utterance finalisation simply skip. Extracted verbatim from the
   * VAD ``speech_end`` branch so the default path stays byte-identical
   * and the semantic turn-detector paths reuse it.
   */
  private finalizeSttForEou(): void {
    try {
      const ret = this.stt?.finalize?.();
      if (ret instanceof Promise) {
        ret.catch((err) =>
          getLogger().debug(`STT finalize threw: ${String(err)}`),
        );
      }
    } catch (err) {
      getLogger().debug(`STT finalize threw: ${String(err)}`);
    }
  }

  async handleStop(): Promise<void> {
    // Abort any in-flight LLM stream and close any in-flight TTS WS so
    // the runPipelineLlm / synthesizeStream awaits unblock immediately
    // instead of waiting up to 30 s for their own watchdog timers.
    // Without this, the carrier's ``stop`` event ends the call but a
    // pending TTS WS frame-wait fires a stale ``LLM loop error`` /
    // ``TTS streaming error`` log line tens of seconds later, and in
    // rapid-conversation scenarios where the user hangs up mid-response
    // the in-flight call kept billing tokens after the carrier was gone.
    if (this.llmAbort !== null) {
      try { this.llmAbort.abort(); } catch { /* defensive */ }
    }
    const ttsCancelable = this.tts as
      | { cancelActiveStream?: () => void }
      | undefined;
    if (typeof ttsCancelable?.cancelActiveStream === 'function') {
      try { ttsCancelable.cancelActiveStream(); } catch { /* defensive */ }
    }
    // PREEMPTIVE GENERATION: stop the interim-stability timer and tear down
    // any in-flight speculation (teardown — not a miss) before adapters
    // close underneath it.
    this.clearInterimStabilityTimer();
    await this.abortSpeculation('teardown', false);
    // Settle the backgrounded turn dispatch (the abort above unblocks it) so
    // no in-flight LLM/TTS work touches adapters after they close — bounded so
    // a hung user onMessage cannot block teardown. Parity with Python cleanup
    // hard-cancelling ``_dispatch_task``.
    await this.settleDispatchForTeardown();
    // Drop any pending barge-in timer BEFORE we tear down metrics /
    // adapters. Without this, a call that ends while a barge-in is
    // pending leaves a setTimeout scheduled to fire ``bargeInConfirmMs``
    // later and call ``metricsAcc.recordOverlapEnd`` on a finalised
    // metrics object — a slow leak in long-running servers and a race
    // producing spurious overlap_end events. Idempotent.
    this.clearPendingBargeIn();
    // Drop pause-and-resume buffers and wake any pause-decision waiter so
    // a call ending mid-pause cannot strand a loop awaiting the (now
    // cancelled) resume timer.
    this.discardPauseState();
    // Drop any active semantic-turn hold so its wall-clock backstop timer
    // cannot fire after teardown and call ``stt.finalize`` on a closed
    // adapter. Idempotent; no-op when no ``turnDetector`` is configured.
    this.cancelSemanticHold();
    // Resolve every pending firstMessage mark waiter before tearing the
    // adapter down. A call that ends mid firstMessage (carrier stop
    // arriving before the paced sender finished) would otherwise leak
    // unresolved promises owned by the send loop.
    this.drainPendingMarks();
    // Reset the firstMessage mark counter so a re-used handler starts
    // ``fm_<n>`` numbering at 1 on the next call. See
    // ``sendPacedFirstMessageBytes`` for the per-send reset that
    // protects the within-call path.
    this.clearGraceTimer();
    this.flushResamplers();
    await this.closeSttOnce();
    try { this.adapter?.close(); } catch { /* ignore */ }
    await this.fireCallEnd();
  }

  /** Handle WebSocket close event. */
  /** Tear down adapter, STT/TTS, and per-call state when the carrier WebSocket closes. */
  async handleWsClose(): Promise<void> {
    // Mirror handleStop's in-flight cleanup so a carrier WebSocket drop
    // unblocks LLM / TTS awaits immediately — see comment there.
    if (this.llmAbort !== null) {
      try { this.llmAbort.abort(); } catch { /* defensive */ }
    }
    const ttsCancelable = this.tts as
      | { cancelActiveStream?: () => void }
      | undefined;
    if (typeof ttsCancelable?.cancelActiveStream === 'function') {
      try { ttsCancelable.cancelActiveStream(); } catch { /* defensive */ }
    }
    // See handleStop — tear down any in-flight speculation (not a miss).
    this.clearInterimStabilityTimer();
    await this.abortSpeculation('teardown', false);
    // Settle the backgrounded turn dispatch before tearing down adapters,
    // bounded so a hung user onMessage cannot block teardown (see handleStop).
    await this.settleDispatchForTeardown();
    // See handleStop — drop pending barge-in timer before cleanup so a
    // dead handler can never fire a stale recordOverlapEnd callback.
    this.clearPendingBargeIn();
    // See handleStop — drop pause-and-resume state so a dead handler can
    // never strand a pause-decision waiter or replay stale audio.
    this.discardPauseState();
    // See handleStop — drop any active semantic-turn hold so its backstop
    // timer cannot fire ``stt.finalize`` against a torn-down adapter.
    this.cancelSemanticHold();
    // See handleStop — drain pending firstMessage marks so an abnormal
    // carrier WS drop during the paced sender cannot leak unresolved
    // promises owned by the send loop, and reset the counter.
    this.drainPendingMarks();
    this.clearGraceTimer();
    this.flushResamplers();
    // Drain STT first so in-flight transcripts fire before onCallEnd.
    await this.closeSttOnce();
    try { this.adapter?.close(); } catch { /* ignore */ }
    await this.fireCallEnd();
    // Ensure telephony call is terminated even if WebSocket closed abnormally
    try { await this.deps.bridge.endCall(this.callId, this.ws); } catch { /* best effort */ }
  }

  /** Close STT at most once; swallow errors. */
  private async closeSttOnce(): Promise<void> {
    if (this.sttClosed) return;
    this.sttClosed = true;
    try { await this.stt?.close(); } catch { /* ignore */ }
  }

  // ---------------------------------------------------------------------------
  // Private: Audio encoding for pipeline mode
  // ---------------------------------------------------------------------------

  /**
   * Encode a PCM 16kHz audio chunk for the telephony provider.
   *
   * Both Twilio and Telnyx negotiate PCMU (mulaw) 8 kHz on the bidirectional
   * media stream — Twilio always, and Telnyx because ``streaming_start``
   * (server.ts) requests ``stream_bidirectional_codec=PCMU`` at 8 kHz. So
   * the wire format for both providers is mulaw 8 kHz; we resample 16 kHz
   * PCM16 → 8 kHz then encode to mulaw. Mirrors the Python pipeline path
   * (libraries/python/getpatter/handlers/telnyx_handler.py::TelnyxAudioSender).
   *
   * Maintains a 1-byte carry across calls so unaligned HTTP chunks from
   * streaming TTS providers never byte-swap the PCM16 samples downstream.
   */
  private encodePipelineAudio(audioChunk: Buffer): string {
    // Local-recording tap (agent side, pipeline mode) — this method is the
    // single chokepoint every outbound pipeline chunk passes through
    // (firstMessage, prewarm, per-sentence TTS). Unlike the AEC far-end
    // taps (which must skip non-PCM bytes), the recording tap DECODES on
    // the carrier-native fast path: μ-law 8 kHz on Twilio/Plivo, PCM16
    // 16 kHz on Telnyx-native and the default transcode path. Parity with
    // Python ``_tap_pipeline_agent_audio``.
    if (this.localRecorder) {
      this.localRecorder.addAgentAudio(
        audioChunk,
        this.ttsOutputFormatNativeForCarrier &&
          this.deps.bridge.telephonyProvider !== 'telnyx'
          ? 'mulaw_8k'
          : 'pcm16_16k',
      );
    }
    // Carrier-native fast path: when the TTS adapter is configured to
    // emit ``ulaw_8000`` (Twilio wire codec) the bytes coming in are
    // already in the format Twilio expects. Skip the 16 kHz → 8 kHz
    // resample and the PCM → μ-law encode entirely — base64 the raw
    // bytes and hand them to the carrier. This eliminates the client-
    // side DSP chain that produced audible artifacts on the prewarmed
    // firstMessage during 0.6.2 acceptance (the resampler-bursting
    // crackle the user reported).
    if (this.ttsOutputFormatNativeForCarrier === true) {
      return audioChunk.toString('base64');
    }
    const aligned = this.alignPcm16(audioChunk);
    if (aligned.length === 0) return '';
    const pcm8k = this.outboundResampler.process(aligned);
    const mulaw = pcm16ToMulaw(pcm8k);
    return mulaw.toString('base64');
  }

  /**
   * Cached result of ``isTtsOutputFormatNativeForCarrier()`` — settled
   * once at ``initPipeline`` time after ``setTelephonyCarrier`` has run
   * on the TTS adapter. Stable for the call lifetime: changes to the
   * adapter's output format mid-call would NOT flip this. ``true`` means
   * ``encodePipelineAudio`` can take the bypass path.
   */
  private ttsOutputFormatNativeForCarrier: boolean = false;

  /**
   * Probe whether the TTS adapter is configured to emit bytes already in
   * the carrier's wire codec. Currently: Twilio expects ``ulaw_8000``,
   * Telnyx expects ``pcm_16000`` (no client transcode in either case if
   * matched). Anything else takes the resample-and-encode path.
   */
  private isTtsOutputFormatNativeForCarrier(): boolean {
    if (!this.tts) return false;
    const fmt = (this.tts as { outputFormat?: string }).outputFormat;
    if (typeof fmt !== 'string') return false;
    const carrier = this.deps.bridge.telephonyProvider;
    // Every supported carrier wire is μ-law 8 kHz — the SDK's own
    // ``streaming_start`` pins Telnyx to PCMU (the old 'pcm_16000'
    // expectation shipped raw PCM16 onto the μ-law wire: static). When the
    // TTS output is already μ-law the pipeline must bypass the PCM
    // resample/re-encode path — otherwise the encoded bytes are mangled.
    if (carrier === 'twilio' || carrier === 'telnyx' || carrier === 'plivo') {
      return fmt === 'ulaw_8000';
    }
    return false;
  }

  /**
   * Prepend any carry byte from the previous chunk, return the even-length
   * portion, and stash the final odd byte (if any) for the next call.
   */
  private alignPcm16(chunk: Buffer): Buffer {
    const combined = this.ttsByteCarry
      ? Buffer.concat([this.ttsByteCarry, chunk])
      : chunk;
    const alignedLen = combined.length & ~1;
    this.ttsByteCarry =
      alignedLen < combined.length ? combined.subarray(alignedLen) : null;
    return combined.subarray(0, alignedLen);
  }

  /**
   * Stream a cached firstMessage buffer in pacing-friendly chunks.
   *
   * Splits ``prewarmBytes`` into 20 ms slices (matching Twilio's PSTN
   * frame quantum) and
   * forwards each through ``deps.bridge.sendAudio`` exactly like the
   * live TTS path does — preserving Twilio mark/clear granularity. A
   * single multi-second sendAudio call would push the whole intro into
   * the carrier in one go and a ``sendClear`` issued mid-buffer would
   * have nothing to clear ("agent keeps talking after barge-in" UX bug
   * on the very first turn).
   *
   * Returns ``true`` when at least one chunk hit the wire — the caller
   * uses that to decide whether to record TTS-first-byte / turn-complete
   * metrics.
   */
  private async streamPrewarmBytes(prewarmBytes: Buffer): Promise<boolean> {
    return this.sendPacedFirstMessageBytes(prewarmBytes);
  }

  /**
   * Iterate ``bytes`` in 20 ms slices (Twilio PSTN frame quantum) and
   * forward each via ``deps.bridge.sendAudio`` with mark-gated pacing
   * (Twilio) or playout-time-based pacing (Telnyx). Caps the carrier-
   * side buffer at ``FIRST_MESSAGE_MARK_WINDOW`` chunks so a barge-in's
   * ``sendClear`` has ~120 ms (Twilio) or zero (Telnyx, immediately
   * after the latest sleep) of audio to flush.
   *
   * Bails immediately when ``isSpeaking`` flips to false — both via the
   * loop's pre-iter check and via ``drainPendingMarks`` (called from
   * ``cancelSpeaking``) which unblocks any in-flight ``waitForMarkWindow``.
   *
   * Returns ``true`` when at least one chunk hit the wire — the caller
   * uses that to decide whether to record TTS-first-byte / turn-complete
   * metrics. See BUG #128 for the regression this fix targets.
   */
  /**
   * Stream the configured greeting — runs as a BACKGROUND task.
   *
   * ``handleCallStart`` used to execute this inline; with the carrier WS
   * events now serialized onto a per-connection FIFO, that blocked EVERY
   * media frame for the whole greeting (VAD/barge-in structurally
   * impossible on the first message, mark acks unread). ``handleCallStart``
   * awaits ``beginSpeaking(true)`` BEFORE spawning this task so the
   * self-hearing guard engages from the very first inbound frame.
   * Mirrors the Python ``_play_first_message`` task.
   */
  private async playFirstMessage(label: string): Promise<void> {
    const firstMessage = this.deps.agent.firstMessage;
    if (!firstMessage || !this.tts) return;
    let firstChunkSent = false;
    this.resetTtsCarry();
    // Check the prewarm cache first. When ``Patter.call`` was made
    // with ``agent.prewarmFirstMessage: true`` the firstMessage has
    // already been synthesised during the ringing window — we send
    // the bytes directly through the carrier-side encoder (which
    // handles native-rate → carrier-rate resampling) and skip the
    // TTS round-trip entirely.
    let prewarmBytes: Buffer | undefined;
    if (this.deps.popPrewarmAudio) {
      try {
        prewarmBytes = this.deps.popPrewarmAudio(this.callId);
      } catch (err) {
        getLogger().debug(`popPrewarmAudio raised: ${String(err)}`);
      }
    }
    try {
      if (prewarmBytes) {
        this.metricsAcc.recordTtsFirstByte();
        await this.emitAudioOut();
        firstChunkSent = await this.streamPrewarmBytes(prewarmBytes);
      } else {
        // Streaming TTS path (no prewarm cache). Uses the same simple
        // per-chunk send as synthesizeSentence — ElevenLabs HTTP streams
        // at near-real-time speed so the carrier-side buffer stays bounded
        // without mark-gated pacing.  Routing streaming chunks through
        // sendPacedFirstMessageBytes caused crackling: its drain+reset on
        // every HTTP chunk destroyed mark back-pressure continuity and the
        // per-sub-chunk sleep slowed delivery below Twilio's playout rate,
        // producing periodic buffer underruns.  The prewarm path (a single
        // pre-synthesised buffer) still uses sendPacedFirstMessageBytes
        // because that buffer can be several seconds long and needs pacing.
        for await (const chunk of this.tts.synthesizeStream(firstMessage)) {
          if (!this.isSpeaking) break;
          if (!firstChunkSent) {
            firstChunkSent = true;
            this.metricsAcc.recordTtsFirstByte();
            await this.emitAudioOut();
          }
          // Same carrier-native gate as the paced path above: on the
          // mulaw fast path these are wire bytes, not PCM16 — pushing
          // them corrupted the AEC reference.
          if (this.aec && !this.ttsOutputFormatNativeForCarrier) this.aec.pushFarEnd(chunk);
          const encoded = this.encodePipelineAudio(chunk);
          this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
          this.markFirstAudioSent();
        }
      }
    } catch (e) {
      getLogger().error(`First message TTS error (${label}):`, e);
    } finally {
      // Drop any partial int16 byte to prevent cross-turn corruption
      // if the stream threw before a complete sample was delivered.
      this.resetTtsCarry();
      // Flip back to not-speaking with grace so the ring buffer
      // accumulated during the intro is flushed and the next user
      // utterance is recognised cleanly.
      this.endSpeakingWithGrace();
    }
    if (firstChunkSent) {
      // Bill the firstMessage TTS characters — they were synthesised
      // at ElevenLabs (or the configured TTS provider) and the
      // customer pays for them. The previous flow only called
      // ``recordTurnComplete`` here, which finalises the turn but does
      // NOT increment the TTS char counter — so a 5-turn call with an
      // 82-char greeting was under-billed by ~22% on TTS cost.
      // ``recordTtsComplete`` is the canonical accumulator entry
      // point for TTS char billing (parity with Python fix).
      this.metricsAcc.recordTtsComplete(firstMessage);
      await this.emitTurnMetrics(this.metricsAcc.recordTurnComplete(firstMessage));
      this.history.push({ role: 'assistant', text: firstMessage, timestamp: Date.now() });
    }
  }

  private async sendPacedFirstMessageBytes(bytes: Buffer): Promise<boolean> {
    // Reset any stale mark state defensively — we don't emit marks on
    // this path but ``onMark`` and the rest of the handler rely on the
    // counter being monotonic across the call lifetime.
    if (this.pendingMarks.length > 0) this.drainPendingMarks();
    let firstChunkSent = false;
    // Slice on the PSTN/G.711 packet quantum (20 ms). Twilio Media
    // Streams emits and consumes 20 ms μ-law frames natively, so each
    // ``sendAudio`` corresponds to exactly one carrier-side frame.
    const PSTN_FRAME_MS = 20;
    const bytesPerMs = this.ttsOutputFormatNativeForCarrier
      ? 8 // μ-law 8 kHz native (one byte per sample, 8000 sps)
      : StreamHandler.PCM16_16K_BYTES_PER_MS; // 32 bytes/ms for PCM16 16 kHz
    const sliceBytes = bytesPerMs * PSTN_FRAME_MS;
    // No pacing, no mark gating. Twilio's media-stream protocol
    // explicitly buffers and plays frames in order received — its FIFO
    // owns the 8 kHz playout clock, not our send loop. Every attempt
    // we've made to "help" Twilio (per-chunk sleep, mark back-pressure,
    // initial-fill burst, absolute-clock scheduling) introduced its own
    // jitter source: setTimeout drift, mark-echo RTT > playout window,
    // or burst-then-stall patterns — audible as choppy or warbled
    // playout caused by our pacing fighting the carrier clock, not the
    // carrier itself.
    //
    // The stable approach: dump every 20 ms slice into the WebSocket
    // back-to-back, return, and let Twilio drain. For prewarm
    // this is ~250 sendAudio calls in <50 ms for a 5 s greeting; the
    // WebSocket buffer absorbs them and the carrier plays at exactly
    // 50 frames/s with no further intervention from us. Barge-in still
    // works via ``sendClear`` which flushes whatever Twilio has queued
    // regardless of marks.
    for (let i = 0; i < bytes.length; i += sliceBytes) {
      if (!this.isSpeaking) break; // barge-in mid-buffer — stop now
      const chunk = bytes.subarray(i, i + sliceBytes);
      if (!firstChunkSent) firstChunkSent = true;
      // Far-end tap is only valid when the bytes are PCM16 — the AEC's
      // ``int16BufferToFloat32`` ingest assumes int16 LE. On the mulaw
      // native fast path we MUST NOT push the wire bytes or AEC's
      // reference signal becomes garbage. AEC is opt-in (off by default
      // on PSTN), so this guard only matters when the caller opted in.
      if (this.aec && !this.ttsOutputFormatNativeForCarrier) {
        this.aec.pushFarEnd(chunk);
      }
      const encoded = this.encodePipelineAudio(chunk);
      this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
      this.markFirstAudioSent();
    }
    return firstChunkSent;
  }

  // ---------------------------------------------------------------------------
  // Private: Pipeline mode
  // ---------------------------------------------------------------------------

  private async initPipeline(resolvedPrompt: string): Promise<void> {
    const label = this.deps.bridge.label;

    this.stt = await this.deps.bridge.createStt(this.deps.agent);

    // v0.5.0+: TTS is a pre-instantiated adapter on ``agent.tts`` or null.
    this.tts = await createTTS(this.deps.agent);

    // Advise the TTS adapter of the telephony carrier so it can pick a
    // wire-native ``outputFormat`` (e.g. ``ulaw_8000`` on Twilio) and
    // skip a client-side transcode. The hook is opt-in per-adapter:
    // adapters that don't expose ``setTelephonyCarrier`` keep their
    // constructed format. Adapters that do (e.g. ElevenLabsWebSocketTTS)
    // only auto-flip when the user did NOT explicitly pass outputFormat.
    if (this.tts) {
      const carrierAware = this.tts as unknown as {
        setTelephonyCarrier?: (c: string) => void;
      };
      if (typeof carrierAware.setTelephonyCarrier === 'function') {
        try {
          carrierAware.setTelephonyCarrier(this.deps.bridge.telephonyProvider);
        } catch (e) {
          getLogger().debug(`TTS setTelephonyCarrier failed (${label}): ${String(e)}`);
        }
      }
      // Re-evaluate after setTelephonyCarrier so the encodePipelineAudio
      // fast path is enabled for the current carrier when the adapter
      // auto-flipped (or the user constructed with a native format).
      this.ttsOutputFormatNativeForCarrier = this.isTtsOutputFormatNativeForCarrier();
      if (this.ttsOutputFormatNativeForCarrier) {
        getLogger().debug(
          `TTS outputFormat matches ${this.deps.bridge.telephonyProvider} wire codec — bypassing client-side transcode`,
        );
      }
    }

    if (!this.stt) {
      getLogger().debug(`Pipeline mode (${label}): no STT configured`);
    }
    if (!this.tts) {
      getLogger().debug(`Pipeline mode (${label}): no TTS configured`);
    }

    // Auto-VAD: load SileroVAD with telephony-tuned defaults if the user
    // didn't pass one. Falls back silently to the STT-endpoint heuristic
    // when onnxruntime-node is missing — same behaviour as before for
    // users who have not installed the optional dep.
    if (!this.deps.agent.vad) {
      try {
        const { SileroVAD } = await import('./providers/silero-vad');
        this.autoVad = await SileroVAD.forPhoneCall();
        getLogger().info(
          `auto-VAD enabled (SileroVAD, phone preset). Pass agent.vad=… to override.`,
        );
      } catch (e) {
        const msg = (e as Error)?.message ?? String(e);
        if (/Cannot find module|onnxruntime-node/i.test(msg)) {
          getLogger().info(
            'auto-VAD unavailable: onnxruntime-node not installed. ' +
              'Run `npm install onnxruntime-node@~1.18.0` for fast barge-in.',
          );
        } else {
          getLogger().warn(
            `auto-VAD load failed (${msg}); falling back to STT-endpoint heuristic`,
          );
        }
      }
    }

    // Acoustic echo cancellation: opt-in.
    //
    // Per the industry consensus on PSTN echo cancellation and Twilio's
    // own guidance, time-domain NLMS server-side AEC is the
    // RIGHT tool only when the SDK has near-direct access to the mic and
    // speaker (browser WebRTC, mobile native). PSTN paths route through
    // a 250–1500 ms Twilio jitter buffer + carrier loop — far outside
    // the 32 ms window of a 512-tap NLMS filter at 16 kHz, so the filter
    // cannot model the echo and silently degenerates into pass-through.
    // Emit a warning so the operator knows to either rely on the
    // self-hearing guard alone (handset / earpiece — minimal bleed) or
    // keep AEC off (default) and tune the VAD ``min_speech_duration`` if
    // bleed-driven false positives appear during firstMessage.
    if (this.deps.agent.echoCancellation) {
      // Every ``CarrierKind`` today is a PSTN carrier (Twilio / Telnyx /
      // Plivo), so the warning fires unconditionally. If a non-PSTN carrier
      // ever lands, lift this onto ``TelephonyBridge`` as a property.
      getLogger().warn(
        `echoCancellation: true on ${this.deps.bridge.telephonyProvider} (PSTN). ` +
          `Server-side NLMS cannot model PSTN's ~250–1500 ms round-trip echo ` +
          `with a 32 ms filter window — it will silently no-op. Best practice: ` +
          `keep echoCancellation: false; rely on the carrier + caller ` +
          `device's built-in echo suppression and Patter's self-hearing ` +
          `guard. Enable AEC only for browser/native deployments where ` +
          `the SDK owns the audio path end-to-end.`,
      );
      try {
        const { NlmsEchoCanceller } = await import('./audio/aec');
        this.aec = new NlmsEchoCanceller({ sampleRate: 16000 });
        getLogger().info(
          'echo cancellation enabled (NLMS, 512 taps + 0.5 s warmup μ=0.5); ' +
            'filter converges within ~250 ms of TTS playback in low-latency loops.',
        );
      } catch (e) {
        getLogger().warn(
          `echo cancellation requested but failed to load: ${String(e)}; ` +
            `falling back to pass-through.`,
        );
      }
    }

    // Prewarm-handoff: try to adopt pre-opened provider WebSockets that
    // the prewarm pipeline (see ``Patter.parkProviderConnections``)
    // parked during the carrier ringing window. When a parked WS is
    // still OPEN we skip the cold ``connect()`` and the STT first-turn
    // can flow audio without paying the 150-400 ms TLS handshake.
    // Failures (cache miss, parked WS died) fall back transparently.
    let parked: import('./client').ParkedProviderConnections | undefined;
    if (this.deps.popPrewarmedConnections) {
      try {
        parked = this.deps.popPrewarmedConnections(this.callId);
      } catch (err) {
        getLogger().debug(`popPrewarmedConnections raised: ${String(err)}`);
      }
    }
    // Adopt the TTS WS first — it's a synchronous handoff (the live
    // ``synthesizeStream`` call below picks it up via the adapter's
    // single-slot adoption queue).
    const parkedTts = parked?.tts;
    if (parkedTts && this.tts) {
      const ttsAny = this.tts as { adoptWebSocket?: (p: typeof parkedTts) => void };
      if (typeof ttsAny.adoptWebSocket === 'function' && parkedTts.ws.readyState === 1 /* OPEN */) {
        try {
          ttsAny.adoptWebSocket(parkedTts);
          getLogger().info(`[CONNECT] callId=${this.callId} provider=tts source=adopted ms=0`);
        } catch (err) {
          getLogger().debug(`TTS adoptWebSocket failed: ${String(err)}; falling back`);
          try { parkedTts.ws.close(); } catch { /* ignore */ }
        }
      } else {
        try { parkedTts.ws.close(); } catch { /* ignore */ }
      }
    }

    // Kick off STT connect WITHOUT awaiting yet — we only need STT ready
    // to receive incoming user audio, not to send the first agent
    // message out. Parallelising STT.connect with the TTS firstMessage
    // synth shaves 200-400 ms off the perceived first-turn latency.
    let sttConnectPromise: Promise<void> | null = null;
    if (this.stt) {
      const sttAny = this.stt as { adoptWebSocket?: (ws: import('ws').WebSocket) => void };
      const sttStarted = Date.now();
      if (
        parked?.stt &&
        typeof sttAny.adoptWebSocket === 'function' &&
        parked.stt.readyState === 1 /* OPEN */
      ) {
        try {
          sttAny.adoptWebSocket(parked.stt);
          getLogger().info(
            `[CONNECT] callId=${this.callId} provider=stt source=adopted ms=${Date.now() - sttStarted}`,
          );
          sttConnectPromise = Promise.resolve();
        } catch (err) {
          getLogger().debug(`STT adoptWebSocket failed: ${String(err)}; falling back`);
          try { parked.stt.close(); } catch { /* ignore */ }
          sttConnectPromise = (async () => {
            await this.stt!.connect();
            getLogger().info(
              `[CONNECT] callId=${this.callId} provider=stt source=fresh ms=${Date.now() - sttStarted}`,
            );
          })();
        }
      } else {
        if (parked?.stt) {
          try { parked.stt.close(); } catch { /* ignore */ }
        }
        sttConnectPromise = (async () => {
          await this.stt!.connect();
          getLogger().info(
            `[CONNECT] callId=${this.callId} provider=stt source=fresh ms=${Date.now() - sttStarted}`,
          );
        })();
      }
    }
    getLogger().debug(`Pipeline mode (${label}): STT connect kicked off`);

    if (this.deps.agent.firstMessage && !this.deps.onMessage && this.tts) {
      this.metricsAcc.startTurn();
      // Mark the agent as speaking for the duration of the first
      // message — without this, the self-hearing guard never engages,
      // the user's audio (mixed with TTS bleed) is forwarded to STT
      // and produces garbage transcripts, and the ring buffer for
      // pre-barge-in audio is never populated. Mirrors the per-turn
      // behaviour in `runPipelineLlm` / `runRegularLlm`.
      // Pass isFirstMessage=true so the canBargeIn() anti-flicker gate
      // starts running NOW — TTFB on the TTS provider often eats 300-800ms,
      // and without an early anchor the firstMessage is uninterruptible
      // during that window.
      await this.beginSpeaking(true);
      // Echo-guard reference: beginSpeaking resets it, and only the
      // streaming-LLM path repopulated it — under forward-STT-while-speaking
      // the echo of the GREETING (the highest-echo window of the call)
      // compared against an empty string and confirmed a phantom barge-in.
      this.currentAgentSpokenText = this.deps.agent.firstMessage;
      // Launch the greeting as a tracked background task — see
      // playFirstMessage for why it must not run inline.
      this.firstMessageTask = this.playFirstMessage(label).catch((err) => {
        getLogger().error(`First message playback failed (${label}): ${String(err)}`);
      });
    }

    // Create LLM loop for pipeline mode when no onMessage handler provided.
    // Precedence: user-supplied ``agent.llm`` > OpenAI default (from openaiKey).
    if (this.deps.agent.llm) {
      if (this.deps.onMessage) {
        throw new Error(
          "Cannot pass both agent({ llm }) and serve({ onMessage }). Pick one — " +
            "`llm` for built-in LLMs, `onMessage` for custom logic.",
        );
      }
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const providerModel = (this.deps.agent.llm as any)?.model ?? '';
      // Inject the built-in transfer_call / end_call tools (and handoff_to
      // when `agent.handoffs` is configured) — parity with the Realtime path
      // which injects them at `server.ts` and dispatches via the bridge in
      // this file's tool dispatcher. Without this, pipeline-mode LLMs never
      // see the built-ins and can't initiate a handoff or hangup no matter
      // what the system prompt says.
      const augmentedTools = this.buildPipelineLlmTools();
      this.llmLoop = new LLMLoop(
        '', // apiKey unused when llmProvider is supplied
        providerModel, // propagate so calculateLlmCost can match the price row
        resolvedPrompt,
        augmentedTools,
        this.deps.agent.llm,
        this.deps.agent.disablePhonePreamble ?? false,
      );
      this.llmLoop.setEventBus(this._eventBus);
      this.llmLoop.setOnToolCall((n, a, r) => this.recordToolCall(n, a, r));
      const llmLabel = this.deps.agent.llm.constructor?.name ?? 'custom';
      getLogger().debug(`Built-in LLM loop active (pipeline, ${label}, llm=${llmLabel})`);
    } else if (!this.deps.onMessage && this.deps.config.openaiKey) {
      let llmModel = this.deps.agent.model || 'gpt-4o-mini';
      if (llmModel.includes('realtime')) llmModel = 'gpt-4o-mini';
      const augmentedTools = this.buildPipelineLlmTools();
      this.llmLoop = new LLMLoop(
        this.deps.config.openaiKey,
        llmModel,
        resolvedPrompt,
        augmentedTools,
        undefined,
        this.deps.agent.disablePhonePreamble ?? false,
      );
      this.llmLoop.setEventBus(this._eventBus);
      this.llmLoop.setOnToolCall((n, a, r) => this.recordToolCall(n, a, r));
      getLogger().debug(`Built-in LLM loop active (pipeline, ${label})`);
    }

    if (this.stt) {
      // Make sure the STT WebSocket is OPEN before we install the
      // transcript handler — the parallel kickoff above may still be
      // resolving when we get here. Failures abort the call.
      if (sttConnectPromise) {
        try {
          await sttConnectPromise;
        } catch (e) {
          getLogger().error(`STT connect FAILED (${label}):`, e);
          try { await this.deps.bridge.endCall(this.callId, this.ws); } catch { /* best effort */ }
          return;
        }
      }
      this.stt.onTranscript(async (transcript) => {
        await this.handleTranscript(transcript);
      });
    }
  }

  /** Build a HookContext for the current call state. */
  private buildHookContext(): HookContext {
    return {
      callId: this.callId,
      caller: this.caller,
      callee: this.callee,
      history: [...this.history.entries],
    };
  }

  /** Synthesize a single sentence through TTS with hooks, sending audio to telephony. */
  private async synthesizeSentence(
    sentence: string,
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
    ttsFirstByteSent: { value: boolean },
    recordSegment = true,
  ): Promise<void> {
    // ``recordSegment=false`` (filler / error-fallback audio) advances the
    // playback clock without adding a heard-prefix segment — that audio is
    // not part of the LLM's reply. See ``heardResponsePrefix``.
    if (!this.tts || !this.isSpeaking) return;

    // Apply text transforms before the beforeSynthesize hook
    let transformed = sentence;
    const transforms = this.currentAgent.textTransforms;
    if (transforms) {
      for (const fn of transforms) {
        transformed = fn(transformed);
      }
    }

    // beforeSynthesize hook (per-sentence)
    const processedText = await hookExecutor.runBeforeSynthesize(transformed, hookCtx);
    if (processedText === null) return;

    this.resetTtsCarry();
    // Pause-and-resume retention: in ``bargeInMode: 'pause_resume'`` every
    // chunk of a RESPONSE sentence is kept in a per-sentence entry so a
    // paused turn can re-send the cleared-but-unheard tail at resume time
    // without re-billing TTS. ``null`` (legacy mode / filler audio /
    // post-overflow) keeps the direct send path byte-identical to today.
    let retainEntry = recordSegment ? this.beginRetainedSentence(processedText) : null;
    try {
      for await (const chunk of this.tts.synthesizeStream(processedText)) {
        if (!this.isSpeaking) break;

        // afterSynthesize hook (per-chunk). The await may yield control to
        // the event loop long enough for VAD to fire `speech_start during
        // TTS → BARGE-IN`, which calls cancelSpeaking() and flips
        // ``isSpeaking`` to false. Re-check below before pushing the
        // resulting audio to the carrier — without this re-check, exactly
        // one trailing chunk (~20–100 ms of audio) would race past the
        // cancel and prolong the perceived "agent didn't stop" window.
        const processedAudio = await hookExecutor.runAfterSynthesize(chunk, processedText, hookCtx);
        if (processedAudio === null) continue;
        if (!this.isSpeaking) break;

        if (!ttsFirstByteSent.value && !this.outputPaused) {
          // While the pause gate holds the chunk in memory it has NOT
          // reached the carrier — the flag (and the audio_out speech
          // event) waits for the first post-resume chunk.
          ttsFirstByteSent.value = true;
          this.metricsAcc.recordTtsFirstByte();
          // Speech-event: per-turn first TTS audio chunk.
          await this.emitAudioOut();
        }
        // Pause-and-resume retention path: the chunk is appended to the
        // sentence's entry; while paused it stays queued, while speaking
        // it is drained (sent) immediately. Segment stamping / AEC tap /
        // playback tracking live in ``drainSentenceEntry`` so they fire at
        // SEND time, not at synthesis time.
        if (retainEntry !== null && this.pauseResumeOverflowed) {
          retainEntry = null; // retention released mid-sentence
        }
        if (retainEntry !== null) {
          if (this.retainPauseChunk(retainEntry, processedAudio)) {
            if (!this.outputPaused) {
              this.drainSentenceEntry(retainEntry);
              if (!this.isSpeaking) break; // cancel raced the drain
            }
            continue;
          }
          if (!this.isSpeaking) break; // paused overflow degraded to cancel
          // Overflow while speaking: retention released — fall through to
          // the direct send path for this chunk and the rest of the turn.
          // The sentence keeps its already stamped segment (if any); the
          // inline stamp below is skipped to avoid a duplicate.
          retainEntry = null;
          recordSegment = false;
        }
        if (this.outputPaused) {
          // Paused with no retention entry (filler / error-fallback
          // audio): drop the chunk — replaying moment-filling audio after
          // a pause is pointless.
          continue;
        }
        // Far-end tap for the echo canceller. On the default path
        // ``processedAudio`` is the exact PCM 16 kHz Buffer the carrier-side
        // encoder is about to transcode + send — the cleanest reference of
        // "what the speaker is about to play". Push BEFORE ``sendAudio`` so
        // a very fast carrier echo is still seen by the next mic frame.
        // SKIPPED on the carrier-native fast path — there these are mulaw
        // wire bytes and the int16 ingest turned the reference to garbage.
        if (this.aec && !this.ttsOutputFormatNativeForCarrier) {
          this.aec.pushFarEnd(processedAudio);
        }
        if (recordSegment) {
          // First audible chunk of this sentence — stamp its start on the
          // per-turn playback timeline so a barge-in can estimate the heard
          // prefix at sentence granularity.
          this.turnSpokenSegments.push({
            text: processedText,
            startMs: this.turnPlaybackTotalMs,
          });
          recordSegment = false;
        }
        const encoded = this.encodePipelineAudio(processedAudio);
        this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
        this.trackOutboundPlayback(processedAudio.length);
        this.markFirstAudioSent();
      }
    } catch (e) {
      getLogger().error(`TTS streaming error (${this.deps.bridge.label}):`, e);
    } finally {
      this.resetTtsCarry();
    }
  }

  /** Handle a final transcript from STT in pipeline mode. */
  private async handleTranscript(transcript: STTTranscript): Promise<void> {
    this.transcriptQueue.push(transcript);
    if (this.transcriptProcessing) return;
    this.transcriptProcessing = true;
    try {
      while (this.transcriptQueue.length > 0) {
        const next = this.transcriptQueue.shift()!;
        await this.processTranscript(next);
      }
    } finally {
      this.transcriptProcessing = false;
    }
  }

  private async processTranscript(transcript: STTTranscript): Promise<void> {
    // Function-scope barge-in flag — set either by the upfront barge-in
    // check, or by the TTS loops downstream when ``isSpeaking`` flips mid-
    // synthesis. Prevents recordTurnComplete double-counting a half-spoken
    // turn (Python uses the same pattern).
    let interrupted = this.handleBargeIn(transcript);

    // Fix 6 (Python parity): start the turn timer on the first non-empty STT
    // partial/final so stt_ms measures from real speech onset rather than from
    // the first silence audio byte. startTurnIfIdle() is a no-op if already open.
    if (transcript.text) {
      this.metricsAcc.startTurnIfIdle();
    }

    // Wave6B: record VAD stop timestamp when the STT provider signals speech end.
    if (transcript.speechFinal) {
      this.metricsAcc.recordVadStop();
    }

    if (!transcript.isFinal || !transcript.text) {
      // PREEMPTIVE GENERATION: a confident interim may start a speculative
      // LLM+TTS dispatch (audio held until the final commits). No-op unless
      // ``agent.preemptiveGeneration``. Awaited so successive interims on the
      // transcript drain loop are processed strictly in order (replacing a
      // speculation fully aborts the old one before the new one starts) —
      // parity with Python's awaited ``_note_interim_transcript``.
      if (transcript.text && !transcript.isFinal) {
        await this.noteInterimTranscript(transcript.text);
      }
      return;
    }
    if (!this.commitTranscript(transcript.text)) {
      // Final transcript dropped (dedup / hallucination / back-to-back).
      // Any VAD ``speech_end`` that fired during this dropped utterance
      // already stamped ``_endpointSignalAt``; if we leave it there, the
      // NEXT legitimate utterance inherits the stale anchor (its
      // agent_response_ms then includes the silence gap between the
      // dropped utterance and the real one).
      this.metricsAcc.anchorUserSpeechStart();
      return;
    }

    const label = this.deps.bridge.label;
    getLogger().debug(`User (${label} pipeline): ${sanitizeLogValue(transcript.text)}`);

    // A final transcript committed — interim-stability tracking for this
    // utterance is over (prevents a stale stability timer from speculating
    // on the just-answered utterance).
    this.resetInterimTracking();

    // Safety net: startTurnIfIdle() was already called above on first partial
    // text; this second call is a no-op in the normal path but guards code paths
    // (e.g. tests) that pass a final transcript without any preceding partial.
    this.metricsAcc.startTurnIfIdle();
    this.metricsAcc.recordSttComplete(transcript.text);
    this.metricsAcc.recordSttFinalTimestamp();

    // PREEMPTIVE GENERATION: when a speculative turn matching this final is
    // in flight, RELEASE it (its task becomes the live dispatch) instead of
    // starting a fresh one; a mismatch discards the speculation here and
    // falls through to the normal dispatch below.
    if (await this.tryReleaseSpeculation(transcript.text)) return;

    // Semantic turn detection (opt-in): a committed transcript supersedes
    // any in-flight hold (the STT endpointed on its own), and the per-turn
    // rolling window restarts so the next turn is scored on its own audio.
    if (this.deps.agent.turnDetector) {
      this.cancelSemanticHold();
      this.resetSemanticWindow();
    }
    // Speech-event: end-of-utterance committed (pipeline analogue of
    // Realtime's input_audio_buffer.committed, which fires at the server
    // commit signal regardless of what the app does with the text). Fires
    // HERE — at transcript commit, before the hook veto and the
    // handler-availability checks — so both the onMessage and built-in LLM
    // paths (and discarded orphan turns) advance the dispatcher's turn
    // index. The helper consumes the semantic detector's stamped trigger
    // when one is configured. Mirrors Python ``_dispatch_turn``.
    await this.emitUserSpeechEos(transcript.text);

    // Endpoint span — silence-detected → LLM-dispatch window. The matching
    // ``end()`` lives below right before ``recordTurnCommitted``. We use a
    // small helper so every early-return path closes the span exactly once.
    const endpointSpan = startSpan(SPAN_ENDPOINT, { 'patter.call.id': this.callId });
    let endpointSpanClosed = false;
    const closeEndpointSpan = (): void => {
      if (endpointSpanClosed) return;
      endpointSpanClosed = true;
      try {
        endpointSpan.end();
      } catch {
        // Swallow — span teardown should never crash the call path.
      }
    };

    if (this.deps.onTranscript) {
      try {
        await this.deps.onTranscript({
          role: 'user',
          text: transcript.text,
          call_id: this.callId,
          history: [...this.history.entries],
        });
      } catch (err) {
        // Observer callbacks must never break the pipeline: a raise here
        // propagated into the STT adapter's un-awaited emit loop and became
        // a process-killing unhandled rejection.
        getLogger().error(`onTranscript callback failed: ${String(err)}`);
      }
    }

    // --- afterTranscribe hook ---
    const hookExecutor = new PipelineHookExecutor(this.deps.agent.hooks);
    const hookCtx = this.buildHookContext();
    const filteredTranscript = await hookExecutor.runAfterTranscribe(transcript.text, hookCtx);
    if (filteredTranscript === null) {
      getLogger().debug(`afterTranscribe hook vetoed turn (${label})`);
      this.metricsAcc.recordTurnInterrupted();
      closeEndpointSpan();
      return;
    }

    // Push filtered text to history (after hook, so LLM sees redacted/modified text).
    // Keep the reference: the LLM snapshot below must EXCLUDE this entry —
    // ``LLMLoop.buildMessages`` replays history and then appends the current
    // user text itself, so including it here sent the utterance twice per turn.
    const ownUserEntry = { role: 'user', text: filteredTranscript, timestamp: Date.now() };
    this.history.push(ownUserEntry);

    // Wave6B: record that the transcript is being committed to the LLM.
    // onUserTurnCompleted hook is not yet wired in TS — record 0 delay so EOU can still emit.
    this.metricsAcc.recordOnUserTurnCompletedDelay(0);
    this.metricsAcc.recordTurnCommitted();
    closeEndpointSpan();

    // Settle the previous turn first (single-in-flight). It is either already
    // done, or this transcript's handleBargeIn above just aborted it — so this
    // await is fast and does not head-of-line-block the drain loop in
    // practice, while preserving strict per-turn history/metrics ordering.
    await this.dispatchTask?.catch(() => {});
    // Snapshot history at launch — AFTER the previous turn's settle above (so
    // its assistant entry is included), BEFORE any later transcript can mutate
    // it, and WITHOUT this turn's own user entry (buildMessages appends the
    // current user text itself — see ownUserEntry above). Mirrors Python's
    // pre-append ``list(self.conversation_history)`` snapshot.
    const historySnapshot = this.history.entries.filter((e) => e !== ownUserEntry);
    // Launch the turn as a tracked background task and RETURN immediately so
    // the transcript drain loop keeps running handleBargeIn against this LIVE
    // turn (the head-of-line-blocking fix). Parity with Python
    // ``create_task(_dispatch_turn(...))``.
    // Attach the catch AT CREATION: dispatchTurn has try/finally only, and
    // the next turn's ``await this.dispatchTask?.catch(...)`` attaches a
    // handler far too late for Node's unhandled-rejection check — a throwing
    // user onTranscript/onMetrics inside the turn killed the process.
    this.dispatchTask = this.dispatchTurn(
      filteredTranscript,
      hookExecutor,
      hookCtx,
      interrupted,
      historySnapshot,
    ).catch((err) => {
      getLogger().error(`LLM dispatch turn failed: ${String(err)}`);
    });
  }

  /**
   * Post-commit turn body (LLM dispatch → TTS → turn-complete) run as a
   * tracked background task so the transcript drain loop is not blocked for
   * the whole (possibly 30-90 s) agent-runtime turn. A barge-in — transcript
   * (now reachable mid-turn) or VAD — aborts the in-flight ``llmAbort`` and
   * flips ``isSpeaking``, which the LLM/TTS loops here observe and break on.
   * Parity with Python ``_dispatch_turn``.
   */
  private async dispatchTurn(
    filteredTranscript: string,
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
    interrupted: boolean,
    historySnapshot: Array<{ role: string; text: string }>,
  ): Promise<void> {
    const label = this.deps.bridge.label;
    let responseText = '';
    try {
      if (this.deps.onMessage && typeof this.deps.onMessage === 'function') {
        try {
          responseText = await this.deps.onMessage({
            text: filteredTranscript,
            call_id: this.callId,
            caller: this.caller,
            callee: this.callee,
            history: historySnapshot,
          });
        } catch (e) {
          getLogger().error(`onMessage error (${label}):`, e);
          return;
        }
        if (!responseText) {
          // Common misuse: onMessage was provided as an observer (returning void)
          // but it actually replaces the built-in LLM loop. Warn loudly — the caller
          // will hear no audio until the handler returns a non-empty string.
          getLogger().warn(
            `onMessage returned empty/void (${label}) — no TTS will play. ` +
            `If you intended to observe transcripts, use onTranscript instead; ` +
            `if you meant to answer via the built-in LLM, remove onMessage and pass openaiKey.`,
          );
        }
      } else if (this.deps.onMessage && isRemoteUrl(this.deps.onMessage)) {
        const msgData = {
          text: filteredTranscript,
          call_id: this.callId,
          caller: this.caller,
          callee: this.callee,
          history: historySnapshot,
        };
        if (isWebSocketUrl(this.deps.onMessage)) {
          await this.handleWebSocketResponse(msgData);
          return;
        }
        try {
          responseText = await this.deps.remoteHandler.callWebhook(this.deps.onMessage, msgData);
        } catch (e) {
          getLogger().error(`Webhook remote error (${label}):`, e);
          return;
        }
      } else if (this.llmLoop) {
        const llmResult = await this.runPipelineLlm(
          filteredTranscript,
          hookExecutor,
          hookCtx,
          historySnapshot,
        );
        responseText = llmResult.text;
        // OR in whether the LLM stream itself was cut short, in addition to a
        // barge-in already seen by handleBargeIn at the top of this turn.
        interrupted = interrupted || llmResult.interrupted;
      } else {
        getLogger().warn(
          `Pipeline (${label}) has no llm/onMessage handler — transcript ` +
            `"${sanitizeLogValue(filteredTranscript.slice(0, 60))}" dropped. ` +
            'Check that agent.llm or onMessage is configured.',
        );
        return;
      }

      if (!responseText) return;

      if (this.llmLoop) {
        // Marker goes to the history/transcript ONLY (so a stateful agent
        // runtime sees it was interrupted); metrics use the PLAIN text and are
        // gated on !interrupted — mirrors Python.
        let spokenText = responseText;
        if (interrupted) {
          // Truncate to what the caller actually HEARD, not everything the
          // LLM generated — an agent-runtime LLM delivers the full reply at
          // once, so by barge-in time ``responseText`` can hold tens of
          // seconds of text the caller never listened to. Falls back to the
          // legacy full-text marker when no playback segments were tracked
          // (e.g. no TTS configured). Mirrors Python
          // ``_process_streaming_response``.
          const heard = this.heardResponsePrefix();
          spokenText =
            heard === null
              ? `${responseText} [interrupted by caller]`
              : heard.text
                ? `${heard.text} [interrupted by caller]`
                : '[interrupted by caller]';
        }
        await this.emitAssistantTranscript(spokenText);
        if (!interrupted) this.metricsAcc.recordTtsComplete(responseText);
      } else {
        // ``runRegularLlm`` returns the possibly-replaced text directly —
        // re-reading ``history[-1]`` raced a concurrently committed user
        // turn and recorded the USER's text as this turn's completion.
        const regular = await this.runRegularLlm(responseText, hookExecutor, hookCtx);
        interrupted = regular.interrupted || interrupted;
        responseText = regular.finalText;
      }

      // Skip turn-complete when barge-in already recorded the turn as
      // interrupted — mirrors Python ``if not interrupted``. Prevents
      // double-counting / turn-count inflation / polluting p95.
      if (!interrupted) {
        await this.emitTurnMetrics(this.metricsAcc.recordTurnComplete(responseText));
      }
    } finally {
      this.dispatchTask = null;
    }
  }

  /**
   * Barge-in: caller spoke over in-flight TTS. Flip ``isSpeaking`` so the
   * sentence loop exits on its next check, clear downstream audio buffers,
   * record the interruption, and return ``true`` so the caller skips the
   * turn-complete record.
   */
  private async handleBargeInAsync(transcript: {
    text?: string;
    isFinal?: boolean;
    speechFinal?: boolean;
  }): Promise<boolean> {
    if (!transcript.text || !this.isSpeaking) return false;
    if (this.tailGraceActive) {
      // A transcript during the post-TTS tail grace is the next turn, not a
      // barge-in (the agent already finished). End the grace and return
      // WITHOUT cancelling — the same transcript then flows on to dispatch as
      // a normal new turn. Closes the race where a transcript lands before
      // the VAD speech_start rescue fires.
      this.endTailGraceForNewTurn();
      return false;
    }
    // Echo guard: when audio is forwarded to STT during TTS (no effective AEC),
    // the agent's own voice can be transcribed and would barge in on itself.
    // Drop transcripts that look like a fragment of what the agent is saying.
    // Active under forwardSttWhileSpeaking AND while output is paused
    // (pause_resume forwards mic audio to STT during the confirm window and
    // the just-cleared audio's PSTN echo tail can lag into it), so the
    // default VAD path is unaffected.
    if (
      (this.forwardSttWhileSpeaking || this.outputPaused) &&
      looksLikeEcho(transcript.text, this.currentAgentSpokenText)
    ) {
      getLogger().info(
        `Barge-in suppressed: transcript matches agent's own speech (echo) — ${sanitizeLogValue(
          transcript.text.slice(0, 40),
        )}`,
      );
      return false;
    }
    if (!this.canBargeIn()) {
      getLogger().info(
        `Barge-in transcript suppressed (agent speaking < gate, aec=${this.aec ? 'on' : 'off'})`,
      );
      return false;
    }
    // Pause-and-resume: while output is paused, only a committed FINAL
    // transcript (non-hallucination, non-duplicate) may confirm the kill —
    // interims and noise wait for the resume timer instead. The confirming
    // transcript then continues through the strategy/legacy decision below
    // exactly as today.
    if (this.outputPaused && !this.passesPausedKillFilters(transcript)) {
      getLogger().debug(
        `Paused turn: transcript ${sanitizeLogValue(
          transcript.text.slice(0, 40),
        )} cannot confirm the kill (interim/hallucination/duplicate) — awaiting resume timer`,
      );
      return false;
    }
    if (this.bargeInStrategies.length > 0) {
      const { evaluateStrategies } = await import(
        './services/barge-in-strategies.js'
      );
      const confirmed = await evaluateStrategies(this.bargeInStrategies, {
        transcript: transcript.text,
        isInterim: transcript.isFinal === false,
        agentSpeaking: this.isSpeaking,
      });
      if (!confirmed) {
        getLogger().debug(
          `Barge-in NOT confirmed by any strategy (${sanitizeLogValue(
            transcript.text.slice(0, 40),
          )}); agent continues talking`,
        );
        return false;
      }
      getLogger().info(
        `Barge-in confirmed by strategy on transcript ${sanitizeLogValue(
          transcript.text.slice(0, 40),
        )}`,
      );
    }
    this.runBargeInCancel(transcript.text);
    return true;
  }

  /**
   * Synchronous wrapper that callers in legacy code paths can keep using.
   * When ``bargeInStrategies`` is empty the work is fully synchronous and
   * the result is correct. With strategies the call is dispatched as a
   * floating promise — non-confirmed transcripts simply skip the cancel
   * and the legacy boolean return is meaningless under that opt-in path.
   */
  private handleBargeIn(transcript: {
    text?: string;
    isFinal?: boolean;
    speechFinal?: boolean;
  }): boolean {
    if (!transcript.text || !this.isSpeaking) return false;
    // Echo guard FIRST — before the tail-grace rescue: the grace window
    // (~1.5 s after TTS) is exactly when the agent's final-sentence echo
    // arrives via STT. Running the rescue first treated that echo as "the
    // next turn", flipped isSpeaking off, and commitTranscript's
    // isSpeaking-gated echo check could no longer fire — the agent answered
    // its own words as a phantom user turn. Mirrors the Python fix. Also
    // active while output is PAUSED (pause_resume forwards mic audio during
    // the confirm window and the just-cleared audio's echo tail can lag
    // into it).
    if (
      (this.forwardSttWhileSpeaking || this.outputPaused) &&
      looksLikeEcho(transcript.text, this.currentAgentSpokenText)
    ) {
      getLogger().info(
        `Barge-in suppressed: transcript matches agent's own speech (echo) — ${sanitizeLogValue(
          transcript.text.slice(0, 40),
        )}`,
      );
      return false;
    }
    if (this.tailGraceActive) {
      // Tail-grace transcript = next turn, not a barge-in. End the grace and
      // let the transcript dispatch normally (parity with the async path).
      this.endTailGraceForNewTurn();
      return false;
    }
    // Pause-and-resume final-only gate (parity with handleBargeInAsync).
    if (this.outputPaused && !this.passesPausedKillFilters(transcript)) {
      getLogger().debug(
        `Paused turn: transcript ${sanitizeLogValue(
          transcript.text.slice(0, 40),
        )} cannot confirm the kill (interim/hallucination/duplicate) — awaiting resume timer`,
      );
      return false;
    }
    if (this.bargeInStrategies.length === 0) {
      // Legacy synchronous path — preserve exact byte-for-byte behaviour
      // for users who haven't opted into the confirm pipeline.
      if (!this.canBargeIn()) {
        getLogger().info(
          `Barge-in transcript suppressed (agent speaking < gate, aec=${this.aec ? 'on' : 'off'})`,
        );
        return false;
      }
      this.runBargeInCancel(transcript.text);
      return true;
    }
    // Opt-in confirm path is async; fire-and-forget. The cancel inside
    // ``runBargeInCancel`` flips ``isSpeaking`` synchronously once it
    // resolves, which is what downstream loops actually observe.
    void this.handleBargeInAsync(transcript).catch((err) =>
      getLogger().debug(`handleBargeInAsync threw: ${String(err)}`),
    );
    return false;
  }

  /**
   * Run the cancel/flush sequence for a confirmed barge-in. Shared by
   * the legacy synchronous path and the strategy-confirmed async path.
   */
  private runBargeInCancel(transcriptText: string): void {
    // Speech-event: agent stop edge — interrupted by the caller.
    void this.emitAgentSpeechEnded(true).catch(() => {});
    // Capture pending state BEFORE clearPendingBargeIn() drops it — if VAD
    // already started the overlap window via ``startPendingBargeIn`` we MUST
    // NOT call ``recordOverlapStart`` again (that would overwrite T1 with
    // T2 and produce a near-zero ``InterruptionMetrics.detection_delay_ms``
    // on the strategy path).
    const hadPending = this.bargeInPendingSince !== null;
    this.clearPendingBargeIn();
    getLogger().debug(
      `Barge-in: caller spoke over agent (${sanitizeLogValue(transcriptText.slice(0, 40))})`,
    );
    if (!hadPending) {
      // Legacy path or VAD never fired — start the overlap window now.
      this.metricsAcc.recordOverlapStart();
    }
    this.metricsAcc.recordBargeinDetected();
    const bargeinSpan = startSpan(SPAN_BARGEIN, { 'patter.call.id': this.callId });
    try {
      // Post-complete barge-in during the buffered tail — rewrite history to
      // the heard prefix BEFORE cancelSpeaking resets the playback cursor
      // (or, for a paused turn, while the frozen pause cursor still holds).
      this.maybeTruncateCompletedTurnHistory();
      // Pause-and-resume: a kill while paused discards the held buffers
      // (queued sentences + retained audio) and wakes any pause-decision
      // waiter, which then observes the interrupt.
      this.discardPauseState();
      this.cancelSpeaking();
      try {
        this.deps.bridge.sendClear(this.ws, this.streamSid);
      } catch (err) {
        getLogger().debug(`sendClear during barge-in failed: ${String(err)}`);
      }
      this.metricsAcc.recordTtsStopped();
      this.metricsAcc.recordTurnInterrupted();
      // Re-anchor turn metrics to the legitimate VAD speech_start so post-
      // barge-in latency anchors don't carry over from the interrupted turn.
      this.metricsAcc.anchorUserSpeechStart();
      this.metricsAcc.recordOverlapEnd(true);
    } finally {
      try {
        bargeinSpan.end();
      } catch {
        // Swallow.
      }
    }
  }

  /** Mark a VAD-detected barge-in as pending (no cancel yet). */
  private startPendingBargeIn(): void {
    if (this.bargeInPendingSince !== null) return;
    this.bargeInPendingSince = Date.now();
    this.metricsAcc.recordOverlapStart();
    getLogger().info(
      'Barge-in PENDING (VAD speech_start during TTS); awaiting strategy confirmation',
    );
    this.bargeInPendingTimer = setTimeout(() => {
      if (this.bargeInPendingSince === null) return;
      getLogger().info(
        `Pending barge-in timed out after ${this.bargeInConfirmMs}ms; agent resumes (no strategy confirmed)`,
      );
      this.metricsAcc.recordOverlapEnd(false);
      // Clear any anchors that drifted during the pending barge-in window.
      this.metricsAcc.anchorUserSpeechStart();
      this.bargeInPendingSince = null;
      this.bargeInPendingTimer = null;
    }, this.bargeInConfirmMs);
  }

  /** Drop pending state without cancelling — used on confirm and on
   * agent stop. Idempotent. */
  private clearPendingBargeIn(): void {
    if (this.bargeInPendingTimer !== null) {
      clearTimeout(this.bargeInPendingTimer);
      this.bargeInPendingTimer = null;
    }
    this.bargeInPendingSince = null;
  }

  // ---------------------------------------------------------------------------
  // Pause-and-resume false-interruption handling (bargeInMode: 'pause_resume').
  // PAUSE output on VAD speech_start, KILL on a committed final
  // transcript within the confirm window, RESUME from the first
  // not-fully-heard sentence otherwise. Mirrors Python `_start_pause_resume`.
  // ---------------------------------------------------------------------------

  /** Retained-audio cap in bytes for the active TTS chunk format (mirrors
   * the bytes-per-ms logic of ``trackOutboundPlayback``). */
  private pauseRetainedCapBytes(): number {
    const bytesPerMs =
      this.ttsOutputFormatNativeForCarrier &&
      this.deps.bridge.telephonyProvider !== 'telnyx'
        ? 8
        : 32;
    return StreamHandler.PAUSE_RESUME_MAX_RETAINED_S * 1000 * bytesPerMs;
  }

  /**
   * Whether a VAD ``speech_start`` during the agent's turn should take the
   * pause-and-resume path instead of cancel/pending. Requires
   * ``bargeInMode: 'pause_resume'`` AND resumable state: a dispatch in
   * flight (the sentence/TTS loops honour the pause gate) or retained
   * sentence audio from a just-completed turn still playing out of the
   * carrier buffer. The firstMessage paced sender keeps today's
   * immediate-cancel behaviour (its prewarm-bytes path has no retained
   * sentences to resume from — known limitation).
   */
  private shouldPauseForBargeIn(): boolean {
    if (this.bargeInMode !== 'pause_resume') return false;
    if (this.pauseResumeOverflowed) return false;
    if (this.outputPaused) return true; // already paused — stay on the path
    if (this.dispatchTask !== null) return true;
    return this.turnSentenceAudio.length > 0;
  }

  /**
   * Resume offset at SENTENCE granularity: the first sentence (into
   * ``turnSpokenSegments`` / ``turnSentenceAudio``) whose playback had NOT
   * completed when the pause landed — computed from the #164
   * playback-cursor bookkeeping (``heard = totalPushed - carrierBacklog``).
   * Granularity choice: the partially-played sentence is replayed from its
   * start (mark/clear bookkeeping is per-sentence and a clipped sentence
   * restarted at its boundary sounds like a natural repair), rather than
   * resumed mid-word at a byte offset.
   */
  private computePauseResumePoint(): { index: number; heardMs: number } {
    const segments = this.turnSpokenSegments;
    const totalMs = this.turnPlaybackTotalMs;
    const remainingMs = Math.max(0, this.playbackBufferedUntil - Date.now());
    const heardMs = Math.max(0, totalMs - remainingMs);
    let index = segments.length;
    for (let i = segments.length - 1; i >= 0; i--) {
      const endMs = i + 1 < segments.length ? segments[i + 1].startMs : totalMs;
      if (endMs > heardMs + 1e-6) index = i;
      else break;
    }
    return { index, heardMs };
  }

  /**
   * PAUSE the agent's output on a VAD ``speech_start`` (pause_resume
   * mode): gate further sends on ``outputPaused``, ``sendClear`` the
   * carrier so queued audio stops quickly, and schedule the
   * false-interruption resume timer. The LLM stream and the TTS provider
   * stream are deliberately NOT cancelled — tokens keep buffering as
   * sentences and synthesized audio queues in memory (both bounded) so a
   * resume can pick up seamlessly.
   */
  private startPauseResume(): void {
    if (this.outputPaused) return;
    // Anchor the overlap window exactly like ``startPendingBargeIn`` so a
    // kill records detection_delay from VAD-T1 (never restarted).
    if (this.bargeInPendingSince === null) {
      this.bargeInPendingSince = Date.now();
      this.metricsAcc.recordOverlapStart();
    }
    // A stale strategy-pending timer is superseded by the pause timer.
    if (this.bargeInPendingTimer !== null) {
      clearTimeout(this.bargeInPendingTimer);
      this.bargeInPendingTimer = null;
    }
    this.outputPaused = true;
    let resolveFn: () => void = () => undefined;
    const promise = new Promise<void>((resolve) => {
      resolveFn = resolve;
    });
    this.pauseDecision = { promise, resolve: resolveFn };
    // Freeze the playback bookkeeping at the heard offset: the clear below
    // drops the carrier backlog, so anything pushed beyond the heard cursor
    // is void. A kill that follows then computes the heard prefix from this
    // frozen state; a resume re-advances it as the tail is re-sent.
    const { index, heardMs } = this.computePauseResumePoint();
    this.pauseResumeIndex = index;
    this.turnPlaybackTotalMs = heardMs;
    this.playbackBufferedUntil = 0;
    // The phase-1 grace wait (carrier backlog) is void after the clear;
    // resume re-arms it for the re-sent tail.
    this.clearGraceTimer();
    this.drainPendingMarks();
    getLogger().info(
      `Barge-in PAUSE (VAD speech_start during TTS); resuming from sentence ` +
        `${index} unless a transcript confirms within ${this.bargeInConfirmMs}ms`,
    );
    try {
      this.deps.bridge.sendClear(this.ws, this.streamSid);
    } catch (err) {
      getLogger().debug(`sendClear during pause failed: ${String(err)}`);
    }
    // Output is silent from here — flush the self-hearing ring so STT
    // receives the user's leading words and can produce the confirming
    // transcript (or nothing, for a cough). ``handleAudio`` forwards
    // subsequent frames to STT while paused for the same reason.
    this.flushInboundAudioRing();
    this.bargeInPendingTimer = setTimeout(() => {
      this.bargeInPendingTimer = null;
      if (!this.outputPaused) return;
      this.resumeAfterFalseInterruption();
    }, this.bargeInConfirmMs);
  }

  /**
   * RESUME output after a pause that no transcript confirmed. Re-sends the
   * cleared-but-unheard tail from the retained sentence audio (sentence
   * granularity, no TTS re-billing), unpauses the live send path, and
   * records the event as a FALSE interruption: the overlap closes via
   * ``recordOverlapEnd(false)`` (the backchannel counter — the
   * interruption count is NOT incremented) and the turn is never marked
   * interrupted.
   */
  private resumeAfterFalseInterruption(): void {
    if (!this.outputPaused) return;
    const entries = this.turnSentenceAudio;
    const idx = Math.max(0, Math.min(this.pauseResumeIndex, entries.length));
    const tail = entries.slice(idx);
    // Drop the stale segment stamps of the sentences about to be replayed
    // — the replay re-stamps them at their new positions on the
    // (frozen-then-resumed) playback timeline, so a later barge-in still
    // maps to an accurate heard prefix without duplicates.
    this.turnSpokenSegments.splice(idx);
    for (const entry of tail) entry.sent = 0;
    // False interruption — the backchannel path. Mirrors the pending
    // barge-in timeout.
    this.metricsAcc.recordOverlapEnd(false);
    this.metricsAcc.anchorUserSpeechStart();
    this.bargeInPendingSince = null;
    getLogger().info(
      `False interruption: no confirming transcript within ` +
        `${this.bargeInConfirmMs}ms — resuming ${tail.length} retained sentence(s)`,
    );
    this._eventBus.emit('false_interruption', { resumedSentences: tail.length });
    // Re-send the unheard tail BEFORE unpausing so the in-flight synthesis
    // (which queues while paused) cannot interleave a newer chunk ahead of
    // the replayed audio.
    for (const entry of tail) {
      if (!this.isSpeaking) break;
      // Sentence boundary — drop any stale PCM16 alignment carry, the same
      // contract ``synthesizeSentence`` keeps per sentence.
      this.resetTtsCarry();
      this.drainSentenceEntry(entry, true);
    }
    this.outputPaused = false;
    // Close the unpause race: a chunk queued between the last drain and the
    // flag flip would otherwise wait for the next live chunk.
    if (tail.length > 0 && this.isSpeaking) {
      this.drainSentenceEntry(tail[tail.length - 1], true);
    }
    const decision = this.pauseDecision;
    this.pauseDecision = null;
    if (decision) decision.resolve();
    // Post-complete turn (carrier was draining the buffered tail when the
    // pause landed): the turn body already finished pushing — its grace
    // timer was cancelled at pause time — so re-arm the grace machinery for
    // the re-sent backlog: phase-1 hold keeps barge-in armed for the whole
    // audible window, exactly as #164. A turn still in flight arms it
    // itself in its ``finally``.
    if (this.turnOutputDone && this.isSpeaking) {
      this.endSpeakingWithGrace();
    }
  }

  /** Drop all pause-and-resume state (flags + buffers) and wake any
   * pause-decision waiter. Used by the kill path, fresh turns, and
   * teardown. Idempotent. */
  private discardPauseState(): void {
    this.outputPaused = false;
    this.pauseResumeIndex = 0;
    this.pausedSentences = [];
    this.turnSentenceAudio = [];
    this.pauseRetainedBytes = 0;
    const decision = this.pauseDecision;
    this.pauseDecision = null;
    if (decision) decision.resolve();
  }

  /** Block until the in-flight pause resolves. ``true`` → resumed (keep
   * speaking); ``false`` → killed (turn interrupted). Bounded: fails open
   * past the confirm window plus margin (the resume timer guarantees a
   * decision; the margin covers teardown races). Mirrors Python
   * ``_await_pause_decision``. */
  private async awaitPauseDecision(): Promise<boolean> {
    while (this.outputPaused && this.isSpeaking) {
      const decision = this.pauseDecision;
      if (decision === null) break;
      const timedOut = await new Promise<boolean>((resolve) => {
        const timer = setTimeout(
          () => resolve(true),
          this.bargeInConfirmMs + 5000,
        );
        void decision.promise.then(() => {
          clearTimeout(timer);
          resolve(false);
        });
      });
      if (timedOut) {
        getLogger().debug('pause decision wait timed out — failing open');
        break;
      }
    }
    return this.isSpeaking;
  }

  /** While paused, buffer ``sentence`` (pre-guardrail text) for the resume
   * drain and return ``true``; return ``false`` when not paused (caller
   * synthesizes normally). Overflow degrades to a full cancel — the
   * bounded buffer is a memory-safety valve, not a speech queue. */
  private bufferSentenceIfPaused(sentence: string): boolean {
    if (!this.outputPaused) return false;
    if (this.pausedSentences.length >= StreamHandler.PAUSE_MAX_BUFFERED_SENTENCES) {
      getLogger().warn(
        `pause_resume sentence buffer overflow (${this.pausedSentences.length}) — degrading to full cancel`,
      );
      this.runBargeInCancel('<pause_resume sentence-buffer overflow>');
      return true; // handled; the loop observes !isSpeaking next
    }
    this.pausedSentences.push(sentence);
    return true;
  }

  /** Pop-and-return every sentence buffered during the pause. */
  private releasePausedSentences(): string[] {
    if (this.pausedSentences.length === 0) return [];
    const out = this.pausedSentences;
    this.pausedSentences = [];
    return out;
  }

  /** Open a retention entry for a response sentence (pause_resume mode
   * only — returns ``null`` otherwise, keeping the legacy send path
   * byte-identical). Filler / error-fallback audio is never retained
   * (``recordSegment=false`` callers skip this). */
  private beginRetainedSentence(
    text: string,
  ): { text: string; chunks: Buffer[]; sent: number } | null {
    if (this.bargeInMode !== 'pause_resume') return null;
    if (this.pauseResumeOverflowed) return null;
    const entry = { text, chunks: [] as Buffer[], sent: 0 };
    this.turnSentenceAudio.push(entry);
    return entry;
  }

  /** Append ``chunk`` to the sentence's retention entry, enforcing the
   * retained-audio cap. Returns ``true`` when retained; ``false`` on
   * overflow (paused → the turn was just killed; speaking → retention was
   * released and the caller falls back to direct sends). */
  private retainPauseChunk(
    entry: { text: string; chunks: Buffer[]; sent: number },
    chunk: Buffer,
  ): boolean {
    entry.chunks.push(chunk);
    this.pauseRetainedBytes += chunk.length;
    if (this.pauseRetainedBytes <= this.pauseRetainedCapBytes()) return true;
    if (this.outputPaused) {
      getLogger().warn(
        `pause_resume retained-audio cap (${StreamHandler.PAUSE_RESUME_MAX_RETAINED_S}s) ` +
          'exceeded while paused — degrading to full cancel',
      );
      this.runBargeInCancel('<pause_resume audio-buffer overflow>');
    } else {
      getLogger().info(
        `pause_resume retained-audio cap (${StreamHandler.PAUSE_RESUME_MAX_RETAINED_S}s) ` +
          'exceeded — disabling pause-resume for this turn (legacy cancel applies)',
      );
      this.pauseResumeOverflowed = true;
      this.pauseRetainedBytes = 0;
      for (const e of this.turnSentenceAudio) {
        e.chunks = [];
        e.sent = 0;
      }
    }
    return false;
  }

  /**
   * Send every not-yet-sent chunk of a retention entry to the carrier
   * (claim-then-send so concurrent drains can never double-send). Stamps
   * the sentence's heard-prefix segment at its first sent chunk — a replay
   * (``sent`` reset to 0) re-stamps at the new timeline position.
   * ``force=true`` bypasses the pause gate (resume path only).
   */
  private drainSentenceEntry(
    entry: { text: string; chunks: Buffer[]; sent: number },
    force = false,
  ): void {
    while (entry.sent < entry.chunks.length) {
      if (!this.isSpeaking) return;
      if (this.outputPaused && !force) return;
      const idx = entry.sent;
      entry.sent = idx + 1;
      const chunk = entry.chunks[idx];
      if (idx === 0) {
        this.turnSpokenSegments.push({
          text: entry.text,
          startMs: this.turnPlaybackTotalMs,
        });
      }
      // Far-end tap mirrors the direct send path: SKIPPED on the
      // carrier-native fast path where these are mulaw wire bytes that
      // would corrupt the int16-PCM-16k AEC reference.
      if (this.aec && !this.ttsOutputFormatNativeForCarrier) this.aec.pushFarEnd(chunk);
      const encoded = this.encodePipelineAudio(chunk);
      this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
      this.trackOutboundPlayback(chunk.length);
      this.markFirstAudioSent();
    }
  }

  /** Whether a transcript may KILL a paused turn: it must be a committed
   * FINAL (interims cannot confirm), not a known STT hallucination, and
   * not a duplicate of the last committed utterance — the same filter
   * family ``commitTranscript`` applies, evaluated without consuming its
   * dedup state (the transcript still flows on to ``commitTranscript`` to
   * become the user's next turn). */
  private passesPausedKillFilters(transcript: {
    text?: string;
    isFinal?: boolean;
    speechFinal?: boolean;
  }): boolean {
    if (transcript.isFinal !== true && transcript.speechFinal !== true) {
      return false;
    }
    const normalised = (transcript.text ?? '').trim().toLowerCase();
    const stripped = normalised.replace(/[.,!?;: ]+$/, '').trim();
    if (HALLUCINATIONS.has(stripped) || stripped === '') return false;
    if (
      normalised === this.lastCommitText &&
      Date.now() - this.lastCommitAt < 2000
    ) {
      return false;
    }
    return true;
  }

  /**
   * Dedup + throttle + hallucination filter for final STT transcripts.
   * Mirrors ``PipelineStreamHandler._stt_loop`` on the Python side.
   * Returns ``true`` when the transcript should be committed to a turn,
   * ``false`` when it must be dropped. Drop reasons:
   *   - text matches common short hallucinations ("you", "thanks", ...)
   *   - duplicate final within 2 s of previous commit
   *   - back-to-back finals under 500 ms (too tight to be real utterances)
   */
  private commitTranscript(text: string): boolean {
    const now = Date.now();
    const normalised = text.trim().toLowerCase();
    const stripped = normalised.replace(/[.,!?;: ]+$/, '').trim();
    const sinceLastMs = now - this.lastCommitAt;
    if (HALLUCINATIONS.has(stripped) || stripped === '') {
      getLogger().debug(`Dropped likely STT hallucination: ${sanitizeLogValue(normalised.slice(0, 40))}`);
      return false;
    }
    // Echo guard: while the agent is still speaking (the forward-STT echo
    // window — or a pause_resume confirm window, which forwards mic audio
    // to STT while the agent formally holds the floor), a transcript that
    // matches the agent's own speech is its TTS bleeding back into STT,
    // not a user turn. Gated on isSpeaking so a real post-turn reply
    // (committed when idle) is never dropped, and the default VAD path is
    // unaffected. Parity with Python.
    if (
      (this.forwardSttWhileSpeaking || this.outputPaused) &&
      this.isSpeaking &&
      looksLikeEcho(text, this.currentAgentSpokenText)
    ) {
      getLogger().debug(
        `Dropped agent-echo transcript (not a user turn): ${sanitizeLogValue(normalised.slice(0, 40))}`,
      );
      return false;
    }
    if (sinceLastMs < 2000 && normalised === this.lastCommitText) {
      getLogger().debug(
        `Dropped duplicate final transcript (${(sinceLastMs / 1000).toFixed(1)}s since last): ${sanitizeLogValue(normalised.slice(0, 40))}`,
      );
      return false;
    }
    // Back-to-back: drop a NEAR-DUPLICATE within 500 ms (Deepgram emitting
    // speech_final then is_final for the SAME utterance). A genuinely DIFFERENT
    // fast follow-up must NOT be swallowed — dropping it unconditionally left
    // an empty [interrupted] turn before this fix. Parity with Python.
    if (sinceLastMs < 500 && isNearDuplicate(normalised, this.lastCommitText)) {
      getLogger().debug(
        `Dropped back-to-back near-duplicate final (${(sinceLastMs / 1000).toFixed(2)}s since last): ${sanitizeLogValue(normalised.slice(0, 40))}`,
      );
      return false;
    }
    this.lastCommitText = normalised;
    this.lastCommitAt = now;
    return true;
  }

  // ---------------------------------------------------------------------------
  // PREEMPTIVE GENERATION (opt-in) — speculative dispatch on a confident
  // interim transcript; commit-or-discard at end of utterance. Mirrors Python
  // ``_note_interim_transcript`` / ``_try_release_speculation``.
  // ---------------------------------------------------------------------------

  /**
   * Whether a speculative dispatch may start right now. Built-in LLM loop
   * only (an ``onMessage`` handler may have external side effects per
   * invocation, so it is never run speculatively), and only while the agent
   * is idle: not speaking (an interim during agent speech is barge-in
   * material, not a next turn) and no turn dispatch in flight
   * (single-in-flight contract). Parity with Python ``_can_speculate``.
   */
  private canSpeculate(): boolean {
    if (!this.preemptiveEnabled) return false;
    if (this.deps.onMessage || !this.llmLoop) return false;
    if (this.isSpeaking) return false;
    return this.dispatchTask === null;
  }

  /**
   * Read-only mirror of the ``commitTranscript`` filters: a candidate
   * interim must pass the same hallucination / echo / duplicate checks a
   * final would face at commit time — otherwise we would speculate on text
   * whose final is guaranteed to be dropped. Never mutates the dedup state.
   * Parity with Python ``_speculation_input_ok``.
   */
  private speculationInputOk(text: string): boolean {
    const normalised = text.trim().toLowerCase();
    const stripped = normalised.replace(/[.,!?;: ]+$/, '').trim();
    if (HALLUCINATIONS.has(stripped) || stripped === '') return false;
    if (
      this.forwardSttWhileSpeaking &&
      this.isSpeaking &&
      looksLikeEcho(text, this.currentAgentSpokenText)
    ) {
      return false;
    }
    // The matching final would be dropped as a duplicate at commit time.
    const sinceLastMs = Date.now() - this.lastCommitAt;
    if (sinceLastMs < 2000 && normalised === this.lastCommitText) return false;
    return true;
  }

  /**
   * Track an interim transcript and start a speculation when it qualifies:
   * (a) it ends with sentence-final punctuation (immediate), or (b) it has
   * been unchanged for ``preemptiveMinStableMs`` (one-shot stability timer).
   * No-op when preemptive generation is disabled or the handler cannot
   * speculate right now. Parity with Python ``_note_interim_transcript``.
   */
  private async noteInterimTranscript(text: string): Promise<void> {
    if (!this.preemptiveEnabled) return;
    const norm = normalizeForEcho(text);
    if (!norm) return;
    const spec = this.speculation;
    if (spec !== null && spec.normText === norm && !spec.failed) {
      return; // already speculating on this exact utterance
    }
    if (!this.canSpeculate()) {
      this.clearInterimStabilityTimer();
      this.interimNorm = '';
      return;
    }
    if (!this.speculationInputOk(text)) return;
    if (endsWithSentenceFinalPunct(text)) {
      // High-confidence interim — speculate immediately (replacing any
      // stale speculation on older text). Awaited so the replaced
      // speculation is fully unwound before the new one registers.
      this.clearInterimStabilityTimer();
      this.interimNorm = norm;
      this.interimText = text;
      await this.startSpeculation(text).catch((err) =>
        getLogger().debug(`startSpeculation threw: ${String(err)}`),
      );
      return;
    }
    if (norm !== this.interimNorm) {
      // Text changed — restart the stability window.
      this.interimNorm = norm;
      this.interimText = text;
      this.clearInterimStabilityTimer();
      if (this.preemptiveMinStableMs <= 0) {
        await this.startSpeculation(text).catch((err) =>
          getLogger().debug(`startSpeculation threw: ${String(err)}`),
        );
        return;
      }
      this.interimStableTimer = setTimeout(() => {
        this.interimStableTimer = null;
        if (this.interimNorm !== norm) return; // a newer interim superseded it
        const current = this.speculation;
        if (current !== null && current.normText === norm && !current.failed) return;
        if (!this.canSpeculate() || !this.speculationInputOk(this.interimText)) return;
        void this.startSpeculation(this.interimText).catch((err) =>
          getLogger().debug(`stability-triggered speculation threw: ${String(err)}`),
        );
      }, this.preemptiveMinStableMs);
    }
  }

  /** Cancel the pending interim-stability timer, if any. Idempotent. */
  private clearInterimStabilityTimer(): void {
    if (this.interimStableTimer !== null) {
      clearTimeout(this.interimStableTimer);
      this.interimStableTimer = null;
    }
  }

  /** Drop interim-stability state — called once a final commits (the
   * utterance is decided) and on teardown. */
  private resetInterimTracking(): void {
    this.clearInterimStabilityTimer();
    this.interimNorm = '';
    this.interimText = '';
  }

  /**
   * Launch a speculative dispatch for ``interimText``, replacing (and
   * counting as a miss) any previous speculation on different text. The
   * task's rejection handler is attached at creation (same contract as
   * ``dispatchTask``). Parity with Python ``_start_speculation``.
   */
  private async startSpeculation(interimText: string): Promise<void> {
    await this.abortSpeculation('replaced_by_newer_interim');
    if (this.speculation !== null) {
      // A concurrent path (stability timer vs. drain loop) registered a
      // NEWER speculation while we awaited the old one's unwind — keep it.
      // Overwriting here would orphan its task parked on the commit
      // decision forever. Parity with Python ``_start_speculation``.
      return;
    }
    const spec = new SpeculativeTurn(interimText);
    this.speculation = spec;
    spec.task = this.runSpeculativeDispatch(spec).catch((err) => {
      getLogger().error('Preemptive: speculative dispatch rejected:', err);
    });
    getLogger().debug(
      `Preemptive: speculation started on interim ${sanitizeLogValue(interimText.slice(0, 60))}`,
    );
  }

  /**
   * Discard the current speculation (if any): signal abort, await the task's
   * unwind (bounded — JS promises are not cancellable, so a provider that
   * ignores the signal must not block the caller), and count a miss unless
   * this is teardown. The speculative task never touched history / carrier /
   * per-turn metrics, so there is nothing to roll back. Idempotent. Parity
   * with Python ``_abort_speculation``.
   */
  private async abortSpeculation(reason: string, countMiss = true): Promise<void> {
    const spec = this.speculation;
    if (spec === null) return;
    // Deregister synchronously so a concurrent commit cannot release a
    // speculation that is already being torn down.
    this.speculation = null;
    spec.failed = true;
    try {
      spec.abort.abort();
    } catch {
      // Defensive — abort() throws nothing in modern runtimes.
    }
    // Wake a task parked on the commit decision; ``released`` stays false so
    // it unwinds as a discard.
    spec.signalDecision();
    if (spec.task) {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const cap = new Promise<void>((resolve) => {
        timer = setTimeout(resolve, 5_000);
      });
      try {
        await Promise.race([spec.task.catch(() => {}), cap]);
      } finally {
        if (timer) clearTimeout(timer);
      }
    }
    if (countMiss) this.metricsAcc.recordPreemptiveMiss();
    getLogger().debug(`Preemptive: speculation discarded (${reason})`);
  }

  /**
   * Self-abort from WITHIN the speculative task (LLM error, buffer overflow,
   * afterTranscribe veto). Marks the speculation unreleasable and
   * deregisters it so the commit path dispatches normally. Never awaits (the
   * caller IS the task). Parity with Python ``_fail_speculation_inline``.
   */
  private failSpeculationInline(spec: SpeculativeTurn, reason: string): void {
    spec.failed = true;
    try {
      spec.abort.abort();
    } catch {
      // Defensive.
    }
    spec.signalDecision();
    if (this.speculation === spec) this.speculation = null;
    this.metricsAcc.recordPreemptiveMiss();
    getLogger().debug(`Preemptive: speculation failed (${reason})`);
  }

  /**
   * Commit-time decision for the in-flight speculation. Returns ``true``
   * when the speculation was RELEASED — the caller must NOT dispatch a
   * normal turn (the speculative task is now the live turn, tracked via
   * ``dispatchTask``). Returns ``false`` when there was no usable
   * speculation (none in flight, failed, or mismatched — the mismatch is
   * discarded here) and the normal dispatch must run.
   *
   * On release, the commit-point bookkeeping the normal path performs in
   * ``processTranscript`` happens HERE — the ``onTranscript`` callback, the
   * conversation-history user push (final transcript text), and the
   * turn-committed metric anchors (so TTFT/latency reflect user-perceived
   * timing from the REAL final-transcript commit) — exactly once per turn.
   * Parity with Python ``_try_release_speculation``.
   */
  private async tryReleaseSpeculation(finalText: string): Promise<boolean> {
    const spec = this.speculation;
    if (spec === null) return false;
    if (
      spec.failed ||
      spec.abort.signal.aborted ||
      !speculationMatches(spec.interimText, finalText)
    ) {
      await this.abortSpeculation('final_mismatch');
      return false;
    }

    // ---- RELEASE ----
    this.speculation = null;
    spec.finalText = finalText;
    // Point the live cancel machinery at the speculative stream so the
    // existing barge-in paths (``cancelSpeaking`` aborts ``llmAbort``) tear
    // it down exactly like a normal turn's stream.
    this.llmAbort = spec.abort;
    this.metricsAcc.recordPreemptiveHit();
    getLogger().debug(`User (${this.deps.bridge.label} pipeline): ${sanitizeLogValue(finalText)}`);

    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'user',
        text: finalText,
        call_id: this.callId,
        history: [...this.history.entries],
      });
    }
    // History/transcript record the FINAL transcript text as the user
    // message (the LLM consumed the matching interim — normalized-equal by
    // definition of the release gate).
    this.history.push({ role: 'user', text: finalText, timestamp: Date.now() });
    this.metricsAcc.recordOnUserTurnCompletedDelay(0);
    this.metricsAcc.recordTurnCommitted();
    // Released turns return before processTranscript's commit bookkeeping:
    // perform the semantic turn-detection cleanup and the committed-EOS
    // speech event here too, so combining ``preemptiveGeneration`` with
    // ``turnDetector`` neither leaks a stale stamped trigger into the next
    // turn nor skips the EOS event. Parity with Python
    // ``_try_release_speculation``.
    if (this.deps.agent.turnDetector) {
      this.cancelSemanticHold();
      this.resetSemanticWindow();
    }
    await this.emitUserSpeechEos(finalText);

    // Settle the previous turn first (single-in-flight) — fast no-op, a
    // speculation can only have started while no dispatch was in flight.
    await this.dispatchTask?.catch(() => {});
    spec.released = true;
    spec.signalDecision();
    // The speculative task is now the live turn.
    this.dispatchTask = spec.task;
    getLogger().info(
      `Preemptive: speculation RELEASED on matching final ${sanitizeLogValue(finalText.slice(0, 60))}`,
    );
    return true;
  }

  /** Playout duration (ms) of the audio a speculation has buffered so far.
   * Same bytes-per-ms model as ``trackOutboundPlayback``. */
  private specBufferMs(spec: SpeculativeTurn): number {
    const bytesPerMs =
      this.ttsOutputFormatNativeForCarrier &&
      this.deps.bridge.telephonyProvider !== 'telnyx'
        ? 8
        : 32;
    return spec.bufferedBytes / bytesPerMs;
  }

  /** Push one (already hook-processed) audio chunk of a RELEASED speculation
   * to the carrier — the same per-chunk bookkeeping ``synthesizeSentence``
   * performs on the live path. */
  private async specSendChunk(spec: SpeculativeTurn, processedAudio: Buffer): Promise<void> {
    if (!spec.ttsFirstByteSent.value) {
      spec.ttsFirstByteSent.value = true;
      this.metricsAcc.recordTtsFirstByte();
      await this.emitAudioOut();
    }
    // Far-end tap mirrors the direct send path: SKIPPED on the
    // carrier-native fast path where these are mulaw wire bytes that
    // would corrupt the int16-PCM-16k AEC reference.
    if (this.aec && !this.ttsOutputFormatNativeForCarrier) {
      this.aec.pushFarEnd(processedAudio);
    }
    const encoded = this.encodePipelineAudio(processedAudio);
    this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
    this.trackOutboundPlayback(processedAudio.length);
    this.markFirstAudioSent();
  }

  /**
   * Idempotent release flush: take the floor (``beginSpeaking``), stamp the
   * post-commit LLM markers, and stream every buffered sentence to the
   * carrier in order. After this the speculative task continues as a plain
   * live turn. No-op until the speculation is released. Parity with Python
   * ``_spec_ensure_flushed``.
   */
  private async specEnsureFlushed(spec: SpeculativeTurn): Promise<void> {
    if (spec.flushed || !spec.released) return;
    spec.flushed = true;
    await this.beginSpeaking();
    // Post-commit metric markers: the user-perceived TTFT for a released
    // speculation is "final commit → audio", so the first-token /
    // first-sentence stamps are recorded NOW (after ``recordTurnCommitted``)
    // rather than back when the speculative stream actually produced them.
    if (spec.responseParts.length > 0) {
      if (!spec.llmFirstTokenRecorded) {
        spec.llmFirstTokenRecorded = true;
        this.metricsAcc.recordLlmFirstToken();
        await this.emitLlmFirstToken();
      }
      // Echo-guard reference for barge-in comparisons during the live
      // continuation (``beginSpeaking`` reset it).
      this.currentAgentSpokenText = spec.responseParts.join('');
    }
    if (spec.buffered.length > 0) {
      this.metricsAcc.recordLlmFirstSentenceComplete();
    }
    for (const { text, chunks } of spec.buffered) {
      if (chunks.length === 0) continue;
      // Per-sentence carry reset, mirroring ``synthesizeSentence``.
      this.resetTtsCarry();
      let recordSegment = true;
      for (const audio of chunks) {
        if (!this.isSpeaking) {
          // Barge-in landed mid-flush — stop exactly like the live
          // sentence loop would.
          spec.interrupted = true;
          spec.buffered = [];
          return;
        }
        if (recordSegment) {
          this.turnSpokenSegments.push({ text, startMs: this.turnPlaybackTotalMs });
          recordSegment = false;
        }
        await this.specSendChunk(spec, audio);
      }
      this.resetTtsCarry();
    }
    spec.buffered = []; // release the held memory
  }

  /**
   * Synthesize one sentence of an UNRELEASED speculation, holding the audio
   * in ``spec.buffered``. Transitions to live sending mid-sentence the
   * moment the release lands. Returns ``false`` when the speculation must
   * stop (aborted, overflow, or barge-in after a mid-sentence release).
   * Parity with Python ``_spec_synthesize_buffered``.
   */
  private async specSynthesizeBuffered(
    spec: SpeculativeTurn,
    sentence: string,
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
  ): Promise<boolean> {
    if (!this.tts) {
      // No TTS configured — nothing audible to hold; still track the
      // sentence so the released turn records it (parity with the live
      // path, which is also silent without TTS).
      spec.buffered.push({ text: sentence, chunks: [] });
      return true;
    }
    let transformed = sentence;
    for (const fn of this.deps.agent.textTransforms ?? []) {
      transformed = fn(transformed);
    }
    const processedText = await hookExecutor.runBeforeSynthesize(transformed, hookCtx);
    if (processedText === null) return true; // hook skipped this sentence

    const chunks: Buffer[] = [];
    // Register BEFORE synthesis so a mid-sentence release flushes the
    // partial chunks collected so far in order.
    spec.buffered.push({ text: processedText, chunks });
    try {
      for await (const chunk of this.tts.synthesizeStream(processedText)) {
        if (spec.abort.signal.aborted && !spec.released) return false;
        const processedAudio = await hookExecutor.runAfterSynthesize(chunk, processedText, hookCtx);
        if (processedAudio === null) continue;
        if (spec.released && !spec.flushed) {
          // The final committed while this sentence was mid-synth — flush
          // everything buffered (including this sentence's earlier chunks)
          // and continue live below.
          await this.specEnsureFlushed(spec);
        }
        if (spec.flushed) {
          if (!this.isSpeaking) {
            spec.interrupted = true;
            return false;
          }
          await this.specSendChunk(spec, processedAudio);
        } else {
          chunks.push(processedAudio);
          spec.bufferedBytes += processedAudio.length;
          if (this.specBufferMs(spec) > StreamHandler.PREEMPTIVE_MAX_BUFFER_MS) {
            this.failSpeculationInline(spec, 'buffer_overflow');
            return false;
          }
        }
      }
    } catch (e) {
      // Mirror the live path: a TTS error never crashes the turn.
      getLogger().error(`TTS streaming error during speculation (${this.deps.bridge.label}):`, e);
    }
    return true;
  }

  /**
   * Guardrails + tier-2 hook + synthesis for one speculative sentence —
   * buffered pre-release, live post-release (same transforms either way).
   * Returns ``false`` when the turn must stop. Parity with Python
   * ``_spec_speak_sentence``.
   */
  private async specSpeakSentence(
    spec: SpeculativeTurn,
    sentence: string,
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
  ): Promise<boolean> {
    // ``currentAgent`` (not deps.agent) so a mid-call handoff's guardrails
    // apply to speculative sentences too.
    const guard = checkGuardrails(sentence, this.currentAgent.guardrails);
    let sentenceText = guard
      ? (guard.replacement ?? "I'm sorry, I can't respond to that.")
      : sentence;
    if (hookExecutor.hasAfterLlmSentence()) {
      const transformed = await hookExecutor.runAfterLlmSentence(sentenceText, hookCtx);
      if (transformed === null) return true; // hook dropped this sentence
      sentenceText = transformed;
    }
    if (spec.released) {
      await this.specEnsureFlushed(spec);
      if (!this.isSpeaking) {
        spec.interrupted = true;
        return false;
      }
      if (!spec.ttsFirstByteSent.value && spec.buffered.length === 0) {
        // First sentence of the turn is being synthesized live (nothing was
        // buffered pre-release) — stamp the boundary the streaming path
        // stamps via ``recordLlmFirstSentenceComplete``.
        this.metricsAcc.recordLlmFirstSentenceComplete();
      }
      await this.synthesizeSentence(sentenceText, hookExecutor, hookCtx, spec.ttsFirstByteSent);
      if (!this.isSpeaking) {
        spec.interrupted = true;
        return false;
      }
      return true;
    }
    return this.specSynthesizeBuffered(spec, sentenceText, hookExecutor, hookCtx);
  }

  /**
   * Turn-complete bookkeeping for a RELEASED speculation — mirrors the tail
   * of ``runPipelineLlm`` + ``dispatchTurn`` (metrics turn record,
   * interrupted heard-prefix marker, assistant history entry). Runs exactly
   * once, after all audio was sent/cancelled. Parity with Python
   * ``_finish_released_speculation``.
   */
  private async finishReleasedSpeculation(spec: SpeculativeTurn, llmError: boolean): Promise<void> {
    const responseText = spec.responseParts.join('');
    const interrupted = spec.interrupted;
    let spokenText = responseText;
    if (interrupted && responseText) {
      const heard = this.heardResponsePrefix();
      spokenText =
        heard === null
          ? `${responseText} [interrupted by caller]`
          : heard.text
            ? `${heard.text} [interrupted by caller]`
            : '[interrupted by caller]';
    }
    if (spokenText) await this.emitAssistantTranscript(spokenText);
    if (!interrupted && !llmError && responseText) {
      this.metricsAcc.recordTtsComplete(responseText);
      await this.emitTurnMetrics(this.metricsAcc.recordTurnComplete(responseText));
    }
  }

  /**
   * Body of one speculative turn: LLM stream → sentence chunking → buffered
   * TTS, then commit-or-discard.
   *
   * Until release this task is side-effect free outside ``spec`` itself —
   * no conversation-history writes, no carrier audio, no per-turn metrics
   * (LLM token usage/cost IS recorded by ``LLMLoop``: the tokens were
   * genuinely consumed either way). After release it behaves exactly like a
   * live ``dispatchTurn`` body. Parity with Python
   * ``_run_speculative_dispatch``.
   */
  private async runSpeculativeDispatch(spec: SpeculativeTurn): Promise<void> {
    let llmError = false;
    let stopped = false;
    let tokenIter: AsyncIterator<string, void, unknown> | null = null;
    const chunker = new SentenceChunker({
      aggressiveFirstFlush: this.deps.agent.aggressiveFirstFlush ?? false,
      language: this.deps.agent.language,
    });
    try {
      const hookExecutor = new PipelineHookExecutor(this.deps.agent.hooks);
      const hookCtx = this.buildHookContext();

      // afterTranscribe gates/edits the text the LLM sees — same as a normal
      // dispatch. A veto means the matching final would be vetoed too; fail
      // the speculation and let the commit path run the hook again on the
      // real final.
      const filteredText = await hookExecutor.runAfterTranscribe(spec.interimText, hookCtx);
      if (filteredText === null) {
        this.failSpeculationInline(spec, 'after_transcribe_veto');
        return;
      }

      // Prompt parity with ``processTranscript``: snapshot history and
      // append the (filtered) user entry to the SNAPSHOT only — the shared
      // history is committed at release time.
      const snapshot = [
        ...this.history.entries,
        { role: 'user', text: filteredText, timestamp: Date.now() },
      ];
      const callCtx = { call_id: this.callId, caller: this.caller, callee: this.callee };
      tokenIter = this.llmLoop!.run(
        filteredText,
        snapshot,
        callCtx,
        this.metricsAcc,
        hookExecutor,
        hookCtx,
        { signal: spec.abort.signal },
      )[Symbol.asyncIterator]();

      try {
        while (true) {
          const nextToken = tokenIter.next();
          let raced: IteratorResult<string, void> | null = null;
          // Pre-release: race the next token against the commit decision so
          // buffered audio flushes the MOMENT the final commits — even while
          // the LLM is silent between tokens (agent-runtime LLMs can pause
          // for seconds mid-stream).
          while (raced === null && !spec.released && !spec.abort.signal.aborted) {
            const DECIDED = {};
            const winner = await Promise.race([
              nextToken,
              spec.decision.then(() => DECIDED as unknown),
            ]);
            if (winner === DECIDED) {
              if (spec.released && !spec.flushed && !spec.abort.signal.aborted) {
                await this.specEnsureFlushed(spec);
              }
              if (!spec.released) break; // discarded — abort path below
            } else {
              raced = winner as IteratorResult<string, void>;
            }
          }
          if (spec.abort.signal.aborted) {
            // Aborted (pre-release discard) or barge-in cancelled
            // (post-release) — abandon the pending token fetch safely.
            void Promise.resolve(nextToken).catch(() => {});
            if (spec.released) spec.interrupted = true;
            stopped = true;
            break;
          }
          const result = raced ?? (await nextToken);
          if (result.done) break;
          const token = result.value;
          spec.responseParts.push(token);
          if (spec.released) {
            // Flush as soon as the release is observed — never hold
            // already-synthesized audio while waiting for the next sentence
            // boundary.
            if (!spec.flushed) await this.specEnsureFlushed(spec);
            // Live continuation — keep the echo-guard reference and
            // user-perceived TTFT current.
            this.currentAgentSpokenText = spec.responseParts.join('');
            if (!spec.llmFirstTokenRecorded) {
              spec.llmFirstTokenRecorded = true;
              this.metricsAcc.recordLlmFirstToken();
              await this.emitLlmFirstToken();
            }
          }
          for (const sentence of chunker.push(token)) {
            if (!(await this.specSpeakSentence(spec, sentence, hookExecutor, hookCtx))) {
              stopped = true;
              break;
            }
          }
          if (stopped) break;
        }
      } catch (e) {
        const isAbort = (e as Error)?.name === 'AbortError' || spec.abort.signal.aborted;
        if (isAbort && !spec.released) return; // torn down by an abort — silent
        if (isAbort) {
          spec.interrupted = true;
          stopped = true;
        } else {
          llmError = true;
          chunker.reset();
          getLogger().error(
            `Preemptive: LLM streaming error during speculation (${this.deps.bridge.label}):`,
            e,
          );
          if (!spec.released) {
            // Unreleased — fail silently; the final dispatches normally
            // (and gets its own, live, error handling).
            this.failSpeculationInline(spec, 'llm_error');
            return;
          }
          // Released — the turn is live: mirror the live error path.
          this.metricsAcc.recordTurnInterrupted();
          const fallback = this.deps.agent.llmErrorMessage;
          if (fallback && !spec.ttsFirstByteSent.value && this.isSpeaking) {
            try {
              await this.synthesizeSentence(fallback, hookExecutor, hookCtx, spec.ttsFirstByteSent, false);
            } catch (err) {
              getLogger().error('llmErrorMessage fallback synthesis failed:', err);
            }
          }
        }
      }

      if (!llmError && !stopped) {
        for (const sentence of chunker.flush()) {
          if (!(await this.specSpeakSentence(spec, sentence, hookExecutor, hookCtx))) {
            stopped = true;
            break;
          }
        }
      }

      if (!spec.released) {
        if (spec.abort.signal.aborted || spec.failed) return; // pre-release discard
        // Generation finished before the final committed — park and hold
        // the audio until the commit decision lands.
        await spec.decision;
        if (!spec.released) return; // discarded
      }

      // Released: flush anything still held (covers "LLM finished before
      // the final committed" — the common case), then run the turn-complete
      // bookkeeping. ``endSpeakingWithGrace`` pairs with the
      // ``beginSpeaking`` inside the flush.
      try {
        if (!spec.interrupted && !llmError) {
          await this.specEnsureFlushed(spec);
        }
      } finally {
        if (spec.flushed) this.endSpeakingWithGrace();
      }
      this.metricsAcc.recordLlmComplete();
      await this.finishReleasedSpeculation(spec, llmError);
    } catch (e) {
      getLogger().error('Preemptive: speculative dispatch failed:', e);
      if (!spec.released) {
        this.failSpeculationInline(spec, 'exception');
      } else if (spec.flushed && this.isSpeaking) {
        // Never leave the floor held on an unexpected released-path failure.
        this.endSpeakingWithGrace();
      }
    } finally {
      // Close the LLM generator so the provider connection is freed even on
      // an early unwind (parity with Python ``result.aclose()``).
      try {
        void tokenIter?.return?.();
      } catch {
        // Best-effort.
      }
      if (this.llmAbort === spec.abort) this.llmAbort = null;
      if (this.speculation === spec) this.speculation = null;
      // A RELEASED speculation became the live turn (``dispatchTask``) — on
      // completion it must clear the handle exactly like ``dispatchTurn``'s
      // ``finally`` does, or ``canSpeculate()`` (which requires
      // ``dispatchTask === null``) would stay false for the REST OF THE
      // CALL after the first hit. Python is immune (``_can_speculate``
      // accepts ``dispatch.done()``); the TS convention is null-on-done.
      if (this.dispatchTask === spec.task) this.dispatchTask = null;
    }
  }

  /**
   * Schedule the opt-in long-turn filler and return its async ``clear()``.
   *
   * When ``agent.longTurnMessage`` is unset / empty the returned clear is a
   * no-op (byte-identical to today's behaviour). Otherwise a one-shot timer
   * fires after ``agent.longTurnMessageAfterS`` seconds and, IFF no audio has
   * reached the carrier this turn (``!ttsFirstByteSent.value``) AND we still own
   * the floor (``this.isSpeaking``), synthesizes the filler ONCE via the same
   * per-sentence TTS primitive every sentence uses.
   *
   * The returned ``clear()`` is **async**: it stops the timer AND, if the filler
   * already started synthesizing (its ``setTimeout`` callback runs in a separate
   * macro-task, so it can fire just before the first real sentence), AWAITS the
   * in-flight synthesis so the filler audio can never interleave with the real
   * sentence that follows. Idempotent; self-synthesis failure degrades to
   * silence (never crashes the turn). The caller must clear on first real audio,
   * on the error branch, and in the finally.
   */
  private scheduleLongTurnFiller(
    ttsFirstByteSent: { value: boolean },
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
    label: string,
  ): () => Promise<void> {
    const message = this.deps.agent.longTurnMessage;
    if (!message) return async () => {};
    const afterS = this.deps.agent.longTurnMessageAfterS ?? 4.0;
    let cancelled = false;
    let inFlight: Promise<void> | null = null;
    const timer = setTimeout(() => {
      // Fire at most once, only if the caller still heard SILENCE this turn, we
      // still hold the floor, and the turn has not already moved on.
      if (cancelled || ttsFirstByteSent.value || !this.isSpeaking) return;
      // Track the in-flight synthesis so clear() can await it — serializing the
      // filler before the real sentence so their audio can never interleave.
      // Filler audio is not part of the LLM's reply — advance the playback
      // clock without a heard-prefix segment (recordSegment=false).
      inFlight = this.synthesizeSentence(
        message,
        hookExecutor,
        hookCtx,
        ttsFirstByteSent,
        false,
      ).catch((err) => {
        getLogger().error(
          `longTurnMessage filler synthesis failed (${label}):`,
          err,
        );
      });
    }, Math.max(0, afterS * 1000));
    return async () => {
      cancelled = true;
      clearTimeout(timer);
      if (inFlight !== null) {
        const pending = inFlight;
        inFlight = null;
        await pending;
      }
    };
  }

  /**
   * Streaming built-in LLM path with sentence chunking and per-sentence
   * guardrails/TTS. Returns the concatenated (plain) response text plus whether
   * the turn was cut short by a barge-in — the caller applies the interrupted
   * marker to history only, keeping metrics on the plain text.
   */
  private async runPipelineLlm(
    filteredTranscript: string,
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
    historySnapshot: Array<{ role: string; text: string }>,
  ): Promise<{ text: string; interrupted: boolean }> {
    const label = this.deps.bridge.label;
    const callCtx = { call_id: this.callId, caller: this.caller, callee: this.callee };
    const chunker = new SentenceChunker({
      aggressiveFirstFlush: this.deps.agent.aggressiveFirstFlush ?? false,
      language: this.deps.agent.language,
    });
    const allParts: string[] = [];
    const ttsFirstByteSent = { value: false };
    await this.beginSpeaking();
    // Fresh AbortController per turn so a stale abort from a previous
    // barge-in cannot terminate this stream.  ``cancelSpeaking`` aborts
    // it; the consumption loop checks ``signal.aborted`` between tokens
    // to break early and free the upstream LLM connection.
    this.llmAbort = new AbortController();
    const llmSignal = this.llmAbort.signal;
    let llmError = false;

    // Opt-in long-turn filler: when the turn is SLOW (agent runtime running
    // tools/memory) and NO audio has reached the carrier yet, speak a short
    // filler instead of dead silence. Distinct from ``llmErrorMessage`` (that
    // fires on an LLM ERROR; this fires on SLOWNESS). The timer waits
    // ``longTurnMessageAfterS`` then, IFF still no audio this turn AND we still
    // own the floor, synthesizes the filler ONCE. Cleared the moment real audio
    // is emitted, on the error branch, and in the finally.
    const clearLongTurnFiller = this.scheduleLongTurnFiller(
      ttsFirstByteSent,
      hookExecutor,
      hookCtx,
      label,
    );

    // Span lifetime: LLM dispatch → final token / TTS handoff. Always closed
    // in the ``finally`` block so an early throw cannot leak a span.
    const llmSpan = startSpan(SPAN_LLM, { 'patter.call.id': this.callId });

    const guardAndSpeak = async (sentence: string, isFirst: boolean): Promise<void> => {
      // Fix 3/5: record first-sentence boundary before synthesizing first sentence.
      if (isFirst) this.metricsAcc.recordLlmFirstSentenceComplete();
      const guard = checkGuardrails(sentence, this.currentAgent.guardrails);
      let sentenceText = guard
        ? (guard.replacement ?? "I'm sorry, I can't respond to that.")
        : sentence;
      // Tier 2 — per-sentence after_llm transform. Runs between the
      // sentence chunker and TTS so PII redaction / persona overlay /
      // refusal swap can edit individual sentences without buffering the
      // full LLM response. Returning null from the hook drops the sentence.
      if (hookExecutor.hasAfterLlmSentence()) {
        const transformed = await hookExecutor.runAfterLlmSentence(sentenceText, hookCtx);
        if (transformed === null) return; // hook dropped this sentence
        sentenceText = transformed;
      }
      // Real audio is about to play — cancel the long-turn filler so it can
      // never fire (or double-speak) once the agent's own reply has started.
      await clearLongTurnFiller();
      await this.synthesizeSentence(sentenceText, hookExecutor, hookCtx, ttsFirstByteSent);
    };
    let firstSentenceEmitted = false;

    try {
      try {
        for await (const token of this.llmLoop!.run(
          filteredTranscript,
          historySnapshot,
          callCtx,
          this.metricsAcc,
          hookExecutor,
          hookCtx,
          { signal: llmSignal },
        )) {
          if (llmSignal.aborted) break;
          // Fix 5: record first token for TTFT metric.
          this.metricsAcc.recordLlmFirstToken();
          // Speech-event: per-turn TTFT marker for SDK callback consumers.
          // Idempotent in the dispatcher.
          await this.emitLlmFirstToken();
          allParts.push(token);
          // Keep the echo-guard reference current as the agent speaks, so a
          // barge-in transcript mid-turn is compared against what the agent has
          // said so far (echo lags the tokens). Parity with Python.
          this.currentAgentSpokenText = allParts.join('');
          let sentences = chunker.push(token);
          // pause_resume: a resume may have fired between tokens — speak
          // the sentences buffered during the pause FIRST so the reply
          // stays in order.
          if (!this.outputPaused) {
            const released = this.releasePausedSentences();
            if (released.length > 0) sentences = [...released, ...sentences];
          }
          for (const sentence of sentences) {
            if (!this.isSpeaking) break;
            // pause_resume: while output is paused, buffer the sentence
            // (bounded) — spoken on resume, discarded on kill. Keeps
            // consuming LLM tokens either way.
            if (this.bufferSentenceIfPaused(sentence)) continue;
            await guardAndSpeak(sentence, !firstSentenceEmitted);
            firstSentenceEmitted = true;
          }
          if (!this.isSpeaking || llmSignal.aborted) break;
        }
      } catch (e) {
        // Treat AbortError as a clean barge-in cancellation, not an LLM error.
        const isAbort =
          (e as Error)?.name === 'AbortError' || llmSignal.aborted;
        // The turn ended (error or clean abort) — stop the filler so it cannot
        // speak over the error fallback below or after a barge-in.
        await clearLongTurnFiller();
        if (!isAbort) {
          llmError = true;
          chunker.reset(); // discard partial content on LLM error
          getLogger().error(`LLM loop error (${label}):`, e);
          // Fix 8: record turn as interrupted so it does not leak in metrics when
          // the LLM throws without emitting any text.
          this.metricsAcc.recordTurnInterrupted();
          // Opt-in spoken fallback: speak the configured line iff no audio was
          // emitted this turn (``!ttsFirstByteSent.value`` — no PCM chunk has
          // reached the carrier, i.e. the caller heard SILENCE) and the agent
          // still owns the floor (``this.isSpeaking``). Gated on emitted audio
          // rather than received tokens, so a provider that streams partial
          // tokens ('Let me check…') and then times out before a sentence
          // boundary (the chunker buffered them, TTS never ran) still triggers
          // the fallback. Reuses the normal per-sentence TTS primitive so the
          // fallback is a regular turn utterance (barge-in honoured per chunk;
          // closed by the ``finally`` ``endSpeakingWithGrace``). A non-empty
          // string is required — unset / empty preserves today's
          // silence-on-error behaviour. Self-synthesis failure must degrade to
          // that silence, never crash the turn.
          const fallback = this.deps.agent.llmErrorMessage;
          if (fallback && !ttsFirstByteSent.value && this.isSpeaking) {
            try {
              // Error-fallback audio is not part of the LLM's reply — no
              // heard-prefix segment (recordSegment=false).
              await this.synthesizeSentence(fallback, hookExecutor, hookCtx, ttsFirstByteSent, false);
            } catch (err) {
              getLogger().error(`llmErrorMessage fallback synthesis failed (${label}):`, err);
            }
          }
        }
      }

      this.metricsAcc.recordLlmComplete(); // record BEFORE TTS flush, not after

      // The outer loop exists for pause_resume: the turn must not end
      // while a pause decision is outstanding — buffered sentences are
      // spoken on resume; a kill aborts the turn. Each wait is bounded by
      // the confirm window (the resume timer guarantees a decision), and
      // legacy mode never pauses so the loop runs exactly once —
      // byte-identical behaviour. Mirrors Python.
      if (!llmError && this.isSpeaking) {
        let pendingSentences = chunker.flush();
        for (;;) {
          for (const sentence of pendingSentences) {
            if (!this.isSpeaking) break;
            if (this.bufferSentenceIfPaused(sentence)) continue;
            await guardAndSpeak(sentence, !firstSentenceEmitted);
            firstSentenceEmitted = true;
          }
          if (!this.isSpeaking) break;
          if (!this.outputPaused && this.pausedSentences.length === 0) break;
          if (!(await this.awaitPauseDecision())) break;
          pendingSentences = this.releasePausedSentences();
        }
      }
    } finally {
      // Ensure the long-turn filler never outlives the turn (idempotent — a
      // no-op when already cleared at the first real audio / error branch).
      await clearLongTurnFiller();
      this.endSpeakingWithGrace();
      // Drop the per-turn abort controller so the next turn starts with a
      // fresh one and barge-ins on the next turn cannot accidentally fire
      // an already-aborted signal.
      this.llmAbort = null;
      try {
        llmSpan.end();
      } catch {
        // Swallow — span teardown should never crash the call path.
      }
    }
    // Return the PLAIN text plus whether the turn was cut short. The caller
    // (dispatchTurn) records metrics on the plain text and applies the
    // ``[interrupted by caller]`` marker only to the history/transcript, so
    // metrics (TTS cost, turn-complete) are never polluted by the marker.
    // Parity with Python, where metrics are recorded on the unmarked text
    // inside ``_process_streaming_response`` before the marker is appended.
    return { text: allParts.join(''), interrupted: llmSignal.aborted };
  }

  /**
   * Non-streaming path (onMessage function / webhook): apply output guardrails,
   * push to history, sentence-chunk the text, synthesize. Returns ``true`` if
   * TTS was interrupted mid-flight so the caller can skip turn-complete.
   */
  private async runRegularLlm(
    responseText: string,
    hookExecutor: PipelineHookExecutor,
    hookCtx: HookContext,
  ): Promise<{ interrupted: boolean; finalText: string }> {
    const guard = checkGuardrails(responseText, this.currentAgent.guardrails);
    let text = responseText;
    if (guard) {
      getLogger().debug(`Guardrail '${guard.name}' triggered (pipeline)`);
      text = guard.replacement ?? "I'm sorry, I can't respond to that.";
    }

    this.metricsAcc.recordLlmComplete();
    await this.emitAssistantTranscript(text);
    // Echo-guard reference: only the streaming path populated it, so the
    // echo of non-streaming replies compared against an empty string under
    // forward-STT-while-speaking and committed as a phantom user turn.
    this.currentAgentSpokenText = text;

    const chunker = new SentenceChunker();
    const sentences = [...chunker.push(text), ...chunker.flush()];
    const ttsFirstByteSent = { value: false };
    await this.beginSpeaking();
    let interrupted = false;

    try {
      // Outer loop mirrors ``runPipelineLlm``: in pause_resume mode the
      // turn waits out an in-flight pause decision (buffered sentences
      // speak on resume, a kill marks interrupted); legacy mode never
      // pauses → single pass.
      let pendingSentences: readonly string[] = sentences;
      for (;;) {
        for (const sentence of pendingSentences) {
          if (!this.isSpeaking) { interrupted = true; break; }
          if (this.bufferSentenceIfPaused(sentence)) continue;
          let sentenceText = sentence;
          // Tier 2 — apply per-sentence after_llm hook on non-streaming
          // path too (parity with the streaming path's guardAndSpeak).
          if (hookExecutor.hasAfterLlmSentence()) {
            const transformed = await hookExecutor.runAfterLlmSentence(sentenceText, hookCtx);
            if (transformed === null) continue; // hook dropped this sentence
            sentenceText = transformed;
          }
          await this.synthesizeSentence(sentenceText, hookExecutor, hookCtx, ttsFirstByteSent);
        }
        if (interrupted) break;
        if (!this.outputPaused && this.pausedSentences.length === 0) break;
        if (!(await this.awaitPauseDecision())) {
          interrupted = true;
          break;
        }
        pendingSentences = this.releasePausedSentences();
      }
    } finally {
      this.endSpeakingWithGrace();
    }

    if (!interrupted) this.metricsAcc.recordTtsComplete(text);
    return { interrupted, finalText: text };
  }

  /** Handle streaming WebSocket remote response with TTS. */
  private async handleWebSocketResponse(msgData: Record<string, unknown>): Promise<void> {
    const onMessage = this.deps.onMessage as string;
    const parts: string[] = [];
    this.metricsAcc.recordLlmComplete();
    await this.beginSpeaking();
    let wsTtsStarted = false;
    let interrupted = false;
    try {
      for await (const chunk of this.deps.remoteHandler.callWebSocket(onMessage, msgData)) {
        // Honour barge-in at the OUTER loop too: only breaking the inner
        // audio loop kept consuming the remote stream and STARTED A FRESH
        // TTS SYNTHESIS PER CHUNK after the caller interrupted — billed
        // audio nobody hears, and the next user turn queued behind the
        // remote stream's end.
        if (!this.isSpeaking) {
          interrupted = true;
          break;
        }
        parts.push(chunk);
        // Echo-guard reference: without it, the echo of WS-remote replies
        // compared against an empty string under forward-STT-while-speaking
        // and committed as a phantom user turn.
        this.currentAgentSpokenText = parts.join('');
        if (this.tts) {
          this.resetTtsCarry();
          for await (const audioChunk of this.tts.synthesizeStream(chunk)) {
            if (!this.isSpeaking) break;
            if (!wsTtsStarted) { wsTtsStarted = true; this.metricsAcc.recordTtsFirstByte(); await this.emitAudioOut(); }
            const encoded = this.encodePipelineAudio(audioChunk);
            this.deps.bridge.sendAudio(this.ws, encoded, this.streamSid);
            this.markFirstAudioSent();
          }
        }
      }
    } catch (e) {
      getLogger().error(`WebSocket remote error (${this.deps.bridge.label}):`, e);
    } finally {
      this.endSpeakingWithGrace();
      this.resetTtsCarry();
    }
    const responseText = parts.join('');
    if (!interrupted) {
      // Gate billing/turn-complete on a clean finish like the other paths —
      // recordTtsComplete on an interrupted turn billed full characters for
      // audio the caller never heard.
      this.metricsAcc.recordTtsComplete(responseText);
      await this.emitTurnMetrics(this.metricsAcc.recordTurnComplete(responseText));
    }
    if (responseText) await this.emitAssistantTranscript(responseText);
  }

  // ---------------------------------------------------------------------------
  // Private: OpenAI Realtime / ElevenLabs ConvAI mode
  // ---------------------------------------------------------------------------

  private async initRealtimeAdapter(resolvedPrompt: string): Promise<void> {
    const label = this.deps.bridge.label;
    // Pass the per-call resolved tool list (MCP + consult merges) so the
    // Realtime session advertises them to the model, not just agent.tools.
    this.adapter = this.deps.buildAIAdapter(resolvedPrompt, this.resolvedTools ?? undefined);

    // Try to adopt a Realtime WS parked during the ringing window.
    // When present we skip the cold ``adapter.connect()`` — the
    // parked socket has already paid the TCP + TLS + HTTP-101 +
    // ``session.update`` ack round-trip (~300-600 ms saved on first
    // audible word). Falls back transparently on cache miss / dead
    // socket / adapter missing ``adoptWebSocket``.
    let parked: import('./client').ParkedProviderConnections | undefined;
    if (typeof this.deps.popPrewarmedConnections === 'function') {
      try {
        parked = this.deps.popPrewarmedConnections(this.callId);
      } catch (err) {
        getLogger().debug(`popPrewarmedConnections raised: ${String(err)}`);
      }
    }
    const parkedRealtimeWs = parked?.openaiRealtime;
    let adoptOk = false;
    if (parkedRealtimeWs !== undefined) {
      const adapterAny = this.adapter as
        | { adoptWebSocket?: (ws: import('ws').WebSocket) => void }
        | undefined;
      const wsAlive = parkedRealtimeWs.readyState === 1 /* OPEN */;
      if (typeof adapterAny?.adoptWebSocket === 'function' && wsAlive) {
        try {
          adapterAny.adoptWebSocket(parkedRealtimeWs);
          getLogger().info(
            `[CONNECT] callId=${this.callId} provider=openai_realtime source=adopted ms=0`,
          );
          adoptOk = true;
        } catch (err) {
          getLogger().debug(`Realtime adoptWebSocket failed: ${String(err)}; falling back`);
        }
      }
      if (!adoptOk) {
        try { parkedRealtimeWs.close(); } catch { /* ignore */ }
      }
    }
    if (!adoptOk) {
      try {
        await this.adapter.connect();
        getLogger().debug(`AI adapter connected (${label})`);
      } catch (e) {
        getLogger().error(`AI adapter connect FAILED (${label}):`, e);
        // Hang up the telephony call so it doesn't stay connected billing
        try { await this.deps.bridge.endCall(this.callId, this.ws); } catch { /* best effort */ }
        return;
      }
    }

    if (this.deps.agent.firstMessage) {
      // Start measuring latency for the first turn (firstMessage → first audio byte)
      this.metricsAcc.startTurn();
      if (this.adapter instanceof OpenAIRealtimeAdapter) {
        // Use ``sendFirstMessage`` (role=assistant) so the AI treats
        // ``firstMessage`` as its OWN opening line, not a user prompt to
        // respond to. Older adapter builds without the method fall back to
        // ``sendText`` (legacy role=user behaviour).
        const sender =
          typeof (this.adapter as unknown as { sendFirstMessage?: (t: string) => Promise<void> }).sendFirstMessage === 'function'
            ? (this.adapter as unknown as { sendFirstMessage: (t: string) => Promise<void> }).sendFirstMessage.bind(this.adapter)
            : this.adapter.sendText.bind(this.adapter);
        await sender(this.deps.agent.firstMessage);
      }
      // ElevenLabs ConvAI sends firstMessage via connection config (handled in adapter.connect())
    }

    this.adapter.onEvent(async (type, eventData) => {
      try {
        await this.handleAdapterEvent(type, eventData);
      } catch (err) {
        getLogger().error(`Adapter event handler error (${label}):`, err);
      }
    });
  }

  private async handleAdapterEvent(type: string, eventData: unknown): Promise<void> {
    const handler = this.adapterEventHandlers[type];
    if (handler) await handler(eventData);
  }

  /** Event-type → handler dispatch table for the Realtime adapter. */
  private readonly adapterEventHandlers: Record<string, (eventData: unknown) => Promise<void>> = {
    audio: async (eventData) => this.onAdapterAudio(eventData as Buffer),
    speech_stopped: async () => this.onAdapterSpeechStopped(),
    transcript_input: async (eventData) => this.onAdapterTranscriptInput(eventData as string),
    transcript_output: async (eventData) => this.onAdapterTranscriptOutput(eventData as string),
    response_done: async (eventData) => this.onAdapterResponseDone(eventData as Record<string, unknown> | null),
    speech_started: async () => this.onAdapterSpeechInterrupt(),
    interruption: async () => this.onAdapterSpeechInterrupt(),
    error: async (eventData) => this.onAdapterError(eventData),
    function_call: async (eventData) => {
      if (this.adapter instanceof OpenAIRealtimeAdapter) {
        await this.handleFunctionCall(eventData as { call_id: string; name: string; arguments: string });
      } else if (this.adapter instanceof ElevenLabsConvAIAdapter) {
        await this.handleConvAIClientTool(
          eventData as { call_id: string; name: string; arguments: Record<string, unknown> },
        );
      }
    },
  };

  // ---- Speech-event helpers ------------------------------------------
  // No-op when the deps don't include a SpeechEvents dispatcher. Tracks
  // wall-clock for `speech_duration_ms` payloads.
  private userSpeechStartMs: number | null = null;
  private agentTurnStartMs: number | null = null;

  private async emitUserSpeechStarted(): Promise<void> {
    if (!this.deps.speechEvents) return;
    this.userSpeechStartMs = Date.now();
    await this.deps.speechEvents.fireUserSpeechStarted();
  }

  private async emitUserSpeechEnded(): Promise<void> {
    if (!this.deps.speechEvents) return;
    const duration =
      this.userSpeechStartMs !== null
        ? Math.max(0, Date.now() - this.userSpeechStartMs)
        : 0;
    this.userSpeechStartMs = null;
    await this.deps.speechEvents.fireUserSpeechEnded({
      speechDurationMs: duration,
    });
  }

  private async emitUserSpeechEos(
    transcriptSoFar?: string,
    trigger?: import('./_speech-events').EouTrigger,
  ): Promise<void> {
    if (!this.deps.speechEvents) return;
    let resolved = trigger;
    if (resolved === undefined) {
      if (this.deps.agent.turnDetector) {
        // Consume the EOU trigger stamped by the semantic finalize paths
        // (``semantic_turn_detector`` when the model approved the commit,
        // ``vad_silence`` otherwise). Single consumption point so the
        // event fires exactly once per committed turn.
        resolved = this.lastEouTrigger;
        this.lastEouTrigger = 'vad_silence';
      } else {
        // No detector: reflect how this commit was driven — local VAD
        // silence when a VAD is active, otherwise the STT provider's own
        // endpointing. Parity with Python ``_dispatch_turn``.
        resolved = (this.deps.agent.vad ?? this.autoVad) ? 'vad_silence' : 'manual_commit';
      }
    }
    await this.deps.speechEvents.fireUserSpeechEos({
      trigger: resolved,
      transcriptSoFar,
    });
  }

  private async emitAgentSpeechStarted(): Promise<void> {
    if (!this.deps.speechEvents) return;
    this.agentTurnStartMs = Date.now();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const ttsKey = (this.deps.agent.tts?.constructor as any)?.providerKey;
    await this.deps.speechEvents.fireAgentSpeechStarted({
      ttsProvider: ttsKey,
      engine: this.deps.agent.provider ?? "openai_realtime",
    });
  }

  private async emitAgentSpeechEnded(interrupted: boolean): Promise<void> {
    if (!this.deps.speechEvents) return;
    if (this.agentTurnStartMs === null) return;
    const duration = Math.max(0, Date.now() - this.agentTurnStartMs);
    this.agentTurnStartMs = null;
    await this.deps.speechEvents.fireAgentSpeechEnded({
      speechDurationMs: duration,
      interrupted,
    });
  }

  /** Fire the per-turn LLM TTFT marker. Idempotent in the dispatcher
   * — guarded by `firstTokenForTurn` on the SpeechEvents instance. */
  private async emitLlmFirstToken(): Promise<void> {
    if (!this.deps.speechEvents) return;
    await this.deps.speechEvents.fireLlmFirstToken({
      llmProvider: this.llmProviderTag,
      model: this.deps.agent.model ?? "",
    });
  }

  /** Fire the per-turn first-TTS-audio marker. Idempotent in the
   * dispatcher — guarded by `firstAudioForTurn`. The provider tag falls
   * back to the engine name for Realtime / ConvAI (no separate TTS). */
  private async emitAudioOut(): Promise<void> {
    if (!this.deps.speechEvents) return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const ttsKey = (this.deps.agent.tts?.constructor as any)?.providerKey;
    const provider =
      ttsKey ?? this.deps.agent.provider ?? "openai_realtime";
    await this.deps.speechEvents.fireAudioOut({ ttsProvider: provider });
  }

  private async onAdapterAudio(eventData: Buffer): Promise<void> {
    // Record time-to-first-audio-byte as latency (Realtime mode). If no
    // startTurn() was called yet (e.g. agent responding again without user
    // input), start a new turn now so latency is still measured.
    if (!this.responseAudioStarted) {
      this.responseAudioStarted = true;
      if (this.metricsAcc.turnActive === false) this.metricsAcc.startTurn();
      this.metricsAcc.recordTtsFirstByte();
      // Speech-event: first wire-time chunk of this agent turn.
      await this.emitAgentSpeechStarted();
      // Speech-event: in Realtime / ConvAI modes the model output IS the
      // TTS audio, so the same edge satisfies the per-turn
      // ``tts_first_audio`` marker for SDK callback consumers. The
      // dispatcher's idempotency guard prevents double-fires.
      await this.emitAudioOut();
    }
    // The GA Realtime adapter (used for both the ``OpenAIRealtime`` and
    // ``OpenAIRealtime2`` engines) has ALREADY transcoded the model's PCM16
    // 24 kHz output down to mulaw 8 kHz internally — see
    // ``OpenAIRealtime2Adapter.translateGaAudioDelta``. Both Twilio and Telnyx
    // expect PCMU/mulaw 8 kHz (Telnyx uses stream_bidirectional_codec=PCMU), so
    // the bytes arriving here are already in the correct wire format — pass
    // through untransformed. Do NOT resample here: inboundResampler is 8k→16k
    // for the STT inbound path; reusing it on the outbound path corrupts both
    // directions.
    //
    // Local-recording tap (agent side, Realtime / ConvAI). Per the comment
    // above the Realtime bytes are μ-law 8 kHz; ConvAI emits μ-law only
    // when ``ulaw_8000`` was negotiated (``forTwilio`` / ``forTelnyx``),
    // else PCM16 16 kHz. The recorder decodes to PCM16 16 kHz internally.
    if (this.localRecorder) {
      const convaiPcm =
        this.adapter instanceof ElevenLabsConvAIAdapter &&
        this.adapter.outputAudioFormat !== 'ulaw_8000';
      this.localRecorder.addAgentAudio(
        eventData,
        convaiPcm ? 'pcm16_16k' : 'mulaw_8k',
      );
    }
    const outAudio = eventData;
    this.deps.bridge.sendAudio(this.ws, outAudio.toString('base64'), this.streamSid);
    this.markFirstAudioSent();
    // Send mark for barge-in accuracy.
    this.chunkCount++;
    this.deps.bridge.sendMark(this.ws, `audio_${this.chunkCount}`, this.streamSid);
  }

  private async onAdapterSpeechStopped(): Promise<void> {
    // Server VAD end-of-speech is the earliest reliable moment to start
    // measuring turn latency in Realtime mode — ``transcript_input``
    // (transcription.completed) arrives noticeably later and understates
    // end-to-end latency.
    if (!this.metricsAcc.turnActive) this.metricsAcc.startTurn();
    this.currentAgentText = '';
    this.responseAudioStarted = false;
    // Reserve the monotonic turn index at the moment the turn OPENS (issue
    // #154, fix 5/6). Threaded through the buffering pipeline into the live
    // per-line transcript events and into recordTurnComplete /
    // recordTurnInterrupted so turn_index is stable under drops/interrupts and
    // the dashboard can sort a late user line above its agent line.
    this.currentTurnIndex = this.metricsAcc.reserveTurnIndex();
    // Mark that a user transcript is expected so the assistant's
    // forthcoming `response.done` event waits for it before being
    // pushed into history. See `userTranscriptPending` doc comment.
    this.userTranscriptPending = true;
    // Response creation (issue #154 — decoupled from Whisper):
    //  - DEFAULT: the GA session sets ``create_response: true``, so the SERVER
    //    auto-creates the response when it commits the user's audio buffer
    //    (``input_audio_buffer.committed``). Patter does NOT drive
    //    ``response.create`` here — firing on speech_stopped raced the
    //    server-side commit (the model generated before the user's audio was a
    //    conversation item → empty / no reply). Letting the server create it on
    //    commit reclaims the ~500 ms Whisper wait AND avoids that race, while
    //    keeping Whisper entirely off the response path.
    //  - LEGACY (``gateResponseOnTranscript`` true): the GA session sets
    //    ``create_response: false`` and the response is driven from
    //    ``onAdapterTranscriptInput`` after the hallucination filter.
    // Speech-event: raw VAD trailing edge. EOU commit happens later on
    // ``transcript_input`` (Realtime emits it after
    // input_audio_buffer.committed).
    await this.emitUserSpeechEnded();
  }

  private async onAdapterTranscriptInput(inputText: string): Promise<void> {
    // Hallucination filter: drop Realtime transcript_input events whose text
    // matches a known Whisper hallucination phrase (empty, common filler, or
    // YouTube-caption closer). These fire on PSTN echo loopback — committing
    // them to the LLM would create phantom user turns the caller never spoke.
    // Parity with Python stream_handler.py `transcript_input` branch.
    if (isSttHallucination(inputText)) {
      getLogger().debug(
        `Realtime transcript_input dropped (likely Whisper hallucination on silence/echo): ${sanitizeLogValue(inputText.slice(0, 60))}`,
      );
      this.userTranscriptPending = false;
      // FIX-1 (issue #154, CRITICAL): the assistant reply for this turn may
      // already be buffered (response.done fired first and parked it on the
      // REALTIME_USER_TRANSCRIPT_WAIT_MS fallback timer waiting for a user
      // transcript that will now never arrive). Without flushing here the
      // reply stalls ~3 s and turns interleave. Capture the buffered turn,
      // null it, cancel the timer, and flush immediately before returning.
      if (this.pendingAssistantTurn !== null) {
        const buffered = this.pendingAssistantTurn;
        this.pendingAssistantTurn = null;
        if (this.pendingAssistantTimer) {
          clearTimeout(this.pendingAssistantTimer);
          this.pendingAssistantTimer = null;
        }
        await this.flushAssistantTurn(buffered);
      }
      return;
    }
    getLogger().debug(`User (${this.deps.bridge.label}): ${sanitizeLogValue(inputText)}`);
    this.history.push({ role: 'user', text: inputText, timestamp: Date.now() });
    // FIX-5 (issue #154): emit the live user transcript line the moment it is
    // known and accepted by the filter, keyed by the reserved turn index. The
    // dashboard sorts the active transcript by (turnIndex, user<assistant) so
    // a late user line still renders ABOVE its agent line. recordTurn (metrics
    // path) de-dups by (turnIndex, role) so this does not double-push.
    this.emitTranscriptLine('user', inputText);
    // Response trigger — LEGACY path only. By default the response was already
    // requested on ``speech_stopped`` (see ``onAdapterSpeechStopped``), so the
    // transcript here is display-only and must NOT drive a second
    // ``response.create``. When ``gateResponseOnTranscript`` is true the
    // response was deliberately deferred to this point: the server VAD is
    // configured with ``create_response: false``, so drive it explicitly now
    // that the hallucination filter has accepted the transcript. Parity with
    // Python stream_handler.py which gates ``request_response()`` on the same
    // flag.
    if (
      this.adapter instanceof OpenAIRealtimeAdapter &&
      this.adapter.getGateResponseOnTranscript()
    ) {
      void this.adapter.requestResponse().catch((err) =>
        getLogger().debug(`Realtime requestResponse failed: ${String(err)}`),
      );
    }
    // Fallback: if speech_stopped was missed (server VAD disabled, custom
    // config, ...) still start the turn here so latency is non-zero.
    if (!this.metricsAcc.turnActive) {
      this.metricsAcc.startTurn();
      this.currentAgentText = '';
      this.responseAudioStarted = false;
    }
    // Speech-event: end-of-utterance committed (Realtime mode emits this
    // on ``input_audio_buffer.committed``, the canonical "user finished"
    // signal). Advances `turnIdx` and arms first-token / first-audio.
    // Explicit trigger: the OpenAI server VAD drove this commit.
    await this.emitUserSpeechEos(inputText, 'vad_silence');
    // Marks ASR as complete — exposes a stt_ms bucket in Realtime mode
    // distinct from the llm+tts portion. Parity with Python handler.
    this.metricsAcc.recordSttComplete(inputText);
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'user',
        text: inputText,
        call_id: this.callId,
        history: [...this.history.entries],
      });
    }
    // User transcript is in — clear the pending flag and flush any
    // assistant turn that was buffered waiting for this.
    this.userTranscriptPending = false;
    if (this.pendingAssistantTurn !== null) {
      const buffered = this.pendingAssistantTurn;
      this.pendingAssistantTurn = null;
      if (this.pendingAssistantTimer) {
        clearTimeout(this.pendingAssistantTimer);
        this.pendingAssistantTimer = null;
      }
      await this.flushAssistantTurn(buffered);
    }
  }

  /**
   * Push an assistant turn into history, fire `onTranscript`, and emit
   * turn-complete metrics. Shared between the immediate path (no user
   * transcript pending) and the buffered path (flushed after user
   * transcript arrives or fallback timer fires).
   */
  private async flushAssistantTurn(text: string): Promise<void> {
    this.history.push({ role: 'assistant', text, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'assistant',
        text,
        call_id: this.callId,
        history: [...this.history.entries],
      });
    }
    // FIX-5 (issue #154): emit the live assistant transcript line keyed by the
    // same reserved turn index as its paired user line. The dashboard sorts by
    // (turnIndex, user<assistant) so the agent reply always renders below its
    // user line even when the user line arrived late.
    const reservedIndex = this.currentTurnIndex;
    this.emitTranscriptLine('assistant', text);
    this.responseAudioStarted = false;
    await this.emitTurnMetrics(
      this.metricsAcc.recordTurnComplete(text, reservedIndex ?? undefined),
    );
  }

  /**
   * Push an assistant turn into history and fire `onTranscript` so host
   * applications observe pipeline-mode replies the same way they observe
   * realtime-mode replies. Mirrors `_emit_assistant_transcript` in the
   * Python SDK and parallels `flushAssistantTurn` (realtime path).
   * Caller is responsible for filtering empty strings.
   */
  private async emitAssistantTranscript(text: string): Promise<void> {
    this.history.push({ role: 'assistant', text, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'assistant',
        text,
        call_id: this.callId,
        history: [...this.history.entries],
      });
    }
  }

  /**
   * Surface a tool invocation from pipeline mode into the transcript
   * timeline. Emits TWO events: one for the call (`name(argsJson)`) and
   * one for the result (`name(...) → result`, truncated to 200 chars).
   * Mirrors realtime mode's two `emitToolEvent` calls in
   * `handleFunctionCall`. Wired as the `LLMLoop` `onToolCall` observer.
   */
  private async recordToolCall(
    name: string,
    args: Record<string, unknown>,
    result: string,
  ): Promise<void> {
    let argsText: string;
    try {
      argsText = JSON.stringify(args ?? {});
    } catch {
      argsText = '{}';
    }
    // 1) Call event
    const callText = `${name}(${argsText})`;
    this.history.push({ role: 'tool', text: callText, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'tool',
        text: callText,
        call_id: this.callId,
        tool_name: name,
        tool_args: args ?? {},
        tool_result: null,
      });
    }
    // 2) Result event (truncated for display, full payload in messages)
    const displayed = result.length > 200 ? result.slice(0, 200) + '…' : result;
    const resText = `${name}(...) → ${displayed}`;
    this.history.push({ role: 'tool', text: resText, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'tool',
        text: resText,
        call_id: this.callId,
        tool_name: name,
        tool_args: args ?? {},
        tool_result: result,
      });
    }
  }

  private async onAdapterTranscriptOutput(outputText: string): Promise<void> {
    if (!outputText) return;
    // Speech-event: per-turn TTFT marker. Idempotent in the dispatcher
    // — guarded by `firstTokenForTurn`. The provider tag matches the
    // engine that produced the transcript (Realtime or ConvAI).
    await this.emitLlmFirstToken();
    const triggered = checkGuardrails(outputText, this.currentAgent.guardrails);
    if (triggered) {
      getLogger().debug(`Guardrail '${triggered.name}' triggered`);
      if (this.adapter instanceof OpenAIRealtimeAdapter) {
        this.adapter.cancelResponse();
        await this.adapter.sendText(triggered.replacement ?? "I'm sorry, I can't respond to that.");
      }
    }
    // Accumulate text — a single history entry is pushed on response_done.
    this.currentAgentText += outputText;
  }

  private async onAdapterResponseDone(responseData: Record<string, unknown> | null): Promise<void> {
    if (responseData) {
      const usage = responseData.usage as {
        input_token_details?: { audio_tokens?: number; text_tokens?: number };
        output_token_details?: { audio_tokens?: number; text_tokens?: number };
      } | undefined;
      if (usage) {
        // ``response.done`` carries the model used for this turn (e.g.
        // ``gpt-realtime-2``); pass it so the cost calc auto-resolves the
        // per-model rate. Falls back to ``this.realtimeModel`` set at call
        // start when the field is absent on the payload.
        const turnModel =
          typeof responseData.model === 'string' ? (responseData.model as string) : null;
        this.metricsAcc.recordRealtimeUsage(usage, turnModel);
      }
    }
    if (!this.currentAgentText) {
      // Empty response — discard the orphaned turn so it doesn't leak.
      this.metricsAcc.recordTurnInterrupted();
      this.responseAudioStarted = false;
      // Speech-event: agent turn ended without text (cancelled).
      await this.emitAgentSpeechEnded(true);
      return;
    }
    // Speech-event: clean agent turn completion (text emitted).
    await this.emitAgentSpeechEnded(false);
    const text = this.currentAgentText;
    this.currentAgentText = '';
    if (this.userTranscriptPending) {
      // Buffer until the user transcript arrives so the rendered order
      // is [user, assistant, user, assistant, ...] rather than the
      // OpenAI Realtime native order [assistant, user, assistant, ...].
      this.pendingAssistantTurn = text;
      if (this.pendingAssistantTimer) clearTimeout(this.pendingAssistantTimer);
      this.pendingAssistantTimer = setTimeout(() => {
        const buffered = this.pendingAssistantTurn;
        this.pendingAssistantTurn = null;
        this.pendingAssistantTimer = null;
        this.userTranscriptPending = false;
        if (buffered !== null) {
          // Fire-and-forget — caller is a setTimeout, can't await.
          this.flushAssistantTurn(buffered).catch((err) =>
            getLogger().error('flushAssistantTurn (fallback timer) failed:', err),
          );
        }
      }, StreamHandler.REALTIME_USER_TRANSCRIPT_WAIT_MS);
      this.responseAudioStarted = false;
      return;
    }
    await this.flushAssistantTurn(text);
  }

  private async onAdapterSpeechInterrupt(): Promise<void> {
    // This handler is SHARED by two engine adapter events via the dispatch
    // table: OpenAI Realtime's ``speech_started`` and ElevenLabs ConvAI's
    // ``interruption``. The behaviour forks on the adapter type AND, for the
    // OpenAI engine, on whether turn-taking is server-managed (default) or
    // client-managed (legacy opt-out, ``gateResponseOnTranscript`` true).
    //
    //   - OpenAI engine, SERVER-MANAGED (DEFAULT): the GA session sets
    //     ``create_response: true`` + ``interrupt_response: true`` — the server
    //     owns VAD, end-of-turn, response creation AND the barge-in cancel.
    //     We do NOT run the anti-flicker gate, do NOT send ``response.cancel``,
    //     and do NOT re-anchor turn metrics (those were a mis-fix that inflated
    //     ``total_ms`` by re-anchoring the engine turn to user-speech-start).
    //     On a WebSocket transport the client STILL must clear the carrier
    //     buffer and truncate the played offset — the server only auto-truncates
    //     on WebRTC/SIP. So: ``sendClear`` + ``truncate()`` only.
    //   - OpenAI engine, CLIENT-MANAGED (legacy opt-out): the session sets
    //     ``interrupt_response: false`` so the server does NOT cancel for us.
    //     Keep the full legacy path — anti-flicker gate, full ``cancelResponse``
    //     (truncate + ``response.cancel``), ``recordBargeinDetected`` +
    //     ``anchorUserSpeechStart``.
    //   - ConvAI: server-managed by ElevenLabs — ``sendClear`` only. No
    //     truncate / cancel concept (ConvAI has no item tracking) and no engine
    //     barge-in metrics. Unchanged from prior behaviour.
    const isEngine = this.adapter instanceof OpenAIRealtimeAdapter;
    const clientManaged =
      isEngine &&
      (this.adapter as OpenAIRealtimeAdapter).getGateResponseOnTranscript();

    // Anti-flicker gate — LEGACY client-managed path only. OpenAI's server VAD
    // fires ``speech_started`` on echo of the agent's own audio in PSTN no-AEC
    // scenarios (carrier loopback feeds our outbound mulaw back into the input
    // buffer). Without this gate every phantom ``speech_started`` cancels the
    // response — most visibly, the firstMessage gets truncated mid-sentence. In
    // server-managed mode the SERVER applies its own VAD threshold /
    // ``interrupt_response`` policy, so the client gate is removed (false
    // barge-ins are tuned via ``turn_detection.threshold`` / ``semantic_vad``
    // eagerness instead). The Realtime adapter manages its own TTS span so
    // ``isSpeaking`` (a pipeline-only flag) stays false; consult the adapter's
    // own response-tracking timestamp as a proxy.
    if (clientManaged) {
      const startedAt = (
        this.adapter as unknown as { currentResponseFirstAudioAt: number | null }
      ).currentResponseFirstAudioAt;
      if (startedAt !== null) {
        const elapsedMs = Date.now() - startedAt;
        if (elapsedMs < StreamHandler.MIN_AGENT_SPEAKING_MS_BEFORE_BARGE_IN_NO_AEC) {
          getLogger().info(
            `Realtime barge-in suppressed (response < gate, ${elapsedMs}ms)`,
          );
          return;
        }
      }
    }
    this.deps.bridge.sendClear(this.ws, this.streamSid);
    if (clientManaged) {
      // LEGACY client-managed barge-in. Stamp barge-in detection (mirrors the
      // pipeline path) so the post-barge-in hygiene gate in _computeTurnLatency
      // (keyed on _lastBargeinAt within 100 ms of _turnStart) fires, then send
      // the FULL cancel (truncate + response.cancel) because
      // ``interrupt_response: false`` means the server won't cancel for us.
      this.metricsAcc.recordBargeinDetected();
      (this.adapter as OpenAIRealtimeAdapter).cancelResponse();
    } else if (isEngine) {
      // SERVER-MANAGED barge-in. ``interrupt_response: true`` → the server
      // already cancelled the response on its own ``speech_started``; sending
      // ``response.cancel`` would be redundant / rejected. We only owe the
      // server the WebSocket-transport obligations: the carrier buffer is
      // cleared above and we truncate the played offset here so phantom
      // assistant text doesn't linger on the conversation. NO gate, NO
      // recordBargeinDetected, NO anchorUserSpeechStart (the engine turn stays
      // anchored at speech_stopped).
      (this.adapter as OpenAIRealtimeAdapter).truncate();
    }
    // ConvAI (and any non-engine adapter): sendClear only, already done above.
    this.metricsAcc.recordTurnInterrupted();
    // Speech-event: user started speaking. If the agent was mid-turn this
    // is a barge-in — close the agent turn as interrupted before flagging
    // the new user-speech edge so consumers see ``agent_ended(true)`` →
    // ``user_started`` in causal order.
    if (this.responseAudioStarted) {
      await this.emitAgentSpeechEnded(true);
    }
    await this.emitUserSpeechStarted();
    // FIX-3 (issue #154) — LEGACY client-managed path only: re-anchor the next
    // turn to the legitimate VAD speech_start, mirroring the pipeline path.
    // This pairs with recordBargeinDetected above so the post-barge-in hygiene
    // gate has a correct _turnStart to compare against. In server-managed mode
    // the engine turn must stay anchored at speech_stopped — re-anchoring here
    // inflated/grew total_ms (it re-anchored the engine turn to
    // user-speech-start).
    if (clientManaged) {
      this.metricsAcc.anchorUserSpeechStart();
    }
    this.currentAgentText = '';
    this.responseAudioStarted = false;
    // A barge-in invalidates any buffered assistant turn — the user
    // interrupted before the response was committed, so we should not
    // surface it as if the agent had finished speaking.
    this.pendingAssistantTurn = null;
    if (this.pendingAssistantTimer) {
      clearTimeout(this.pendingAssistantTimer);
      this.pendingAssistantTimer = null;
    }
    this.userTranscriptPending = false;
  }

  /**
   * Handle a Realtime ``error`` event (issue #154, fix 4).
   *
   * Both Realtime providers dispatch ``('error', …)`` for server-side errors,
   * non-normal socket closes, and socket errors, but the stream handler
   * previously had no entry for it in the dispatch table so these were
   * silently swallowed. We surface them at WARN level with ONLY the error
   * envelope fields (``type`` / ``code`` / ``message``) — never any audio or
   * transcript body, to avoid logging PII. The call is NOT terminated: the
   * provider decides whether to recover, and many of these (e.g. a transient
   * ``input_audio_buffer_commit_empty``) are non-fatal. Parity with the
   * Python ``elif ev_type == 'error'`` branches.
   */
  private async onAdapterError(eventData: unknown): Promise<void> {
    const err = (eventData ?? {}) as {
      type?: unknown;
      code?: unknown;
      message?: unknown;
    };
    const type = typeof err.type === 'string' ? err.type : 'unknown';
    const code = typeof err.code === 'string' ? err.code : '';
    const message = typeof err.message === 'string' ? err.message : '';
    getLogger().warn(
      `Realtime error (${this.deps.bridge.label}) type=${type} code=${code} message=${sanitizeLogValue(message)}`,
    );
  }

  /**
   * Emit a tool-invocation event into the transcript timeline. Pushes a
   * `role=tool` entry into `history` (so it appears in the dashboard
   * transcript next to user/assistant turns) AND fires `onTranscript` so
   * the host application can log / persist / render it. `result` is
   * truncated for log readability — the full payload is in history.
   */
  private async emitToolEvent(
    name: string,
    args: unknown,
    result: string | null,
  ): Promise<void> {
    const argsText = JSON.stringify(args);
    const text = result === null
      ? `${name}(${argsText})`
      : `${name}(${argsText}) → ${result.length > 200 ? result.slice(0, 200) + '…' : result}`;
    this.history.push({ role: 'tool', text, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({
        role: 'tool',
        text,
        call_id: this.callId,
        tool_name: name,
        tool_args: args,
        tool_result: result,
      });
    }
  }


  /**
   * Execute an ElevenLabs ``client_tool_call`` and ALWAYS answer it — a
   * missing client_tool_result stalls the ElevenLabs agent until its own
   * tool timeout. transfer_call/end_call declared as ElevenLabs client
   * tools route to the carrier helpers. Mirrors Python
   * ``_handle_convai_client_tool``.
   */
  private async handleConvAIClientTool(fc: {
    call_id: string;
    name: string;
    arguments: Record<string, unknown>;
  }): Promise<void> {
    const adapter = this.adapter as ElevenLabsConvAIAdapter;
    const respond = (result: string, isError = false): void => {
      try {
        adapter.sendClientToolResult(fc.call_id, result, isError);
      } catch (err) {
        getLogger().warn(`client_tool_result send failed: ${String(err)}`);
      }
    };
    const args = fc.arguments ?? {};

    if (fc.name === 'transfer_call') {
      const number = String((args as { number?: unknown }).number ?? '');
      if (!/^\+[1-9]\d{6,14}$/.test(number)) {
        respond(JSON.stringify({ error: 'Invalid phone number format', status: 'rejected' }), true);
        return;
      }
      try {
        await this.deps.bridge.transferCall(this.callId, number);
        respond(`Transferring to ${number}`);
      } catch (err) {
        respond(JSON.stringify({ error: String(err).slice(0, 200) }), true);
      }
      return;
    }
    if (fc.name === 'end_call') {
      respond('Call ended');
      try {
        await this.deps.bridge.endCall(this.callId, this.ws);
      } catch (err) {
        getLogger().warn(`end_call failed: ${String(err)}`);
      }
      return;
    }

    const tools = (this.resolvedTools ?? this.deps.agent.tools ?? []) as ToolDefinition[];
    const toolDef = tools.find((t) => t.name === fc.name);
    if (!toolDef || (!toolDef.webhookUrl && !toolDef.handler)) {
      getLogger().warn(`ConvAI client_tool_call for unregistered tool '${fc.name}'`);
      respond(JSON.stringify({ error: `Tool '${fc.name}' is not registered`, fallback: true }), true);
      return;
    }

    try {
      const executor = new DefaultToolExecutor();
      const result = await executor.execute(toolDef, args, {
        call_id: this.callId,
        caller: this.caller,
        callee: this.callee,
      });
      respond(result);
      this.recordToolCall(fc.name, args, result);
    } catch (err) {
      getLogger().error(`ConvAI client tool '${fc.name}' failed: ${String(err)}`);
      respond(JSON.stringify({ error: String(err).slice(0, 200), fallback: true }), true);
    }
  }

  private async handleFunctionCall(fc: { call_id: string; name: string; arguments: string }): Promise<void> {
    const adapter = this.adapter as OpenAIRealtimeAdapter;

    if (fc.name === 'transfer_call') {
      let transferArgs: { number?: string; mode?: string; summary?: string };
      try {
        transferArgs = JSON.parse(fc.arguments || '{}') as { number?: string; mode?: string; summary?: string };
      } catch {
        transferArgs = {};
      }
      const transferTo = transferArgs.number ?? '';
      const transferMode = transferArgs.mode || 'cold';
      const transferSummary = transferArgs.summary ?? '';
      if (transferMode !== 'cold' && transferMode !== 'warm') {
        const rejection = JSON.stringify({
          error: `Invalid transfer mode '${transferMode}' — use 'cold' or 'warm'`,
          status: 'rejected',
        });
        await adapter.sendFunctionResult(fc.call_id, rejection);
        await this.emitToolEvent('transfer_call', transferArgs, rejection);
        return;
      }
      if (!isValidE164(transferTo)) {
        getLogger().warn(`transfer_call rejected (${this.deps.bridge.label}): invalid number ${JSON.stringify(transferTo)}`);
        const rejection = JSON.stringify({ error: 'Invalid phone number format', status: 'rejected' });
        await adapter.sendFunctionResult(fc.call_id, rejection);
        await this.emitToolEvent('transfer_call', transferArgs, rejection);
        return;
      }
      if (transferMode === 'warm') {
        // Warm transfer: run the carrier sequence FIRST so an unsupported
        // carrier / REST failure surfaces an error envelope and the AI keeps
        // the call instead of going dark. Parity with the Python handler.
        const outcome = await this.deps.bridge.transferCall(this.callId, transferTo, {
          mode: 'warm',
          summary: transferSummary,
        });
        const resultObj: TransferCallResult =
          outcome && typeof outcome === 'object'
            ? outcome
            : { status: 'transferring', mode: 'warm', to: transferTo };
        const result = JSON.stringify(resultObj);
        await adapter.sendFunctionResult(fc.call_id, result);
        await this.emitToolEvent('transfer_call', transferArgs, result);
        if (!resultObj.error && this.deps.onTranscript) {
          await this.deps.onTranscript({ role: 'system', text: `Call transferred (warm) to ${transferTo}`, call_id: this.callId });
        }
        return;
      }
      getLogger().debug(`Transferring call to ${transferTo}`);
      const result = JSON.stringify({ status: 'transferring', to: transferTo });
      await adapter.sendFunctionResult(fc.call_id, result);
      await this.emitToolEvent('transfer_call', transferArgs, result);
      await this.deps.bridge.transferCall(this.callId, transferTo);
      if (this.deps.onTranscript) {
        await this.deps.onTranscript({ role: 'system', text: `Call transferred to ${transferTo}`, call_id: this.callId });
      }
      return;
    }

    if (fc.name === 'end_call') {
      let endArgs: { reason?: string };
      try {
        endArgs = JSON.parse(fc.arguments || '{}') as { reason?: string };
      } catch {
        endArgs = {};
      }
      const reason = endArgs.reason ?? 'conversation_complete';
      getLogger().debug(`Ending call (${this.deps.bridge.label}): ${reason}`);
      const result = JSON.stringify({ status: 'ending', reason });
      await adapter.sendFunctionResult(fc.call_id, result);
      await this.emitToolEvent('end_call', endArgs, result);
      await this.deps.bridge.endCall(this.callId, this.ws);
      if (this.deps.onTranscript) {
        await this.deps.onTranscript({ role: 'system', text: `Call ended: ${reason}`, call_id: this.callId });
      }
      return;
    }

    if (fc.name === HANDOFF_TOOL_NAME && this.currentAgent.handoffs) {
      await this.handleHandoffFunctionCall(fc);
      return;
    }

    // User-defined tool — supports either `handler` (in-process function)
    // or `webhookUrl` (HTTP POST). Dispatched through ``DefaultToolExecutor``
    // so both paths get retry-with-exponential-backoff and a per-tool
    // circuit breaker. Previously only `webhookUrl` worked in Realtime
    // mode (handler tools fell through and hung the model); now both are
    // routed through the same robust executor used by pipeline mode.
    const effectiveTools = (this.resolvedTools ?? this.deps.agent.tools) as ToolDefinition[] | undefined;
    const toolDef = effectiveTools?.find((t) => t.name === fc.name);
    if (!toolDef) {
      getLogger().warn(`Realtime tool '${fc.name}' not found in agent.tools — skipping`);
      const result = JSON.stringify({ error: `Tool '${fc.name}' not registered`, fallback: true });
      await adapter.sendFunctionResult(fc.call_id, result);
      await this.emitToolEvent(fc.name, {}, result);
      return;
    }
    let parsedArgs: Record<string, unknown>;
    try {
      parsedArgs = JSON.parse(fc.arguments || '{}') as Record<string, unknown>;
    } catch {
      parsedArgs = {};
    }
    // Surface the invocation into the transcript before execution so it
    // appears in the dashboard timeline at the right point even if the
    // handler throws or hangs.
    await this.emitToolEvent(fc.name, parsedArgs, null);

    // Schedule a "reassurance" filler if this tool has one configured —
    // bridges the silence when a slow tool call would otherwise leave
    // the caller hanging. Cleared on tool completion below. Currently
    // Realtime-only (sendText path); pipeline mode silently skips.
    const reassurance = (toolDef as { reassurance?: string | { message: string; afterMs?: number } })
      .reassurance;
    let reassuranceTimer: ReturnType<typeof setTimeout> | null = null;
    if (reassurance) {
      const msg = typeof reassurance === 'string' ? reassurance : reassurance.message;
      const afterMs = typeof reassurance === 'string' ? 1500 : (reassurance.afterMs ?? 1500);
      if (msg && this.adapter instanceof OpenAIRealtimeAdapter) {
        const realtimeAdapter = this.adapter;
        reassuranceTimer = setTimeout(() => {
          // Fire-and-forget — caller is a setTimeout, can't await. Errors
          // are non-fatal: a missed reassurance is just a longer silence.
          //
          // Route through ``sendReassurance`` so the filler is the
          // assistant's own in-band audio (a bare ``response.create`` with
          // explicit instructions) and NOT a phantom ``role:user`` turn that
          // would corrupt the transcript. Falls back to ``sendText`` only for
          // older adapter builds lacking the dedicated method.
          const fire =
            typeof (realtimeAdapter as { sendReassurance?: unknown }).sendReassurance === 'function'
              ? realtimeAdapter.sendReassurance(msg)
              : realtimeAdapter.sendText(msg);
          fire.catch((e: unknown) => {
            getLogger().warn(`Reassurance message failed for tool '${fc.name}': ${String(e)}`);
          });
        }, afterMs);
      }
    }

    // Progress sink: when the handler is an async generator that yields
    // ``{ progress: "..." }``, forward each progress message to the
    // OpenAI Realtime adapter so the agent speaks the update inline.
    // Pipeline mode and non-Realtime adapters silently drop progress
    // (no clean injection point yet — follow-up).
    const onProgress = this.adapter instanceof OpenAIRealtimeAdapter
      ? async (text: string): Promise<void> => {
          try {
            await (this.adapter as OpenAIRealtimeAdapter).sendText(text);
          } catch (e) {
            getLogger().warn(`Tool progress message failed for '${fc.name}': ${String(e)}`);
          }
        }
      : undefined;

    let result: string;
    try {
      result = await this.toolExecutor.execute(
        toolDef as ToolDefinition,
        parsedArgs,
        {
          call_id: this.callId,
          caller: this.caller,
          callee: this.callee,
        },
        onProgress,
      );
    } finally {
      if (reassuranceTimer) clearTimeout(reassuranceTimer);
    }
    await adapter.sendFunctionResult(fc.call_id, result);
    // Emit a follow-up event with the result so the dashboard timeline
    // shows both invocation and outcome.
    await this.emitToolEvent(fc.name, parsedArgs, result);
  }

  /**
   * The effective per-call tool list for the CURRENT agent: target tools plus
   * the built-in consult tool when configured (deduped by name). Used after a
   * handoff to rebuild `resolvedTools`.
   */
  private effectiveToolsForCurrentAgent(): ToolDefinition[] {
    const effective = [...((this.currentAgent.tools as ToolDefinition[] | undefined) ?? [])];
    if (this.currentAgent.consult) {
      const consultTool = buildConsultTool(this.currentAgent.consult);
      if (!effective.some((t) => t.name === consultTool.name)) {
        effective.push(consultTool);
      }
    }
    return effective;
  }

  /**
   * Dispatch the built-in `handoff_to` tool on the Realtime path.
   *
   * Swaps the live session to the target agent's configuration via a
   * mid-session `session.update` (new `instructions` + `tools`), updates
   * `currentAgent` / `resolvedTools` so subsequent tool dispatch resolves
   * against the target's tool list, and records a system-style history entry
   * so transcripts show the handoff. ALWAYS sends a function result — an
   * unknown name / malformed args produce an error envelope, never silence
   * (a missing function result would wedge the model).
   *
   * Voice is intentionally NOT swapped: OpenAI Realtime rejects a voice
   * change once the session has produced audio, so the session keeps the
   * voice established at call start (documented limitation; an info log is
   * emitted when the target requested a different voice). Parity with the
   * Python `_handle_handoff_function_call`.
   */
  private async handleHandoffFunctionCall(fc: { call_id: string; name: string; arguments: string }): Promise<void> {
    const adapter = this.adapter as OpenAIRealtimeAdapter;
    let args: { name?: string; reason?: string } | null;
    try {
      args = JSON.parse(fc.arguments || '{}') as { name?: string; reason?: string };
    } catch {
      args = null;
    }
    if (!args || typeof args !== 'object') {
      const result = JSON.stringify({ error: 'Malformed handoff_to arguments', status: 'rejected' });
      await adapter.sendFunctionResult(fc.call_id, result);
      await this.emitToolEvent(HANDOFF_TOOL_NAME, {}, result);
      return;
    }
    const name = typeof args.name === 'string' ? args.name : '';
    const reason = typeof args.reason === 'string' ? args.reason : '';
    const handoffs = this.currentAgent.handoffs ?? {};
    const target = handoffs[name];
    if (!target) {
      const result = JSON.stringify({
        error: `Unknown handoff agent '${name}'`,
        available: Object.keys(handoffs).sort(),
      });
      await adapter.sendFunctionResult(fc.call_id, result);
      await this.emitToolEvent(HANDOFF_TOOL_NAME, args, result);
      return;
    }

    if (target.voice && target.voice !== this.currentAgent.voice) {
      getLogger().info(
        `handoff_to '${name}': voice change is not supported mid-session on ` +
          'OpenAI Realtime — keeping the current voice.',
      );
    }

    this.currentAgent = applyHandoffTarget(this.currentAgent, target);
    const effective = this.effectiveToolsForCurrentAgent();
    this.resolvedTools = effective;

    // Build the new wire tool list: target tools + built-ins (+ onward
    // handoff tool when the target has its own handoff map). Mirrors the
    // construction in `buildAIAdapter`.
    const wireTools: Array<{ name: string; description: string; parameters: Record<string, unknown>; strict?: boolean }> = effective.map((t) => {
      const entry: { name: string; description: string; parameters: Record<string, unknown>; strict?: boolean } = {
        name: t.name,
        description: t.description ?? '',
        parameters: (t.parameters ?? {}) as Record<string, unknown>,
      };
      if ((t as { strict?: boolean }).strict === true) entry.strict = true;
      return entry;
    });
    wireTools.push(TRANSFER_CALL_TOOL, END_CALL_TOOL);
    const onwardHandoffs = this.currentAgent.handoffs;
    if (onwardHandoffs && Object.keys(onwardHandoffs).length > 0) {
      wireTools.push(buildHandoffTool(Object.keys(onwardHandoffs)));
    }

    const vars = this.deps.sanitizeVariables({ ...(this.currentAgent.variables ?? {}) });
    const resolvedPrompt = this.deps.resolveVariables(this.currentAgent.systemPrompt, vars);
    const newInstructions = applyToolCallPreambles(
      resolvedPrompt,
      (this.currentAgent as { toolCallPreambles?: boolean | string }).toolCallPreambles,
    );

    // session.update FIRST, then the function result — the result triggers
    // the next `response.create`, which must already run under the new
    // instructions so the model replies as the target agent.
    await adapter.updateSession({ instructions: newInstructions, tools: wireTools });

    const handoffText = handoffHistoryText(name, reason);
    this.history.push({ role: 'system', text: handoffText, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({ role: 'system', text: handoffText, call_id: this.callId });
    }

    const result = JSON.stringify({ status: 'handed_off', to: name });
    await adapter.sendFunctionResult(fc.call_id, result);
    await this.emitToolEvent(HANDOFF_TOOL_NAME, args, result);
  }

  /**
   * Swap the live pipeline call to the named handoff target agent.
   *
   * Updates `currentAgent` (the shared `AgentOptions` is never mutated),
   * swaps the LLM loop's system prompt + tool list so the NEXT turn runs as
   * the target agent, and appends a system-style history entry recording the
   * handoff. ALWAYS returns a tool-result string — an unknown name produces
   * an error envelope, never silence.
   *
   * Live audio infrastructure (STT/TTS/VAD instances — and therefore the
   * speaking voice) established at call start is intentionally retained:
   * swapping a connected TTS provider mid-call is not supported in v1.
   * Parity with the Python `_perform_handoff`.
   */
  private async performHandoff(name: string, reason: string): Promise<string> {
    const handoffs = this.currentAgent.handoffs ?? {};
    const target = handoffs[name];
    if (!target) {
      return JSON.stringify({
        error: `Unknown handoff agent '${name}'`,
        available: Object.keys(handoffs).sort(),
      });
    }
    if (target.voice && target.voice !== this.currentAgent.voice) {
      getLogger().info(
        `handoff_to '${name}': voice change is not supported mid-call in ` +
          'pipeline mode (the TTS adapter is already connected) — keeping the current voice.',
      );
    }
    this.currentAgent = applyHandoffTarget(this.currentAgent, target);
    this.resolvedTools = this.effectiveToolsForCurrentAgent();
    const vars = this.deps.sanitizeVariables({ ...(this.currentAgent.variables ?? {}) });
    const resolvedPrompt = this.deps.resolveVariables(this.currentAgent.systemPrompt, vars);
    if (this.llmLoop) {
      this.llmLoop.updateAgent({
        systemPrompt: resolvedPrompt,
        tools: this.buildPipelineLlmTools(),
        disablePhonePreamble: this.currentAgent.disablePhonePreamble ?? false,
      });
    }
    const handoffText = handoffHistoryText(name, reason);
    this.history.push({ role: 'system', text: handoffText, timestamp: Date.now() });
    if (this.deps.onTranscript) {
      await this.deps.onTranscript({ role: 'system', text: handoffText, call_id: this.callId });
    }
    return JSON.stringify({ status: 'handed_off', to: name });
  }

  /**
   * Build the full pipeline tool list for the CURRENT agent: user tools +
   * built-in `transfer_call` / `end_call` + the `handoff_to` tool when
   * handoff targets are configured. Re-invoked after a handoff so the LLM
   * loop advertises the target agent's tools (including its onward handoff
   * map). Parity with the Python `_build_combined_pipeline_tools`.
   */
  private buildPipelineLlmTools(): ToolDefinition[] {
    const augmented = augmentWithBuiltinHandoffTools(
      (this.resolvedTools ?? this.currentAgent.tools) as ToolDefinition[] | null | undefined,
      {
        transferCall: (number, options) => this.deps.bridge.transferCall(this.callId, number, options),
        endCall: () => this.deps.bridge.endCall(this.callId, this.ws),
      },
    );
    const handoffs = this.currentAgent.handoffs;
    if (handoffs && Object.keys(handoffs).length > 0) {
      augmented.push({
        ...buildHandoffTool(Object.keys(handoffs)),
        handler: async (args: Record<string, unknown>): Promise<string> =>
          this.performHandoff(
            typeof args.name === 'string' ? args.name : '',
            typeof args.reason === 'string' ? args.reason : '',
          ),
      });
    }
    return augmented;
  }

  // ---------------------------------------------------------------------------
  // Private: call end / metrics finalization
  // ---------------------------------------------------------------------------

  private async fireCallEnd(): Promise<void> {
    if (this.callEndFired) return;
    this.callEndFired = true;
    if (this.maxDurationTimer) { clearTimeout(this.maxDurationTimer); this.maxDurationTimer = null; }
    // Flush any buffered assistant turn whose user transcript never
    // arrived — better to surface it (out of strict order) than lose it.
    if (this.pendingAssistantTimer) {
      clearTimeout(this.pendingAssistantTimer);
      this.pendingAssistantTimer = null;
    }
    if (this.pendingAssistantTurn !== null) {
      const buffered = this.pendingAssistantTurn;
      this.pendingAssistantTurn = null;
      try { await this.flushAssistantTurn(buffered); } catch { /* best effort */ }
    }
    // Close MCP connections — best effort, swallow errors so a flaky
    // MCP server can't derail call-end teardown.
    if (this.mcpManager) {
      try { await this.mcpManager.close(); } catch { /* ignore */ }
      this.mcpManager = null;
    }

    // Finalize the carrier-neutral local recording (if any): drain the
    // agent FIFO, flush the write buffer, patch the WAV header, close the
    // file. Idempotent + exception-safe — both ``handleStop`` and
    // ``handleWsClose`` funnel here, so abnormal teardown (carrier WS
    // drop) still yields a parseable file. Done BEFORE the cost queries
    // below so the WAV is finalized promptly even when those take seconds.
    let recordingPath: string | null = null;
    if (this.localRecorder) {
      try {
        recordingPath = this.localRecorder.close();
      } catch (err) {
        getLogger().debug(`Local recorder close failed: ${String(err)}`);
      }
    }

    await this.deps.bridge.queryTelephonyCost(this.metricsAcc, this.callId);

    // Deepgram cost query — pull the key off the adapter when STT is a
    // DeepgramSTT instance.
    if (this.stt instanceof DeepgramSTT && this.stt.requestId) {
      const dgKey = (this.stt as unknown as { apiKey?: string }).apiKey;
      if (dgKey) {
        await queryDeepgramCost(this.metricsAcc, dgKey, this.stt.requestId);
      }
    }

    const finalMetrics = this.metricsAcc.endCall();
    const callEndData = {
      call_id: this.callId,
      caller: this.caller,
      callee: this.callee,
      ended_at: Date.now() / 1000,
      transcript: [...this.history.entries],
      metrics: finalMetrics as unknown as Record<string, unknown>,
      // Surface the local recording path when local recording was active
      // for this call (``null`` when the recorder broke mid-call); the key
      // is absent entirely when the feature is off. Parity with the Python
      // bridges' ``recording_path`` handling.
      ...(this.localRecorder ? { recording_path: recordingPath } : {}),
    };

    // Single INFO line per call-end — duration, turns, cost, latency.
    // "p95 wait" = agent_response_ms (user-perceived wait after they stop
    // speaking). Matches the dashboard "p95 wait" tile. Fallback to total_ms
    // for legacy/short calls where agent_response_ms is undefined.
    const cost = (finalMetrics.cost as { total?: number } | undefined)?.total ?? 0;
    const p95Obj = finalMetrics.latency_p95 as
      | { agent_response_ms?: number; total_ms?: number }
      | undefined;
    const latencyP95 = p95Obj?.agent_response_ms ?? p95Obj?.total_ms ?? 0;
    getLogger().info(
      `Call ended: ${this.callId} (${finalMetrics.duration_seconds.toFixed(1)}s, ` +
        `${finalMetrics.turns.length} turns, cost=$${cost.toFixed(4)}, p95 wait=${Math.round(latencyP95)}ms)`,
    );
    this.deps.metricsStore.recordCallEnd(
      callEndData,
      finalMetrics as unknown as Record<string, unknown>,
    );
    // Notify standalone dashboard (if running)
    try {
      const { notifyDashboard } = await import('./dashboard/persistence');
      notifyDashboard(callEndData);
    } catch { /* ignore */ }
    if (this.deps.onCallEnd) {
      try {
        await this.deps.onCallEnd(callEndData);
      } catch (err) {
        // On the ws 'close' path nothing upstream catches — a throwing user
        // callback became an unhandled rejection that killed the process.
        getLogger().error(`onCallEnd callback failed: ${String(err)}`);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Shared cost query helper
// ---------------------------------------------------------------------------

async function queryDeepgramCost(
  metricsAcc: CallMetricsAccumulator,
  deepgramKey: string,
  deepgramRequestId: string,
): Promise<void> {
  try {
    const projResp = await fetch('https://api.deepgram.com/v1/projects', {
      headers: { 'Authorization': `Token ${deepgramKey}` },
      signal: AbortSignal.timeout(5000),
    });
    if (projResp.ok) {
      const projData = await projResp.json() as { projects?: Array<{ project_id?: string }> };
      const projectId = projData.projects?.[0]?.project_id;
      if (projectId) {
        const reqResp = await fetch(
          `https://api.deepgram.com/v1/projects/${projectId}/requests/${deepgramRequestId}`,
          {
            headers: { 'Authorization': `Token ${deepgramKey}` },
            signal: AbortSignal.timeout(5000),
          },
        );
        if (reqResp.ok) {
          const reqData = await reqResp.json() as { response?: { details?: { usd?: number } } };
          const usd = reqData.response?.details?.usd;
          if (usd != null) {
            metricsAcc.setActualSttCost(usd);
            getLogger().debug(`Deepgram actual cost: $${usd}`);
          }
        }
      }
    }
  } catch {
    // Fallback to estimated cost
  }
}
