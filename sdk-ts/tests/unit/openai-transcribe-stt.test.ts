/**
 * Tests for OpenAITranscribeSTT — first-class wrapper around gpt-4o-transcribe.
 */
import { describe, it, expect } from 'vitest';
import { OpenAITranscribeSTT } from '../../src/providers/openai-transcribe-stt';
import { STT as OpenAITranscribeSTTNamespaced } from '../../src/stt/openai-transcribe';

describe('OpenAITranscribeSTT', () => {
  describe('constructor', () => {
    it('defaults to gpt-4o-transcribe', () => {
      const stt = new OpenAITranscribeSTT('sk-test');
      // `model` is private on WhisperSTT — use the bracket-access shape that
      // matches the verification snippet in the task.
      expect((stt as unknown as { model: string }).model).toBe('gpt-4o-transcribe');
    });

    it('accepts gpt-4o-mini-transcribe', () => {
      const stt = new OpenAITranscribeSTT('sk-test', 'en', 'gpt-4o-mini-transcribe');
      expect((stt as unknown as { model: string }).model).toBe('gpt-4o-mini-transcribe');
    });

    it('rejects whisper-1', () => {
      expect(() => new OpenAITranscribeSTT('sk-test', 'en', 'whisper-1')).toThrow(
        /unsupported model/,
      );
    });

    it('namespaced STT class works with options object', () => {
      const stt = new OpenAITranscribeSTTNamespaced({ apiKey: 'sk-test' });
      expect((stt as unknown as { model: string }).model).toBe('gpt-4o-transcribe');
    });

    it('namespaced STT requires an api key', () => {
      const previous = process.env.OPENAI_API_KEY;
      delete process.env.OPENAI_API_KEY;
      try {
        expect(() => new OpenAITranscribeSTTNamespaced()).toThrow(/apiKey/);
      } finally {
        if (previous !== undefined) process.env.OPENAI_API_KEY = previous;
      }
    });
  });
});
