import { validateWebhookUrl } from './server';
import { getLogger } from './logger';

/** A single entry in the per-call conversation history. */
export interface HistoryEntry {
  readonly role: string;
  readonly text: string;
  readonly timestamp: number;
}

/**
 * Create a capped conversation history manager.
 *
 * Returns helpers that append entries to an internal array (oldest entries are
 * evicted once ``maxSize`` is reached) and retrieve a snapshot of the current
 * history.
 */
export function createHistoryManager(maxSize: number): {
  push: (entry: HistoryEntry) => void;
  getHistory: () => ReadonlyArray<HistoryEntry>;
  /** Direct reference to the underlying array (read-only outside this module). */
  readonly entries: HistoryEntry[];
} {
  const entries: HistoryEntry[] = [];

  const push = (entry: HistoryEntry): void => {
    if (entries.length >= maxSize) entries.shift();
    entries.push(entry);
  };

  const getHistory = (): ReadonlyArray<HistoryEntry> => [...entries];

  return { push, getHistory, entries };
}

/** Context passed alongside tool arguments when executing a webhook. */
export interface ToolCallContext {
  readonly callId: string;
  readonly caller: string;
}

/**
 * Execute a tool webhook with SSRF validation, 3 attempts (2 retries),
 * and a 1 MB response cap.
 *
 * @returns The JSON-stringified response body, or a JSON error envelope after
 *          all retries are exhausted.
 */
export async function executeToolWebhook(
  webhookUrl: string,
  toolName: string,
  parsedArgs: unknown,
  context: ToolCallContext,
  label = '',
): Promise<string> {
  try {
    validateWebhookUrl(webhookUrl);
  } catch (e) {
    const tag = label ? ` (${label})` : '';
    getLogger().error(`Tool webhook URL rejected${tag}: ${String(e)}`);
    return JSON.stringify({ error: String(e), fallback: true });
  }

  let result = '';
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const resp = await fetch(webhookUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tool: toolName,
          arguments: parsedArgs,
          call_id: context.callId,
          caller: context.caller,
          attempt: attempt + 1,
        }),
        signal: AbortSignal.timeout(10_000),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      result = JSON.stringify(await resp.json() as unknown);
      // Cap response body at 1 MB to prevent oversized payloads (aligned with Python SDK)
      const MAX_RESPONSE_BYTES = 1 * 1024 * 1024;
      if (result.length > MAX_RESPONSE_BYTES) {
        const tag = label ? ` (${label})` : '';
        getLogger().warn(`Tool webhook response too large: ${result.length} bytes (max ${MAX_RESPONSE_BYTES})${tag}`);
        return JSON.stringify({ error: `Webhook response too large: ${result.length} bytes (max ${MAX_RESPONSE_BYTES})`, fallback: true });
      }
      return result;
    } catch (e) {
      if (attempt < 2) {
        const tag = label ? ` (${label})` : '';
        getLogger().info(`Tool webhook retry ${attempt + 1}${tag}: ${String(e)}`);
        await new Promise<void>((r) => setTimeout(r, 500));
      } else {
        result = JSON.stringify({ error: `Tool failed after 3 attempts: ${String(e)}`, fallback: true });
      }
    }
  }
  return result;
}
