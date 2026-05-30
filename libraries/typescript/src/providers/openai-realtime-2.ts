/**
 * OpenAI Realtime adapter for the GA Realtime API (`gpt-realtime-2`).
 *
 * `gpt-realtime-2` is served from the same `wss://api.openai.com/v1/realtime`
 * endpoint as the v1-beta family, but the GA endpoint:
 *   - REJECTS the legacy `OpenAI-Beta: realtime=v1` header (returns
 *     `invalid_model` with message "Model X is only available on the GA API").
 *   - REQUIRES `session.type === "realtime"` at the root of `session.update`.
 *   - Uses `output_modalities` (was `modalities`).
 *   - Nests audio config under `audio.{input,output}` with MIME `type`
 *     strings (`audio/pcmu`, `audio/pcma`, `audio/pcm`) instead of the v1
 *     enum strings (`g711_ulaw`, `g711_alaw`, `pcm16`) and moves `voice`
 *     under `audio.output.voice`, `transcription` + `turn_detection`
 *     under `audio.input`.
 *
 * Everything ELSE (event names, audio delta dispatch, barge-in / truncate
 * semantics, heartbeat, tool calling) is API-compatible with the v1 family,
 * so this adapter subclasses {@link OpenAIRealtimeAdapter} and overrides
 * only `connect()`. The runtime behaviour (`sendAudio`, `cancelResponse`,
 * `sendText`, `sendFirstMessage`, …) is inherited unchanged.
 */

import WebSocket from 'ws';
import { getLogger } from '../logger';
import {
  OpenAIRealtimeAdapter,
  OpenAIRealtimeVADType,
  OpenAITranscriptionModel,
} from './openai-realtime';
import {
  mulawToPcm16,
  pcm16ToMulaw,
  StatefulResampler,
} from '../audio/transcoding';

/**
 * Mapping from GA Realtime event names back to the v1 names the rest of
 * Patter (`StreamHandler`, metrics, dashboard) listens for. The GA API
 * renamed several events but kept payload shapes identical, so we can
 * translate at the WebSocket boundary and reuse the v1 message handler
 * untouched. Empty target means "pass through unchanged".
 */
const GA_TO_V1_EVENT_NAMES: Readonly<Record<string, string>> = {
  'response.output_audio.delta': 'response.audio.delta',
  'response.output_audio.done': 'response.audio.done',
  'response.output_audio_transcript.delta': 'response.audio_transcript.delta',
  'response.output_audio_transcript.done': 'response.audio_transcript.done',
};

/**
 * Realtime WebSocket adapter speaking OpenAI's GA Realtime API.
 *
 * Note on audio transport: the GA endpoint accepts only PCM-16-LE with
 * `rate >= 24000` for both `session.audio.input.format` and
 * `session.audio.output.format`. The `audio/pcmu` MIME type appears to be
 * accepted at the protocol level but the server's audio engine does not
 * actually decode mulaw 8 kHz frames — they're silently dropped, the input
 * buffer stays empty, `input_audio_buffer.commit` returns
 * "buffer only has 0.00ms of audio", and the call ends up muted. Until
 * OpenAI documents native g711_ulaw on the GA endpoint we transcode on
 * both directions on the Patter side:
 *  - inbound (Twilio/Telnyx → model): mulaw 8 kHz → PCM 24 kHz
 *  - outbound (model → Twilio/Telnyx): PCM 24 kHz → mulaw 8 kHz
 *
 * The outbound path needs a stateful resampler instance because the
 * 24 kHz → 8 kHz decimator carries phase between chunks; sharing a single
 * instance across the call eliminates the boundary clicks a stateless
 * helper would produce on every audio delta.
 */
export class OpenAIRealtime2Adapter extends OpenAIRealtimeAdapter {
  /** Two-stage outbound resampler for 24 kHz → 8 kHz. Created lazily on
   *  the first audio frame so each Realtime session has its own state.
   *
   *  We chain `24k → 16k → 8k` instead of using the direct `24k → 8k`
   *  variant of {@link StatefulResampler}: the direct path is a 3:1
   *  decimation with linear interpolation only — no anti-alias filter
   *  — so any energy above 4 kHz in the source aliases down into the
   *  audible band and is heard as raspy/scratchy artefacts on speech.
   *  `gpt-realtime-2` outputs voice with significant content above
   *  4 kHz. The second stage (16k → 8k) uses a 5-tap FIR anti-alias
   *  filter which removes the offending band before decimation, and
   *  empirically (see commit message) the chain produces audibly
   *  cleaner output. The 24k → 16k step is still pure linear-interp
   *  but the inputs to it stay below the Nyquist of the 16 kHz stage,
   *  so it doesn't introduce new artefacts.
   */
  private outboundResampler24To16: StatefulResampler | null = null;
  private outboundResampler16To8: StatefulResampler | null = null;

  /** Last 8 kHz input sample carried across chunk boundaries for the
   *  direct 3× linear upsample (see `transcodeInboundMulaw8ToPcm24`).
   *  The carry guarantees the very first output of each chunk
   *  interpolates from the *real* preceding sample, not from the chunk's
   *  own first sample replicated — without it every 20 ms Twilio frame
   *  boundary becomes a small DC step that the GA server VAD interprets
   *  as constant low-energy noise, which never crosses the speech
   *  threshold. */
  private inbound8kCarry: number | null = null;

  /** GA-shape `session.update` payload. See module-level docstring. */
  private buildGASessionConfig(): Record<string, unknown> {
    const opts = this.options;
    // The GA endpoint requires audio/pcm with rate >= 24000 for both
    // directions. mulaw / pcma are not honoured by the audio engine
    // even though the protocol accepts the MIME type (see class doc).
    const fmt = { type: 'audio/pcm', rate: 24000 };
    const config: Record<string, unknown> = {
      type: 'realtime',
      output_modalities: opts.modalities ?? ['audio'],
      audio: {
        input: {
          format: fmt,
          transcription: {
            model: opts.inputAudioTranscriptionModel ?? OpenAITranscriptionModel.WHISPER_1,
          },
          // VAD threshold raised back to the OpenAI default (0.5) on
          // 2026-05-22. The earlier 0.1 tuning (motivated by the
          // upsampled telephony-band loss in high frequencies) made the
          // server VAD trigger on the carrier-loopback echo of the
          // agent's OWN outbound audio in PSTN no-AEC scenarios.
          // Combined with the default ``turn_detection.create_response:
          // true``, every phantom ``speech_started`` ended a turn early
          // and auto-created a new response that the agent immediately
          // spoke over, leading to a runaway loop where the first
          // message was repeatedly cut and re-generated.
          turn_detection: {
            type: opts.vadType ?? OpenAIRealtimeVADType.SERVER_VAD,
            threshold: 0.5,
            prefix_padding_ms: 300,
            silence_duration_ms: opts.silenceDurationMs ?? 300,
            // Defer ``response.create`` to the application: when OpenAI's
            // server VAD commits an ``input_audio_buffer.committed`` segment
            // that turns out to be a Whisper hallucination on silence/echo,
            // auto-creating a response would generate a phantom turn (the
            // model reads the hallucinated text as user input). Patter
            // triggers ``response.create`` explicitly in the Realtime
            // stream-handler AFTER validating ``transcript_input`` against
            // the hallucination filter. Pair with ``interrupt_response:
            // false`` so server VAD also leaves in-flight responses alone —
            // barge-in is gated client-side.
            create_response: false,
            interrupt_response: false,
          },
        },
        output: {
          format: fmt,
          voice: this.voice,
        },
      },
      instructions: this.instructions || 'You are a helpful voice assistant. Be concise.',
    };
    if (opts.temperature !== undefined) config.temperature = opts.temperature;
    if (opts.maxResponseOutputTokens !== undefined) {
      config.max_output_tokens = opts.maxResponseOutputTokens;
    }
    if (opts.toolChoice !== undefined) config.tool_choice = opts.toolChoice;
    if (opts.reasoningEffort !== undefined) {
      config.reasoning = { effort: opts.reasoningEffort };
    }
    if (this.tools?.length) {
      config.tools = this.tools.map((t) => {
        const def: Record<string, unknown> = {
          type: 'function',
          name: t.name,
          description: t.description,
          parameters: t.parameters,
        };
        if ((t as { strict?: boolean }).strict === true) def.strict = true;
        return def;
      });
    }
    return config;
  }

  /**
   * Open the Realtime WebSocket against the GA endpoint and apply the GA
   * session configuration. Header `OpenAI-Beta: realtime=v1` is OMITTED
   * (the GA endpoint rejects it). Wire shape uses nested `audio.{input,
   * output}` + `output_modalities` + `session.type === "realtime"`.
   */
  async connect(): Promise<void> {
    const url = `wss://api.openai.com/v1/realtime?model=${encodeURIComponent(this.model)}`;
    this.ws = new WebSocket(url, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });

    // Install a wire-level translation shim BEFORE any listener is
    // attached. We patch `ws.on` (not `ws.emit`) so that every
    // `ws.on('message', handler)` call — whether it comes from this
    // subclass' setup listener, the parent `ensureMessageListener`, or
    // any other code path — gets a wrapped handler that rewrites the
    // GA event-name aliases back to the v1 names before forwarding the
    // (re-serialised) frame on to the original handler. Patching `on`
    // is more robust than patching `emit` because the `ws` library
    // sometimes invokes `EventEmitter.prototype.emit.call(this, ...)`
    // internally — that bypass means an instance-level `emit` override
    // is silently skipped and the original (untranslated) frame reaches
    // every listener. Patching the registration entry point, on the
    // other hand, wraps the listener itself, which the emit path always
    // calls regardless of how emission is dispatched.
    //
    // Without this, GA event types fall through to the catch-all
    // (no-op) branch of the parent dispatcher and audio is silently
    // dropped — manifesting as a "successful" call with zero audio
    // bytes forwarded to Twilio/Telnyx.
    const wsRef = this.ws as unknown as {
      on: (event: string, handler: (...args: unknown[]) => void) => unknown;
    };
    const originalOn = wsRef.on.bind(this.ws);
    wsRef.on = (event: string, handler: (...args: unknown[]) => void): unknown => {
      if (event !== 'message') return originalOn(event, handler);
      const wrapped = (raw: unknown, ...rest: unknown[]): void => {
        try {
          const text = typeof raw === 'string' ? raw : (raw as Buffer).toString();
          const parsed = JSON.parse(text) as { type?: string };
          const t = parsed.type;
          if (t && t in GA_TO_V1_EVENT_NAMES) {
            const newType = GA_TO_V1_EVENT_NAMES[t];
            // Audio deltas need two transformations: rate transcoding
            // (PCM-24 → mulaw-8) AND chunk splitting. The GA server
            // emits audio deltas at the model's natural granularity —
            // empirically ~200–400 ms per delta. Twilio's media-stream
            // pipeline assumes ~20 ms frames (160 bytes mulaw @ 8 kHz);
            // shipping one big frame stalls Twilio's playout scheduler
            // for the chunk's full duration and the caller hears either
            // silence followed by a burst or nothing at all if Twilio
            // drops the frame for being out-of-band. Splitting the
            // transcoded mulaw into 20 ms slices and emitting one
            // synthetic `response.audio.delta` per slice gives the
            // parent dispatcher → StreamHandler → bridge.sendAudio
            // chain the natural cadence it expects.
            if (t === 'response.output_audio.delta' && typeof (parsed as { delta?: string }).delta === 'string') {
              this.translateGaAudioDelta(parsed as Record<string, unknown>, handler, rest);
              return;
            }
            (parsed as { type: string }).type = newType;
            handler(Buffer.from(JSON.stringify(parsed)), ...rest);
            return;
          }
        } catch {
          /* not JSON or parse failed — pass through */
        }
        handler(raw, ...rest);
      };
      return originalOn(event, wrapped);
    };

    await new Promise<void>((resolve, reject) => {
      let sessionCreated = false;
      let settled = false;
      const ws = this.ws!;

      const onSetupMessage = (raw: Buffer | string): void => {
        let msg: { type: string; error?: { message?: string } };
        try {
          msg = JSON.parse(raw.toString()) as { type: string; error?: { message?: string } };
        } catch (e) {
          getLogger().warn(`OpenAI Realtime 2: failed to parse message: ${String(e)}`);
          return;
        }
        if (msg.type === 'session.created' && !sessionCreated) {
          sessionCreated = true;
          ws.send(JSON.stringify({ type: 'session.update', session: this.buildGASessionConfig() }));
        } else if (msg.type === 'session.updated') {
          cleanup();
          resolve();
        } else if (msg.type === 'error') {
          // Surface real GA-side rejection ("invalid_model",
          // "missing_required_parameter") so the caller doesn't wait 15 s
          // for a meaningless timeout.
          cleanup();
          try { ws.close(); } catch { /* ignore */ }
          reject(new Error(`OpenAI Realtime 2 setup error: ${msg.error?.message ?? JSON.stringify(msg)}`));
        }
      };

      const onSetupError = (err: Error): void => {
        cleanup();
        try { ws.close(); } catch { /* ignore */ }
        reject(err);
      };

      const cleanup = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ws.off('message', onSetupMessage);
        ws.off('error', onSetupError);
      };

      const timer = setTimeout(() => {
        cleanup();
        try { ws.close(); } catch { /* ignore */ }
        reject(new Error('OpenAI Realtime 2 connect timeout'));
      }, 15000);

      ws.on('message', onSetupMessage);
      ws.on('error', onSetupError);
    });

    this.armHeartbeatAndListener();
  }

  /**
   * GA-API variant of {@link OpenAIRealtimeAdapter.openParkedConnection}.
   * Opens a fresh Realtime WS against the GA endpoint, exchanges
   * `session.created` → GA-shape `session.update` → `session.updated`
   * so the upstream session is fully primed, and returns the OPEN
   * socket WITHOUT taking it on `this.ws` or arming the heartbeat /
   * message listener.
   *
   * Used by `Patter.parkProviderConnections` during the carrier
   * ringing window so the per-call `StreamHandler` can adopt the
   * primed socket at carrier `start` — eliminating the TCP + TLS +
   * HTTP-101 + `session.update` ack round-trip from the critical path.
   * Saves ~300-600 ms of first-audible-word latency.
   *
   * Bounded by 8 s. Throws on timeout / handshake failure / GA-side
   * rejection. Callers treat any error as a cache miss and fall
   * through to the cold {@link connect} path.
   *
   * Billing safety: confirmed by OpenAI's Managing Realtime Costs
   * guide — `session.update` does NOT invoke the model and bills no
   * tokens. An idle parked socket costs $0.
   */
  override async openParkedConnection(): Promise<WebSocket> {
    const url = `wss://api.openai.com/v1/realtime?model=${encodeURIComponent(this.model)}`;
    const ws = new WebSocket(url, {
      headers: { Authorization: `Bearer ${this.apiKey}` },
    });
    await new Promise<void>((resolve, reject) => {
      let sessionCreated = false;
      let settled = false;
      const onMessage = (raw: Buffer | string): void => {
        let msg: { type?: string; error?: { message?: string } };
        try {
          msg = JSON.parse(raw.toString()) as { type?: string; error?: { message?: string } };
        } catch {
          return;
        }
        if (msg.type === 'session.created' && !sessionCreated) {
          sessionCreated = true;
          try {
            ws.send(JSON.stringify({ type: 'session.update', session: this.buildGASessionConfig() }));
          } catch (err) {
            cleanup();
            reject(err instanceof Error ? err : new Error(String(err)));
          }
        } else if (msg.type === 'session.updated') {
          cleanup();
          resolve();
        } else if (msg.type === 'error') {
          cleanup();
          reject(new Error(`OpenAI Realtime 2 parked-setup error: ${msg.error?.message ?? JSON.stringify(msg)}`));
        }
      };
      const onError = (err: Error): void => {
        cleanup();
        reject(err);
      };
      const cleanup = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ws.off('message', onMessage);
        ws.off('error', onError);
      };
      const timer = setTimeout(() => {
        cleanup();
        try { ws.close(); } catch { /* ignore */ }
        reject(new Error('OpenAI Realtime 2 park connect timeout'));
      }, 8000);
      ws.on('message', onMessage);
      ws.on('error', onError);
    });
    // Application-level keepalive. Empirically, OpenAI's GA Realtime
    // edge closes idle parked sockets within ~6-7 s — WS-level PINGs
    // alone are not counted as activity. Re-sending the (idempotent)
    // `session.update` every 3 s keeps the session alive across the
    // 3-15 s ringing window. Cancelled in `adoptWebSocket` when the
    // live adapter takes over. Billing safety: `session.update` bills
    // no tokens (no model invocation).
    const keepalive = setInterval(() => {
      if (ws.readyState !== ws.OPEN) {
        clearInterval(keepalive);
        return;
      }
      try {
        ws.send(JSON.stringify({ type: 'session.update', session: this.buildGASessionConfig() }));
      } catch {
        clearInterval(keepalive);
      }
    }, 3000);
    (ws as unknown as { _parkedKeepalive?: NodeJS.Timeout })._parkedKeepalive = keepalive;
    return ws;
  }

  /**
   * GA-API variant of {@link OpenAIRealtimeAdapter.adoptWebSocket}. Takes
   * over a WS that {@link openParkedConnection} produced (already through
   * `session.created` + `session.update` + `session.updated`) and arms
   * the heartbeat + message listener so the GA event-translation shim
   * is wired up. Skips the cold-connect path — saves ~300-600 ms on
   * first audible word.
   *
   * Caller MUST verify `ws.readyState === OPEN` before calling. If the
   * parked WS died between park and adopt, fall back to {@link connect}.
   */
  override adoptWebSocket(ws: WebSocket): void {
    // Cancel the parked keepalive before the live adapter starts
    // sending its own frames — otherwise the interval would race
    // input_audio_buffer.append writes on the same socket.
    const wsAny = ws as unknown as { _parkedKeepalive?: NodeJS.Timeout };
    if (wsAny._parkedKeepalive) {
      clearInterval(wsAny._parkedKeepalive);
      delete wsAny._parkedKeepalive;
    }
    this.ws = ws;
    // Re-attach the GA event-translation `ws.on` shim BEFORE
    // `armHeartbeatAndListener` registers the persistent message
    // listener — otherwise GA event names fall through to the v1
    // dispatcher's no-op branch and audio is silently dropped. This
    // mirrors the patch the parent `connect` installs on its
    // freshly-opened socket; we apply it to the adopted one too.
    const wsRef = ws as unknown as {
      on: (event: string, handler: (...args: unknown[]) => void) => unknown;
    };
    const originalOn = wsRef.on.bind(ws);
    wsRef.on = (event: string, handler: (...args: unknown[]) => void): unknown => {
      if (event !== 'message') return originalOn(event, handler);
      const wrapped = (raw: unknown, ...rest: unknown[]): void => {
        try {
          const text = typeof raw === 'string' ? raw : (raw as Buffer).toString();
          const parsed = JSON.parse(text) as { type?: string };
          const t = parsed.type;
          if (t && Object.prototype.hasOwnProperty.call(GA_TO_V1_EVENT_NAMES, t)) {
            // Audio deltas require transcoding (PCM-24 → mulaw-8) and 20 ms
            // frame splitting — delegate to the shared helper used by connect().
            if (t === 'response.output_audio.delta' && typeof (parsed as { delta?: string }).delta === 'string') {
              this.translateGaAudioDelta(parsed as Record<string, unknown>, handler, rest);
              return;
            }
            (parsed as { type?: string }).type = GA_TO_V1_EVENT_NAMES[t];
            handler(Buffer.from(JSON.stringify(parsed)), ...rest);
            return;
          }
        } catch {
          /* fall through */
        }
        handler(raw, ...rest);
      };
      return originalOn(event, wrapped);
    };
    this.armHeartbeatAndListener();
  }

  /**
   * GA-API variant of {@link OpenAIRealtimeAdapter.sendFirstMessage}. Two
   * differences from the v1 path:
   *
   * 1. The v1 implementation sends `response.modalities` which the GA
   *    endpoint rejects with `Unknown parameter: 'response.modalities'`.
   *    Use `output_modalities` to match the GA `session.update` shape.
   *
   * 2. The GA `response.create` does NOT inherit `audio.output.voice`
   *    from the session — it falls back to the server-side default
   *    (`marin`, female) when the field is omitted on the response
   *    itself. Session-level `voice: "alloy"` only affects subsequent
   *    server-VAD-triggered responses, NOT this explicit
   *    `response.create`. We re-inject the configured voice here so the
   *    first-message voice matches the rest of the call.
   */
  /**
   * Override the parent `sendAudio` to transcode inbound carrier audio
   * (mulaw 8 kHz from Twilio/Telnyx) into PCM-16 24 kHz before sending
   * `input_audio_buffer.append`. The GA server's audio engine ignores
   * mulaw frames (commit returns "buffer only has 0.00ms of audio") even
   * though it accepts `audio/pcmu` at the protocol level.
   */
  sendAudio(mulawAudio: Buffer): void {
    if (!this.ws || this.ws.readyState !== this.ws.OPEN) return;
    const pcm24k = this.transcodeInboundMulaw8ToPcm24(mulawAudio);
    this.ws.send(JSON.stringify({
      type: 'input_audio_buffer.append',
      audio: pcm24k.toString('base64'),
    }));
  }

  /**
   * mulaw 8 kHz Buffer → PCM-16-LE 24 kHz Buffer.
   *
   * Direct 3× linear-interpolation upsample with a one-sample carry
   * across chunk boundaries. For every consecutive pair of 8 kHz
   * samples `(s_a, s_b)` we emit three 24 kHz samples:
   *
   *     out_0 = s_a
   *     out_1 = 2/3·s_a + 1/3·s_b
   *     out_2 = 1/3·s_a + 2/3·s_b
   *
   * The carry stores the last 8 kHz sample of the chunk so the next
   * chunk can start by pairing `(carry, firstNewSample)` — that's what
   * keeps the output rate exact (each input sample → 3 output samples)
   * and eliminates the chunk-boundary DC step that confused the GA
   * server VAD. The first chunk has no carry and loses 3 samples at
   * the leading edge (375 µs of audio); that's well below any audible
   * artefact and well below the GA VAD's 300 ms prefix-padding window.
   */
  private transcodeInboundMulaw8ToPcm24(mulaw: Buffer): Buffer {
    const pcm8 = mulawToPcm16(mulaw);
    const samples8 = pcm8.length / 2;
    if (samples8 === 0) return Buffer.alloc(0);

    // Gain boost: telephony-band audio decoded from mulaw typically
    // sits in roughly ±8000 amplitude (-12 dB peak). The GA server VAD
    // is calibrated against 24 kHz studio audio whose peaks reach
    // ±16000-±24000, so the raw telephony level is below its speech
    // threshold and inbound utterances never trigger `speech_started`.
    // 2× gain brings us into the VAD's expected band; we clamp to
    // ±32767 to avoid Int16 overflow / wrap-around.
    const GAIN = 2;
    const inputs: number[] = [];
    if (this.inbound8kCarry !== null) inputs.push(this.inbound8kCarry);
    for (let i = 0; i < samples8; i++) {
      const raw = pcm8.readInt16LE(i * 2) * GAIN;
      inputs.push(Math.max(-32768, Math.min(32767, raw)));
    }
    // Save last sample for the next chunk.
    this.inbound8kCarry = inputs[inputs.length - 1];

    const numPairs = inputs.length - 1;
    if (numPairs <= 0) return Buffer.alloc(0);
    const out = Buffer.allocUnsafe(numPairs * 3 * 2);
    for (let i = 0; i < numPairs; i++) {
      const s0 = inputs[i];
      const s1 = inputs[i + 1];
      out.writeInt16LE(s0, i * 6);
      out.writeInt16LE(Math.round((s0 * 2 + s1) / 3), i * 6 + 2);
      out.writeInt16LE(Math.round((s0 + s1 * 2) / 3), i * 6 + 4);
    }
    return out;
  }

  /**
   * Shared audio-delta translation helper. Transcodes a GA
   * `response.output_audio.delta` payload (base64 PCM-16-LE 24 kHz)
   * into mulaw 8 kHz and splits the result into 160-byte (20 ms) frames,
   * dispatching one synthetic `response.audio.delta` event per frame.
   *
   * Called from BOTH the `connect()` shim and the `adoptWebSocket()` shim
   * so that warm-path (prewarm/adopted) calls receive identical transcoding
   * to cold-path calls. Without this, adopted sockets forwarded raw PCM-24
   * to Twilio/Telnyx, producing garbled or silent audio on every warm call.
   *
   * @param parsed  - The parsed GA event object (type already checked to be
   *                  `response.output_audio.delta` with a string `delta`).
   * @param handler - The downstream message listener to dispatch each frame to.
   * @param rest    - Extra arguments forwarded from the original `message` event.
   * @returns `true` if frames were dispatched (caller should return early),
   *          `false` if the resampler is still warming up (zero output bytes).
   */
  private translateGaAudioDelta(
    parsed: Record<string, unknown>,
    handler: (...args: unknown[]) => void,
    rest: unknown[],
  ): boolean {
    const newType = GA_TO_V1_EVENT_NAMES['response.output_audio.delta'];
    const mulaw = this.transcodeOutboundPcm24ToMulaw8Buffer(parsed.delta as string);
    const FRAME_BYTES = 160; // 20 ms of mulaw at 8 kHz
    if (mulaw.length === 0) return false; // resampler warmup — drop silently
    for (let off = 0; off < mulaw.length; off += FRAME_BYTES) {
      const slice = mulaw.subarray(off, Math.min(off + FRAME_BYTES, mulaw.length));
      const frame = { ...parsed, type: newType, delta: slice.toString('base64') };
      handler(Buffer.from(JSON.stringify(frame)), ...rest);
    }
    return true;
  }

  /**
   * Base64 PCM-16-LE 24 kHz → Base64 mulaw 8 kHz. Used by the WS
   * translation shim on each `response.output_audio.delta`. The stateful
   * resampler is created lazily and reused across all deltas in this
   * session so the 3:1 decimator's phase carries across chunk
   * boundaries — without that, every chunk boundary produces a click.
   */
  private transcodeOutboundPcm24ToMulaw8Buffer(deltaB64: string): Buffer {
    if (!this.outboundResampler24To16) {
      this.outboundResampler24To16 = new StatefulResampler({ srcRate: 24000, dstRate: 16000 });
      this.outboundResampler16To8 = new StatefulResampler({ srcRate: 16000, dstRate: 8000 });
    }
    const pcm24 = Buffer.from(deltaB64, 'base64');
    const pcm16 = this.outboundResampler24To16.process(pcm24);
    const pcm8 = this.outboundResampler16To8!.process(pcm16);
    if (pcm8.length === 0) return Buffer.alloc(0);
    return pcm16ToMulaw(pcm8);
  }

  async sendFirstMessage(text: string): Promise<void> {
    // ``reasoning.effort`` is only accepted by the flagship GA variants
    // (``gpt-realtime``, ``gpt-realtime-2``). The cost-tier
    // ``gpt-realtime-mini`` rejects it with "Unsupported option for
    // this model" and the first message never reaches the carrier.
    // Forward the field only when the caller explicitly opted into a
    // tier — the session.update already configured the inherited tier
    // for subsequent VAD-driven turns.
    const responseBody: Record<string, unknown> = {
      output_modalities: ['audio'],
      audio: { output: { voice: this.voice } },
      instructions: `Say exactly the following sentence as your first turn and nothing else: "${text}"`,
    };
    if (this.options.reasoningEffort !== undefined) {
      responseBody.reasoning = { effort: this.options.reasoningEffort };
    }
    this.ws?.send(JSON.stringify({ type: 'response.create', response: responseBody }));
  }
}
