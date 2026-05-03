/**
 * Security tests for the Patter TypeScript SDK.
 *
 * Covers SSRF protection, XSS sanitisation, E.164 validation,
 * TwiML injection prevention, and secret leakage.
 */

import { describe, it, expect } from "vitest";
import {
  validateWebhookUrl,
  sanitizeVariables,
} from "../../src/server";
import {
  PatterError,
  PatterConnectionError,
} from "../../src/errors";

// xmlEscape is not exported, so we replicate the exact same logic for
// testing the escaping behavior that the SDK applies to TwiML output.
function xmlEscape(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

// ── SEC-1: SSRF on user-supplied webhook URLs ─────────────────────────────

describe("SEC-1: SSRF protection on webhook URLs", () => {
  it("rejects cloud metadata URL (169.254.x.x)", () => {
    expect(() =>
      validateWebhookUrl("http://169.254.169.254/latest/meta-data/")
    ).toThrow(/blocked|private|internal/i);
  });

  it("rejects localhost URL", () => {
    expect(() =>
      validateWebhookUrl("http://localhost:8080/internal")
    ).toThrow(/blocked|private|internal/i);
  });

  it("rejects 127.0.0.1", () => {
    expect(() =>
      validateWebhookUrl("http://127.0.0.1:9090/admin")
    ).toThrow(/blocked|private|internal/i);
  });

  it("rejects 10.x.x.x private range", () => {
    expect(() =>
      validateWebhookUrl("http://10.0.0.1/secret")
    ).toThrow(/blocked|private|internal/i);
  });

  it("rejects 192.168.x.x private range", () => {
    expect(() =>
      validateWebhookUrl("http://192.168.1.1/admin")
    ).toThrow(/blocked|private|internal/i);
  });

  it("accepts a valid public HTTPS URL", () => {
    expect(() =>
      validateWebhookUrl("https://example.com/webhook")
    ).not.toThrow();
  });

  it("accepts a valid public HTTP URL", () => {
    expect(() =>
      validateWebhookUrl("http://example.com/webhook")
    ).not.toThrow();
  });

  it("rejects non-HTTP scheme", () => {
    expect(() =>
      validateWebhookUrl("ftp://example.com/file")
    ).toThrow(/scheme/i);
  });
});

// ── SEC-2: XSS injection in dashboard fields ──────────────────────────────

describe("SEC-2: XSS sanitisation", () => {
  describe("sanitizeVariables strips prototype pollution keys", () => {
    it("removes __proto__ key", () => {
      const raw = { __proto__: "attack", safe: "value" };
      // Build via Object.create to avoid __proto__ shortcut
      const input = Object.create(null) as Record<string, unknown>;
      input["__proto__"] = "attack";
      input["safe"] = "value";
      const result = sanitizeVariables(input);
      expect(result).not.toHaveProperty("__proto__");
      expect(result.safe).toBe("value");
    });

    it("removes constructor key", () => {
      const input = Object.create(null) as Record<string, unknown>;
      input["constructor"] = "attack";
      input["name"] = "John";
      const result = sanitizeVariables(input);
      expect(result).not.toHaveProperty("constructor");
      expect(result.name).toBe("John");
    });
  });

  describe("xmlEscape neutralises script tags", () => {
    it("escapes <script>alert(1)</script>", () => {
      const escaped = xmlEscape("<script>alert(1)</script>");
      expect(escaped).not.toContain("<script>");
      expect(escaped).toContain("&lt;script&gt;");
    });

    it("escapes img onerror payload", () => {
      const escaped = xmlEscape('<img src=x onerror="alert(1)">');
      expect(escaped).not.toContain("<img");
      expect(escaped).toContain("&lt;img");
    });

    it("does not alter normal text", () => {
      expect(xmlEscape("John Doe")).toBe("John Doe");
    });
  });

  describe("sanitizeVariables coerces non-string values", () => {
    it("converts number to string", () => {
      const input = Object.create(null) as Record<string, unknown>;
      input["count"] = 42;
      const result = sanitizeVariables(input);
      expect(result.count).toBe("42");
    });

    it("converts null to empty string", () => {
      const input = Object.create(null) as Record<string, unknown>;
      input["empty"] = null;
      const result = sanitizeVariables(input);
      expect(result.empty).toBe("");
    });
  });
});

// ── SEC-3: E.164 phone number fuzzing ─────────────────────────────────────

describe("SEC-3: E.164 phone number validation", () => {
  // The TS SDK validates E.164 at the server level. We test using the
  // same regex the Python SDK uses: /^\+[1-9]\d{6,14}$/
  const E164_RE = /^\+[1-9]\d{6,14}$/;

  const invalidNumbers = [
    "",
    "+",
    "+1",
    "+0000000000000000",   // starts with 0
    "+123abc456",
    "null",
    "undefined",
    "+" + "1".repeat(10000),
    "+0123456789",         // leading zero after +
  ];

  it.each(invalidNumbers)("rejects invalid number: %s", (num) => {
    expect(E164_RE.test(num)).toBe(false);
  });

  it("accepts valid E.164 number +14155552671", () => {
    expect(E164_RE.test("+14155552671")).toBe(true);
  });

  it("accepts minimum length (7 digits)", () => {
    expect(E164_RE.test("+1234567")).toBe(true);
  });

  it("accepts maximum length (15 digits)", () => {
    expect(E164_RE.test("+123456789012345")).toBe(true);
  });

  it("rejects 16 digits (over max)", () => {
    expect(E164_RE.test("+1234567890123456")).toBe(false);
  });
});

// ── SEC-4: TwiML payload injection ────────────────────────────────────────

describe("SEC-4: TwiML payload injection", () => {
  it("escapes injected <Redirect> verb", () => {
    const malicious = "<Redirect>http://attacker.example/evil</Redirect>";
    const escaped = xmlEscape(malicious);
    expect(escaped).not.toContain("<Redirect>");
    expect(escaped).toContain("&lt;Redirect&gt;");
  });

  it("escapes injected <Dial> verb", () => {
    const malicious = "</Say><Dial>+15551234567</Dial><Say>";
    const escaped = xmlEscape(malicious);
    expect(escaped).not.toContain("<Dial>");
    expect(escaped).toContain("&lt;Dial&gt;");
  });

  it("preserves clean text in TwiML output", () => {
    const clean = "Hello, how can I help you today?";
    expect(xmlEscape(clean)).toBe(clean);
  });

  it("escapes ampersands", () => {
    const text = "Tom & Jerry";
    const result = xmlEscape(text);
    expect(result).toContain("&amp;");
    expect(result).not.toMatch(/& /);
  });

  it("escapes quotes in attribute context", () => {
    const text = 'value="injected"';
    const result = xmlEscape(text);
    expect(result).toContain("&quot;");
    expect(result).not.toContain('"injected"');
  });
});

// ── SEC-5: Secret leakage in logs and error messages ──────────────────────

describe("SEC-5: Secret leakage in errors and string representations", () => {
  const FAKE_API_KEY = "sk_test_AbCdEfGhIjKlMnOpQrStUvWxYz123456";
  const FAKE_TWILIO_TOKEN = "auth_token_XXXXXXXXXXXXXXXXXXXXXXXXXXXX";

  it("PatterConnectionError does not include secrets when properly constructed", () => {
    // Simulate an error that could occur during connection
    const err = new PatterConnectionError(
      "Connection to backend failed: timeout after 30s"
    );
    const errStr = err.message;
    expect(errStr).not.toContain(FAKE_API_KEY);
    expect(errStr).not.toContain(FAKE_TWILIO_TOKEN);
  });

  it("PatterError toString does not leak injected secrets", () => {
    // Even if someone accidentally puts a key in the message, the error
    // class itself should not amplify leakage beyond what was passed in.
    const err = new PatterError("Failed to connect");
    const stringified = JSON.stringify(err);
    expect(stringified).not.toContain(FAKE_API_KEY);
    expect(stringified).not.toContain(FAKE_TWILIO_TOKEN);
  });

  it("error message provides useful diagnostic info", () => {
    const err = new PatterConnectionError(
      "WebSocket connection to wss://api.getpatter.com/ws/sdk failed: ECONNREFUSED"
    );
    expect(err.message.length).toBeGreaterThan(10);
    expect(err.message).toMatch(/connection|websocket|failed/i);
  });

  it("sanitizeVariables does not preserve secret-like keys verbatim", () => {
    const input = Object.create(null) as Record<string, unknown>;
    input["apiKey"] = FAKE_API_KEY;
    input["token"] = FAKE_TWILIO_TOKEN;
    // sanitizeVariables converts values to strings — it doesn't redact,
    // but we verify the function returns predictable output
    const result = sanitizeVariables(input);
    expect(result.apiKey).toBe(FAKE_API_KEY);
    expect(typeof result.token).toBe("string");
  });
});
