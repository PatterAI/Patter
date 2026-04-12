import { test, expect } from "@playwright/test";

const BASE = "http://localhost:8765";

test.describe("Answering Machine Detection (AMD)", () => {
  test("AMD webhook endpoint accepts human detection result", async ({
    request,
  }) => {
    const response = await request.post(`${BASE}/webhooks/twilio/amd`, {
      form: {
        CallSid: "CA_e2e_amd_001",
        AnsweredBy: "human",
        AccountSid: "ACtest123456789",
      },
    });

    // AMD webhook returns 204 No Content
    expect(response.status()).toBe(204);
  });

  test("AMD webhook accepts machine_start detection", async ({ request }) => {
    const response = await request.post(`${BASE}/webhooks/twilio/amd`, {
      form: {
        CallSid: "CA_e2e_amd_002",
        AnsweredBy: "machine_start",
        AccountSid: "ACtest123456789",
      },
    });

    expect(response.status()).toBe(204);
  });

  test("AMD webhook accepts machine_end_beep detection", async ({
    request,
  }) => {
    // With no voicemailMessage configured, no TwiML update is attempted.
    const response = await request.post(`${BASE}/webhooks/twilio/amd`, {
      form: {
        CallSid: "CA_e2e_amd_003",
        AnsweredBy: "machine_end_beep",
        AccountSid: "ACtest123456789",
      },
    });

    expect(response.status()).toBe(204);
  });

  test("AMD webhook accepts machine_end_silence detection", async ({
    request,
  }) => {
    const response = await request.post(`${BASE}/webhooks/twilio/amd`, {
      form: {
        CallSid: "CA_e2e_amd_004",
        AnsweredBy: "machine_end_silence",
        AccountSid: "ACtest123456789",
      },
    });

    expect(response.status()).toBe(204);
  });

  test("AMD webhook accepts fax detection", async ({ request }) => {
    const response = await request.post(`${BASE}/webhooks/twilio/amd`, {
      form: {
        CallSid: "CA_e2e_amd_005",
        AnsweredBy: "fax",
        AccountSid: "ACtest123456789",
      },
    });

    expect(response.status()).toBe(204);
  });

  test("dashboard remains stable after AMD webhook events", async ({
    page,
    request,
  }) => {
    // Fire AMD events
    await request.post(`${BASE}/webhooks/twilio/amd`, {
      form: { CallSid: "CA_e2e_amd_stable", AnsweredBy: "human" },
    });

    // Navigate to dashboard — should load with key elements visible
    await page.goto(BASE);
    await expect(page.locator("header h1")).toContainText("Patter");
    await expect(page.locator("#stat-total")).toBeVisible();

    // Verify the dashboard still renders the stat cards correctly after webhooks
    await expect(page.locator(".card")).toHaveCount(4);
    await expect(page.locator("#stat-active")).toBeVisible();
  });
});
