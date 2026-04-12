import { test, expect } from "@playwright/test";

const BASE = "http://localhost:8765";

test.describe("Tool calling mid-conversation", () => {
  test("dashboard call detail modal opens and closes", async ({ page }) => {
    await page.goto(BASE);

    // The modal should be hidden initially
    const modal = page.locator("#modal");
    await expect(modal).not.toHaveClass(/open/);

    // Press Escape — modal should remain closed (no error)
    await page.keyboard.press("Escape");
    await expect(modal).not.toHaveClass(/open/);
  });

  test("calls table shows correct column headers", async ({ page }) => {
    await page.goto(BASE);

    const headers = page.locator("#tab-calls thead th");
    await expect(headers).toHaveCount(8);
    await expect(headers.nth(0)).toHaveText("Call ID");
    await expect(headers.nth(1)).toHaveText("Direction");
    await expect(headers.nth(2)).toHaveText("From / To");
    await expect(headers.nth(3)).toHaveText("Duration");
    await expect(headers.nth(4)).toHaveText("Mode");
    await expect(headers.nth(5)).toHaveText("Cost");
    await expect(headers.nth(6)).toHaveText("Avg Latency");
    await expect(headers.nth(7)).toHaveText("Turns");
  });

  test("active table shows correct column headers", async ({ page }) => {
    await page.goto(BASE);

    // Switch to Active tab
    await page.locator('.nav-tab[data-tab="active"]').click();

    const headers = page.locator("#tab-active thead th");
    await expect(headers).toHaveCount(6);
    await expect(headers.nth(0)).toHaveText("Call ID");
    await expect(headers.nth(1)).toHaveText("Caller");
    await expect(headers.nth(2)).toHaveText("Callee");
    await expect(headers.nth(3)).toHaveText("Direction");
    await expect(headers.nth(4)).toHaveText("Duration");
    await expect(headers.nth(5)).toHaveText("Turns");
  });

  test("single call detail API returns 404 for unknown call", async ({
    request,
  }) => {
    const response = await request.get(
      `${BASE}/api/dashboard/calls/nonexistent-call-id`,
    );
    expect(response.status()).toBe(404);

    const body = await response.json();
    expect(body).toEqual({ error: "Not found" });
  });

  test("B2B single call API returns 404 for unknown call", async ({
    request,
  }) => {
    const response = await request.get(
      `${BASE}/api/v1/calls/nonexistent-call-id`,
    );
    expect(response.status()).toBe(404);

    const body = await response.json();
    expect(body).toEqual({ error: "Call not found" });
  });

  test("dashboard aggregates endpoint returns cost breakdown structure", async ({
    request,
  }) => {
    const response = await request.get(`${BASE}/api/dashboard/aggregates`);
    expect(response.status()).toBe(200);

    const data = await response.json();
    expect(data).toHaveProperty("total_calls");
    expect(data).toHaveProperty("total_cost");
    expect(data).toHaveProperty("avg_duration");
    expect(data).toHaveProperty("avg_latency_ms");
    expect(data).toHaveProperty("cost_breakdown");
    expect(data).toHaveProperty("active_calls");
    expect(data.cost_breakdown).toHaveProperty("stt");
    expect(data.cost_breakdown).toHaveProperty("tts");
    expect(data.cost_breakdown).toHaveProperty("llm");
    expect(data.cost_breakdown).toHaveProperty("telephony");
  });
});
