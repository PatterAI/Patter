import { test, expect } from "@playwright/test";

const BASE = "http://localhost:8765";

test.describe("Call transfer", () => {
  test("voice webhook encodes caller/callee in stream URL", async ({
    request,
  }) => {
    const response = await request.post(`${BASE}/webhooks/twilio/voice`, {
      form: {
        CallSid: "CA_transfer_001",
        From: "+14155550001",
        To: "+15551234567",
      },
    });

    expect(response.status()).toBe(200);
    const body = await response.text();

    // Caller/callee are passed via TwiML <Parameter> elements (not query params)
    expect(body).toContain('name="caller"');
    expect(body).toContain("+14155550001");
    expect(body).toContain('name="callee"');
    expect(body).toContain("+15551234567");
  });

  test("multiple webhook calls produce unique stream URLs", async ({
    request,
  }) => {
    const resp1 = await request.post(`${BASE}/webhooks/twilio/voice`, {
      form: {
        CallSid: "CA_transfer_unique_001",
        From: "+14155550001",
        To: "+15551234567",
      },
    });
    const resp2 = await request.post(`${BASE}/webhooks/twilio/voice`, {
      form: {
        CallSid: "CA_transfer_unique_002",
        From: "+14155550002",
        To: "+15551234567",
      },
    });

    const body1 = await resp1.text();
    const body2 = await resp2.text();

    expect(body1).toContain("CA_transfer_unique_001");
    expect(body2).toContain("CA_transfer_unique_002");
    expect(body1).not.toEqual(body2);
  });

  test("health endpoint returns ok status", async ({ request }) => {
    const response = await request.get(`${BASE}/health`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toEqual({ status: "ok", mode: "local" });
  });

  test("dashboard active calls list is empty initially", async ({
    request,
  }) => {
    const response = await request.get(`${BASE}/api/dashboard/active`);
    expect(response.status()).toBe(200);

    const data = await response.json();
    expect(Array.isArray(data)).toBe(true);
    expect(data).toHaveLength(0);
  });
});
