import { test, expect } from "@playwright/test";

const BASE = "http://localhost:8765";

test.describe("Outbound call scenario", () => {
  test("dashboard renders correctly on fresh load", async ({ page }) => {
    await page.goto(BASE);

    // The dashboard title should be visible
    await expect(page.locator("header h1")).toBeVisible();
    await expect(page.locator("header h1")).toContainText("Patter");

    // All four stat cards should exist
    await expect(page.locator(".card")).toHaveCount(4);

    // Verify card labels
    const labels = page.locator(".card .label");
    await expect(labels.nth(0)).toHaveText("Total Calls");
    await expect(labels.nth(1)).toHaveText("Total Cost");
    await expect(labels.nth(2)).toHaveText("Avg Duration");
    await expect(labels.nth(3)).toHaveText("Avg Latency");
  });

  test("call list API responds with empty array initially", async ({
    request,
  }) => {
    const response = await request.get(`${BASE}/api/dashboard/calls?limit=10`);
    expect(response.status()).toBe(200);

    const data = await response.json();
    expect(Array.isArray(data)).toBe(true);
  });

  test("B2B call list API responds with paginated envelope", async ({
    request,
  }) => {
    const response = await request.get(`${BASE}/api/v1/calls?limit=5`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty("data");
    expect(body).toHaveProperty("pagination");
    expect(Array.isArray(body.data)).toBe(true);
    expect(body.pagination).toMatchObject({
      limit: 5,
      offset: 0,
    });
  });

  test("B2B active calls API responds", async ({ request }) => {
    const response = await request.get(`${BASE}/api/v1/calls/active`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty("data");
    expect(body).toHaveProperty("count");
    expect(body.count).toBe(0);
  });

  test("B2B analytics overview responds with aggregates", async ({
    request,
  }) => {
    const response = await request.get(`${BASE}/api/v1/analytics/overview`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty("data");
    expect(body.data).toMatchObject({
      total_calls: 0,
      total_cost: 0,
      avg_duration: 0,
      avg_latency_ms: 0,
    });
  });

  test("B2B analytics costs responds with breakdown", async ({ request }) => {
    const response = await request.get(`${BASE}/api/v1/analytics/costs`);
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body).toHaveProperty("data");
    expect(body.data).toHaveProperty("total_cost");
    expect(body.data).toHaveProperty("breakdown");
    expect(body.data.breakdown).toMatchObject({
      stt: 0,
      tts: 0,
      llm: 0,
      telephony: 0,
    });
  });
});
