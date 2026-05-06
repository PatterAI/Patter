import { useEffect, useRef, useState } from 'react';
import type { Call } from './CallTable';
import { fmtDuration } from './format';
import { IconForward, IconHangup, IconMic, IconRecord } from './icons';

interface LiveDurationProps {
  start: number;
}

function LiveDuration({ start }: LiveDurationProps) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);
  return <>{fmtDuration((Date.now() - start) / 1000)}</>;
}

export interface TranscriptTurn {
  who: 'user' | 'bot' | 'tool';
  txt?: string;
  args?: Record<string, string | number>;
  typing?: boolean;
  lat?: { stt?: number; llm?: number; tts?: number; total?: number };
}

export interface LiveCallPanelProps {
  call: Call | null;
  transcript: TranscriptTurn[];
  onEnd: () => void;
  recording: boolean;
  setRecording: (v: boolean) => void;
  muted: boolean;
  setMuted: (v: boolean) => void;
}

export function LiveCallPanel({
  call,
  transcript,
  onEnd,
  recording,
  setRecording,
  muted,
  setMuted,
}: LiveCallPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [transcript]);

  if (!call) {
    return (
      <div className="rr-card">
        <h3>No live call selected</h3>
        <div className="meta">Select a call from the table — or wait for the next ring.</div>
      </div>
    );
  }

  const isLive = call.status === 'live';

  return (
    <div className="rr-card">
      <h3>
        Live call
        <span className={'pill ' + (isLive ? 'live' : 'done')}>{call.status}</span>
      </h3>
      <div className="meta">
        <strong>{call.direction === 'inbound' ? call.from : call.to}</strong>
        <span className="sep">·</span>
        {call.agent}
      </div>

      <div className="duration-block">
        <span className="l">duration</span>
        <span className="agent">
          {call.direction === 'inbound' ? 'inbound' : 'outbound'} ·{' '}
          {call.carrier === 'twilio' ? 'Twilio' : 'Telnyx'}
        </span>
        <span className="v">
          {isLive && call.durationStart ? (
            <LiveDuration start={call.durationStart} />
          ) : (
            fmtDuration(call.duration || 0)
          )}
        </span>
      </div>

      <div className="transcript" ref={scrollRef}>
        {transcript.map((t, i) => {
          if (t.who === 'tool') {
            return (
              <div key={i} className="turn tool">
                <div className="av">⚙</div>
                <div className="body">
                  <div className="who">tool · {t.txt}</div>
                  {t.args && (
                    <div className="tool-call">
                      {Object.entries(t.args).map(([k, v]) => (
                        <span key={k}>
                          <span className="k">{k}:</span> "{String(v)}"{'  '}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          }
          return (
            <div key={i} className={'turn ' + t.who}>
              <div className="av">{t.who === 'user' ? 'U' : 'P'}</div>
              <div className="body">
                <div className="who">
                  {t.who === 'user' ? 'caller' : 'agent'}
                  {t.typing && ' · typing'}
                </div>
                <div className="txt">
                  {t.typing ? (
                    <span className="typing">
                      <span></span>
                      <span></span>
                      <span></span>
                    </span>
                  ) : (
                    t.txt
                  )}
                </div>
                {t.lat && !t.typing && (
                  <div className="lat">
                    {t.lat.stt && `stt ${t.lat.stt} ms`}
                    {t.lat.total &&
                      `total ${t.lat.total} ms · llm ${t.lat.llm} · tts ${t.lat.tts}`}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {isLive && (
        <div className="controls">
          <button
            type="button"
            className={'ctrl' + (muted ? ' active' : '')}
            onClick={() => setMuted(!muted)}
          >
            <IconMic /> {muted ? 'unmute' : 'mute'}
          </button>
          <button type="button" className="ctrl">
            <IconForward /> transfer
          </button>
          <button
            type="button"
            className={'ctrl' + (recording ? ' active' : '')}
            onClick={() => setRecording(!recording)}
          >
            <IconRecord /> {recording ? 'stop rec' : 'record'}
          </button>
          <button type="button" className="ctrl danger" onClick={onEnd}>
            <IconHangup /> end
          </button>
        </div>
      )}
    </div>
  );
}
