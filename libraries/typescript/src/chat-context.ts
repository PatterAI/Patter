/**
 * Typed conversation history management with truncation support.
 *
 * Replaces raw `list[dict]` history with a structured ChatContext class
 * that provides immutable messages, automatic ID generation, truncation
 * preserving system prompts, and format conversion for OpenAI / Anthropic.
 */

import { randomUUID } from "node:crypto";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ChatRole = "system" | "user" | "assistant" | "tool";

export interface ChatMessage {
  readonly id: string;
  readonly role: ChatRole;
  readonly content: string;
  readonly timestamp: number;
  readonly name?: string;
  readonly toolCallId?: string;
}

export interface OpenAIMessage {
  role: string;
  content: string;
  name?: string;
  tool_call_id?: string;
}

export interface AnthropicMessage {
  role: string;
  content: string;
}

export interface AnthropicConversion {
  system: string | undefined;
  messages: ReadonlyArray<AnthropicMessage>;
}

interface ChatContextJSON {
  messages: ReadonlyArray<ChatMessage>;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function generateId(): string {
  return randomUUID().replace(/-/g, "").slice(0, 12);
}

function createMessage(
  role: ChatRole,
  content: string,
  options?: { name?: string; toolCallId?: string },
): ChatMessage {
  return Object.freeze({
    id: generateId(),
    role,
    content,
    timestamp: Date.now(),
    ...(options?.name !== undefined ? { name: options.name } : {}),
    ...(options?.toolCallId !== undefined
      ? { toolCallId: options.toolCallId }
      : {}),
  });
}

// ---------------------------------------------------------------------------
// ChatContext
// ---------------------------------------------------------------------------

export class ChatContext {
  private items: ChatMessage[];

  constructor(systemPrompt?: string) {
    this.items = [];
    if (systemPrompt !== undefined) {
      this.items.push(createMessage("system", systemPrompt));
    }
  }

  // -------------------------------------------------------------------------
  // Add messages
  // -------------------------------------------------------------------------

  addUser(content: string): ChatMessage {
    const msg = createMessage("user", content);
    this.items = [...this.items, msg];
    return msg;
  }

  addAssistant(content: string): ChatMessage {
    const msg = createMessage("assistant", content);
    this.items = [...this.items, msg];
    return msg;
  }

  addSystem(content: string): ChatMessage {
    const msg = createMessage("system", content);
    this.items = [...this.items, msg];
    return msg;
  }

  addToolResult(content: string, toolCallId: string): ChatMessage {
    const msg = createMessage("tool", content, { toolCallId });
    this.items = [...this.items, msg];
    return msg;
  }

  // -------------------------------------------------------------------------
  // Access
  // -------------------------------------------------------------------------

  getMessages(): ReadonlyArray<ChatMessage> {
    return [...this.items];
  }

  getLastN(n: number): ReadonlyArray<ChatMessage> {
    if (n <= 0) return [];
    return [...this.items.slice(-n)];
  }

  get length(): number {
    return this.items.length;
  }

  // -------------------------------------------------------------------------
  // Truncation
  // -------------------------------------------------------------------------

  /**
   * Keep the first system message (if any) plus the last `maxMessages`
   * non-system-first messages. When no system message exists at index 0,
   * simply keeps the last `maxMessages` messages.
   */
  truncate(maxMessages: number): void {
    if (maxMessages < 0) return;

    const hasLeadingSystem =
      this.items.length > 0 && this.items[0].role === "system";

    if (hasLeadingSystem) {
      const systemMsg = this.items[0];
      const rest = this.items.slice(1);
      const kept = maxMessages > 0 ? rest.slice(-maxMessages) : [];
      this.items = [systemMsg, ...kept];
    } else {
      this.items = maxMessages > 0 ? [...this.items.slice(-maxMessages)] : [];
    }
  }

  // -------------------------------------------------------------------------
  // Provider format conversion
  // -------------------------------------------------------------------------

  toOpenAI(): OpenAIMessage[] {
    return this.items.map((msg) => {
      const result: OpenAIMessage = {
        role: msg.role,
        content: msg.content,
      };
      if (msg.name !== undefined) {
        result.name = msg.name;
      }
      if (msg.toolCallId !== undefined) {
        result.tool_call_id = msg.toolCallId;
      }
      return result;
    });
  }

  /**
   * Convert to Anthropic format. The first system message (if present)
   * is extracted into a separate `system` field, and only user/assistant
   * messages are included in the messages array.
   */
  toAnthropic(): AnthropicConversion {
    let system: string | undefined;
    const messages: AnthropicMessage[] = [];

    for (const msg of this.items) {
      if (msg.role === "system") {
        if (system === undefined) {
          system = msg.content;
        }
        continue;
      }
      messages.push({ role: msg.role, content: msg.content });
    }

    return { system, messages };
  }

  // -------------------------------------------------------------------------
  // Copy
  // -------------------------------------------------------------------------

  copy(): ChatContext {
    const ctx = new ChatContext();
    ctx.items = this.items.map((msg) => ({ ...msg }));
    return ctx;
  }

  // -------------------------------------------------------------------------
  // Serialization
  // -------------------------------------------------------------------------

  toJSON(): ChatContextJSON {
    return { messages: [...this.items] };
  }

  static fromJSON(data: ChatContextJSON): ChatContext {
    const ctx = new ChatContext();
    ctx.items = (data.messages ?? []).map((msg) => Object.freeze({ ...msg }));
    return ctx;
  }
}
