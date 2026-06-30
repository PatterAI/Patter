/** Isolate which Live config field blocks setupComplete in TEXT mode. */
import { readFileSync } from 'node:fs';
import { GoogleGenAI } from '@google/genai';

const KEY = (readFileSync('/Users/fra/Desktop/codes/patter/.env', 'utf8').match(/^GEMINI_API_KEY=(.+)$/m)?.[1] ?? '').trim();
const MODEL = 'gemini-3.1-flash-live-preview';
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function probe(label: string, config: Record<string, unknown>): Promise<void> {
  const ai = new GoogleGenAI({ apiKey: KEY });
  let setup = false, closed = '';
  const events: string[] = [];
  try {
    const session = await ai.live.connect({
      model: MODEL,
      config,
      callbacks: {
        onopen: () => events.push('open'),
        onmessage: (m: unknown) => { if ((m as { setupComplete?: unknown }).setupComplete) setup = true; events.push(Object.keys(m as object).join('+')); },
        onerror: (e: unknown) => events.push(`err:${(e as { message?: string })?.message ?? String(e)}`),
        onclose: (e: unknown) => { const c = e as { code?: number; reason?: string }; closed = `close ${c?.code} ${c?.reason ?? ''}`; },
      },
    });
    await sleep(4000);
    try { await (session as { close?: () => void }).close?.(); } catch { /* noop */ }
  } catch (e) {
    events.push(`throw:${(e as Error).message}`);
  }
  console.log(`${setup ? 'PASS' : 'FAIL'}  ${label}  | setup=${setup} | ${closed || 'no-close'} | ${events.slice(0, 6).join(',')}`);
}

async function main(): Promise<void> {
  console.log(`key ${KEY.slice(0, 6)}...${KEY.slice(-4)}\n`);
  await probe('A TEXT+affective+inputTx+tools', { responseModalities: ['TEXT'], inputAudioTranscription: {}, enableAffectiveDialog: true, temperature: 0.8, systemInstruction: { parts: [{ text: 'You are Ashley.' }] }, tools: [{ functionDeclarations: [{ name: 'web_search', description: 'Search', parameters: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] } }] }] });
  await probe('B TEXT only', { responseModalities: ['TEXT'], systemInstruction: { parts: [{ text: 'You are Ashley.' }] } });
  await probe('C TEXT+inputTx', { responseModalities: ['TEXT'], inputAudioTranscription: {}, systemInstruction: { parts: [{ text: 'You are Ashley.' }] } });
  await probe('D TEXT+affective', { responseModalities: ['TEXT'], enableAffectiveDialog: true, systemInstruction: { parts: [{ text: 'You are Ashley.' }] } });
  await probe('E TEXT+inputTx+tools', { responseModalities: ['TEXT'], inputAudioTranscription: {}, temperature: 0.8, systemInstruction: { parts: [{ text: 'You are Ashley.' }] }, tools: [{ functionDeclarations: [{ name: 'web_search', description: 'Search', parameters: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] } }] }] });
  await probe('F AUDIO baseline', { responseModalities: ['AUDIO'], inputAudioTranscription: {}, outputAudioTranscription: {}, systemInstruction: { parts: [{ text: 'You are Ashley.' }] } });
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
