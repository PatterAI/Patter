# Patter SDK — Riepilogo revisione tecnica + nuove feature (giugno 2026)

> Branch: `claude/affectionate-davinci-kzje88` · PR: [#171](https://github.com/PatterAI/Patter/pull/171) → `main`
> 42 commit oltre `origin/main` · piena parità Python ⇄ TypeScript in tutto il lavoro.

---

## 1. Contesto e obiettivo

Patter è l'SDK voice-AI open-source con due implementazioni **a parità**: Python (`libraries/python/getpatter/`) e TypeScript (`libraries/typescript/src/`). Naming mappato `snake_case` ⇄ `camelCase`, stessi default, stessa tassonomia errori; ogni feature pubblica deve esistere in entrambi.

Obiettivo della sessione, in due tranche:

1. **Revisione tecnica approfondita** di ogni componente (telephony, audio, STT, TTS, realtime, LLM, dashboard, tools/MCP, observability), confronto con framework analoghi (LiveKit Agents, Pipecat), e **fix dei problemi confermati**.
2. **Implementazione in parallelo** di 7 nuove capacità avanzate.

Vincoli di processo rispettati: nessun force-push; sviluppo solo sul branch designato; commit con trailer di sessione; embargo pre-GA (nessun push su SDK pubblici esterni a questo flusso).

---

## 2. Metodologia

- **Revisione fan-out con agent paralleli** per area, con riferimenti `file:riga` per ogni problema; confronto architetturale con LiveKit/Pipecat per validare le scelte.
- **Feature in worktree isolati**: 7 agent in `git worktree` separati, ciascuno su un branch `worktree-agent-<id>`, senza push. Io ho: scritto le spec, **rivisto criticamente** il loro output (trovando bug reali — vedi §6), risolto i **conflitti di merge** e fatto le **riconciliazioni cross-feature** (§5).
- **Cherry-pick verificato** dei commit-feature nel branch principale, con suite complete dopo ogni integrazione.
- **Parità garantita** dalla cross-SDK parity suite (ripristinata in questa sessione) + esecuzione integrale delle due suite unitarie.

---

## 3. Tranche 1 — Ondata di fix dalla revisione (~70 bug confermati)

Raggruppati per area. Tutti con parità Python/TS salvo dove indicato.

### Pipeline orchestrator (il cuore)
- **Dispatch del turno disaccoppiato** in un task in background tracciato (`_dispatch_task` / `dispatchTask`): il loop di ricezione continua a drenare i transcript, quindi la **barge-in funziona *durante*** turni lunghi (LLM agent-runtime da 30–90 s). Prima il loop restava bloccato in `await` e l'interruzione arrivava solo a turno finito.
- **Playback cursor** (`_playback_buffered_until` / `playbackBufferedUntil`) + **troncamento "heard-prefix"** stile LiveKit: la storia di un turno interrotto registra ciò che il caller ha **davvero sentito**, non tutto ciò che l'LLM ha generato.
- **Greeting come task in background**: il `first_message` non blocca più il read-loop del carrier → barge-in possibile già sulla prima frase.
- **Watchdog idle-reset** sullo stream LLM al posto del tetto fisso di 30 s (i turni lunghi con tool non muoiono più a metà).
- **Tail-grace** che non scambia più il turno successivo per una barge-in; cancel-event per-turno che non "perde" nel turno seguente; guardia eco per il forward-STT senza AEC.

### STT
- **Clone per-chiamata** dell'adapter configurato: cambio di *contratto* del provider — aggiunto `clone()` con cattura degli arg del costruttore (`__init_subclass__` in Python, `patterCtorArgs` in TS). Un'istanza condivisa serializzava chiamate concorrenti sullo stesso WebSocket/recognizer (audio interlacciato, transcript incrociati, doppi close). Fallback con warning rumoroso se il clone è impossibile.

### Engines (realtime / ConvAI / Gemini Live / Ultravox)
- **ConvAI `client_tool_call`** finalmente gestito: i tool dell'agente (e i builtin `transfer_call`/`end_call`) ora girano e rispondono **sempre** via `send_client_tool_result`; prima la sessione ConvAI restava appesa.
- **Gemini Live**: richieste ed esposte le trascrizioni input/output (config `input_audio_transcription`/`output_audio_transcription`); `goAway` loggato.
- **Ultravox**: semantica eventi corretta — transcript agente delta-only con dedup sui final, `response_done` finalmente emesso, `listening` non finge più speech utente.
- **GA realtime**: listener leak chiuso.
- **Python**: aggiunto il watchdog 1 h di durata massima chiamata (TS lo aveva già).

### Audio / AEC
- **Gating far-end stantio** (250 ms) sul canceller NLMS + alzato il floor di adattamento: durante i silenzi lunghi dell'agente l'AEC non distorce più l'audio del caller.
- **Gate formato-nativo carrier** propagato a *tutti* i path di invio: i byte μ-law 8 kHz wire non vengono mai spinti nel riferimento PCM16-16k dell'AEC (corrompeva il riferimento, e i chunk dispari crashavano `np.frombuffer`).

### Telephony / server
- Telnyx outbound pinnato a **PCMU 8 kHz**; chiavi **Ed25519 raw 32 byte** del portale accettate per la verifica webhook; hardening WebSocket; Plivo wait; mark-name corretti (Twilio/Plivo); finestra anti-replay Telnyx allineata `[-30 s, +300 s]`.

### Observability
- I **7 eventi speech-edge** ora emessi anche in **pipeline mode** (prima solo realtime), con dispatcher propagato nei 3 bridge Python.
- **`events.jsonl`** (documentato da 0.6, mai scritto) ora registra `tool_call`/`tool_result` (dalle righe `role="tool"`), `barge_in` (con `turn_index` + `bargein_ms`) ed `error` (da `CallMetrics.error_code`, salvato anche come campo `error` di `metadata.json`).

### API
- **Default `persist` unificato**: TS ora ON come Python e come da docs (prima TS perdeva la storia dashboard a ogni restart).
- **`call(first_message=…)`** di nuovo onorato in entrambi gli SDK.
- **TS `onCallStart`**: gli override per-chiamata (`system_prompt`, `voice`, `model`, `language`, `first_message`, `provider`, `tools`, `variables`) ora applicati (il wrapper TS scartava il return).

### Dashboard SPA
> Stessa dashboard di prima — **4 fix di comportamento**, nessun ridisegno. Architettura: SPA React+Vite in `dashboard-app/`, compilata in **un singolo `ui.html`** e sincronizzata identica nei due SDK (`npm run build && npm run sync`, stesso md5).

1. **Finestra temporale congelata**: la finestra catturava `Date.now()` una volta sola al mount → si ri-ancora ogni 30 s.
2. **Stato `ringing`/`queued`** mostrato come "ended": ora mappa al pill `queued` (già stilizzato ma inutilizzato) e conta come in corso.
3. **Chiamate cancellate che "resuscitavano"**: set di **tombstone** (locale + evento SSE `calls_deleted`) consultato dalla merge.
4. **Conteggi**: `turnCount` di fallback dimezzato (`ceil(len/2)`) e range "All" che deriva davvero la finestra dai dati (il sentinel `{fromMs:0}` era truthy).

### Parità e changelog
- **Parity suite cross-SDK ripristinata** — ha subito pescato una divergenza reale (validazione eager della OpenAI key mancante in TS `agent()`), poi corretta. Ora **10/10**.
- **CHANGELOG** aggiornato sotto `## Unreleased` per tutta l'ondata (regola AGENTS.md "stessa PR").

---

## 4. Tranche 2 — Le 7 nuove feature

Tutte **opt-in**, con default invariato (zero cambiamenti di comportamento se non configurate) e piena parità tra i due SDK.

| # | Feature | Config (snake / camel) | Cosa fa |
|---|---------|------------------------|---------|
| 1 | **Turn-detection semantico** (smart-turn v3 ONNX) | `turn_detector`, `max_semantic_hold_ms` | Dopo il silenzio VAD, *score* la finestra audio; **trattiene** la chiusura finché il modello predice "frase incompleta", con cap temporale. EOS marcato `semantic_turn_detector` vs `vad_silence`. Runtime/modello assenti → fallback pulito a VAD con un solo warning. |
| 2 | **Pause-and-resume false interruzioni** | `barge_in_mode="pause_resume"` | Alla barge-in l'output si **congela e bufferizza**; se nessun transcript reale conferma nella finestra, l'agente **riprende** la frase invece di zittirsi. Seconda macchina a stati di turn-taking accanto a quella esistente. |
| 3 | **Generazione preemptiva** | `preemptive_generation`, `preemptive_min_stable_ms` | Su un interim **stabile**, parte un LLM+TTS **speculativo** tenuto in memoria; al final → **release** (vantaggio di latenza) se combacia, altrimenti **discard** silenzioso. L'audio non raggiunge mai il caller prima del commit. |
| 4 | **Warm transfer + handoff multi-agente** | `transfer_call(mode="warm", summary=…)`, `agent(handoffs={…})` | *Warm*: conference Twilio completa (attesa → briefing supervisore → bridge → l'AI esce) con webhook di recovery; stub espliciti Telnyx/Plivo. *Handoff*: tool `handoff_to` che fa lo **swap dell'agente in-call** (prompt/tool/voce/guardrail) preservando la storia. |
| 5 | **Registrazione carrier-neutral** | `local_recording` | WAV **stereo** SDK-side (caller a sinistra, agente a destra, PCM16 16 kHz) nella directory del call-log, coperto dalla retention, scritture bufferizzate (64 KiB), finalizzato anche su teardown anomalo. |
| 6 | **EvalSession** | (Python) `EvalSession(...)`, `expect(...)` | Harness che guida la **vera** pipeline (vero STT-loop, LLMLoop, ToolExecutor, hook, guardrail) fakeando solo il confine pagato; asserzioni concatenabili + LLM judge. Prima il runner valutava una callback finta. |
| 7 | **InputProcessingChain** (decomposizione, slice 1) | — (refactor) | Estratta la metà inbound dei due handler (decode → resample → AEC → **audio_filter** → VAD) in una classe componibile. Effetto: `agent.audio_filter` (Krisp/DeepFilterNet) **ora viene applicato** — era un no-op documentato. Design completo in `docs/architecture/pipeline-stages.md`. |

---

## 5. Riconciliazioni cross-feature (decisioni all'integrazione)

Fondendo insieme le 7 feature emergono sovrapposizioni; qui ho preso decisioni architetturali:

- **EOS unico per turno**: consolidata **una sola** emissione di end-of-utterance al commit del transcript (smart-turn era scritto su una base dove la pipeline non la emetteva). Consuma il trigger semantico e copre anche il path `on_message`, gli orphan turn e il bypass della generazione preemptiva.
- **Gating AEC native-format** propagato ai nuovi path di invio (drain pause-resume, flush speculativo) in entrambi gli SDK.
- **Tap di registrazione** aggiunti ai 3 nuovi path Python (greeting estratto, drain, speculazione); in TS già coperti dal chokepoint `encodePipelineAudio`.
- **Guardrail post-handoff**: il path speculativo leggeva l'agente originale invece di `currentAgent`.

---

## 6. Bug reali trovati nella review critica degli agenti

Gli agent non sono stati accettati a scatola chiusa; la review ha trovato e corretto:

- **smart-turn (TS)**: aliasing dei buffer scratch nella DFT a 25 punti → il test di valore di riferimento cross-SDK falliva; corretto con buffer dedicati.
- **preemptive (TS)**: il task speculativo rilasciato non azzerava `dispatchTask` nel `finally` → la speculazione si **auto-disabilitava** dopo il primo hit. Regressione aggiunta.
- **preemptive (Python)**: race di registrazione in `_start_speculation` che orfanava una speculazione concorrente (audio trattenuto, stream LLM aperto, nessun miss contato). Guard + regressione.
- **pause-resume (TS)**: mancava l'attesa fail-open bounded sulla decisione di pausa → un teardown poteva incagliare il loop di dispatch per sempre.
- **warm transfer (TS)**: l'adapter GA realtime non aggiungeva il discriminatore `"type":"realtime"` all'`session.update` di handoff → OpenAI GA avrebbe rifiutato ogni handoff.
- **recording (TS)**: un `fs.writeSync` per frame da 20 ms (violava "no I/O per-frame") → buffer 64 KiB con scritture batch.

Inoltre, durante il run completo sono emersi **6 test su contratti obsoleti** (mock WS senza `recv()`, validazione `transfer_call`, guardrail via `send_reassurance`) e un crash reale (i costruttori ConvAI/Pipeline non accettavano ancora `speech_events`) — tutti corretti.

---

## 7. Validazione finale (su HEAD `fab43bd`)

| Suite | Esito |
|-------|-------|
| Python `pytest tests/ -m "not soak"` | **2579 passed**, 26 skipped, 2 xfailed (exit 0) |
| TypeScript `npm test` | **2056 passed**, 8 skipped, 127 file |
| TypeScript `tsc --noEmit` | pulito |
| Cross-SDK parity suite | **10/10** metodi a parità |

---

## 8. Note residue / follow-up consigliati

- **Warm transfer su Telnyx/Plivo**: attualmente stub espliciti con envelope d'errore + fallback cold (Twilio è completo).
- **Modello smart-turn non bundlato**: via `model_path`/env var; assente → fallback a VAD con warning singolo.
- **Cambio voce/TTS a metà handoff**: fuori scope v1 (limite engine, loggato).
- **Decomposizione pipeline**: questa è la *slice 1* (InputProcessingChain). Le slice successive (TurnManager / OutputChain) sono progettate in `docs/architecture/pipeline-stages.md` ma non ancora implementate.
- Un paio di pagine `docs/*.mdx` per recording non aggiornate (CHANGELOG e docstring/type sì).

---

## 9. Stato consegna

- Branch **`claude/affectionate-davinci-kzje88`** pushato su origin (`fab43bd`) — riutilizzabile in altre sessioni.
- PR **[#171](https://github.com/PatterAI/Patter/pull/171)** aperta verso `main` con la descrizione completa.
- Nessuna PR/force-push fuori da questo flusso; embargo pre-GA rispettato.
