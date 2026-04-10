/**
 * Remote message handler for B2B webhook and WebSocket integration.
 *
 * Allows onMessage to be a URL string instead of a callable:
 * - HTTP webhook: onMessage="https://api.customer.com/patter/message"
 * - WebSocket: onMessage="ws://localhost:9000/stream"
 */

import crypto from 'node:crypto';
import { getLogger } from './logger';

const MAX_RESPONSE_BYTES = 64 * 1024;

export class RemoteMessageHandler {
  private readonly webhookSecret: string | undefined;

  /**
   * @param webhookSecret Optional HMAC secret. When provided, outgoing webhook
   *   requests include an `X-Patter-Signature` header so the receiver can
   *   verify the payload originated from Patter.
   */
  constructor(webhookSecret?: string) {
    this.webhookSecret = webhookSecret;
  }

  /**
   * Compute HMAC-SHA256 hex digest for the given body.
   */
  private signPayload(body: string): string {
    if (!this.webhookSecret) {
      throw new Error('Cannot sign without a webhookSecret');
    }
    return crypto.createHmac('sha256', this.webhookSecret).update(body).digest('hex');
  }

  /**
   * Release resources held by this handler.
   */
  close(): void {
    // No persistent HTTP client in the TS SDK (fetch is stateless),
    // but provided for API parity with the Python SDK.
  }

  /**
   * POST transcript to HTTP webhook, return response text.
   *
   * The webhook receives a JSON payload:
   *   { text, call_id, caller, callee, history }
   *
   * The response can be plain text or JSON { text: "..." }.
   *
   * When `webhookSecret` was provided at construction time, the request
   * includes an `X-Patter-Signature` header with the HMAC-SHA256 hex
   * digest of the JSON body.
   */
  async callWebhook(url: string, data: Record<string, unknown>): Promise<string> {
    if (url.startsWith('http://')) {
      getLogger().warn(
        'Webhook URL uses unencrypted http:// — call transcripts ' +
        'and phone numbers will be sent in plaintext. Use https:// in production.'
      );
    }
    const body = JSON.stringify(data);
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (this.webhookSecret) {
      headers['X-Patter-Signature'] = this.signPayload(body);
    }
    const response = await fetch(url, {
      method: 'POST',
      headers,
      body,
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      throw new Error(`Webhook returned HTTP ${response.status}`);
    }

    const text = await response.text();
    if (text.length > MAX_RESPONSE_BYTES) {
      throw new Error(`Webhook response too large: ${text.length} bytes (max ${MAX_RESPONSE_BYTES})`);
    }

    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      try {
        const body = JSON.parse(text);
        if (typeof body === 'object' && body !== null && 'text' in body) {
          return String(body.text);
        }
        return String(body);
      } catch {
        return text;
      }
    }
    return text;
  }

  /**
   * Send transcript via WebSocket, yield response chunks.
   *
   * Sends the message data as JSON. Receives one or more JSON frames
   * with { text: "..." } - multiple frames enable streaming.
   * A frame with { done: true } signals end of response.
   */
  async *callWebSocket(url: string, data: Record<string, unknown>): AsyncGenerator<string, void, unknown> {
    if (url.startsWith('ws://')) {
      getLogger().warn(
        'WebSocket URL uses unencrypted ws:// — call transcripts ' +
        'and phone numbers will be sent in plaintext. Use wss:// in production.'
      );
    }
    // Dynamic import to avoid hard dependency on ws
    const { WebSocket } = await import('ws');
    const ws = new WebSocket(url);

    const chunks: string[] = [];
    let done = false;
    let error: Error | null = null;

    await new Promise<void>((resolve, reject) => {
      ws.on('open', () => {
        ws.send(JSON.stringify(data));
        resolve();
      });
      ws.on('error', (err: Error) => {
        error = err;
        reject(err);
      });
    });

    let resolveNext: ((value: string | null) => void) | null = null;

    ws.on('message', (raw: Buffer | string) => {
      const rawStr = raw.toString();
      let text: string | null = null;

      try {
        const frame = JSON.parse(rawStr);
        if (typeof frame === 'object' && frame !== null) {
          if (frame.done) {
            done = true;
            ws.close();
            if (resolveNext) resolveNext(null);
            return;
          }
          text = frame.text || null;
        } else {
          text = String(frame);
        }
      } catch {
        text = rawStr;
      }

      if (text && resolveNext) {
        resolveNext(text);
        resolveNext = null;
      } else if (text) {
        chunks.push(text);
      }
    });

    ws.on('close', () => {
      done = true;
      if (resolveNext) resolveNext(null);
    });

    ws.on('error', (err: Error) => {
      error = err;
      done = true;
      if (resolveNext) resolveNext(null);
    });

    // Yield buffered chunks first
    while (chunks.length > 0) {
      yield chunks.shift()!;
    }

    // Then wait for new messages
    while (!done && !error) {
      const text = await new Promise<string | null>((resolve) => {
        if (chunks.length > 0) {
          resolve(chunks.shift()!);
        } else {
          resolveNext = resolve;
        }
      });
      if (text === null) break;
      yield text;
    }

    if (error) throw error;
  }
}

/** Check if onMessage is a remote URL string. */
export function isRemoteUrl(onMessage: unknown): onMessage is string {
  if (typeof onMessage !== 'string') return false;
  return onMessage.startsWith('http://') ||
    onMessage.startsWith('https://') ||
    onMessage.startsWith('ws://') ||
    onMessage.startsWith('wss://');
}

/** Check if a URL is a WebSocket URL. */
export function isWebSocketUrl(url: string): boolean {
  return url.startsWith('ws://') || url.startsWith('wss://');
}
