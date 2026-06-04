/**
 * Built-in ``consult`` tool — lets the in-call agent escalate to the caller's
 * own back-office agent for deeper reasoning or fresh information, then speak
 * the answer.
 *
 * This is the *dispatch + consult* pattern: Patter conducts the call (STT +
 * LLM/voice + TTS + carrier); when the in-call agent hits something it cannot
 * answer directly, it invokes this tool, which reaches the configured
 * back-office agent and returns the reply for the agent to speak. The
 * back-office agent stays off the per-turn path — consulted only on demand, so
 * ordinary turns keep their low latency.
 *
 * Two targets are supported (see {@link ConsultConfig}):
 *
 * - ``url`` — the generic webhook path: POSTs ``{ request, call_id, caller,
 *   callee }`` to your endpoint and reads a ``reply`` field back.
 * - ``openaiCompatible`` — speaks an OpenAI-compatible ``/chat/completions``
 *   endpoint directly (e.g. an OpenClaw agent, or vLLM / Ollama / Groq) with no
 *   hand-written adapter: POSTs ``{ model, messages, user }`` and speaks
 *   ``choices[0].message.content``. Use {@link openclawConsult}.
 *
 * The handler does the HTTP call itself so the per-consult timeout and auth from
 * {@link ConsultConfig} are honoured. ``config.reassurance``, when set, is
 * attached so the agent speaks a filler while the consult runs (Realtime mode
 * only).
 */

import { getLogger } from './logger';
import { validateWebhookUrl } from './server';
import type { ConsultConfig, OpenAICompatibleConsult, ToolDefinition } from './types';

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_TOOL_NAME = 'consult_agent';
const DEFAULT_DESCRIPTION =
  'Consult your back-office agent for deeper reasoning, fresh information, or ' +
  'actions beyond this call. Use when the caller asks something you cannot ' +
  'answer directly.';
// Cap the response fed back to the LLM, mirroring the webhook-tool executor's
// 1 MB ceiling.
const MAX_RESPONSE_CHARS = 1_000_000;
// Reply fields checked (in order) when a generic webhook returns a JSON object.
const REPLY_KEYS = ['reply', 'response', 'text', 'result', 'answer', 'message'] as const;
// Spoken-friendly fallback when the back-office agent is unreachable or errors.
const GRACEFUL_FALLBACK = "I wasn't able to reach the system to get that answer right now.";

// --- OpenClaw preset constants (mirror models.py) ---------------------------
const OPENCLAW_DEFAULT_BASE_URL = 'http://127.0.0.1:18789/v1';
const OPENCLAW_API_KEY_ENV = 'OPENCLAW_API_KEY';
const OPENCLAW_SESSION_HEADER = 'x-openclaw-session-key';
const OPENCLAW_DESCRIPTION =
  'Consult your OpenClaw agent for anything account-specific — appointments, ' +
  'customer records, schedules, or actions in the back-office system. NEVER ' +
  'state an appointment time, customer detail, or schedule fact from your own ' +
  'memory; ALWAYS call this tool for those and read back what it returns.';
const OPENCLAW_REASSURANCE = 'Let me check on that for you, one moment.';
// Agent ids cross into the gateway via the model string — restrict to a safe set.
const OPENCLAW_AGENT_RE = /^[A-Za-z0-9._:/-]+$/;

const PARAMETERS = {
  type: 'object',
  properties: {
    request: {
      type: 'string',
      description:
        'The question or task to send to your back-office agent for deeper ' +
        'reasoning, fresh information, or an action beyond this call. State it ' +
        'self-containedly — the dialog history is not forwarded with the consult.',
    },
  },
  required: ['request'],
} as const;

/** True if *baseUrl*'s host is loopback / private / link-local. */
function isLoopbackOrPrivateHost(baseUrl: string): boolean {
  let host: string;
  try {
    host = new URL(baseUrl).hostname.toLowerCase();
  } catch {
    return false;
  }
  if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1);
  if (host === 'localhost' || host === '0.0.0.0' || host === '::1') return true;
  if (host.endsWith('.local')) return true;
  if (/^127\./.test(host) || /^10\./.test(host) || /^192\.168\./.test(host)) return true;
  if (/^169\.254\./.test(host)) return true;
  const m = host.match(/^172\.(\d+)\./);
  if (m) {
    const octet = Number(m[1]);
    if (octet >= 16 && octet <= 31) return true;
  }
  // IPv6 unique-local (fc00::/7) + link-local (fe80::/10), matching Python's
  // ipaddress.is_private / is_link_local coverage (host already bracket-stripped).
  if (host.includes(':') && (/^f[cd][0-9a-f]{2}:/.test(host) || /^fe[89ab][0-9a-f]:/.test(host))) {
    return true;
  }
  return false;
}

/**
 * Build a {@link ConsultConfig} that consults a specific OpenClaw agent directly
 * (no hand-written adapter) — the TypeScript equivalent of Python's
 * ``ConsultConfig.openclaw(...)``.
 *
 * ``agent`` is the OpenClaw agent id (e.g. ``"receptionist"``) → targets
 * ``model="openclaw/<agent>"``. An already-namespaced target (``"openclaw/x"``,
 * ``"openclaw:x"``, ``"agent:x"``) is passed through. ``allowLoopback`` defaults
 * to ``true`` when ``baseUrl`` is loopback/private (the intended co-located
 * deployment). The gateway bearer is read from ``apiKey`` or the
 * ``OPENCLAW_API_KEY`` env var (operator-grade — never logged). Sized at the
 * phone-safe 30 s default; raise only for batch-style agents, never above 30 s
 * on a live call.
 */
export function openclawConsult(
  agent: string,
  opts: {
    readonly baseUrl?: string;
    readonly apiKey?: string;
    readonly timeoutMs?: number;
    readonly toolName?: string;
    readonly description?: string;
    readonly reassurance?: string | Readonly<{ message: string; afterMs?: number }>;
    readonly headers?: Readonly<Record<string, string>>;
    readonly allowLoopback?: boolean;
  } = {},
): ConsultConfig {
  if (!agent || !OPENCLAW_AGENT_RE.test(agent)) {
    throw new Error(
      'OpenClaw agent must be a non-empty id of letters, digits, and ._:/- only',
    );
  }
  const baseUrl = opts.baseUrl ?? OPENCLAW_DEFAULT_BASE_URL;
  const model = agent.includes('/') || agent.includes(':') ? agent : `openclaw/${agent}`;
  return {
    openaiCompatible: {
      baseUrl,
      model,
      apiKey: opts.apiKey,
      apiKeyEnv: OPENCLAW_API_KEY_ENV,
      sessionHeader: OPENCLAW_SESSION_HEADER,
    },
    timeoutMs: opts.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    toolName: opts.toolName ?? DEFAULT_TOOL_NAME,
    description: opts.description ?? OPENCLAW_DESCRIPTION,
    reassurance: opts.reassurance ?? OPENCLAW_REASSURANCE,
    headers: opts.headers,
    allowLoopback: opts.allowLoopback ?? isLoopbackOrPrivateHost(baseUrl),
  };
}

/**
 * Build the consult tool (schema + handler) for *config*.
 *
 * The orchestrator URL is SSRF-validated at build time (throws on a
 * private/loopback/link-local host or non-HTTP scheme unless
 * ``config.allowLoopback`` relaxes the host check). Returns a ``ToolDefinition``
 * in the same shape the built-in ``transfer_call`` / ``end_call`` tools use, so
 * it merges into ``agent.tools`` and is dispatched in both Realtime and Pipeline
 * modes. ``config.reassurance``, when set, is attached so the agent speaks a
 * filler while the consult runs (Realtime mode only).
 */
export function buildConsultTool(config: ConsultConfig): ToolDefinition {
  const hasUrl = config.url != null;
  const hasOpenAI = config.openaiCompatible != null;
  if (hasUrl === hasOpenAI) {
    throw new Error('ConsultConfig requires exactly one of url or openaiCompatible');
  }

  const timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const baseHeaders: Record<string, string> = {
    ...(config.headers ?? {}),
    'Content-Type': 'application/json',
  };

  const handler = hasOpenAI
    ? buildOpenAIHandler(config.openaiCompatible as OpenAICompatibleConsult, baseHeaders, timeoutMs, config.allowLoopback ?? false)
    : buildWebhookHandler(config.url as string, baseHeaders, timeoutMs, config.allowLoopback ?? false);

  const tool: ToolDefinition = {
    name: config.toolName ?? DEFAULT_TOOL_NAME,
    description: config.description ?? DEFAULT_DESCRIPTION,
    parameters: PARAMETERS,
    handler,
  };
  return config.reassurance != null ? { ...tool, reassurance: config.reassurance } : tool;
}

type Handler = (
  args: Record<string, unknown>,
  context: Record<string, unknown>,
) => Promise<string>;

/** Generic webhook target: POST ``{ request, call_id, caller, callee }``. */
function buildWebhookHandler(
  url: string,
  headers: Record<string, string>,
  timeoutMs: number,
  allowLoopback: boolean,
): Handler {
  validateWebhookUrl(url, allowLoopback); // throws on SSRF / bad scheme

  return async (args, context): Promise<string> => {
    const requestText = typeof args?.request === 'string' ? args.request : '';
    const payload = {
      request: requestText,
      call_id: (context?.call_id as string | undefined) ?? '',
      caller: (context?.caller as string | undefined) ?? '',
      callee: (context?.callee as string | undefined) ?? '',
    };
    let body: string;
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!resp.ok) {
        getLogger().warn(`consult tool: orchestrator returned HTTP ${resp.status}`);
        return GRACEFUL_FALLBACK;
      }
      body = (await resp.text()).slice(0, MAX_RESPONSE_CHARS);
    } catch (e) {
      getLogger().warn(
        `consult tool: orchestrator call failed: ${e instanceof Error ? e.name : 'error'}`,
      );
      return GRACEFUL_FALLBACK;
    }

    try {
      const data = JSON.parse(body) as unknown;
      if (data && typeof data === 'object' && !Array.isArray(data)) {
        const obj = data as Record<string, unknown>;
        for (const key of REPLY_KEYS) {
          if (typeof obj[key] === 'string') return obj[key] as string;
        }
      }
      return JSON.stringify(data);
    } catch {
      return body;
    }
  };
}

/**
 * OpenAI-compatible target: POST ``{ model, messages, user }`` to
 * ``{baseUrl}/chat/completions`` and speak ``choices[0].message.content``.
 */
function buildOpenAIHandler(
  oc: OpenAICompatibleConsult,
  baseHeaders: Record<string, string>,
  timeoutMs: number,
  allowLoopback: boolean,
): Handler {
  const endpoint = oc.baseUrl.replace(/\/+$/, '') + '/chat/completions';
  validateWebhookUrl(endpoint, allowLoopback); // throws on SSRF / bad scheme

  // Resolve the bearer once (explicit wins over env). Operator-grade — never logged.
  const apiKey = oc.apiKey ?? (oc.apiKeyEnv ? process.env[oc.apiKeyEnv] : undefined);
  const headers: Record<string, string> = { ...baseHeaders };
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
  const sessionHeader = oc.sessionHeader;
  const model = oc.model;

  return async (args, context): Promise<string> => {
    const requestText = typeof args?.request === 'string' ? args.request : '';
    const callId = (context?.call_id as string | undefined) ?? '';
    const caller = (context?.caller as string | undefined) ?? '';
    const callee = (context?.callee as string | undefined) ?? '';

    const contextLines = ['You are answering an inbound phone call relayed by a voice agent.'];
    if (caller) contextLines.push(`Caller: ${caller}`);
    if (callee) contextLines.push(`Line dialed: ${callee}`);
    contextLines.push(
      'Reply concisely in a spoken, conversational style — it is read aloud to the caller.',
    );

    const reqHeaders: Record<string, string> = { ...headers };
    if (sessionHeader && callId) reqHeaders[sessionHeader] = callId;

    const payload: Record<string, unknown> = {
      model,
      messages: [
        { role: 'system', content: contextLines.join('\n') },
        { role: 'user', content: requestText },
      ],
      stream: false,
    };
    // Harmless secondary to the session header; lets gateways that key on the
    // OpenAI ``user`` field derive a stable per-call session.
    if (callId) payload.user = callId;

    try {
      const resp = await fetch(endpoint, {
        method: 'POST',
        headers: reqHeaders,
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (resp.status === 404) {
        getLogger().warn(
          'consult tool: OpenAI-compatible endpoint returned 404 — is it enabled? ' +
            '(OpenClaw: set gateway.http.endpoints.chatCompletions.enabled = true)',
        );
        return GRACEFUL_FALLBACK;
      }
      if (!resp.ok) {
        getLogger().warn(`consult tool: openai-compatible returned HTTP ${resp.status}`);
        return GRACEFUL_FALLBACK;
      }
      const data = (await resp.json()) as {
        choices?: Array<{ message?: { content?: unknown } }>;
      };
      const content = data?.choices?.[0]?.message?.content;
      if (typeof content === 'string' && content.trim()) {
        return content.trim().slice(0, MAX_RESPONSE_CHARS);
      }
      getLogger().warn('consult tool: response missing choices[0].message.content');
      return GRACEFUL_FALLBACK;
    } catch (e) {
      // Never log the endpoint/headers/key — type only.
      getLogger().warn(
        `consult tool: openai-compatible call failed: ${e instanceof Error ? e.name : 'error'}`,
      );
      return GRACEFUL_FALLBACK;
    }
  };
}

// --- Post-call notify (on_call_end → OpenClaw) ------------------------------

// Default instruction prepended to the post-call record sent to OpenClaw.
const POSTCALL_INSTRUCTION =
  'A phone call handled by the voice agent has just ended. Here is the record ' +
  'of the call. Log it and follow up if anything needs action.';
// Cap the transcript we forward so a very long call doesn't bloat the request.
const POSTCALL_MAX_TRANSCRIPT_CHARS = 12_000;

/** Render the ``on_call_end`` payload into a spoken-call record string. */
function buildPostCallRecord(data: Record<string, unknown>, includeTranscript: boolean): string {
  const lines: string[] = [];
  const caller = data.caller as string | undefined;
  const callee = data.callee as string | undefined;
  if (caller) lines.push(`Caller: ${caller}`);
  if (callee) lines.push(`Line dialed: ${callee}`);
  const metrics = data.metrics as
    | { durationSeconds?: number; duration_seconds?: number }
    | undefined;
  const duration = metrics?.durationSeconds ?? metrics?.duration_seconds;
  if (typeof duration === 'number') lines.push(`Duration: ${Math.round(duration)}s`);
  if (includeTranscript) {
    const entries = (data.transcript as Array<{ role?: string; text?: string }>) ?? [];
    const rendered = entries
      .filter((e) => e && typeof e === 'object')
      .map((e) => `${e.role ?? '?'}: ${e.text ?? ''}`)
      .join('\n');
    if (rendered) lines.push('Transcript:\n' + rendered.slice(0, POSTCALL_MAX_TRANSCRIPT_CHARS));
  }
  return lines.length ? lines.join('\n') : '(no call details available)';
}

/**
 * Return an ``on_call_end`` callback that posts the finished call's record to a
 * specific OpenClaw agent, so the brain has the record and can follow up — the
 * TypeScript equivalent of Python's ``openclaw_post_call_notifier``.
 *
 * Wire it on ``serve``:
 *
 *     await phone.serve({ agent, onCallEnd: openclawPostCallNotifier('receptionist') });
 *
 * The record is POSTed to the same OpenClaw agent over its OpenAI-compatible
 * ``/chat/completions`` gateway, keyed to the call id (the ``user`` field +
 * ``x-openclaw-session-key`` header) so it lands in the SAME OpenClaw session as
 * the in-call ``consult`` turns. Fire-and-forget: any error is logged by type
 * only (never the URL / headers / key) and never thrown into teardown. Args
 * mirror {@link openclawConsult}; the bearer is read from ``apiKey`` or
 * ``OPENCLAW_API_KEY`` (operator-grade — never logged).
 */
export function openclawPostCallNotifier(
  agent: string,
  opts: {
    readonly baseUrl?: string;
    readonly apiKey?: string;
    readonly timeoutMs?: number;
    readonly allowLoopback?: boolean;
    readonly includeTranscript?: boolean;
    readonly instruction?: string;
  } = {},
): (data: Record<string, unknown>) => Promise<void> {
  const cfg = openclawConsult(agent, {
    baseUrl: opts.baseUrl,
    apiKey: opts.apiKey,
    timeoutMs: opts.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    allowLoopback: opts.allowLoopback,
  });
  const oc = cfg.openaiCompatible as OpenAICompatibleConsult; // openclawConsult always sets it
  const endpoint = oc.baseUrl.replace(/\/+$/, '') + '/chat/completions';
  validateWebhookUrl(endpoint, cfg.allowLoopback ?? false); // throws on SSRF / bad scheme
  const apiKey = oc.apiKey ?? (oc.apiKeyEnv ? process.env[oc.apiKeyEnv] : undefined);
  const sessionHeader = oc.sessionHeader;
  const model = oc.model;
  const timeoutMs = cfg.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const includeTranscript = opts.includeTranscript ?? true;
  const instruction = opts.instruction ?? POSTCALL_INSTRUCTION;

  return async (data: Record<string, unknown>): Promise<void> => {
    const callId = ((data ?? {}).call_id as string | undefined) ?? '';
    const record = buildPostCallRecord(data ?? {}, includeTranscript);
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
    if (sessionHeader && callId) headers[sessionHeader] = callId;
    const payload: Record<string, unknown> = {
      model,
      messages: [
        { role: 'system', content: instruction },
        { role: 'user', content: record },
      ],
      stream: false,
    };
    if (callId) payload.user = callId;
    try {
      const resp = await fetch(endpoint, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!resp.ok) {
        getLogger().warn(`openclaw post-call notify: HTTP ${resp.status}`);
      }
    } catch (e) {
      // Fire-and-forget: never throw into call teardown; log type only.
      getLogger().warn(
        `openclaw post-call notify failed: ${e instanceof Error ? e.name : 'error'}`,
      );
    }
  };
}
