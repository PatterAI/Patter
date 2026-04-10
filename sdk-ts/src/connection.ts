import WebSocket from "ws";
import type { IncomingMessage, MessageHandler, CallEventHandler } from "./types";
import { PatterConnectionError } from "./errors";
import { getLogger } from "./logger";

const DEFAULT_BACKEND_URL = "wss://api.patter.dev";

export class PatterConnection {
  readonly apiKey: string;
  readonly backendUrl: string;
  private wsUrl: string;
  private ws: WebSocket | null = null;
  private onMessage: MessageHandler | null = null;
  private onCallStart: CallEventHandler | null = null;
  private onCallEnd: CallEventHandler | null = null;

  constructor(apiKey: string, backendUrl: string = DEFAULT_BACKEND_URL) {
    this.apiKey = apiKey;
    this.backendUrl = backendUrl.replace(/\/+$/, "");
    this.wsUrl = `${this.backendUrl}/ws/sdk`;
  }

  get isConnected(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }

  async connect(options: {
    onMessage: MessageHandler;
    onCallStart?: CallEventHandler;
    onCallEnd?: CallEventHandler;
  }): Promise<void> {
    this.onMessage = options.onMessage;
    this.onCallStart = options.onCallStart ?? null;
    this.onCallEnd = options.onCallEnd ?? null;

    return new Promise<void>((resolve, reject) => {
      this.ws = new WebSocket(this.wsUrl, {
        headers: { "X-API-Key": this.apiKey },
      });

      this.ws.on("open", () => {
        this.setupListeners();
        resolve();
      });

      this.ws.on("error", (err) => {
        reject(new PatterConnectionError(`Failed to connect: ${err.message}`));
      });
    });
  }

  private setupListeners(): void {
    if (!this.ws) return;

    this.ws.on("error", (err) => {
      getLogger().error(`WebSocket error: ${err.message}`);
    });

    this.ws.on("message", async (data: WebSocket.Data) => {
      const raw = data.toString();
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(raw);
      } catch {
        return;
      }

      const msgType = parsed.type as string;

      if (msgType === "message" && this.onMessage) {
        const msg: IncomingMessage = {
          text: parsed.text as string,
          callId: parsed.call_id as string,
          caller: (parsed.caller as string) ?? "",
        };
        try {
          const response = await this.onMessage(msg);
          if (response != null) {
            await this.sendResponse(msg.callId, response);
          }
        } catch {
          // Don't crash on handler errors
        }
      } else if (msgType === "call_start" && this.onCallStart) {
        await this.onCallStart(parsed);
      } else if (msgType === "call_end" && this.onCallEnd) {
        await this.onCallEnd(parsed);
      }
    });

    this.ws.on("close", () => {
      this.ws = null;
    });
  }

  async sendResponse(callId: string, text: string): Promise<void> {
    if (!this.ws) throw new PatterConnectionError("Not connected");
    this.ws.send(JSON.stringify({ type: "response", call_id: callId, text }));
  }

  async requestCall(fromNumber: string, toNumber: string, firstMessage: string = ""): Promise<void> {
    if (!this.ws) throw new PatterConnectionError("Not connected");
    this.ws.send(
      JSON.stringify({
        type: "call",
        from: fromNumber,
        to: toNumber,
        first_message: firstMessage,
      })
    );
  }

  async disconnect(): Promise<void> {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  parseMessage(raw: string): IncomingMessage | null {
    try {
      const data = JSON.parse(raw);
      if (data.type !== "message") return null;
      return {
        text: data.text,
        callId: data.call_id,
        caller: data.caller ?? "",
      };
    } catch {
      return null;
    }
  }
}
