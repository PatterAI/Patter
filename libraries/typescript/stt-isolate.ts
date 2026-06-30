/** De-risk: can Gemini generateContent read caller TONE from audio + transcribe? */
import { readFileSync } from 'node:fs';
import { GoogleGenAI } from '@google/genai';

const KEY = (readFileSync('/Users/fra/Desktop/codes/patter/.env', 'utf8').match(/^GEMINI_API_KEY=(.+)$/m)?.[1] ?? '').trim();
const WAV = '/Users/fra/Desktop/codes/patter/code/demos/phone-number/spikes/gemini-voice-spike/out/caller-16k.wav';

const PROMPT =
  'You are the transcription stage of a phone system. Transcribe the caller verbatim, then judge their emotional tone from HOW they sound (energy, pace, pitch) — not just words. Output exactly one line: [tone: <1-2 words>] <transcript>';

async function probe(model: string): Promise<void> {
  const ai = new GoogleGenAI({ apiKey: KEY });
  const audioB64 = readFileSync(WAV).toString('base64');
  const t0 = Date.now();
  try {
    const res = await ai.models.generateContent({
      model,
      contents: [{ role: 'user', parts: [
        { text: PROMPT },
        { inlineData: { mimeType: 'audio/wav', data: audioB64 } },
      ] }],
      config: { temperature: 0.2, maxOutputTokens: 200 },
    });
    const text = (res as { text?: string }).text
      ?? (res as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> }).candidates?.[0]?.content?.parts?.map((p) => p.text).join('') ?? '';
    console.log(`PASS ${model} (${Date.now() - t0}ms): ${JSON.stringify(text.trim())}`);
  } catch (e) {
    console.log(`FAIL ${model}: ${(e as Error).message}`);
  }
}

async function main(): Promise<void> {
  console.log(`key ${KEY.slice(0, 6)}...${KEY.slice(-4)}\n`);
  await probe('gemini-3.1-flash');
  await probe('gemini-2.5-flash');
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
