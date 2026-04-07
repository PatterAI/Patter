import { describe, it, expect } from "vitest";
import { PatterConnection } from "../src/connection";

describe("PatterConnection", () => {
  it("builds correct WebSocket URL", () => {
    const conn = new PatterConnection("pt_test", "wss://api.patter.dev");
    expect(conn["wsUrl"]).toBe("wss://api.patter.dev/ws/sdk");
  });

  it("strips trailing slashes from backend URL", () => {
    const conn = new PatterConnection("pt_test", "wss://api.patter.dev/");
    expect(conn["wsUrl"]).toBe("wss://api.patter.dev/ws/sdk");
  });

  it("isConnected is false when not connected", () => {
    const conn = new PatterConnection("pt_test");
    expect(conn.isConnected).toBe(false);
  });

  it("parseMessage returns IncomingMessage for message type", () => {
    const conn = new PatterConnection("pt_test");
    const msg = conn.parseMessage(
      JSON.stringify({
        type: "message",
        text: "hello",
        call_id: "c1",
        caller: "+39111",
      })
    );
    expect(msg).toEqual({
      text: "hello",
      callId: "c1",
      caller: "+39111",
    });
  });

  it("parseMessage returns null for non-message type", () => {
    const conn = new PatterConnection("pt_test");
    expect(
      conn.parseMessage(JSON.stringify({ type: "call_start", call_id: "c1" }))
    ).toBeNull();
  });

  it("parseMessage returns null for invalid JSON", () => {
    const conn = new PatterConnection("pt_test");
    expect(conn.parseMessage("not json")).toBeNull();
  });
});
