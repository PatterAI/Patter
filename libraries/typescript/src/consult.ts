/**
 * Built-in ``consult`` tool — lets the in-call agent escalate to the caller's
 * own back-office agent over HTTP for deeper reasoning or fresh information,
 * then speak the answer.
 *
 * This is the *dispatch + consult* pattern: Patter conducts the call (STT +
 * LLM/voice + TTS + carrier); when the in-call agent hits something it cannot
 * answer directly, it invokes this tool, which POSTs the request to the
 * configured orchestrator endpoint and returns the reply for the agent to
 * speak. The orchestrator stays off the per-turn path — consulted only on
 * demand, so ordinary turns keep their low latency.
 *
 * Implemented as a normal handler-tool (it rides the existing tool-dispatch
 * path in both Realtime and Pipeline modes); the handler does the HTTP call
 * itself so the per-consult timeout and auth headers from
 * {@link ConsultConfig} are honoured (the generic webhook-tool path uses a
 * fixed 10 s timeout and sends no headers).
 */

import { getLogger } from './logger';
import { validateWebhookUrl } from './server';
import type { ConsultConfig, ToolDefinition } from './types';

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_TOOL_NAME = 'consult_agent';
const DEFAULT_DESCRIPTION =
  'Consult your back-office agent for deeper reasoning, fresh information, or ' +
  'actions beyond this call. Use when the caller asks something you cannot ' +
  'answer directly.';
// Cap the orchestrator response fed back to the LLM, mirroring the
// webhook-tool executor's 1 MB ceiling.
const MAX_RESPONSE_CHARS = 1_000_000;
// Reply fields checked (in order) when the orchestrator returns a JSON object.
const REPLY_KEYS = ['reply', 'response', 'text', 'result', 'answer', 'message'] as const;

/**
 * Build the consult tool (schema + handler) for *config*.
 *
 * Validates the orchestrator URL for SSRF at build time (throws on a
 * private/loopback/link-local host or non-HTTP scheme), then returns a
 * ``ToolDefinition`` in the same shape the built-in ``transfer_call`` /
 * ``end_call`` tools use, so it merges into ``agent.tools`` and is dispatched
 * by ``DefaultToolExecutor`` in both Realtime and Pipeline modes.
 */
export function buildConsultTool(config: ConsultConfig): ToolDefinition {
  // SSRF guard at build time. ``allowLoopback`` (opt-in, default false) relaxes
  // loopback/private/link-local targets for a trusted local back-office agent;
  // the non-HTTP(S) scheme rejection is never relaxed.
  validateWebhookUrl(config.url, config.allowLoopback ?? false); // throws on SSRF / bad scheme

  const url = config.url;
  const headers: Record<string, string> = { ...(config.headers ?? {}), 'Content-Type': 'application/json' };
  const timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const handler = async (
    args: Record<string, unknown>,
    context: Record<string, unknown>,
  ): Promise<string> => {
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
        return "I wasn't able to reach the system to get that answer right now.";
      }
      body = (await resp.text()).slice(0, MAX_RESPONSE_CHARS);
    } catch (e) {
      // Never log the URL or headers (may carry a secret); type/message only.
      getLogger().warn(`consult tool: orchestrator call failed: ${e instanceof Error ? e.name : 'error'}`);
      return "I wasn't able to reach the system to get that answer right now.";
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

  return {
    name: config.toolName ?? DEFAULT_TOOL_NAME,
    description: config.description ?? DEFAULT_DESCRIPTION,
    parameters: {
      type: 'object',
      properties: {
        request: {
          type: 'string',
          description:
            'The question or task to send to your back-office agent for deeper ' +
            'reasoning, fresh information, or an action beyond this call.',
        },
      },
      required: ['request'],
    },
    handler,
  };
}
