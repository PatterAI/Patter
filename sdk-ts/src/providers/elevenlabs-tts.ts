const ELEVENLABS_BASE_URL = 'https://api.elevenlabs.io/v1';

export class ElevenLabsTTS {
  constructor(
    private readonly apiKey: string,
    private readonly voiceId: string = '21m00Tcm4TlvDq8ikWAM',
    private readonly modelId: string = 'eleven_turbo_v2_5',
    private readonly outputFormat: string = 'pcm_16000',
  ) {}

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
      reader.releaseLock();
    }
  }
}
