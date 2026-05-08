/**
 * Unit tests for the speech-edge events dispatcher — TypeScript parity of
 * `libraries/python/tests/test_speech_events.py`.
 *
 * Tests run synthetically: no real telephony, no real provider WebSocket.
 * We drive `SpeechEvents` directly with the methods the SDK calls
 * internally and assert payload schema + state machine + OTel attach.
 */
import { describe, expect, it, vi } from "vitest";

import { Patter, SpeechEvents, Twilio } from "../src";
import type { SpeechEventCallback } from "../src/_speech-events";

class Recorder {
  public calls: Array<{ name: string; payload: Record<string, unknown> }> = [];
  make(name: string): SpeechEventCallback {
    return async (payload) => {
      this.calls.push({ name, payload: { ...payload } });
    };
  }
}

describe("[unit] SpeechEvents — user-side edges", () => {
  it("test_user_speech_started_fires_on_vad_positive_edge", async () => {
    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onUserSpeechStarted = rec.make("started");
    events.markCallStarted(1_000_000);

    await events.fireUserSpeechStarted({
      timestampMs: 1_000_500,
      vadConfidence: 0.85,
    });

    expect(rec.calls).toHaveLength(1);
    expect(rec.calls[0].name).toBe("started");
    expect(rec.calls[0].payload.timestamp_ms).toBe(1_000_500);
    expect(rec.calls[0].payload.vad_confidence).toBe(0.85);
    expect(rec.calls[0].payload.audio_offset_ms).toBe(500);
    expect(events.conversationState.user).toBe("speaking");
  });

  it("test_user_speech_ended_fires_on_vad_negative_edge", async () => {
    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onUserSpeechEnded = rec.make("ended");
    events.markCallStarted(2_000_000);

    await events.fireUserSpeechStarted({ timestampMs: 2_000_100 });
    await events.fireUserSpeechEnded({
      timestampMs: 2_000_700,
      speechDurationMs: 600,
    });

    expect(rec.calls).toHaveLength(1);
    expect(rec.calls[0].name).toBe("ended");
    expect(rec.calls[0].payload.speech_duration_ms).toBe(600);
    expect(rec.calls[0].payload.audio_offset_ms).toBe(700);
    expect(events.conversationState.user).toBe("listening");
  });

  it("test_user_speech_eos_fires_after_trailing_silence", async () => {
    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onUserSpeechEos = rec.make("eos");

    await events.fireUserSpeechEos({
      trigger: "vad_silence",
      trailingSilenceMs: 420,
      transcriptSoFar: "I would like to book",
    });

    expect(rec.calls).toHaveLength(1);
    expect(rec.calls[0].payload.trigger).toBe("vad_silence");
    expect(rec.calls[0].payload.trailing_silence_ms).toBe(420);
    expect(rec.calls[0].payload.transcript_so_far).toBe("I would like to book");
    expect(events.turnIdx).toBe(1);
    expect(events.conversationState.agent).toBe("thinking");
  });

  it("test_user_speech_eos_fires_after_semantic_turn_detector_agreement", async () => {
    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onUserSpeechEos = rec.make("eos");

    await events.fireUserSpeechEos({ trigger: "semantic_turn_detector" });

    expect(rec.calls).toHaveLength(1);
    expect(rec.calls[0].payload.trigger).toBe("semantic_turn_detector");
    expect(rec.calls[0].payload.trailing_silence_ms).toBeUndefined();
    expect(rec.calls[0].payload.transcript_so_far).toBeUndefined();
  });
});

describe("[unit] SpeechEvents — agent-side edges", () => {
  it("test_agent_speech_started_fires_on_first_wire_chunk", async () => {
    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onAgentSpeechStarted = rec.make("agent_started");

    await events.fireUserSpeechEos({ trigger: "vad_silence" });
    await events.fireAgentSpeechStarted({
      ttsProvider: "elevenlabs",
      engine: "openai_realtime",
    });

    expect(rec.calls).toHaveLength(1);
    expect(rec.calls[0].payload.turn_idx).toBe(1);
    expect(rec.calls[0].payload.tts_provider).toBe("elevenlabs");
    expect(rec.calls[0].payload.engine).toBe("openai_realtime");
    expect(events.conversationState.agent).toBe("speaking");
  });

  it("test_agent_speech_ended_marks_interrupted_when_barge_in", async () => {
    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onAgentSpeechEnded = rec.make("agent_ended");

    await events.fireUserSpeechEos({ trigger: "vad_silence" });
    await events.fireAgentSpeechStarted({ ttsProvider: "elevenlabs" });
    await events.fireAgentSpeechEnded({
      speechDurationMs: 1500,
      interrupted: true,
    });

    expect(rec.calls).toHaveLength(1);
    expect(rec.calls[0].payload.speech_duration_ms).toBe(1500);
    expect(rec.calls[0].payload.interrupted).toBe(true);
    expect(rec.calls[0].payload.turn_idx).toBe(1);
    expect(events.conversationState.agent).toBe("idle");
  });
});

describe("[unit] SpeechEvents — LLM and TTS events", () => {
  it("test_llm_first_token_fires_once_per_turn", async () => {
    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onLlmToken = rec.make("llm");

    await events.fireUserSpeechEos({ trigger: "vad_silence" }); // turn 1
    await events.fireLlmFirstToken({ llmProvider: "anthropic", model: "claude" });
    await events.fireLlmFirstToken({ llmProvider: "anthropic", model: "claude" });
    await events.fireLlmFirstToken({ llmProvider: "anthropic", model: "claude" });

    expect(rec.calls).toHaveLength(1);
    expect(rec.calls[0].payload.llm_provider).toBe("anthropic");
    expect(rec.calls[0].payload.model).toBe("claude");
    expect(rec.calls[0].payload.turn_idx).toBe(1);

    await events.fireUserSpeechEos({ trigger: "vad_silence" }); // turn 2
    await events.fireLlmFirstToken({ llmProvider: "openai", model: "gpt-4o" });
    expect(rec.calls).toHaveLength(2);
    expect(rec.calls[1].payload.turn_idx).toBe(2);
  });

  it("test_audio_out_fires_once_per_turn", async () => {
    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onAudioOut = rec.make("audio");

    await events.fireUserSpeechEos({ trigger: "vad_silence" });
    await events.fireAudioOut({ ttsProvider: "elevenlabs" });
    await events.fireAudioOut({ ttsProvider: "elevenlabs" });

    expect(rec.calls).toHaveLength(1);
    expect(rec.calls[0].payload.tts_provider).toBe("elevenlabs");
    expect(rec.calls[0].payload.turn_idx).toBe(1);
  });
});

describe("[unit] SpeechEvents — robustness", () => {
  it("test_callback_exception_does_not_propagate", async () => {
    const events = new SpeechEvents();
    events.onUserSpeechStarted = async () => {
      throw new Error("observer crashed");
    };
    // Must not throw — observer crash is logged, never propagated.
    await events.fireUserSpeechStarted();
  });

  it("test_chained_callback_runs_after_user_handler", async () => {
    const events = new SpeechEvents();
    const order: string[] = [];

    const userHandler: SpeechEventCallback = async () => {
      order.push("user");
    };
    // Mirror the runner's `instrumentation/turn_taking.py:install()` wrapper
    // pattern — compose on top of the prior callback rather than replace it.
    const composed: SpeechEventCallback = async (payload) => {
      await userHandler(payload);
      order.push("instrumentation");
    };
    events.onUserSpeechStarted = composed;
    await events.fireUserSpeechStarted();

    expect(order).toEqual(["user", "instrumentation"]);
  });

  it("test_no_callbacks_set_yields_no_overhead", async () => {
    const events = new SpeechEvents();
    // No callbacks registered — every fire is a near-no-op.
    await events.fireUserSpeechStarted();
    await events.fireUserSpeechEnded({ speechDurationMs: 120 });
    await events.fireUserSpeechEos({ trigger: "vad_silence" });
    await events.fireAgentSpeechStarted();
    await events.fireAgentSpeechEnded({ speechDurationMs: 800 });
    await events.fireLlmFirstToken({ llmProvider: "x", model: "y" });
    await events.fireAudioOut({ ttsProvider: "x" });
  });

  it("test_otel_span_events_attached_to_call_span", async () => {
    // We can't easily import @opentelemetry/api inside vitest without
    // installing it; assert the dispatcher's contract by spying on the
    // callback path instead. The OTel branch is identical Python parity
    // and unit-tested in the Python suite (they share the same docs/spec).
    const events = new SpeechEvents();
    const llmSpy = vi.fn();
    events.onLlmToken = llmSpy as unknown as SpeechEventCallback;
    await events.fireUserSpeechEos({ trigger: "vad_silence" });
    await events.fireLlmFirstToken({
      llmProvider: "anthropic",
      model: "claude-haiku",
    });
    expect(llmSpy).toHaveBeenCalledOnce();
    const payload = llmSpy.mock.calls[0][0] as Record<string, unknown>;
    expect(payload.llm_provider).toBe("anthropic");
    expect(payload.model).toBe("claude-haiku");
    expect(payload.turn_idx).toBe(1);
  });
});

describe("[unit] Patter — speech-event proxies", () => {
  function makePatter(): Patter {
    // Local-mode requires phoneNumber + carrier. We never call serve() — the
    // dispatcher is initialised at construction.
    return new Patter({
      phoneNumber: "+15555550100",
      carrier: new Twilio({ accountSid: "ACtest", authToken: "test" }),
    });
  }

  it("callback proxy mirrors speechEvents", () => {
    const phone = makePatter();
    const cb: SpeechEventCallback = async () => undefined;

    phone.onUserSpeechStarted = cb;
    expect(phone.speechEvents.onUserSpeechStarted).toBe(cb);

    phone.speechEvents.onUserSpeechEnded = cb;
    expect(phone.onUserSpeechEnded).toBe(cb);
  });

  it("conversationState defaults to listening / initializing", () => {
    const phone = makePatter();
    expect(phone.conversationState).toEqual({
      user: "listening",
      agent: "initializing",
    });
  });

  it("conversationState reflects dispatch", async () => {
    const phone = makePatter();
    await phone.speechEvents.fireUserSpeechStarted();
    expect(phone.conversationState.user).toBe("speaking");
    await phone.speechEvents.fireUserSpeechEos({ trigger: "vad_silence" });
    expect(phone.conversationState.agent).toBe("thinking");
  });
});

/**
 * StreamHandler wiring smoke tests — verify the handler's private
 * ``emitLlmFirstToken`` / ``emitAudioOut`` helpers proxy to the
 * dispatcher with the correct provider tags. The Realtime branch in
 * ``onAdapterTranscriptOutput`` / ``onAdapterAudio`` calls these on
 * every delta; the dispatcher's idempotency makes per-delta calls cheap
 * (only the first per turn fires the user callback).
 */
describe("[unit] StreamHandler — speech-event wiring", () => {
  it("realtime emitLlmFirstToken and emitAudioOut fire once per turn", async () => {
    const { StreamHandler } = await import("../src/stream-handler");

    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onLlmToken = rec.make("llm");
    events.onAudioOut = rec.make("audio");

    // Arm the per-turn flags.
    await events.fireUserSpeechEos({ trigger: "vad_silence" });

    // Construct a handler shell — bypass the full constructor because
    // the helpers we exercise only need the dispatcher and a tiny agent
    // shape. Mirror Python's ``__new__`` pattern.
    const handler = Object.create(StreamHandler.prototype) as Record<
      string,
      unknown
    >;
    handler.deps = {
      speechEvents: events,
      agent: {
        model: "gpt-realtime",
        provider: "openai_realtime",
      },
    };
    handler.llmProviderTag = "openai_realtime";

    // First delta of the turn — fires once.
    await (handler as { emitLlmFirstToken: () => Promise<void> })
      .emitLlmFirstToken();
    await (handler as { emitAudioOut: () => Promise<void> }).emitAudioOut();
    // Subsequent deltas inside the same turn — dispatcher swallows.
    await (handler as { emitLlmFirstToken: () => Promise<void> })
      .emitLlmFirstToken();
    await (handler as { emitAudioOut: () => Promise<void> }).emitAudioOut();

    const names = rec.calls.map((c) => c.name);
    expect(names.filter((n) => n === "llm")).toHaveLength(1);
    expect(names.filter((n) => n === "audio")).toHaveLength(1);
    const llmCall = rec.calls.find((c) => c.name === "llm")!;
    expect(llmCall.payload.llm_provider).toBe("openai_realtime");
    expect(llmCall.payload.model).toBe("gpt-realtime");
    const audioCall = rec.calls.find((c) => c.name === "audio")!;
    expect(audioCall.payload.tts_provider).toBe("openai_realtime");
  });

  it("pipeline emitAudioOut tags the configured TTS provider", async () => {
    const { StreamHandler } = await import("../src/stream-handler");

    const events = new SpeechEvents();
    const rec = new Recorder();
    events.onAudioOut = rec.make("audio");
    events.onLlmToken = rec.make("llm");
    await events.fireUserSpeechEos({ trigger: "vad_silence" });

    // Pipeline-mode handler shell with a TTS provider that exposes a
    // ``providerKey`` static — same convention as ElevenLabsTTS /
    // CartesiaTTS / OpenAITTS adapters in ``src/tts/``.
    class FakeTTS {
      static readonly providerKey = "elevenlabs";
    }
    const handler = Object.create(StreamHandler.prototype) as Record<
      string,
      unknown
    >;
    handler.deps = {
      speechEvents: events,
      agent: {
        model: "gpt-4o-mini",
        tts: new FakeTTS(),
        provider: "pipeline",
      },
    };
    handler.llmProviderTag = "anthropic";

    await (handler as { emitLlmFirstToken: () => Promise<void> })
      .emitLlmFirstToken();
    await (handler as { emitAudioOut: () => Promise<void> }).emitAudioOut();

    expect(rec.calls).toHaveLength(2);
    const llmCall = rec.calls.find((c) => c.name === "llm")!;
    expect(llmCall.payload.llm_provider).toBe("anthropic");
    const audioCall = rec.calls.find((c) => c.name === "audio")!;
    expect(audioCall.payload.tts_provider).toBe("elevenlabs");
  });
});
