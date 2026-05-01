/**
 * IVR auto-navigation activity for telephony calls (TypeScript port).
 *
 * Detects IVR prompts via transcribed speech, forwards DTMF responses
 * through `CallControl.sendDtmf`, and recovers from two common failure
 * modes:
 *
 * 1. The agent hears the same IVR prompt repeated several times
 *    (loop detection). `TfidfLoopDetector` flags this by comparing the
 *    cosine similarity of recent transcript chunks.
 * 2. The IVR falls silent while both parties are passive (silence
 *    detection). A debounced timer triggers a follow-up after
 *    `maxSilenceDuration` seconds of combined silence.
 *
 * The Python port uses scikit-learn for TF-IDF; TypeScript has no
 * equivalent battle-tested package in the std library, so we ship a
 * minimal in-house bag-of-words + cosine-similarity implementation.
 * It is intentionally simple — enough to match repeated IVR prompts.
 */

import type { CallControl } from "../metrics";
import { getLogger } from "../logger";

// ---------------------------------------------------------------------------
// DTMF event taxonomy
// ---------------------------------------------------------------------------

/** Valid DTMF tone values (keypad characters). */
export const DTMF_EVENTS = [
  "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
  "*", "#", "A", "B", "C", "D",
] as const;

export type DtmfEvent = (typeof DTMF_EVENTS)[number];

/** Join DTMF events into a space-separated debug string. */
export function formatDtmf(events: DtmfEvent[]): string {
  return events.join(" ");
}

// ---------------------------------------------------------------------------
// Bag-of-words + cosine similarity (minimal TF-IDF-ish loop detector)
// ---------------------------------------------------------------------------

const WORD_RE = /[a-z0-9]+/g;

function tokenize(text: string): string[] {
  return (text.toLowerCase().match(WORD_RE) ?? []);
}

/** Term-frequency bag-of-words vector. */
function bagOfWords(text: string): Map<string, number> {
  const counts = new Map<string, number>();
  for (const token of tokenize(text)) {
    counts.set(token, (counts.get(token) ?? 0) + 1);
  }
  return counts;
}

function cosineSimilarity(a: Map<string, number>, b: Map<string, number>): number {
  if (a.size === 0 || b.size === 0) return 0;

  let dot = 0;
  // Iterate the smaller map for efficiency.
  const [small, big] = a.size <= b.size ? [a, b] : [b, a];
  for (const [term, weight] of small) {
    const other = big.get(term);
    if (other !== undefined) {
      dot += weight * other;
    }
  }

  let normA = 0;
  for (const w of a.values()) normA += w * w;
  let normB = 0;
  for (const w of b.values()) normB += w * w;

  const denom = Math.sqrt(normA) * Math.sqrt(normB);
  if (denom === 0) return 0;
  return dot / denom;
}

// ---------------------------------------------------------------------------
// TfidfLoopDetector
// ---------------------------------------------------------------------------

export interface TfidfLoopDetectorOptions {
  /** Number of recent chunks to keep in the comparison window. */
  windowSize?: number;
  /** Cosine similarity above which two chunks are "the same prompt". */
  similarityThreshold?: number;
  /** Consecutive near-duplicates required before firing. */
  consecutiveThreshold?: number;
}

/**
 * Detects repeated IVR prompts via cosine similarity on bag-of-words
 * vectors. Not a full TF-IDF implementation — good enough for catching
 * IVRs that re-read the same menu.
 */
export class TfidfLoopDetector {
  private readonly windowSize: number;
  private readonly similarityThreshold: number;
  private readonly consecutiveThreshold: number;
  private chunks: Array<{ text: string; vec: Map<string, number> }> = [];
  private consecutiveSimilar = 0;

  constructor(opts: TfidfLoopDetectorOptions = {}) {
    const {
      windowSize = 20,
      similarityThreshold = 0.85,
      consecutiveThreshold = 3,
    } = opts;

    if (windowSize <= 0) {
      throw new Error("windowSize must be greater than 0");
    }
    if (similarityThreshold < 0 || similarityThreshold > 1) {
      throw new Error("similarityThreshold must be between 0.0 and 1.0");
    }
    if (consecutiveThreshold <= 0) {
      throw new Error("consecutiveThreshold must be greater than 0");
    }

    this.windowSize = windowSize;
    this.similarityThreshold = similarityThreshold;
    this.consecutiveThreshold = consecutiveThreshold;
  }

  reset(): void {
    this.chunks = [];
    this.consecutiveSimilar = 0;
  }

  addChunk(text: string): void {
    this.chunks.push({ text, vec: bagOfWords(text) });
    if (this.chunks.length > this.windowSize) {
      this.chunks = this.chunks.slice(-this.windowSize);
    }
  }

  checkLoopDetection(): boolean {
    if (this.chunks.length < 2) return false;

    const last = this.chunks[this.chunks.length - 1];
    let maxSim = 0;
    for (let i = 0; i < this.chunks.length - 1; i++) {
      const sim = cosineSimilarity(last.vec, this.chunks[i].vec);
      if (sim > maxSim) maxSim = sim;
    }

    if (maxSim > this.similarityThreshold) {
      this.consecutiveSimilar += 1;
    } else {
      this.consecutiveSimilar = 0;
    }

    return this.consecutiveSimilar >= this.consecutiveThreshold;
  }
}

// ---------------------------------------------------------------------------
// Debounced silence helper
// ---------------------------------------------------------------------------

class DebouncedCall {
  private timer: ReturnType<typeof setTimeout> | null = null;
  constructor(
    private readonly callback: () => Promise<void> | void,
    private readonly delayMs: number,
  ) {}

  schedule(): void {
    this.cancel();
    this.timer = setTimeout(() => {
      this.timer = null;
      Promise.resolve(this.callback()).catch((err) => {
        getLogger().error("IVR silence callback raised:", err);
      });
    }, this.delayMs);
  }

  cancel(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}

// ---------------------------------------------------------------------------
// IVRActivity
// ---------------------------------------------------------------------------

/** Async callback fired when the TF-IDF detector trips. */
export type LoopCallback = () => Promise<void> | void;
/** Async callback fired after sustained silence. */
export type SilenceCallback = () => Promise<void> | void;

export interface IVRActivityOptions {
  /** Seconds of combined silence before firing `onSilence`. Default `5.0`. */
  maxSilenceDuration?: number;
  /** Enable the TF-IDF loop detector. Default `true`. */
  loopDetector?: boolean;
  /** Fired when the loop detector trips. */
  onLoopDetected?: LoopCallback;
  /** Fired after `maxSilenceDuration` seconds of combined silence. */
  onSilence?: SilenceCallback;
}

/** OpenAI-style function tool spec with attached handler. */
export interface IVRToolDefinition {
  name: string;
  description: string;
  parameters: {
    type: "object";
    properties: Record<string, unknown>;
    required?: string[];
  };
  handler: (args: { events: string[] }) => Promise<string>;
}

/**
 * Coordinate IVR navigation heuristics for a single call.
 *
 * Usage::
 *
 *     const ivr = new IVRActivity(callControl);
 *     await ivr.start();
 *
 *     // In the STT loop, on each final transcript:
 *     await ivr.onUserTranscribed(text);
 *
 *     // When done:
 *     await ivr.stop();
 */
export class IVRActivity {
  private readonly callControl: CallControl;
  private readonly maxSilenceDurationMs: number;
  private readonly loopDetector: TfidfLoopDetector | null;
  private readonly onLoopDetected?: LoopCallback;
  private readonly onSilence?: SilenceCallback;

  private currentUserState: string | null = null;
  private currentAgentState: string | null = null;
  private readonly debouncedSilence: DebouncedCall;
  private lastShouldSchedule: boolean | null = null;
  private started = false;

  constructor(callControl: CallControl, opts: IVRActivityOptions = {}) {
    const {
      maxSilenceDuration = 5.0,
      loopDetector = true,
      onLoopDetected,
      onSilence,
    } = opts;

    this.callControl = callControl;
    this.maxSilenceDurationMs = maxSilenceDuration * 1000;
    this.loopDetector = loopDetector ? new TfidfLoopDetector() : null;
    this.onLoopDetected = onLoopDetected;
    this.onSilence = onSilence;

    this.debouncedSilence = new DebouncedCall(
      () => this.onSilenceDetected(),
      this.maxSilenceDurationMs,
    );
  }

  async start(): Promise<void> {
    this.started = true;
  }

  async stop(): Promise<void> {
    this.debouncedSilence.cancel();
    this.started = false;
  }

  async onUserTranscribed(text: string): Promise<void> {
    if (!this.started || !text) return;

    if (this.loopDetector !== null) {
      this.loopDetector.addChunk(text);
      if (this.loopDetector.checkLoopDetection()) {
        this.loopDetector.reset();
        if (this.onLoopDetected) {
          try {
            await this.onLoopDetected();
          } catch (err) {
            getLogger().error("IVR onLoopDetected callback raised:", err);
          }
        }
      }
    }
  }

  noteUserState(state: string): void {
    this.currentUserState = state;
    this.scheduleSilenceCheck();
  }

  noteAgentState(state: string): void {
    this.currentAgentState = state;
    this.scheduleSilenceCheck();
  }

  get tools(): IVRToolDefinition[] {
    return [this.buildSendDtmfTool()];
  }

  // -------- internals --------

  private scheduleSilenceCheck(): void {
    const shouldSchedule = this.shouldScheduleCheck();
    if (shouldSchedule) {
      if (this.lastShouldSchedule) return;
      this.debouncedSilence.schedule();
    } else {
      this.debouncedSilence.cancel();
    }
    this.lastShouldSchedule = shouldSchedule;
  }

  private shouldScheduleCheck(): boolean {
    const userSilent =
      this.currentUserState === "listening" || this.currentUserState === "away";
    const agentSilent =
      this.currentAgentState === "idle" || this.currentAgentState === "listening";
    return userSilent && agentSilent;
  }

  private async onSilenceDetected(): Promise<void> {
    if (this.onSilence) {
      try {
        await this.onSilence();
      } catch (err) {
        getLogger().error("IVR onSilence callback raised:", err);
      }
    }
  }

  private buildSendDtmfTool(): IVRToolDefinition {
    const allowedValues = [...DTMF_EVENTS];

    const handler = async (args: { events: string[] }): Promise<string> => {
      const events = args.events ?? [];
      const validated: string[] = [];
      for (const raw of events) {
        if (!allowedValues.includes(raw as DtmfEvent)) {
          return `Failed to send DTMF event: invalid digit '${raw}'`;
        }
        validated.push(raw);
      }

      const digits = validated.join("");
      try {
        if (!this.callControl.sendDtmf) {
          return "Failed to send DTMF events: CallControl.sendDtmf not implemented";
        }
        await this.callControl.sendDtmf(digits, { delayMs: 300 });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return `Failed to send DTMF events: ${msg}`;
      }
      return `Successfully sent DTMF events: ${validated.join(" ")}`;
    };

    return {
      name: "send_dtmf_events",
      description:
        "Send a list of DTMF events to the telephony provider. " +
        "Call when the IVR is asking for keypad input.",
      parameters: {
        type: "object",
        properties: {
          events: {
            type: "array",
            description: "Ordered list of DTMF digits to send.",
            items: {
              type: "string",
              enum: allowedValues,
            },
          },
        },
        required: ["events"],
      },
      handler,
    };
  }
}
