#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

declare -a TOPICS=(
  "01|quickstart|Quickstart|Install, env check, three operating modes (cloud/self-hosted/local), three voice modes (Realtime/ConvAI/Pipeline), 'hello phone' minimal agent."
  "02|realtime|Realtime providers|OpenAI Realtime, Gemini Live, Ultravox, ElevenLabs ConvAI."
  "03|pipeline_stt|Pipeline STT|Deepgram, Whisper, AssemblyAI, Soniox, Speechmatics, Cartesia."
  "04|pipeline_tts|Pipeline TTS|ElevenLabs, OpenAI, Cartesia, LMNT, Rime."
  "05|pipeline_llm|Pipeline LLM|OpenAI, Anthropic, Gemini, Groq, Cerebras, custom on_message, LLMLoop, tool-call protocol."
  "06|telephony_twilio|Telephony — Twilio|Webhook parsing, HMAC-SHA1, AMD, DTMF, recording, transfer, ring timeout, status callback, TwiML emission."
  "07|telephony_telnyx|Telephony — Telnyx|Call Control, Ed25519, AMD, DTMF, track filter, anti-replay."
  "08|tools|Tools|@tool/defineTool, auto-injected transfer_call/end_call, dynamic variables, custom tools, schema validation."
  "09|guardrails_hooks|Guardrails & hooks|Keyword block, PII redact, pipeline hooks, text transforms, sentence chunker."
  "10|advanced|Advanced|Scheduler, fallback LLM chain, background audio, noise filter, custom STT/TTS, custom LLM HTTP."
  "11|metrics_dashboard|Metrics & dashboard|CallMetricsAccumulator, MetricsStore, dashboard SSE, CSV/JSON export, pricing, basic auth."
  "12|security|Security|HMAC, Ed25519, SSRF guard, webhook URL validation, secret hygiene, dashboard auth, cost cap."
)

for entry in "${TOPICS[@]}"; do
  IFS='|' read -r ID SLUG TITLE BRIEF <<< "$entry"
  for LANG in python typescript; do
    OUT="examples/notebooks/${LANG}/${ID}_${SLUG}.ipynb"
    if [[ -f "$OUT" ]]; then
      echo "skip (exists) $OUT"
      continue
    fi
    PYTHONPATH=scripts python3 -c "
from scaffold_notebook import build_notebook
import json, pathlib
nb = build_notebook(topic_id='${ID}', title='${TITLE}', language='${LANG}', brief='''${BRIEF}''')
pathlib.Path('${OUT}').parent.mkdir(parents=True, exist_ok=True)
pathlib.Path('${OUT}').write_text(json.dumps(nb, indent=1) + '\n')
print('wrote ${OUT}')
"
  done
done
