import { test, expect } from "@playwright/test";

const BASE = "http://localhost:8765";

test.describe("Recording", () => {
  test("recording webhook endpoint exists and accepts POST", async ({
    request,
  }) => {
    const response = await request.post(`${BASE}/webhooks/twilio/recording`, {
      form: {
        RecordingSid: "RE_e2e_rec_001",
        RecordingUrl: "https://api.twilio.com/recordings/RE_e2e_rec_001",
        CallSid: "CA0000000000000000000000000000d001",
        RecordingDuration: "30",
      },
    });

    // The server responds with 204 No Content for recording webhooks
    expect(response.status()).toBe(204);
  });

  test("export endpoint returns JSON format by default", async ({
    request,
  }) => {
    const response = await request.get(
      `${BASE}/api/dashboard/export/calls?format=json`,
    );
    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("application/json");
    expect(response.headers()["content-disposition"]).toContain(
      "patter_calls.json",
    );
  });

  test("export endpoint returns CSV format when requested", async ({
    request,
  }) => {
    const response = await request.get(
      `${BASE}/api/dashboard/export/calls?format=csv`,
    );
    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("text/csv");
    expect(response.headers()["content-disposition"]).toContain(
      "patter_calls.csv",
    );
  });

  test("export endpoint supports date range filtering", async ({
    request,
  }) => {
    const from = "2024-01-01T00:00:00Z";
    const to = "2025-12-31T23:59:59Z";
    const response = await request.get(
      `${BASE}/api/dashboard/export/calls?format=json&from=${from}&to=${to}`,
    );
    expect(response.status()).toBe(200);
  });

  test("B2B costs endpoint supports date range filtering", async ({
    request,
  }) => {
    const response = await request.get(
      `${BASE}/api/v1/analytics/costs?from=2024-01-01&to=2025-12-31`,
    );
    expect(response.status()).toBe(200);

    const body = await response.json();
    expect(body.data.period).toMatchObject({
      from: "2024-01-01",
      to: "2025-12-31",
    });
  });
});
