# TypeScript Core — Test Report

**Run at:** 2026-04-12T03:22:55Z
**Working dir:** /Users/francescorosciano/docs/patter/Patter/sdk-ts
**Command:** `npx vitest run tests/call-control.test.ts tests/deepgram-stt.test.ts tests/elevenlabs-convai.test.ts tests/handler-utils.test.ts tests/llm-loop.test.ts tests/metrics.test.ts tests/new-features.test.ts tests/openai-realtime.test.ts tests/openai-tts.test.ts tests/pricing.test.ts tests/providers.test.ts tests/remote-message.test.ts tests/server.test.ts tests/test-mode.test.ts tests/transcoding.test.ts tests/types.test.ts tests/whisper-stt.test.ts tests/client.test.ts tests/connection.test.ts --reporter=verbose`
**Runtime:** 18 seconds (wall clock); vitest self-reported 11.99s

## Summary
- Test files: 19 passed / 0 failed
- Tests: 286 passed / 0 failed / 0 skipped
- Total: 286
- Exit code: 0

## Failures
None.

## Full output (tail)
```
 ✓ tests/providers.test.ts > elevenlabs > returns TTSConfig with default voice
 ✓ tests/providers.test.ts > elevenlabs > accepts custom voice
 ✓ tests/providers.test.ts > openaiTts > returns TTSConfig with default voice
 ✓ tests/providers.test.ts > openaiTts > accepts custom voice
 ✓ tests/providers.test.ts > DeepgramSTT > forTwilio() configures mulaw 8kHz
 ✓ tests/providers.test.ts > DeepgramSTT > default constructor uses linear16 16kHz
 ✓ tests/providers.test.ts > DeepgramSTT > stores the api key
 ✓ tests/providers.test.ts > ElevenLabsTTS > stores api key and voice id
 ✓ tests/providers.test.ts > ElevenLabsTTS > uses default voice id when not specified
 ✓ tests/providers.test.ts > ElevenLabsTTS > uses eleven_turbo_v2_5 model by default
 ✓ tests/providers.test.ts > ElevenLabsTTS > uses pcm_16000 output format by default
 ✓ tests/pricing.test.ts > DEFAULT_PRICING > includes all expected providers
 ✓ tests/pricing.test.ts > mergePricing > returns defaults when no overrides
 ✓ tests/pricing.test.ts > mergePricing > overrides individual provider values
 ✓ tests/pricing.test.ts > mergePricing > adds new providers
 ✓ tests/pricing.test.ts > calculateSttCost > calculates deepgram cost for 60 seconds
 ✓ tests/pricing.test.ts > calculateSttCost > returns 0 for unknown provider
 ✓ tests/pricing.test.ts > calculateTtsCost > calculates elevenlabs cost for 1000 characters
 ✓ tests/pricing.test.ts > calculateTtsCost > calculates openai_tts cost
 ✓ tests/pricing.test.ts > calculateRealtimeCost > calculates from token usage
 ✓ tests/pricing.test.ts > calculateRealtimeCost > returns 0 for empty usage
 ✓ tests/pricing.test.ts > calculateTelephonyCost > calculates twilio cost for 120 seconds
 ✓ tests/pricing.test.ts > calculateTelephonyCost > calculates telnyx cost
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > initializes with required api key
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > accepts custom model
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > accepts custom voice
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > accepts instructions string
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > accepts tools array
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > accepts empty tools array
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > is not connected initially
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > sendAudio does not throw when not connected
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > cancelResponse does not throw when not connected
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > close can be called multiple times without error
 ✓ tests/openai-realtime.test.ts > OpenAIRealtimeAdapter > onEvent does not throw when not connected
 ✓ tests/call-control.test.ts > CallControl interface > can be implemented with required fields
 ✓ tests/call-control.test.ts > CallControl interface > transfer and hangup are callable
 ✓ tests/call-control.test.ts > ToolDefinition with local handler > supports handler function alongside webhookUrl
 ✓ tests/call-control.test.ts > ToolDefinition with local handler > handler is optional
 ✓ tests/deepgram-stt.test.ts > DeepgramSTT > initializes with required api key
 ✓ tests/deepgram-stt.test.ts > DeepgramSTT > accepts custom language
 ✓ tests/deepgram-stt.test.ts > DeepgramSTT > accepts custom model
 ✓ tests/deepgram-stt.test.ts > DeepgramSTT > accepts custom encoding
 ✓ tests/deepgram-stt.test.ts > DeepgramSTT > accepts custom sample rate
 ✓ tests/deepgram-stt.test.ts > DeepgramSTT > forTwilio creates instance with mulaw 8kHz
 ✓ tests/deepgram-stt.test.ts > DeepgramSTT > forTwilio accepts custom language
 ✓ tests/deepgram-stt.test.ts > DeepgramSTT > close does not throw when not connected
 ✓ tests/deepgram-stt.test.ts > DeepgramSTT > sendAudio does not throw when not connected
 ✓ tests/connection.test.ts > PatterConnection > builds correct WebSocket URL
 ✓ tests/connection.test.ts > PatterConnection > strips trailing slashes from backend URL
 ✓ tests/connection.test.ts > PatterConnection > isConnected is false when not connected
 ✓ tests/connection.test.ts > PatterConnection > parseMessage returns IncomingMessage for message type
 ✓ tests/connection.test.ts > PatterConnection > parseMessage returns null for non-message type
 ✓ tests/connection.test.ts > PatterConnection > parseMessage returns null for invalid JSON
 ✓ tests/types.test.ts > STTConfig > toDict includes all fields
 ✓ tests/types.test.ts > STTConfig > is readonly
 ✓ tests/types.test.ts > TTSConfig > toDict includes all fields
 ✓ tests/transcoding.test.ts > mulawToPcm16 > decodes silence (mulaw 0xFF) to near-zero PCM
 ✓ tests/transcoding.test.ts > mulawToPcm16 > doubles the buffer length
 ✓ tests/transcoding.test.ts > mulawToPcm16 > produces valid signed 16-bit values
 ✓ tests/transcoding.test.ts > pcm16ToMulaw > encodes silence (PCM 0) to mulaw 0xFF
 ✓ tests/transcoding.test.ts > pcm16ToMulaw > halves the buffer length
 ✓ tests/transcoding.test.ts > pcm16ToMulaw > ignores trailing odd byte
 ✓ tests/transcoding.test.ts > mulaw round-trip > round-trips all 256 mulaw values within tolerance
 ✓ tests/transcoding.test.ts > mulaw round-trip > round-trips known audio values
 ✓ tests/transcoding.test.ts > resample8kTo16k > returns empty buffer for empty input
 ✓ tests/transcoding.test.ts > resample8kTo16k > doubles the number of samples
 ✓ tests/transcoding.test.ts > resample8kTo16k > interpolates between consecutive samples
 ✓ tests/transcoding.test.ts > resample16kTo8k > returns empty buffer for empty input
 ✓ tests/transcoding.test.ts > resample16kTo8k > halves the number of samples
 ✓ tests/transcoding.test.ts > resample16kTo8k > takes every 2nd sample
 ✓ tests/transcoding.test.ts > resample24kTo16k > returns empty buffer for empty input
 ✓ tests/transcoding.test.ts > resample24kTo16k > takes 2 of every 3 samples
 ✓ tests/transcoding.test.ts > resample24kTo16k > produces correct output length for various input sizes
 ✓ tests/transcoding.test.ts > resample24kTo16k > handles non-multiple-of-3 sample counts

 Test Files  19 passed (19)
      Tests  286 passed (286)
   Start at  20:22:43
   Duration  11.99s (transform 550ms, setup 0ms, collect 2.84s, tests 1.94s, environment 3ms, prepare 1.31s)
```
