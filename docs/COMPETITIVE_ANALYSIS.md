# Patter vs LiveKit Agents vs Cloudflare Agents — Analisi Comparativa

Data: 2026-04-16 · Branch: `worktree-refactored-singing-gosling`

> Fonti verificate: `gh api repos/livekit/agents` e `repos/cloudflare/agents`,
> docs Cloudflare Voice API, blog voice-agents. Inventario Patter da esplorazione
> diretta del worktree. Licenze: LiveKit Apache-2.0, Cloudflare MIT.

---

## 1. TL;DR

- **Patter**: SDK voice-AI leggero per telefonia (Twilio/Telnyx). Modalità local/self-hosted/cloud, dashboard integrata, tunnel Cloudflare built-in, test mode REPL, TS/Python parità. Target: dev che vuole un AI agent su un numero in minuti.
- **LiveKit Agents**: framework voice-AI più maturo dell'ecosistema OSS (Apache 2.0). **65+ plugin** inclusi Telnyx, Krisp (noise cancellation), Browser (web browsing agent), turn-detector ML, **AMD modulo dedicato**, **IVR built-in**, **background_audio**, avatar (~15 provider). Richiede LiveKit Server.
- **Cloudflare Agents Voice**: **già in beta operativa** (aprile 2026), non "coming soon". `@cloudflare/voice` con `withVoice(Agent)` pattern, Workers AI integrata (Deepgram Flux, Nova-3, Aura senza API keys), Twilio adapter disponibile, persistenza SQLite automatica. Lock-in Cloudflare Workers.

**Dove Patter vince oggi**: time-to-first-call, portabilità (nessun server obbligatorio), TS/Python parità, dashboard voice-specifica con cost breakdown.

**Dove siamo indietro**: quantità provider, turn detection ML, noise cancellation, AMD/IVR strutturati, avatar/vision, evals framework, background audio, persistenza cross-session.

---

## 2. Feature matrix — dati verificati

Legenda: ✅ supportato · ⚠️ parziale/beta · ❌ non supportato

| Categoria | Patter | LiveKit Agents | Cloudflare Agents Voice |
|---|---|---|---|
| **Linguaggi SDK** | Python + TypeScript | Python + Node beta | TypeScript (Workers) |
| **Stato voice** | produzione | produzione (stable) | **beta** (aprile 2026) |
| **Licenza** | proprietaria | Apache 2.0 | MIT |
| **Server required** | ❌ (embedded FastAPI) | ✅ LiveKit Server | ✅ Cloudflare Workers |
| **Telephony Twilio** | ✅ | ✅ (plugin) | ✅ `@cloudflare/voice-twilio` |
| **Telephony Telnyx** | ⚠️ beta | ✅ `livekit-plugins-telnyx` | ⚠️ roadmap |
| **SIP nativo** | ❌ | ✅ LiveKit SIP service | ❌ |
| **STT providers** | Deepgram, Whisper | **~20** (deepgram, assemblyai, azure, google, speechmatics, gladia, fal, clova, aws, cartesia, soniox, speechify, spitch, rtzr, sarvam, fishaudio, minimax, asyncai, cambai, ecc.) | Deepgram Flux/Nova-3 (via Workers AI) + plugin deepgram |
| **TTS providers** | ElevenLabs, OpenAI | **~25** (cartesia, elevenlabs, openai, azure, google, playai, rime, resemble, neuphonic, hume, lmnt, smallestai, speechify, murf, upliftai, hedra voice, fishaudio, minimax, phonic, inworld, ecc.) | Deepgram Aura (Workers AI) + ElevenLabs plugin |
| **LLM providers** | OpenAI + custom via `on_message` | openai, anthropic, google, aws, azure, mistralai, cerebras, groq, xai, fireworksai, baseten, nvidia, ollama (implicito), langchain bridge | Workers AI LLMs + bring-your-own via AI Gateway |
| **Realtime S2S** | OpenAI Realtime, ElevenLabs ConvAI | OpenAI Realtime, Google Gemini Live, Ultravox, FishAudio | via `withVoice(Agent)` con LLM custom |
| **Semantic turn detection (ML)** | ❌ | ✅ `livekit-plugins-turn-detector` (transformer) | ✅ nativo nel modello Deepgram Flux |
| **VAD acustico** | server-side (OpenAI Realtime) + Deepgram endpointing | ✅ `silero` + custom | incluso nel STT |
| **Noise cancellation** | ❌ | ✅ `livekit-plugins-krisp` | ❌ |
| **Barge-in / interruption** | ✅ | ✅ semantico multi-turno | ✅ |
| **AMD (answering machine)** | ✅ via Twilio AMD + voicemail drop | ✅ **`voice/amd/` modulo dedicato** | ❌ |
| **IVR (menu a risposta)** | ❌ | ✅ **`voice/ivr/` modulo dedicato** | ❌ |
| **Background audio (musica attesa)** | ❌ | ✅ `voice/background_audio.py` | ❌ |
| **Recording** | ✅ via Twilio | ✅ LiveKit Egress (S3/GCS/Azure) + `voice/recorder_io/` | ❌ |
| **Transfer (cold)** | ✅ E.164 validato | ✅ | ❌ |
| **Transfer (warm / attended)** | ❌ | ✅ | ❌ |
| **DTMF send+receive** | ⚠️ ricezione base | ✅ | ❌ |
| **Outbound dialing** | ✅ (Twilio REST) | ✅ (SIP + providers) | ⚠️ via Twilio |
| **Function tools** | ✅ `@tool` decorator + webhook SSRF-protected | ✅ `@function_tool` + MCP nativo | ✅ + MCP nativo |
| **MCP client/server** | ❌ | ✅ | ✅ |
| **Multi-agent / handoff** | ❌ | ✅ `Agent.handoff()` | ✅ (Durable Objects state) |
| **Vision / multimodal** | ❌ | ✅ Gemini Live + video frames | ⚠️ via Workers AI |
| **Avatar video (lip-sync)** | ❌ | ✅ **15 provider**: tavus, hedra, bithuman, lemonslice, simli, anam, avatario, avatartalk, did, liveavatar, bey, keyframe, runway, trugen | ❌ |
| **Browser (web agent)** | ❌ | ✅ `livekit-plugins-browser` | ❌ |
| **Guardrails** | ✅ check + blocked_terms | ✅ `livekit-blockguard` | generico |
| **Evals framework** | unit + soak + parity tests | ✅ `agents/evals/` + `livekit-plugins-hamming` (LLM judge) | ❌ |
| **Observability / OTEL** | metriche per-turn, SSE, cost | ✅ `telemetry/` + `observability.py` | Workers Observability |
| **Dashboard UI voice-specific** | ✅ SSE + CSV export | ❌ (export via OTEL) | ❌ |
| **Scheduling (cron/recurring)** | ❌ | ❌ | ✅ built-in (reminder vocali) |
| **Persistenza cross-session** | ❌ (in-memory + dashboard SQLite) | via Room state | ✅ **Durable Objects SQLite auto** |
| **Test mode senza telefono** | ✅ REPL `TestSession` | ✅ `cli run-agent` + playground web | Durable Object in-memory |
| **Dev tunnel integrato** | ✅ Cloudflare Quick Tunnel | cli interno | ❌ (già on-edge) |
| **Plugin registry / BYO provider** | ❌ (provider hardcoded) | ✅ `plugin.py` registry + entry points | ✅ interfaces minimali |
| **React hooks voice** | ❌ | ❌ | ✅ `useVoiceAgent`, `useVoiceInput` |
| **IPC worker model (fault isolation)** | ❌ | ✅ `ipc/` | implicito (DO hibernation) |

---

## 3. LiveKit Agents — approfondimento verificato

### 3.1 Core path `livekit-agents/livekit/agents/`
Cartelle confermate: `beta/`, `cli/`, `evals/`, `inference/`, `ipc/`, `llm/`, `metrics/`, `resources/`, `stt/`, `telemetry/`, `tokenize/`, `tts/`, `utils/`, `voice/`.
File core: `vad.py`, `worker.py`, `plugin.py`, `observability.py`, `job.py`, `inference_runner.py`, `types.py`, `language.py`, `_language_data.py`, `_exceptions.py`.

### 3.2 Sottocartella `voice/` (il cuore del pipeline)
Contenuto confermato:
- `agent.py`, `agent_activity.py`, `agent_session.py` — class `AgentSession` che orchestra STT→LLM→TTS con interruption/metrics.
- `amd/` — modulo **Answering Machine Detection** dedicato (equivalente al nostro ma strutturato come sottopacchetto).
- `avatar/` — abstraction per lip-sync video.
- `ivr/` — **Interactive Voice Response** (menu "premi 1"...). Patter non ce l'ha.
- `endpointing.py` — endpointing/turn detection configurabile.
- `turn.py` — state machine del turno conversazionale.
- `audio_recognition.py` — riconoscimento eventi audio (rumori, toni, ecc.).
- `background_audio.py` — musica d'attesa, suoni di conferma.
- `recorder_io/` — abstraction recording.
- `room_io/` — room-based I/O.
- `remote_session.py` — supporto sessioni remote.
- `transcription/` — gestione live transcripts.
- `speech_handle.py` — handle per speech stream (pause/resume/interrupt).
- `report.py`, `run_result.py`, `events.py`, `generation.py`, `io.py`.

### 3.3 Plugin ecosystem (`livekit-plugins/`)
**65+ plugin** confermati via `gh api`. Categorie:
- **STT**: deepgram, assemblyai, azure, google, speechmatics, gladia, fal, clova, aws, soniox, speechify, rtzr, sarvam, spitch, cambai, asyncai, fishaudio, minimax.
- **TTS**: cartesia, elevenlabs, openai, azure, google, playai, rime, resemble, neuphonic, hume, lmnt, smallestai, speechify, murf, upliftai, fishaudio, minimax, phonic, inworld, hedra.
- **LLM**: openai, anthropic, google, aws, azure, mistralai, cerebras, groq, xai, fireworksai, baseten, nvidia.
- **Realtime**: openai, google (Gemini Live), ultravox.
- **VAD / audio**: silero, krisp (noise cancellation).
- **Turn detection**: turn-detector (transformer).
- **Avatar**: tavus, hedra, bithuman, lemonslice, simli, anam, avatario, avatartalk, did, liveavatar, bey, keyframe, runway, trugen.
- **Telephony**: telnyx.
- **Altri**: browser (web agent), langchain (bridge), nltk (tokenize), blockguard (guardrails), hamming (evals), durable (persistenza), minimal.
- **Famiglie meta**: `livekit-blingfire`, `livekit-blockguard`, `livekit-durable` (non plugin-* standard).

### 3.4 Punti di forza univoci vs Patter
1. **65+ provider** pronti all'uso — oggi abbiamo 2 STT + 2 TTS + 2 realtime.
2. **Noise cancellation** via Krisp — UX enorme su chiamate telefoniche rumorose.
3. **Turn detector ML** — riduce false interruption.
4. **IVR** built-in — per "premi 1 per vendite" automatizzato con AI.
5. **AMD come modulo**, non solo callback webhook.
6. **Background audio** — musica di attesa, audio cues.
7. **Avatar multimodale** con 15 provider.
8. **Browser plugin** — agente che naviga web durante la call.
9. **Regional plugins** (sarvam, spitch, rtzr, clova, upliftai) — mercati non-anglofoni (India, Africa, Corea).
10. **Evals con LLM judge** (agents/evals/ + plugin hamming).

### 3.5 Dove Patter resta più agile
1. **Dashboard voice-specifica** (SSE + CSV + cost per-turn) built-in.
2. **Tunnel integrato** per webhook locali (Cloudflare Quick Tunnel).
3. **Niente LiveKit Server** — zero infra obbligatoria.
4. **TS/Python parità** — LiveKit Node è beta.
5. **Test mode REPL** per test senza telefono.
6. **Footprint deps** molto ridotto (niente WebRTC client).

---

## 4. Cloudflare Agents Voice — approfondimento verificato

### 4.1 Stato reale (aprile 2026)
**Il voice è già disponibile in beta**, non "coming soon". Package `packages/voice/` + repo root `voice-providers/` con subfolder `deepgram/`, `elevenlabs/`, `twilio/`. Blog ufficiale: architettura 7-step integrata.

### 4.2 Architettura voice
```
Phone → Twilio → WebSocket → TwilioAdapter → Durable Object (VoiceAgent)
                                                 ├─ STT (Deepgram Flux via Workers AI binding)
                                                 ├─ LLM (onTurn callback)
                                                 ├─ TTS (ElevenLabs PCM o Deepgram Aura)
                                                 └─ SQLite (conversation history auto)
```

### 4.3 Feature voice confermate
- Pattern: `withVoice(Agent)` per full loop, `withVoiceInput(Agent)` per STT-only.
- Built-in Workers AI (**senza API key**): Deepgram Flux STT, Nova-3 STT, Aura TTS.
- Turn detection: demandata al modello STT (Deepgram Flux ha endpointing nativo).
- Single WebSocket connection client↔agent.
- Persistenza SQLite automatica (`getConversationHistory()`).
- `keepAlive` per non evictare durante call attive.
- React hooks: `useVoiceAgent`, `useVoiceInput`.
- Pattern single-speaker enforcement (custom).

### 4.4 Limiti verificati
- Workers AI TTS (Aura) emette MP3 — **non compatibile con Twilio Media Streams** che richiede PCM. Workaround: ElevenLabs con `outputFormat: "pcm_16000"`.
- Niente DTMF, niente transfer, niente recording, niente AMD, niente IVR.
- Solo TypeScript (niente Python).
- Solo on Cloudflare Workers (lock-in).
- Provider limitati rispetto a LiveKit (Deepgram + ElevenLabs per ora, Twilio solo telephony).

### 4.5 Punti di forza
1. **Zero-setup auth** con Workers AI (niente API keys Deepgram/ElevenLabs se usi i built-in).
2. **Persistenza SQLite automatica** per conversation history cross-session.
3. **Scheduling integrato** — agent può mandare reminder vocali su cron.
4. **React hooks** pronti per frontend.
5. **Hibernation + auto-scale** edge.
6. **Latenza**: tutto nella stessa rete Workers, pochi hop.

### 4.6 Dove Patter è meglio
- Python SDK (CF è solo TS).
- Feature telefonia: DTMF, transfer, recording, AMD (tutti assenti in CF).
- Portabilità (CF solo su Cloudflare).
- Dashboard voice-specifica con cost breakdown.

### 4.7 Dove CF è meglio
- **Persistenza stato** cross-session (Durable Objects SQLite).
- **Scheduling** built-in.
- **React hooks** pronti.
- **Workers AI zero-config**.
- **Edge deployment** automatico.

---

## 5. Piano di porting concreto

Principio: **non riscriviamo**, importiamo dai source Apache-2.0/MIT con attribuzione (formato `sentence_chunker.py` già in repo).

### 5.1 PRIORITÀ ALTA — gap UX grosso, effort ragionevole

#### A1 · Silero VAD plugin (LiveKit)
- **Source**: `livekit-plugins/livekit-plugins-silero/` (Apache 2.0)
- **Perché**: VAD acustico lato server pre-STT. Riduce falsi interim-STT (meno costo Deepgram) e migliora barge-in.
- **Come**: wrap onnxruntime + modello `silero_vad.onnx` in `patter/providers/silero_vad.py`, Protocol `VADProvider`, integrazione opzionale in `stream_handler.py`.
- **Effort**: 1 settimana. Modello ~2MB.

#### A2 · Turn detector ML (LiveKit)
- **Source**: `livekit-plugins/livekit-plugins-turn-detector/` (Apache 2.0)
- **Perché**: UX differenziale. Oggi barge-in su interim-STT è euristico → interruzioni premature su pause riflessive. Turn-detector decide se l'utente ha finito davvero.
- **Come**: portare transformer quantizzato + runtime onnxruntime (Python) / `onnxruntime-node` (TS). Classe `TurnDetector` con `endOfUtterance(text_so_far) → bool`. Flag `enable_turn_detector=True` in `Patter()`.
- **Effort**: 2-3 settimane. Modello ~100MB (da valutare quantizzazione).

#### A3 · Noise cancellation (Krisp plugin mirror)
- **Source**: `livekit-plugins/livekit-plugins-krisp/` — SDK Krisp è commercial, ma il pattern di integrazione è OSS.
- **Perché**: chiamate PSTN sono rumorose. Krisp rimuove rumori di fondo lato audio caller prima di STT. Riduce WER.
- **Come**: integrazione Krisp SDK (BYO licenza) via Protocol `AudioFilter`, applicato a `pre_stt_pipeline`.
- **Effort**: 1-2 settimane. Dipende da licenza Krisp (SDK free tier può bastare per OSS).

#### A4 · IVR module
- **Source**: `livekit-agents/livekit/agents/voice/ivr/` (Apache 2.0)
- **Perché**: casi "premi 1 per vendite". Oggi Patter gestisce solo conversazione libera. IVR strutturato è pattern molto richiesto (call center).
- **Come**: portare state machine `IVRMenu(options=[...], prompt="...")` in `patter/services/ivr.py` + wire DTMF ingress.
- **Effort**: 1-2 settimane.

#### A5 · Background audio
- **Source**: `livekit-agents/livekit/agents/voice/background_audio.py`
- **Perché**: musica durante "let me check that for you", audio cue per silenzi lunghi, professional feel.
- **Come**: classe `BackgroundAudio(file=..., loop=True, mix_ratio=0.1)` che mixa audio di sottofondo al TTS.
- **Effort**: 3-5 giorni.

#### A6 · Plugin registry pattern
- **Source**: `livekit-agents/livekit/agents/plugin.py` (Apache 2.0)
- **Perché**: oggi aggiungere un provider richiede modificare il core di Patter. Con registry `@register_stt("provider-name")` gli utenti possono fare pip install patter-stt-assemblyai senza PR al core.
- **Come**: `patter/plugins.py` con entry-points setuptools (Python) + import ESM dinamico (TS).
- **Effort**: 1 settimana.

### 5.2 PRIORITÀ MEDIA

#### B1 · AMD modulare (LiveKit)
- **Source**: `livekit-agents/livekit/agents/voice/amd/`
- **Perché**: oggi facciamo AMD via Twilio callback. LiveKit lo fa come modulo pluggable (include BYO detector se Twilio AMD non basta).
- **Effort**: 1 settimana.

#### B2 · Evals framework (LiveKit)
- **Source**: `livekit-agents/livekit/agents/evals/` + `livekit-plugins-hamming`
- **Perché**: regression testing per voice AI. LLM judge sulle conversazioni di CI.
- **Effort**: 2 settimane. `patter eval run suite.yaml` CLI.

#### B3 · MCP client
- **Source**: SDK `modelcontextprotocol` ufficiale (non copia da LiveKit)
- **Perché**: sia LiveKit sia Cloudflare lo hanno nativo. Tool registrati su server MCP esterni (Claude Desktop, Cursor, ecc.).
- **Effort**: 1-2 settimane.

#### B4 · Multi-agent handoff (LiveKit)
- **Source**: `livekit-agents/livekit/agents/voice/agent.py` (pattern `handoff_to`)
- **Perché**: triage → billing → support. Pattern molto richiesto.
- **Effort**: 2 settimane.

#### B5 · Scheduling (Cloudflare pattern)
- **Source**: `packages/agents/src/schedule.ts`
- **Perché**: call outbound ricorrenti ("chiamami ogni lunedì alle 9"). Pattern, non codice diretto (CF usa DO).
- **Come**: wrap `apscheduler` (Python) + `node-cron` (TS) con API unificata.
- **Effort**: 1 settimana.

#### B6 · Persistenza cross-session (Durable Objects pattern)
- **Source**: Cloudflare pattern SQLite auto
- **Perché**: oggi history è in-memory per call. Gli utenti vogliono "ricorda il cliente da una call all'altra".
- **Come**: backend pluggable `SessionStore` (memory / sqlite / redis / postgres). Richiamabile come `patter.history.get(caller_number)`.
- **Effort**: 2 settimane.

#### B7 · OpenTelemetry tracing
- **Source**: `livekit-agents/livekit/agents/telemetry/` + `observability.py`
- **Perché**: enterprise. Span STT/LLM/TTS/tool call con parent "call".
- **Effort**: 1 settimana.

### 5.3 PRIORITÀ BASSA

- **Avatar multimodale** (Tavus/Hedra): niche, alto effort.
- **Browser agent** (livekit-plugins-browser): niche per voice-telephony.
- **SIP nativo**: enterprise, 4-6 settimane.
- **IPC worker model**: ottimizzazione a scala.
- **Regional plugins** (sarvam, spitch, clova): demand-driven.

### 5.4 Cosa NON portare (intenzionale)

1. **LiveKit Server** — fuori scope. Patter resta "bring Twilio/Telnyx".
2. **WebRTC client SDK** (iOS/Android/Web mult-party) — use case conferenza, non telefonia 1:1.
3. **Durable Objects runtime** — non replicabile fuori Workers.
4. **Spatial audio / simulcast** — feature WebRTC multi-publisher.
5. **`withVoice(Agent)` pattern** — troppo legato a Durable Objects lifecycle.

---

## 6. Priorità di esecuzione

Stima effort (1 dev full-time):

| # | Item | Effort | Valore UX | Rischio |
|---|------|--------|-----------|---------|
| 1 | Turn detector ML (A2) | 2-3w | ⭐⭐⭐⭐⭐ | medio |
| 2 | Silero VAD (A1) | 1w | ⭐⭐⭐⭐ | basso |
| 3 | Plugin registry (A6) | 1w | ⭐⭐⭐⭐ (scala ecosystem) | basso |
| 4 | IVR module (A4) | 1-2w | ⭐⭐⭐⭐ (mercato call center) | basso |
| 5 | Background audio (A5) | 3-5d | ⭐⭐⭐ (polish) | basso |
| 6 | Noise cancellation (A3) | 1-2w | ⭐⭐⭐⭐ | medio (licenza) |
| 7 | AMD modulare (B1) | 1w | ⭐⭐ | basso |
| 8 | MCP client (B3) | 1-2w | ⭐⭐⭐ | basso |
| 9 | Evals framework (B2) | 2w | ⭐⭐⭐ | basso |
| 10 | Multi-agent handoff (B4) | 2w | ⭐⭐⭐ | medio |
| 11 | Scheduling (B5) | 1w | ⭐⭐ | basso |
| 12 | Persistenza cross-session (B6) | 2w | ⭐⭐⭐ | basso |
| 13 | OTEL tracing (B7) | 1w | ⭐⭐ (enterprise) | basso |

**Raccomandazione sprint 1** (4-5 settimane):
- A1 (Silero VAD) + A2 (Turn detector ML) in parallelo → closing del gap UX più percepibile.
- A6 (Plugin registry) → abilita contributi esterni per #2 più STT/TTS senza toccare core.
- A4 (IVR) → sblocca segmento call center.

**Sprint 2** (3-4 settimane):
- A3 (Krisp) + A5 (Background audio) → polish percepito.
- B1 (AMD modulare) + B3 (MCP client) → parity con LiveKit+CF.

---

## 7. Posizionamento strategico

Patter ha una nicchia chiara: **il modo più leggero per mettere un AI agent su un numero di telefono**. LiveKit è la soluzione completa ma complessa (server, 65 plugin, avatar, WebRTC). Cloudflare è la soluzione integrata ma lock-in.

Piano: chiudere i gap di **qualità** (turn detection, noise cancel) e di **mercato** (IVR, AMD modulare, plugin ecosystem) **importando da LiveKit** dove OSS-compatibile. Mantenere i differentiator: DX, dashboard, TS/Python parità, zero-infra local mode.

Target Q2-Q3 2026: arrivare a feature parity con LiveKit sulle voice capabilities core, senza adottarne lo stack server.

---

## Appendice — File sorgenti per porting (con attribuzione)

Per ogni import aprire issue con: link commit SHA, header di attribuzione, diff adattamenti.

### LiveKit (Apache 2.0)
- `livekit-agents/livekit/agents/vad.py` — interfaccia VAD base
- `livekit-agents/livekit/agents/plugin.py` — registry
- `livekit-agents/livekit/agents/voice/ivr/` — IVR completo
- `livekit-agents/livekit/agents/voice/amd/` — AMD strutturato
- `livekit-agents/livekit/agents/voice/background_audio.py`
- `livekit-agents/livekit/agents/voice/endpointing.py`
- `livekit-agents/livekit/agents/voice/turn.py`
- `livekit-agents/livekit/agents/evals/` — eval framework
- `livekit-agents/livekit/agents/telemetry/` — OTEL
- `livekit-plugins/livekit-plugins-silero/livekit/plugins/silero/vad.py`
- `livekit-plugins/livekit-plugins-turn-detector/` — intero pacchetto
- `livekit-plugins/livekit-plugins-krisp/` — pattern integrazione (Krisp SDK è commercial)

### Cloudflare (MIT) — pattern, non codice diretto
- `packages/voice/src/withVoice.ts` — decorator pattern
- `packages/agents/src/schedule.ts` — scheduling
- `voice-providers/deepgram/` — `DeepgramSTT` implementation reference
- `voice-providers/twilio/` — Twilio adapter reference

---

*Report generato al termine del ciclo di bug review CRITICAL/HIGH (commit `c7fd65a`). Prossimo step operativo suggerito: aprire RFC-001 per plugin registry (A6) e RFC-002 per turn detector ML (A2).*
