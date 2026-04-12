// Shared test utilities for Patter TypeScript SDK tests
// This file is loaded via vitest setupFiles configuration

import { vi } from "vitest";

// ---- Mock factories ----

/** Create a mock WebSocket instance */
export function mockWebSocket() {
  return {
    send: vi.fn(),
    close: vi.fn(),
    on: vi.fn(),
    once: vi.fn(),
    readyState: 1, // WebSocket.OPEN
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
}

/** Create a configurable fetch mock */
export function mockFetch(responses: Record<string, { status: number; body: unknown }>) {
  return vi.fn(async (url: string | URL) => {
    const urlStr = typeof url === "string" ? url : url.toString();
    const match = Object.entries(responses).find(([pattern]) => urlStr.includes(pattern));
    if (match) {
      const [, resp] = match;
      return { ok: resp.status < 400, status: resp.status, json: async () => resp.body, text: async () => JSON.stringify(resp.body) };
    }
    return { ok: false, status: 404, json: async () => ({}), text: async () => "Not found" };
  });
}

/** Create default AgentOptions for testing */
export function makeAgent(overrides?: Record<string, unknown>) {
  return {
    name: "test-agent",
    prompt: "You are a test agent.",
    voiceMode: "pipeline" as const,
    sttProvider: "deepgram",
    ttsProvider: "elevenlabs",
    ...overrides,
  };
}

/** Create default LocalOptions for testing */
export function makeConfig(overrides?: Record<string, unknown>) {
  return {
    mode: "local" as const,
    phoneNumber: "+15551234567",
    twilioAccountSid: "ACtest123",
    twilioAuthToken: "test_auth_token_123",
    webhookUrl: "https://example.com/webhooks",
    ...overrides,
  };
}

/** Generate a Buffer of PCM silence */
export function fakeAudioBuffer(durationMs = 20, sampleRate = 16000): Buffer {
  const numSamples = Math.floor((sampleRate * durationMs) / 1000);
  return Buffer.alloc(numSamples * 2); // 16-bit = 2 bytes per sample
}

/** Generate a Buffer of mulaw silence (0xFF = mulaw zero) */
export function fakeMulawBuffer(durationMs = 20): Buffer {
  const numSamples = Math.floor((8000 * durationMs) / 1000); // mulaw is always 8kHz
  return Buffer.alloc(numSamples, 0xff); // 0xFF is mulaw silence
}
