/**
 * Local empirical verification of GeminiCascadeAdapter against the REAL Gemini
 * Live + TTS APIs with the funded key. Proves: setupComplete fires (TEXT +
 * enableAffectiveDialog), the intro clip produces audio, and a fed caller
 * utterance yields reply audio. Throwaway diagnostic.
 *
 * SECURITY: key is read at runtime from the gitignored root .env; never echoed.
 */
import { readFileSync } from 'node:fs';
import { GeminiCascadeAdapter } from './src/providers/gemini-cascade';
import { StatefulResampler, pcm16ToMulaw } from './src/audio/transcoding';

const ROOT_ENV = '/Users/fra/Desktop/codes/patter/.env';
const CALLER_WAV = '/Users/fra/Desktop/codes/patter/code/demos/phone-number/spikes/gemini-voice-spike/out/caller-16k.wav';

const KEY = (readFileSync(ROOT_ENV, 'utf8').match(/^GEMINI_API_KEY=(.+)$/m)?.[1] ?? '').trim();
if (!KEY) throw new Error('GEMINI_API_KEY not in root .env');
console.log(`key: ${KEY.slice(0, 6)}...${KEY.slice(-4)} (len ${KEY.length})`);

const PERSONA =
  'You are Ashley, a Patter customer-support rep doing customer discovery. Warm, professional, straightforward. One or two sentences. Use [warm] / [short pause] delivery tags. Ask what the caller is building.';

/** caller-16k.wav (PCM16 @16k) → mulaw 8k 160-byte frames. */
function callerFrames(): Buffer[] {
  const wav = readFileSync(CALLER_WAV);
  const pcm16k = wav.subarray(44); // strip canonical WAV header
  const pcm8k = new StatefulResampler({ srcRate: 16000, dstRate: 8000 }).process(pcm16k);
  const mulaw = pcm16ToMulaw(pcm8k);
  const frames: Buffer[] = [];
  for (let off = 0; off + 160 <= mulaw.length; off += 160) {
    frames.push(Buffer.from(mulaw.subarray(off, off + 160)));
  }
  return frames;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main(): Promise<void> {
  const t0 = Date.now();
  const ms = () => Date.now() - t0;
  const adapter = new GeminiCascadeAdapter(KEY, {
    voice: 'Kore',
    instructions: PERSONA,
    tools: [{ name: 'web_search', description: 'Search the web', parameters: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] } }],
  });

  let audioBytes = 0;
  let firstAudioMs: number | null = null;
  let introAudioBytes = 0;
  let replyAudioBytes = 0;
  let callerSentMs: number | null = null;
  const transcriptsIn: string[] = [];
  const transcriptsOut: string[] = [];

  console.log(`[${ms()}ms] connecting...`);
  await adapter.connect();
  console.log(`[${ms()}ms] connected (setupComplete fired)`);

  adapter.onEvent((type, data) => {
    if (type === 'audio') {
      const b = data as Buffer;
      audioBytes += b.length;
      if (firstAudioMs === null) firstAudioMs = ms();
      if (callerSentMs === null) introAudioBytes += b.length;
      else replyAudioBytes += b.length;
    } else if (type === 'transcript_input') transcriptsIn.push(data as string);
    else if (type === 'transcript_output') transcriptsOut.push(data as string);
    else if (type === 'response_done') console.log(`[${ms()}ms] response_done`);
    else if (type === 'speech_started') console.log(`[${ms()}ms] speech_started (barge-in)`);
    else if (type === 'function_call') console.log(`[${ms()}ms] function_call: ${JSON.stringify(data)}`);
    else if (type === 'error') console.log(`[${ms()}ms] ERROR: ${String(data)}`);
  });

  // Greeting → intro clip
  await adapter.sendFirstMessage('Hi, this is Ashley over at Patter. Thanks for calling in. So — what are you building these days?');
  console.log(`[${ms()}ms] sendFirstMessage done; waiting for intro audio...`);
  await sleep(4000);
  console.log(`[${ms()}ms] intro audio so far: ${introAudioBytes} bytes`);

  // Feed caller utterance
  const frames = callerFrames();
  console.log(`[${ms()}ms] feeding ${frames.length} caller frames (${(frames.length * 0.02).toFixed(1)}s)...`);
  callerSentMs = ms();
  for (const f of frames) { adapter.sendAudio(f); await sleep(20); }
  console.log(`[${ms()}ms] caller audio fed; waiting for reply...`);
  await sleep(9000);

  console.log('\n=== RESULT ===');
  console.log(`total audio bytes: ${audioBytes}`);
  console.log(`intro audio bytes: ${introAudioBytes}  reply audio bytes: ${replyAudioBytes}`);
  console.log(`first audio at: ${firstAudioMs}ms`);
  console.log(`transcript_input: ${JSON.stringify(transcriptsIn.join(' '))}`);
  console.log(`transcript_output: ${JSON.stringify(transcriptsOut.join(' '))}`);
  console.log(`VERDICT: intro=${introAudioBytes > 0 ? 'OK' : 'SILENT'} reply=${replyAudioBytes > 0 ? 'OK' : 'SILENT'}`);

  await adapter.close();
  process.exit(0);
}

main().catch((e) => { console.error('fatal:', e); process.exit(1); });
