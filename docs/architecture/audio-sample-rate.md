# Rate-aware outbound audio: provider rate → carrier wire

Status: **accepted** — implemented in both SDKs.
Audience: SDK maintainers. Internal architecture document, intentionally
outside `docs/docs.json` nav (same convention as `pipeline-stages.md`).

Scope: the pipeline-mode TTS → carrier audio path in
`libraries/python/getpatter/stream_handler.py` +
`libraries/python/getpatter/telephony/{twilio,telnyx,plivo}.py` and
`libraries/typescript/src/stream-handler.ts` +
`libraries/typescript/src/audio/{format,transcoding}.ts`.

---

## 1. The bug this fixes

The outbound sender used to **assume every TTS provider emits PCM16 @ 16 kHz**
and ran a hardcoded 16 kHz → 8 kHz decimator before μ-law encoding for the
carrier wire. Any provider configured for a different rate broke:

- `CartesiaTTS({ sampleRate: 8000 })` on Twilio → the 8 kHz PCM was decimated
  *again* (treated as 16 kHz) → ~2× pitch ("chipmunk" voice).
- Leaving the default 16 kHz gave correct pitch but the gentle 5-tap decimator
  under-attenuated, leaving an aliasing hiss.

## 2. Single source of truth for formats

`audio/format.ts` / `getpatter/audio/format.py` define one `AudioFormat`
(`encoding` + `sampleRate`) and two facts:

- **`CARRIER_WIRE_FORMAT`** — every supported carrier's media stream is G.711
  **μ-law @ 8 kHz**: Twilio always; Telnyx because `streaming_start` pins
  `stream_bidirectional_codec=PCMU`; Plivo because the answer XML pins the
  μ-law content type. This is the *one* place "what the wire wants" lives.
- **`resolveTtsSourceFormat(tts)`** — what the TTS adapter actually emits,
  resolved in priority order:
  1. `sourceAudioFormat()` (uniform contract, preferred),
  2. `outputFormat` string (ElevenLabs-style: `ulaw_8000`, `pcm_16000`, …),
  3. `sampleRate` field (Cartesia/OpenAI/LMNT/Rime/Inworld),
  4. legacy fallback = PCM16 @ 16 kHz (preserves pre-fix behaviour for any
     custom adapter that declares nothing — fully backward compatible).

## 3. The decision (once, at pipeline init)

`StreamHandler.configureOutboundAudio()` (TS) /
`PipelineStreamHandler.start()` (Py) resolve the source format after
`setTelephonyCarrier`, then either:

- **Passthrough** — when the source already equals the carrier wire format
  (μ-law 8 kHz), forward bytes unchanged: **zero resample, zero re-encode,
  bit-clean**. This generalises the old ElevenLabs `ulaw_8000` fast path to
  *any* μ-law-native provider (e.g. `CartesiaTTS.forTwilio()` now requests
  `pcm_mulaw` @ 8 kHz).
- **Rate-aware resample** — otherwise build a resampler from the provider's
  **real** rate → the wire's 8 kHz, then μ-law encode. TS uses a new
  `RateAwareResampler`; Python uses `StatefulResampler` over `audioop.ratecv`
  (rate-agnostic and anti-aliased natively).

No caller config, no per-provider special-casing in the sender. Choosing 8 k
or 16 k on any provider now yields correct pitch and clean audio.

## 4. Anti-aliasing (downsample)

`RateAwareResampler` (TS) prepends a **linear-phase windowed-sinc FIR
low-pass** (`StatefulFirLowpass`) at ~`0.45 × dstRate` before fractional-phase
interpolation, so energy above the destination Nyquist is removed before
decimation has anything to fold. Tap count scales with the decimation ratio
and is clamped to keep the added group-delay latency **< ~1 ms** at telephony
rates (real-time safe — reported via `antiAliasLatencyMs`). `audioop.ratecv`
provides the equivalent band-limiting on the Python side.

## 5. Backward compatibility

- Adapters that declare nothing → legacy PCM16 @ 16 kHz path (unchanged).
- `forTwilio` / `forTelnyx` factories on Cartesia now emit μ-law 8 kHz
  natively (passthrough). The bare constructor default is unchanged
  (PCM16 @ 16 kHz for web/dashboard use).
- No public field removed or renamed; the new `encoding` option (Cartesia)
  and `sourceAudioFormat()` method are additive and optional.
