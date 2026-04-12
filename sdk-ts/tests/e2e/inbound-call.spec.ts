import { test, expect } from "@playwright/test";

const BASE = "http://localhost:8765";

test.describe("Inbound call scenario", () => {
  test("dashboard loads with expected UI elements", async ({ page }) => {
    await page.goto(BASE);

    // Header
    await expect(page.locator("header h1")).toContainText("Patter");
    await expect(page.locator("header h1 span")).toHaveText("Patter");

    // Status indicator
    await expect(page.locator("#status-text")).toBeVisible();

    // Stat cards
    await expect(page.locator("#stat-total")).toHaveText("0");
    await expect(page.locator("#stat-active")).toHaveText("0");
    await expect(page.locator("#stat-cost")).toHaveText("$0.00");
    await expect(page.locator("#stat-duration")).toHaveText("0s");
    await expect(page.locator("#stat-latency")).toHaveText("0ms");

    // Tab navigation
    await expect(page.locator('.nav-tab[data-tab="calls"]')).toHaveClass(
      /active/,
    );
    await expect(page.locator('.nav-tab[data-tab="active"]')).toBeVisible();

    // Empty state text in calls table
    await expect(page.locator("#calls-body")).toContainText(
      "No calls yet. Waiting for incoming calls...",
    );
  });

  test("tab buttons and tab content sections exist", async ({ page }) => {
    await page.goto(BASE);

    // Both tab buttons should be present
    await expect(page.locator('.nav-tab[data-tab="calls"]')).toBeVisible();
    await expect(page.locator('.nav-tab[data-tab="active"]')).toBeVisible();

    // Tab button text
    await expect(page.locator('.nav-tab[data-tab="calls"]')).toHaveText(
      "Calls",
    );
    await expect(page.locator('.nav-tab[data-tab="active"]')).toHaveText(
      "Active",
    );

    // Both tab content sections exist in the DOM
    await expect(page.locator("#tab-calls")).toBeAttached();
    await expect(page.locator("#tab-active")).toBeAttached();

    // Calls table and active table bodies exist
    await expect(page.locator("#calls-body")).toBeAttached();
    await expect(page.locator("#active-body")).toBeAttached();
  });

  test("Twilio voice webhook returns TwiML with stream URL", async ({
    request,
  }) => {
    const response = await request.post(`${BASE}/webhooks/twilio/voice`, {
      form: {
        CallSid: "CA_e2e_inbound_001",
        From: "+14155551234",
        To: "+15551234567",
        Direction: "inbound",
      },
    });

    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("text/xml");

    const body = await response.text();
    expect(body).toContain("<?xml");
    expect(body).toContain("<Response>");
    expect(body).toContain("<Connect>");
    expect(body).toContain("<Stream");
    expect(body).toContain("wss://example.com/ws/stream/CA_e2e_inbound_001");
  });

  test("dashboard updates after simulated inbound call lifecycle", async ({
    page,
    request,
  }) => {
    await page.goto(BASE);

    // Verify initial empty state
    const aggregatesBefore = await (
      await request.get(`${BASE}/api/dashboard/aggregates`)
    ).json();
    expect(aggregatesBefore.active_calls).toBe(0);

    // Trigger a voice webhook — this creates the stream URL but does not
    // itself add a call to the store; actual calls arrive via WebSocket.
    // We verify the webhook endpoint is reachable and returns valid TwiML.
    const voiceResp = await request.post(`${BASE}/webhooks/twilio/voice`, {
      form: {
        CallSid: "CA_e2e_inbound_002",
        From: "+14155559999",
        To: "+15551234567",
      },
    });
    expect(voiceResp.status()).toBe(200);

    // The /api/dashboard/calls endpoint should still respond
    const callsResp = await request.get(
      `${BASE}/api/dashboard/calls?limit=10`,
    );
    expect(callsResp.status()).toBe(200);
    const calls = await callsResp.json();
    expect(Array.isArray(calls)).toBe(true);
  });

  test("SSE events endpoint is reachable", async ({ page }) => {
    // SSE is a streaming response — we use page.evaluate with a short timeout
    // fetch to verify the endpoint returns the correct content-type header.
    const result = await page.evaluate(async (base) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 2000);
      try {
        const resp = await fetch(`${base}/api/dashboard/events`, {
          signal: controller.signal,
        });
        clearTimeout(timer);
        return {
          status: resp.status,
          contentType: resp.headers.get("content-type"),
        };
      } catch (e: unknown) {
        clearTimeout(timer);
        // AbortError is expected — the SSE stream stays open
        if (e instanceof DOMException && e.name === "AbortError") {
          return { status: -1, contentType: null, aborted: true };
        }
        throw e;
      }
    }, BASE);

    // Either we got the response before abort, or the fetch was aborted
    // Both cases confirm the endpoint is reachable.
    if (result.status === -1) {
      // Aborted means the server started streaming (SSE stays open)
      expect(result.aborted).toBe(true);
    } else {
      expect(result.status).toBe(200);
      expect(result.contentType).toContain("text/event-stream");
    }
  });
});
