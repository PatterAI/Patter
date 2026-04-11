/**
 * Tool calling — agent checks inventory during the call.
 *
 * Tools are defined with a name, description, JSON schema, and a webhookUrl.
 * Patter calls the webhook when the agent decides to use the tool and feeds
 * the result back into the conversation.
 */
import { createServer } from "http";
import { Patter } from "getpatter";

// Mock inventory webhook — replace with your real endpoint
const toolServer = createServer((req, res) => {
  let body = "";
  req.on("data", (chunk) => (body += chunk));
  req.on("end", () => {
    const data: { arguments?: { product?: string } } = JSON.parse(body);
    const stock: Record<string, boolean> = { widget: true, gadget: false };
    const product = data.arguments?.product ?? "";
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ in_stock: stock[product] ?? false }));
  });
});
toolServer.listen(8001, () => console.log("Tool webhook listening on :8001"));

const phone = new Patter({
  mode: "local",
  twilioSid: "AC...",
  twilioToken: "...",
  openaiKey: "sk-...",
  phoneNumber: "+1...",
  webhookUrl: "xxx.ngrok-free.dev",
});

const agent = phone.agent({
  systemPrompt:
    "You help customers check product availability. " +
    "Always use the check_stock tool before answering stock questions.",
  voice: "alloy",
  firstMessage: "Hello! I can help you check if a product is in stock. What are you looking for?",
  tools: [
    {
      name: "check_stock",
      description: "Check if a product is in stock",
      parameters: {
        type: "object",
        properties: { product: { type: "string", description: "Product name to look up" } },
        required: ["product"],
      },
      webhookUrl: "http://localhost:8001/stock",
    },
  ],
});

console.log("Listening for calls...");
await phone.serve({ agent, port: 8000 });
