const ELEVENLABS_BASE_URL = 'https://api.elevenlabs.io/v1';

// Curated map of common ElevenLabs voice display names to their voice IDs.
// The public API only accepts voice IDs — callers that pass a human-readable
// name like "rachel" would otherwise hit 404. Mirrors the Python SDK map.
const ELEVENLABS_VOICE_ID_BY_NAME: Record<string, string> = {
  rachel: '21m00Tcm4TlvDq8ikWAM',
  drew: '29vD33N1CtxCmqQRPOHJ',
  clyde: '2EiwWnXFnvU5JabPnv8n',
  paul: '5Q0t7uMcjvnagumLfvZi',
  domi: 'AZnzlk1XvdvUeBnXmlld',
  dave: 'CYw3kZ02Hs0563khs1Fj',
  fin: 'D38z5RcWu1voky8WS1ja',
  bella: 'EXAVITQu4vr4xnSDxMaL',
  antoni: 'ErXwobaYiN019PkySvjV',
  thomas: 'GBv7mTt0atIp3Br8iCZE',
  charlie: 'IKne3meq5aSn9XLyUdCD',
  george: 'JBFqnCBsd6RMkjVDRZzb',
  emily: 'LcfcDJNUP1GQjkzn1xUU',
  elli: 'MF3mGyEYCl7XYWbV9V6O',
  callum: 'N2lVS1w4EtoT3dr4eOWO',
  patrick: 'ODq5zmih8GrVes37Dizd',
  harry: 'SOYHLrjzK2X1ezoPC6cr',
  liam: 'TX3LPaxmHKxFdv7VOQHJ',
  dorothy: 'ThT5KcBeYPX3keUQqHPh',
  josh: 'TxGEqnHWrfWFTfGW9XjX',
  arnold: 'VR6AewLTigWG4xSOukaG',
  charlotte: 'XB0fDUnXU5powFXDhCwa',
  matilda: 'XrExE9yKIg1WjnnlVkGX',
  matthew: 'Yko7PKHZNXotIFUBG7I9',
  james: 'ZQe5CZNOzWyzPSCn5a3c',
  joseph: 'Zlb1dXrM653N07WRdFW3',
  jeremy: 'bVMeCyTHy58xNoL34h3p',
  michael: 'flq6f7yk4E4fJM5XTYuZ',
  ethan: 'g5CIjZEefAph4nQFvHAz',
  gigi: 'jBpfuIE2acCO8z3wKNLl',
  freya: 'jsCqWAovK2LkecY7zXl4',
  brian: 'nPczCjzI2devNBz1zQrb',
  grace: 'oWAxZDx7w5VEj9dCyTzz',
  daniel: 'onwK4e9ZLuTAKqWW03F9',
  lily: 'pFZP5JQG7iQjIQuC4Bku',
  serena: 'pMsXgVXv3BLzUgSXRplE',
  adam: 'pNInz6obpgDQGcFmaJgB',
  nicole: 'piTKgcLEGmPE4e6mEKli',
  bill: 'pqHfZKP75CvOlQylNhV4',
  jessie: 't0jbNlBVZ17f02VDIeMI',
  ryan: 'wViXBPUzp2ZZixB1xQuM',
  sam: 'yoZ06aMxZJJ28mfd3POQ',
  glinda: 'z9fAnlkpzviPz146aGWa',
  giovanni: 'zcAOhNBS3c14rBihAFp1',
  mimi: 'zrHiDhphv9ZnVXBqCLjz',
  alloy: '21m00Tcm4TlvDq8ikWAM',
};

const VOICE_ID_PATTERN = /^[A-Za-z0-9]{20}$/;

/**
 * Return an ElevenLabs voice ID from either a UUID-like ID or a display name.
 *
 * Opaque ElevenLabs voice IDs are 20-char alphanumeric tokens — anything
 * matching that shape is returned verbatim. Known display names (case-
 * insensitive) are resolved via the internal table. Unknown strings are
 * returned as-is so custom voices keep working.
 */
export function resolveVoiceId(voice: string): string {
  if (!voice) return voice;
  if (VOICE_ID_PATTERN.test(voice)) return voice;
  return ELEVENLABS_VOICE_ID_BY_NAME[voice.toLowerCase()] ?? voice;
}

export class ElevenLabsTTS {
  private readonly voiceId: string;

  constructor(
    private readonly apiKey: string,
    voiceId: string = '21m00Tcm4TlvDq8ikWAM',
    private readonly modelId: string = 'eleven_turbo_v2_5',
    private readonly outputFormat: string = 'pcm_16000',
  ) {
    this.voiceId = resolveVoiceId(voiceId);
  }

  /**
   * Synthesise text to speech and return the full audio as a single Buffer.
   *
   * For large chunks (or when latency matters) call `synthesizeStream` instead.
   */
  async synthesize(text: string): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of this.synthesizeStream(text)) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  /**
   * Synthesise text and yield audio chunks as they arrive (streaming).
   *
   * The yielded buffers are raw PCM at 16 kHz (or whatever `outputFormat` is
   * configured to).
   */
  async *synthesizeStream(text: string): AsyncGenerator<Buffer> {
    const url = `${ELEVENLABS_BASE_URL}/text-to-speech/${encodeURIComponent(this.voiceId)}/stream?output_format=${encodeURIComponent(this.outputFormat)}`;

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'xi-api-key': this.apiKey,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ text, model_id: this.modelId }),
      signal: AbortSignal.timeout(30_000),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`ElevenLabs TTS error ${response.status}: ${body}`);
    }

    if (!response.body) {
      throw new Error('ElevenLabs TTS: no response body');
    }

    const reader = response.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value && value.length > 0) {
          yield Buffer.from(value);
        }
      }
    } finally {
      // Cancel the HTTP stream to stop ElevenLabs from synthesizing further
      // characters (they bill per character, even if we stop consuming).
      if (typeof reader.cancel === 'function') await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}
