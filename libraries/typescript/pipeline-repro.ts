/**
 * End-to-end local verification of the Gemini pipeline:
 *   caller audio → GeminiSTT (tone) → GoogleLLM (Ashley reply) → GeminiTTS (Kore).
 * Proves the full chain with the real APIs + funded key before deploy. Throwaway.
 */
import { readFileSync } from 'node:fs';
import { GeminiSTT } from './src/providers/gemini-stt';
import { GeminiTTS } from './src/providers/gemini-tts';
import { GoogleLLMProvider } from './src/providers/google-llm';

const KEY = (readFileSync('/Users/fra/Desktop/codes/patter/.env', 'utf8').match(/^GEMINI_API_KEY=(.+)$/m)?.[1] ?? '').trim();
const CALLER_WAV = '/Users/fra/Desktop/codes/patter/code/demos/phone-number/spikes/gemini-voice-spike/out/caller-16k.wav';

const PERSONA =
  "You are Ashley, a Patter customer-support rep doing customer discovery. Californian, professional, straightforward, warm but measured. One or two sentences. The caller's transcript is prefixed with their detected vocal tone in [tone: ...] — mirror their mood. Annotate your reply with delivery cues like [warm] / [short pause]. Ask what they're building.";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main(): Promise<void> {
  const t0 = Date.now();
  const ms = () => Date.now() - t0;
  console.log(`key ${KEY.slice(0, 6)}...${KEY.slice(-4)}\n`);

  // --- STT: feed caller audio, finalize, get tone-annotated transcript ---
  const stt = new GeminiSTT(KEY);
  let transcript = '';
  stt.onTranscript((t) => { transcript = t.text; });
  await stt.connect();
  const pcm16k = readFileSync(CALLER_WAV).subarray(44);
  for (let off = 0; off < pcm16k.length; off += 640) stt.sendAudio(pcm16k.subarray(off, off + 640));
  console.log(`[${ms()}ms] caller audio buffered (${(pcm16k.length / 32000).toFixed(1)}s); finalize...`);
  stt.finalize();
  for (let i = 0; i < 60 && !transcript; i++) await sleep(100);
  console.log(`[${ms()}ms] STT transcript: ${JSON.stringify(transcript)}`);
  if (!transcript) { console.log('VERDICT: STT SILENT'); process.exit(1); }

  // --- LLM: Ashley generates a reply (streaming, with bracket tags) ---
  const llm = new GoogleLLMProvider({ apiKey: KEY, model: 'gemini-2.5-flash' });
  let reply = '';
  const llmStart = ms();
  let firstTokMs: number | null = null;
  for await (const chunk of llm.stream(
    [{ role: 'system', content: PERSONA }, { role: 'user', content: transcript }],
    null,
  )) {
    if (chunk.type === 'text') { if (firstTokMs === null) firstTokMs = ms() - llmStart; reply += chunk.content; }
  }
  console.log(`[${ms()}ms] LLM reply (first tok +${firstTokMs}ms): ${JSON.stringify(reply)}`);
  if (!reply) { console.log('VERDICT: LLM SILENT'); process.exit(1); }
  const hasTags = /\[[a-z][a-z ]+\]/i.test(reply);
  console.log(`[${ms()}ms] bracket tags present: ${hasTags}`);

  // --- TTS: synthesize Ashley's reply in Kore, count audio ---
  const tts = new GeminiTTS(KEY, 'Kore');
  let ttsBytes = 0;
  let ttsFirstMs: number | null = null;
  const ttsStart = ms();
  for await (const buf of tts.synthesizeStream(reply)) { if (ttsFirstMs === null) ttsFirstMs = ms() - ttsStart; ttsBytes += buf.length; }
  console.log(`[${ms()}ms] TTS Kore audio: ${ttsBytes} bytes (first chunk +${ttsFirstMs}ms)`);

  console.log(`\n=== VERDICT: STT=${transcript ? 'OK' : 'FAIL'} LLM=${reply ? 'OK' : 'FAIL'} tags=${hasTags ? 'OK' : 'none'} TTS=${ttsBytes > 0 ? 'OK' : 'SILENT'} ===`);
  await stt.close();
  process.exit(0);
}
main().catch((e) => { console.error('fatal:', e); process.exit(1); });
