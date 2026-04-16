import { describe, it, expect, beforeEach } from "vitest";
import { ChatContext } from "../../src/chat-context";
import type { ChatMessage } from "../../src/chat-context";

// ---------------------------------------------------------------------------
// ChatContext — Unit Tests
// ---------------------------------------------------------------------------

describe("ChatContext", () => {
  // -------------------------------------------------------------------------
  // Constructor
  // -------------------------------------------------------------------------

  describe("constructor", () => {
    it("creates an empty context when no system prompt provided", () => {
      const ctx = new ChatContext();
      expect(ctx.length).toBe(0);
      expect(ctx.getMessages()).toEqual([]);
    });

    it("creates context with system prompt as first message", () => {
      const ctx = new ChatContext("You are a helpful assistant.");
      expect(ctx.length).toBe(1);
      const msgs = ctx.getMessages();
      expect(msgs[0].role).toBe("system");
      expect(msgs[0].content).toBe("You are a helpful assistant.");
    });
  });

  // -------------------------------------------------------------------------
  // Add messages
  // -------------------------------------------------------------------------

  describe("addUser", () => {
    it("appends a user message and returns it", () => {
      const ctx = new ChatContext();
      const msg = ctx.addUser("Hello");
      expect(msg.role).toBe("user");
      expect(msg.content).toBe("Hello");
      expect(ctx.length).toBe(1);
    });
  });

  describe("addAssistant", () => {
    it("appends an assistant message and returns it", () => {
      const ctx = new ChatContext();
      const msg = ctx.addAssistant("Hi there!");
      expect(msg.role).toBe("assistant");
      expect(msg.content).toBe("Hi there!");
      expect(ctx.length).toBe(1);
    });
  });

  describe("addSystem", () => {
    it("appends a system message and returns it", () => {
      const ctx = new ChatContext();
      const msg = ctx.addSystem("New instructions");
      expect(msg.role).toBe("system");
      expect(msg.content).toBe("New instructions");
      expect(ctx.length).toBe(1);
    });
  });

  describe("addToolResult", () => {
    it("appends a tool result message with toolCallId", () => {
      const ctx = new ChatContext();
      const msg = ctx.addToolResult("result data", "call_abc123");
      expect(msg.role).toBe("tool");
      expect(msg.content).toBe("result data");
      expect(msg.toolCallId).toBe("call_abc123");
      expect(ctx.length).toBe(1);
    });
  });

  // -------------------------------------------------------------------------
  // Auto-generated IDs
  // -------------------------------------------------------------------------

  describe("auto-generated IDs", () => {
    it("each message gets a unique ID", () => {
      const ctx = new ChatContext();
      const msg1 = ctx.addUser("one");
      const msg2 = ctx.addUser("two");
      const msg3 = ctx.addAssistant("three");

      expect(msg1.id).toBeTruthy();
      expect(msg2.id).toBeTruthy();
      expect(msg3.id).toBeTruthy();
      expect(msg1.id).not.toBe(msg2.id);
      expect(msg2.id).not.toBe(msg3.id);
      expect(msg1.id).not.toBe(msg3.id);
    });

    it("IDs are 12-character hex strings", () => {
      const ctx = new ChatContext();
      const msg = ctx.addUser("test");
      expect(msg.id).toMatch(/^[0-9a-f]{12}$/);
    });
  });

  // -------------------------------------------------------------------------
  // Timestamps
  // -------------------------------------------------------------------------

  describe("timestamps", () => {
    it("messages have numeric timestamps close to now", () => {
      const before = Date.now();
      const ctx = new ChatContext();
      const msg = ctx.addUser("test");
      const after = Date.now();

      expect(msg.timestamp).toBeGreaterThanOrEqual(before);
      expect(msg.timestamp).toBeLessThanOrEqual(after);
    });
  });

  // -------------------------------------------------------------------------
  // getMessages
  // -------------------------------------------------------------------------

  describe("getMessages", () => {
    it("returns a copy of the messages array", () => {
      const ctx = new ChatContext("system");
      ctx.addUser("hello");
      const msgs = ctx.getMessages();
      expect(msgs).toHaveLength(2);
      // Modifying the returned array should not affect the context
      (msgs as ChatMessage[]).push({
        id: "fake",
        role: "user",
        content: "injected",
        timestamp: 0,
      });
      expect(ctx.length).toBe(2);
    });
  });

  // -------------------------------------------------------------------------
  // getLastN
  // -------------------------------------------------------------------------

  describe("getLastN", () => {
    let ctx: ChatContext;

    beforeEach(() => {
      ctx = new ChatContext("system prompt");
      ctx.addUser("msg1");
      ctx.addAssistant("msg2");
      ctx.addUser("msg3");
    });

    it("returns the last N messages", () => {
      const last2 = ctx.getLastN(2);
      expect(last2).toHaveLength(2);
      expect(last2[0].content).toBe("msg2");
      expect(last2[1].content).toBe("msg3");
    });

    it("returns all messages when N > length", () => {
      const all = ctx.getLastN(100);
      expect(all).toHaveLength(4);
    });

    it("returns empty array when N is 0", () => {
      expect(ctx.getLastN(0)).toHaveLength(0);
    });

    it("returns empty array when N is negative", () => {
      expect(ctx.getLastN(-1)).toHaveLength(0);
    });
  });

  // -------------------------------------------------------------------------
  // truncate
  // -------------------------------------------------------------------------

  describe("truncate", () => {
    it("preserves system message + last N when system message exists", () => {
      const ctx = new ChatContext("You are helpful.");
      ctx.addUser("msg1");
      ctx.addAssistant("msg2");
      ctx.addUser("msg3");
      ctx.addAssistant("msg4");

      ctx.truncate(2);

      const msgs = ctx.getMessages();
      expect(msgs).toHaveLength(3); // system + last 2
      expect(msgs[0].role).toBe("system");
      expect(msgs[0].content).toBe("You are helpful.");
      expect(msgs[1].content).toBe("msg3");
      expect(msgs[2].content).toBe("msg4");
    });

    it("keeps last N when no system message exists", () => {
      const ctx = new ChatContext();
      ctx.addUser("msg1");
      ctx.addAssistant("msg2");
      ctx.addUser("msg3");

      ctx.truncate(2);

      const msgs = ctx.getMessages();
      expect(msgs).toHaveLength(2);
      expect(msgs[0].content).toBe("msg2");
      expect(msgs[1].content).toBe("msg3");
    });

    it("is a no-op when maxMessages exceeds current count", () => {
      const ctx = new ChatContext("sys");
      ctx.addUser("hi");
      ctx.truncate(100);
      expect(ctx.length).toBe(2);
    });

    it("keeps only system message when maxMessages is 0", () => {
      const ctx = new ChatContext("sys");
      ctx.addUser("msg1");
      ctx.addAssistant("msg2");

      ctx.truncate(0);

      const msgs = ctx.getMessages();
      expect(msgs).toHaveLength(1);
      expect(msgs[0].role).toBe("system");
    });

    it("empties context when no system message and maxMessages is 0", () => {
      const ctx = new ChatContext();
      ctx.addUser("msg1");

      ctx.truncate(0);

      expect(ctx.length).toBe(0);
    });

    it("handles negative maxMessages as no-op", () => {
      const ctx = new ChatContext();
      ctx.addUser("msg1");
      ctx.truncate(-1);
      expect(ctx.length).toBe(1);
    });
  });

  // -------------------------------------------------------------------------
  // toOpenAI
  // -------------------------------------------------------------------------

  describe("toOpenAI", () => {
    it("converts messages to OpenAI format", () => {
      const ctx = new ChatContext("You are helpful.");
      ctx.addUser("Hello");
      ctx.addAssistant("Hi!");

      const openai = ctx.toOpenAI();

      expect(openai).toEqual([
        { role: "system", content: "You are helpful." },
        { role: "user", content: "Hello" },
        { role: "assistant", content: "Hi!" },
      ]);
    });

    it("includes tool_call_id for tool messages", () => {
      const ctx = new ChatContext();
      ctx.addToolResult("result", "call_123");

      const openai = ctx.toOpenAI();

      expect(openai[0]).toEqual({
        role: "tool",
        content: "result",
        tool_call_id: "call_123",
      });
    });

    it("omits name and tool_call_id when not set", () => {
      const ctx = new ChatContext();
      ctx.addUser("hello");

      const openai = ctx.toOpenAI();

      expect(openai[0]).toEqual({ role: "user", content: "hello" });
      expect("name" in openai[0]).toBe(false);
      expect("tool_call_id" in openai[0]).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // toAnthropic
  // -------------------------------------------------------------------------

  describe("toAnthropic", () => {
    it("separates system message from conversation messages", () => {
      const ctx = new ChatContext("You are helpful.");
      ctx.addUser("Hello");
      ctx.addAssistant("Hi!");

      const result = ctx.toAnthropic();

      expect(result.system).toBe("You are helpful.");
      expect(result.messages).toEqual([
        { role: "user", content: "Hello" },
        { role: "assistant", content: "Hi!" },
      ]);
    });

    it("returns undefined system when no system message exists", () => {
      const ctx = new ChatContext();
      ctx.addUser("Hello");

      const result = ctx.toAnthropic();

      expect(result.system).toBeUndefined();
      expect(result.messages).toHaveLength(1);
    });

    it("uses first system message only", () => {
      const ctx = new ChatContext("first system");
      ctx.addUser("user msg");
      ctx.addSystem("second system");

      const result = ctx.toAnthropic();

      expect(result.system).toBe("first system");
      // Second system message is excluded from messages too
      expect(result.messages).toHaveLength(1);
      expect(result.messages[0].content).toBe("user msg");
    });

    it("converts tool messages to their role string", () => {
      const ctx = new ChatContext();
      ctx.addUser("call the tool");
      ctx.addToolResult("tool output", "call_456");

      const result = ctx.toAnthropic();

      expect(result.messages[1]).toEqual({
        role: "tool",
        content: "tool output",
      });
    });
  });

  // -------------------------------------------------------------------------
  // copy
  // -------------------------------------------------------------------------

  describe("copy", () => {
    it("creates an independent copy", () => {
      const ctx = new ChatContext("system");
      ctx.addUser("hello");
      ctx.addAssistant("hi");

      const cloned = ctx.copy();

      // Same content
      expect(cloned.length).toBe(ctx.length);
      expect(cloned.getMessages().map((m) => m.content)).toEqual(
        ctx.getMessages().map((m) => m.content),
      );

      // Independent — mutating clone does not affect original
      cloned.addUser("new message");
      expect(cloned.length).toBe(4);
      expect(ctx.length).toBe(3);
    });

    it("preserves message IDs in the copy", () => {
      const ctx = new ChatContext();
      const msg = ctx.addUser("test");
      const cloned = ctx.copy();
      expect(cloned.getMessages()[0].id).toBe(msg.id);
    });
  });

  // -------------------------------------------------------------------------
  // Serialization (toJSON / fromJSON)
  // -------------------------------------------------------------------------

  describe("serialization", () => {
    it("round-trips through toJSON / fromJSON", () => {
      const ctx = new ChatContext("system prompt");
      ctx.addUser("hello");
      ctx.addAssistant("world");
      ctx.addToolResult("data", "call_789");

      const json = ctx.toJSON();
      const restored = ChatContext.fromJSON(json as any);

      expect(restored.length).toBe(ctx.length);

      const originalMsgs = ctx.getMessages();
      const restoredMsgs = restored.getMessages();

      for (let i = 0; i < originalMsgs.length; i++) {
        expect(restoredMsgs[i].id).toBe(originalMsgs[i].id);
        expect(restoredMsgs[i].role).toBe(originalMsgs[i].role);
        expect(restoredMsgs[i].content).toBe(originalMsgs[i].content);
        expect(restoredMsgs[i].timestamp).toBe(originalMsgs[i].timestamp);
        expect(restoredMsgs[i].toolCallId).toBe(originalMsgs[i].toolCallId);
      }
    });

    it("fromJSON handles empty messages array", () => {
      const ctx = ChatContext.fromJSON({ messages: [] } as any);
      expect(ctx.length).toBe(0);
    });

    it("fromJSON handles missing messages key", () => {
      const ctx = ChatContext.fromJSON({} as any);
      expect(ctx.length).toBe(0);
    });
  });

  // -------------------------------------------------------------------------
  // Empty context
  // -------------------------------------------------------------------------

  describe("empty context", () => {
    it("getMessages returns empty array", () => {
      const ctx = new ChatContext();
      expect(ctx.getMessages()).toEqual([]);
    });

    it("getLastN returns empty array", () => {
      const ctx = new ChatContext();
      expect(ctx.getLastN(5)).toEqual([]);
    });

    it("length is 0", () => {
      const ctx = new ChatContext();
      expect(ctx.length).toBe(0);
    });

    it("truncate on empty context is safe", () => {
      const ctx = new ChatContext();
      ctx.truncate(5);
      expect(ctx.length).toBe(0);
    });

    it("toOpenAI returns empty array", () => {
      const ctx = new ChatContext();
      expect(ctx.toOpenAI()).toEqual([]);
    });

    it("toAnthropic returns undefined system and empty messages", () => {
      const ctx = new ChatContext();
      const result = ctx.toAnthropic();
      expect(result.system).toBeUndefined();
      expect(result.messages).toEqual([]);
    });

    it("copy of empty context is independent empty context", () => {
      const ctx = new ChatContext();
      const cloned = ctx.copy();
      cloned.addUser("test");
      expect(ctx.length).toBe(0);
      expect(cloned.length).toBe(1);
    });
  });
});
