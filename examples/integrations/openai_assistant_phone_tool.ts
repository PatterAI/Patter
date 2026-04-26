/**
 * Example: expose Patter as a tool in an OpenAI Assistants run.
 *
 * Pattern: your existing OpenAI Assistant orchestrates the conversation; when
 * it decides to make a phone call, it emits a `make_phone_call` tool_call;
 * your dispatcher invokes `tool.execute(...)` and submits the JSON result back.
 *
 * Required env:
 *   OPENAI_API_KEY
 *   TWILIO_ACCOUNT_SID
 *   TWILIO_AUTH_TOKEN
 *   TWILIO_PHONE_NUMBER
 *   PATTER_WEBHOOK_URL     stable HTTPS hostname, e.g. agent.example.com
 *   DEEPGRAM_API_KEY
 *   GROQ_API_KEY
 *   ELEVENLABS_API_KEY
 */
import 'dotenv/config';
import OpenAI from 'openai';
import {
  Patter,
  Twilio,
  DeepgramSTT,
  GroqLLM,
  ElevenLabsTTS,
  PatterTool,
} from 'getpatter';

const phone = new Patter({
  carrier: new Twilio(),
  phoneNumber: process.env.TWILIO_PHONE_NUMBER!,
  webhookUrl: process.env.PATTER_WEBHOOK_URL!,
});

const tool = new PatterTool({
  phone,
  agent: {
    stt: new DeepgramSTT(),
    llm: new GroqLLM(),
    tts: new ElevenLabsTTS(),
  },
});

await tool.start();

const openai = new OpenAI();

const completion = await openai.chat.completions.create({
  model: 'gpt-4o',
  messages: [
    {
      role: 'system',
      content:
        'You are a helpful assistant. When the user asks you to phone someone, ' +
        'use the make_phone_call tool. Pass a clear `goal` so the in-call agent ' +
        'knows what to do.',
    },
    {
      role: 'user',
      content: 'Please call +15551234567 and book a haircut for tomorrow at 3pm.',
    },
  ],
  tools: [tool.openaiSchema()],
});

const toolCall = completion.choices[0].message.tool_calls?.[0];
if (toolCall && toolCall.type === 'function' && toolCall.function.name === tool.name) {
  const args = JSON.parse(toolCall.function.arguments);
  const result = await tool.execute(args);
  console.log('Call result:', JSON.stringify(result, null, 2));
}

await tool.stop();
